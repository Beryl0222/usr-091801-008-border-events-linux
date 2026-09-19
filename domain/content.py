"""内容版本、翻译绑定与政策复核。

事实链：

* 责任单位提交**中文事实源**版本（zh v1、v2……），只有 ``published`` 版本对外；
* 译员的每个**俄文版本**都绑定到一个具体中文版本号；中文一旦出新版本而俄文尚未
  跟上，旧俄文立即标记为 :attr:`RussianVersion.STALE`，不得再当作当前内容返回；
* 通关政策页的每个中文版本还必须挂一条**政策复核**记录与生效区间。系统只返回
  “当前时刻落在生效区间内、且俄文绑定在该已复核版本上”的内容；窗口外一律硬闸，
  即使边缘节点仍缓存着旧报文也不返回（见 :mod:`domain.cache`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .store import AuditLog, Clock, IdGen
from .timeutil import Window


class ContentError(Exception):
    """内容域错误基类，``code`` 供 API 映射为状态码。"""

    code = "content_error"


class NotFound(ContentError):
    code = "not_found"


class InvalidState(ContentError):
    code = "invalid_state"


class NotServable(ContentError):
    """内容当前不可对外返回。``http_status`` 给出对外语义。"""

    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


# ---- 版本状态 ----------------------------------------------------------------

class ZhStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETRACTED = "retracted"


class RuStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    # 绑定的中文版本已被新版本取代——旧译文“在版但过期”。
    STALE = "stale"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REVOKED = "revoked"  # 紧急废止（如新政策叫停旧口径）


# ---- 数据结构 ----------------------------------------------------------------

@dataclass
class ChineseVersion:
    no: int
    body: dict
    author: str
    note: str
    status: ZhStatus = ZhStatus.DRAFT
    created_utc: str = ""
    published_utc: str | None = None

    def to_dict(self) -> dict:
        return {
            "no": self.no,
            "lang": "zh",
            "status": self.status.value,
            "author": self.author,
            "note": self.note,
            "created_utc": self.created_utc,
            "published_utc": self.published_utc,
            "body": self.body,
        }


@dataclass
class RussianVersion:
    no: int
    zh_no: int
    body: dict
    translator: str
    note: str
    status: RuStatus = RuStatus.DRAFT
    created_utc: str = ""
    published_utc: str | None = None

    def to_dict(self) -> dict:
        return {
            "no": self.no,
            "lang": "ru",
            "zh_no": self.zh_no,
            "status": self.status.value,
            "translator": self.translator,
            "note": self.note,
            "created_utc": self.created_utc,
            "published_utc": self.published_utc,
            "body": self.body,
        }


@dataclass
class PolicyReview:
    zh_no: int
    reviewer: str
    window: Window
    status: ReviewStatus
    note: str
    decided_utc: str

    def to_dict(self) -> dict:
        return {
            "zh_no": self.zh_no,
            "reviewer": self.reviewer,
            "status": self.status.value,
            "note": self.note,
            "decided_utc": self.decided_utc,
            "window": self.window.to_dict(),
        }


@dataclass
class ContentItem:
    id: str
    kind: str  # activity / policy / transport / merchant
    scope: int  # visibility.Scope 的数值
    organizer_id: str | None
    zh: list[ChineseVersion] = field(default_factory=list)
    ru: list[RussianVersion] = field(default_factory=list)
    reviews: list[PolicyReview] = field(default_factory=list)

    @property
    def is_policy(self) -> bool:
        return self.kind == "policy"

    def latest_published_zh(self) -> ChineseVersion | None:
        published = [v for v in self.zh if v.status == ZhStatus.PUBLISHED]
        return published[-1] if published else None

    def ru_for(self, zh_no: int) -> list[RussianVersion]:
        return [r for r in self.ru if r.zh_no == zh_no]

    def current_ru(self) -> RussianVersion | None:
        """当前应展示的俄文版本：已发布且绑定在最新中文版本上。"""
        zh = self.latest_published_zh()
        if zh is None:
            return None
        bound = [r for r in self.ru if r.zh_no == zh.no and r.status == RuStatus.PUBLISHED]
        return bound[-1] if bound else None

    def effective_review(self, at) -> PolicyReview | None:
        """时刻 ``at`` 生效的复核记录：已批准、未废止、窗口覆盖 ``at``，且挂在最新中文版本上。"""
        zh = self.latest_published_zh()
        if zh is None:
            return None
        for review in reversed(self.reviews):
            if review.zh_no != zh.no:
                continue
            if review.status == ReviewStatus.APPROVED and review.window.contains(at):
                return review
        return None


# ---- 领域服务 ----------------------------------------------------------------

class ContentService:
    def __init__(self, clock: Clock, idgen: IdGen, audit: AuditLog):
        self.clock = clock
        self.idgen = idgen
        self.audit = audit
        self._items: dict[str, ContentItem] = {}

    # 注册与查询
    def register(self, kind: str, scope: int, organizer_id: str | None = None,
                 item_id: str | None = None) -> ContentItem:
        item_id = item_id or self.idgen.next(
            {"activity": "ev", "policy": "pol", "transport": "shuttle", "merchant": "shop"}[kind]
        )
        if item_id in self._items:
            raise InvalidState(f"内容已存在: {item_id}")
        item = ContentItem(id=item_id, kind=kind, scope=scope, organizer_id=organizer_id)
        self._items[item_id] = item
        return item

    def get(self, item_id: str) -> ContentItem:
        item = self._items.get(item_id)
        if item is None:
            raise NotFound(f"内容不存在: {item_id}")
        return item

    def list_ids(self, kind: str | None = None) -> list[str]:
        return [i.id for i in self._items.values() if kind is None or i.kind == kind]

    # 中文事实源
    def submit_zh(self, item_id: str, author: str, body: dict, note: str = "",
                  publish: bool = False) -> ChineseVersion:
        item = self.get(item_id)
        version = ChineseVersion(
            no=len(item.zh) + 1,
            body=dict(body),
            author=author,
            note=note,
            created_utc=self.clock().isoformat(),
        )
        item.zh.append(version)
        self.audit.record(author, "zh.submit", item_id, {"no": version.no, "note": note})
        if publish:
            self.publish_zh(item_id, version.no, actor=author)
        return version

    def publish_zh(self, item_id: str, no: int, actor: str) -> ChineseVersion:
        item = self.get(item_id)
        version = self._zh_version(item, no)
        if version.status != ZhStatus.DRAFT:
            raise InvalidState(f"中文 v{no} 状态为 {version.status.value}，不可发布")
        version.status = ZhStatus.PUBLISHED
        version.published_utc = self.clock().isoformat()
        # 旧译文立刻过期：新中文公告已发，旧俄文不能再冒充现行口径。
        for ru in item.ru:
            if ru.zh_no < no and ru.status == RuStatus.PUBLISHED:
                ru.status = RuStatus.STALE
        self.audit.record(actor, "zh.publish", item_id, {"no": no})
        return version

    def retract_zh(self, item_id: str, no: int, actor: str, reason: str) -> ChineseVersion:
        item = self.get(item_id)
        version = self._zh_version(item, no)
        version.status = ZhStatus.RETRACTED
        self.audit.record(actor, "zh.retract", item_id, {"no": no, "note": reason})
        return version

    # 俄文翻译
    def submit_ru(self, item_id: str, zh_no: int, translator: str, body: dict,
                  note: str = "", publish: bool = False) -> RussianVersion:
        item = self.get(item_id)
        self._zh_version(item, zh_no)  # 绑定的中文版本必须真实存在
        version = RussianVersion(
            no=len(item.ru) + 1,
            zh_no=zh_no,
            body=dict(body),
            translator=translator,
            note=note,
            created_utc=self.clock().isoformat(),
        )
        item.ru.append(version)
        self.audit.record(translator, "ru.submit", item_id,
                          {"no": version.no, "zh_no": zh_no, "note": note})
        if publish:
            self.publish_ru(item_id, version.no, actor=translator)
        return version

    def publish_ru(self, item_id: str, no: int, actor: str | None = None) -> RussianVersion:
        item = self.get(item_id)
        version = self._ru_version(item, no)
        zh = self._zh_version(item, version.zh_no)
        if zh.status != ZhStatus.PUBLISHED:
            raise InvalidState("绑定的中文版本尚未发布，俄文不能先行上线")
        latest = item.latest_published_zh()
        if latest is not None and version.zh_no < latest.no:
            raise InvalidState("绑定的中文版本已被取代，该译文只能以 stale 状态留档")
        version.status = RuStatus.PUBLISHED
        version.published_utc = self.clock().isoformat()
        self.audit.record(actor or version.translator, "ru.publish", item_id,
                          {"no": no, "zh_no": version.zh_no})
        return version

    # 政策复核
    def approve_policy(self, item_id: str, reviewer: str, window: Window,
                       zh_no: int | None = None, note: str = "") -> PolicyReview:
        item = self.get(item_id)
        if not item.is_policy:
            raise InvalidState("只有通关类页面需要政策复核")
        zh = self._zh_version(item, zh_no) if zh_no else item.latest_published_zh()
        if zh is None or zh.status != ZhStatus.PUBLISHED:
            raise InvalidState("复核必须针对一个已发布的中文版本")
        review = PolicyReview(
            zh_no=zh.no,
            reviewer=reviewer,
            window=window,
            status=ReviewStatus.APPROVED,
            note=note,
            decided_utc=self.clock().isoformat(),
        )
        item.reviews.append(review)
        self.audit.record(reviewer, "policy.approve", item_id,
                          {"zh_no": zh.no, "note": note, **window.to_dict()})
        return review

    def revoke_policy(self, item_id: str, reviewer: str, reason: str) -> PolicyReview:
        """紧急废止当前生效口径（例如上级临时叫停）。废止后页面立即不可返回。"""
        item = self.get(item_id)
        at = self.clock()
        current = item.effective_review(at)
        target = current or (item.reviews[-1] if item.reviews else None)
        if target is None:
            raise InvalidState("没有可废止的复核记录")
        target.status = ReviewStatus.REVOKED
        self.audit.record(reviewer, "policy.revoke", item_id,
                          {"zh_no": target.zh_no, "note": reason})
        return target

    # ---- 对外解析 ------------------------------------------------------------

    def resolve(self, item_id: str, lang: str = "ru", at=None) -> dict:
        """返回当前可对外展示的内容视图；不可展示时抛 :class:`NotServable`。"""
        item = self.get(item_id)
        at = at or self.clock()
        zh = item.latest_published_zh()
        if zh is None:
            raise NotServable("no_published_source", "没有已发布的中文事实源", 503)

        # 译文时效优先判定：新中文公告一旦发布，旧俄文必须立刻显形为 stale，
        # 哪怕新版本的政策复核尚未完成——避免“旧译文顶着旧复核继续外发”。
        ru = None
        if lang == "ru":
            ru = item.current_ru()
            if ru is None:
                stale = [r for r in item.ru if r.status == RuStatus.STALE]
                code = "ru_stale" if stale else "ru_missing"
                raise NotServable(code, "俄文未就绪：译文仍在处理，旧译文不得对外返回", 409)

        review = self._gated_policy_review(item, zh, at) if item.is_policy else None

        if lang == "zh":
            return self._view(item, zh, None, review, at, lang="zh")
        return self._view(item, zh, ru, review, at, lang="ru")

    def _gated_policy_review(self, item: ContentItem, zh: ChineseVersion, at) -> PolicyReview:
        # 最新中文版本是否有任何复核记录？
        reviews_for_zh = [r for r in item.reviews if r.zh_no == zh.no]
        if not reviews_for_zh:
            raise NotServable("policy_review_required",
                              "该中文版本尚未通过政策复核", 409)
        active = [r for r in reviews_for_zh if r.status == ReviewStatus.APPROVED]
        if not active:
            raise NotServable("policy_revoked", "现行口径已被紧急废止", 410)
        current = item.effective_review(at)
        if current is None:
            nearest = max(active, key=lambda r: r.window.start)
            if at < nearest.window.start:
                raise NotServable("policy_not_yet_effective",
                                  "新口径尚未到生效时刻", 425)
            raise NotServable("policy_expired",
                              "政策已过生效区间，旧内容不得对外返回", 410)
        return current

    def _view(self, item, zh, ru, review, at, lang: str) -> dict:
        body = ru.body if ru is not None else zh.body
        return {
            "id": item.id,
            "kind": item.kind,
            "language": lang,
            "title": body.get("title"),
            "body": body,
            "zh_version": zh.no,
            "ru_version": ru.no if ru else None,
            "ru_bound_zh": ru.zh_no if ru else None,
            "ru_status": ru.status.value if ru else None,
            "resolved_utc": at.isoformat(),
            "policy": review.to_dict() if review else None,
            # 边缘节点据此设置硬过期：政策页在窗口结束的瞬间必须失效。
            "effective_until_utc": review.window.end.isoformat() if review else None,
        }

    # ---- 追溯关系 ------------------------------------------------------------

    def lineage(self, item_id: str) -> dict:
        """中俄文追溯关系：每个中文版本 -> 其俄文译文 / 政策复核。"""
        item = self.get(item_id)
        return {
            "id": item.id,
            "kind": item.kind,
            "organizer_id": item.organizer_id,
            "zh_versions": [
                {
                    **v.to_dict(),
                    "ru_versions": [r.to_dict() for r in item.ru_for(v.no)],
                    "reviews": [r.to_dict() for r in item.reviews if r.zh_no == v.no],
                }
                for v in item.zh
            ],
        }

    # ---- 内部 ----------------------------------------------------------------

    @staticmethod
    def _zh_version(item: ContentItem, no: int) -> ChineseVersion:
        for v in item.zh:
            if v.no == no:
                return v
        raise NotFound(f"中文 v{no} 不存在")

    @staticmethod
    def _ru_version(item: ContentItem, no: int) -> RussianVersion:
        for v in item.ru:
            if v.no == no:
                return v
        raise NotFound(f"俄文 v{no} 不存在")
