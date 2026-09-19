"""应用装配层：把内容、事件、缓存、更正管线与隐私账串成一个门面。

所有跨域编排（发布联动清缓存、紧急更正全链路、按角色裁剪）都收敛在这里，
HTTP 层只做协议适配。
"""

from __future__ import annotations

from dataclasses import dataclass

from .cache import CacheResult, EdgeCache
from .content import ContentService, NotServable
from .events import EventService
from .pipeline import CorrectionPipeline
from .privacy import PrivacyLedger, hash_visitor
from .store import AuditLog, Clock, EventBus, IdGen
from .timeutil import Window
from .visibility import Scope, can_read, public_view


class ServiceError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass
class ServedContent:
    body: dict
    cache_status: str
    http_status: int = 200


class PortalApp:
    def __init__(self, clock: Clock | None = None, k_threshold: int = 5):
        self.clock = clock or Clock()
        self.idgen = IdGen()
        self.audit = AuditLog(self.clock)
        self.bus = EventBus(self.clock, self.idgen)
        self.content = ContentService(self.clock, self.idgen, self.audit)
        self.events = EventService(self.clock, self.idgen, self.bus, self.content, self.audit)
        self.cache = EdgeCache(self.clock, self.audit)
        self.pipeline = CorrectionPipeline(self.clock, self.idgen, self.audit)
        self.privacy = PrivacyLedger(threshold=k_threshold)
        # 承办方联系人：联系人级行级隔离（organizer_id）。
        self.contacts: dict[str, dict] = {}

    # ---- 联系人 --------------------------------------------------------------

    def add_contact(self, contact_id: str, organizer_id: str, name: str,
                    phone: str, role_title: str) -> dict:
        record = {
            "id": contact_id,
            "organizer_id": organizer_id,
            "name": name,
            "phone": phone,
            "role_title": role_title,
            "scope": Scope.ORGANIZER_CONTACTS.value,
        }
        self.contacts[contact_id] = record
        return record

    def list_contacts(self, role: str, organizer_id: str | None = None) -> list[dict]:
        out = []
        for record in self.contacts.values():
            view = public_view(record, role, organizer_id)
            if view is not None:
                out.append(view)
        return out

    def list_dispatches(self, event_seq: int, role: str) -> list[dict]:
        if not can_read(role, Scope.INTERNAL_DISPATCH):
            raise ServiceError("forbidden", "处置记录仅限运营中心与联动部门", 403)
        return self.events.dispatches_for(event_seq)

    # ---- 对外读取（走边缘缓存硬闸） ------------------------------------------

    def serve(self, item_id: str, lang: str, role: str = "visitor",
              organizer_id: str | None = None) -> ServedContent:
        item = self.content.get(item_id)  # NotFound 向上抛
        if not can_read(role, Scope(item.scope), organizer_id, item.organizer_id):
            raise ServiceError("forbidden", "该内容不在当前角色的可见范围", 403)

        key = f"{item_id}:{lang}"
        result, cached = self.cache.get(key)
        if result == CacheResult.HIT:
            return ServedContent(body=cached, cache_status="hit")

        try:
            view = self.content.resolve(item_id, lang=lang, at=self.clock())
        except NotServable as exc:
            # 过期/废止/未生效：缓存节点也必须空。向调用方给出对外状态码。
            status = {
                "policy_expired": 410,
                "policy_revoked": 410,
                "policy_not_yet_effective": 425,
            }.get(exc.code, exc.http_status)
            raise ServiceError(exc.code, str(exc), status) from exc

        self.cache.put(
            key,
            view,
            tags={f"entity:{item_id}", f"kind:{item.kind}"},
            effective_until_utc=view.get("effective_until_utc"),
        )
        return ServedContent(
            body=view,
            cache_status="expired_suppressed" if result == CacheResult.EXPIRED_SUPPRESSED
            else "miss",
        )

    # ---- 发布联动清缓存 ------------------------------------------------------

    def publish_zh(self, item_id: str, no: int, actor: str) -> dict:
        version = self.content.publish_zh(item_id, no, actor)
        purged = self.cache.purge_entity(item_id, actor="content")
        return {"zh": version.to_dict(), "purged_keys": purged}

    def publish_ru(self, item_id: str, no: int, actor: str | None = None) -> dict:
        version = self.content.publish_ru(item_id, no, actor)
        purged = self.cache.purge_entity(item_id, actor="content")
        return {"ru": version.to_dict(), "purged_keys": purged}

    def approve_policy(self, item_id: str, reviewer: str, window: Window,
                       zh_no: int | None = None, note: str = "") -> dict:
        review = self.content.approve_policy(item_id, reviewer, window, zh_no, note)
        purged = self.cache.purge_entity(item_id, actor="policy")
        return {"review": review.to_dict(), "purged_keys": purged}

    def revoke_policy(self, item_id: str, reviewer: str, reason: str) -> dict:
        review = self.content.revoke_policy(item_id, reviewer, reason)
        purged = self.cache.purge_entity(item_id, actor="policy")
        return {"review": review.to_dict(), "purged_keys": purged}

    # ---- 事件接入（取消等联动清缓存） ----------------------------------------

    _PURGE_ON = {"cancel", "postpone", "shuttle_change", "merchant_hours"}

    def ingest_event(self, kind: str, entity_id: str, payload: dict,
                     actor: str) -> dict:
        outcome = self.events.ingest(kind, entity_id, payload, actor)
        purged = []
        if kind in self._PURGE_ON:
            purged = self.cache.purge_entity(entity_id, actor="events")
        outcome["purged_keys"] = purged
        return outcome

    # ---- 紧急更正全链路（快速通道） ------------------------------------------

    def urgent_correction(self, item_id: str, *, reason: str, zh_body: dict,
                          ru_body: dict, window: Window | None,
                          opener: str, unit_author: str, translator: str,
                          reviewer: str | None) -> dict:
        """驱动一条紧急更正走完所有必需阶段并计时。

        政策页必须提供 ``reviewer`` 与 ``window``；非政策页省略。终态前会实测：
        旧缓存键已清空，且新内容可对外解析，才标记 ``publicly_visible``。
        """
        item = self.content.get(item_id)
        corr = self.pipeline.open(item_id, reason, opener)
        all_purged: list[str] = []

        zh = self.content.submit_zh(item_id, unit_author, zh_body,
                                    note=f"紧急更正：{reason}")
        self.pipeline.mark(corr.id, "zh_submitted")
        all_purged += self.publish_zh(item_id, zh.no, unit_author)["purged_keys"]
        self.pipeline.mark(corr.id, "zh_published")

        ru = self.content.submit_ru(item_id, zh.no, translator, ru_body,
                                    note=f"紧急更正译文：{reason}")
        self.pipeline.mark(corr.id, "ru_submitted")
        all_purged += self.publish_ru(item_id, ru.no, translator)["purged_keys"]
        self.pipeline.mark(corr.id, "ru_published")

        if item.is_policy:
            if reviewer is None or window is None:
                raise ServiceError("review_required",
                                   "政策类紧急更正必须复核并给出生效区间", 422)
            all_purged += self.approve_policy(
                item_id, reviewer, window, zh_no=zh.no,
                note=f"紧急复核：{reason}")["purged_keys"]
            self.pipeline.mark(corr.id, "policy_approved")

        all_purged += self.cache.purge_entity(item_id, actor="urgent-correction")
        # 去重，保留首次清除顺序。
        purged = list(dict.fromkeys(all_purged))
        self.pipeline.mark(corr.id, "cache_purged")

        # 终态验证：旧键不存在；游客侧能解析到新版本，且俄文绑定在新中文版上。
        for lang in ("zh", "ru"):
            assert not self.cache.has(f"{item_id}:{lang}"), "缓存仍残留旧报文"
        view = self.content.resolve(item_id, lang="ru", at=self.clock())
        assert view["zh_version"] == zh.no and view["ru_version"] == ru.no
        self.pipeline.mark(corr.id, "publicly_visible")
        return {
            "correction": self.pipeline.get(corr.id).to_dict(),
            "purged_keys": purged,
            "new_view": view,
        }

    # ---- 隐私账（浏览/到场） -------------------------------------------------

    def record_view(self, visitor_token: str, entity_id: str) -> None:
        self.privacy.record_view(hash_visitor(visitor_token), entity_id)

    def record_arrival(self, visitor_token: str, entity_id: str) -> None:
        self.privacy.record_arrival(hash_visitor(visitor_token), entity_id)

    def conversion(self, entity_id: str, role: str) -> dict:
        if not can_read(role, Scope.INTERNAL_DISPATCH):
            raise ServiceError("forbidden", "转化数据仅限运营视角", 403)
        metric = self.privacy.conversion(entity_id)
        if metric is None:
            return {"suppressed": True,
                    "reason": "distinct_visitors_below_k",
                    "k": self.privacy._threshold}
        return metric
