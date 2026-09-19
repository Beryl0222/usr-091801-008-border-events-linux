"""事件流、跨日收藏与带替代方案的通知。

事件（容量调整 / 临时取消 / 延期 / 接驳变化 / 商户停业）由承办方通过事件流汇入，
更新运营态表后，通知引擎扫描所有游客的**跨日安排**：受影响的安排生成一条中俄
双语通知，并尽量附上替代方案（同片区同时段仍在举办的活动、同走廊的备用接驳、
同片区仍营业的商户）。

服务端只按游客令牌哈希保存收藏关系；通知同样按键控哈希存储，不保存任何可还原
个人行程的明文。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .content import ContentService, NotServable
from .store import AuditLog, Clock, EventBus, IdGen
from .timeutil import PORTAL_TZ, portal_today
from .privacy import hash_visitor
from .visibility import Scope

# 受支持的事件种类与其影响语义。
EVENT_KINDS = {
    "capacity",        # 容量调整（payload: capacity / open_capacity）
    "cancel",          # 临时取消（payload: reason_zh/reason_ru）
    "postpone",        # 延期（payload: new_start_utc）
    "shuttle_change",  # 接驳班次变化（payload: trip, corridor, status, alternative_trip）
    "merchant_hours",  # 商户营业变化（payload: closed, open_change）
}


class EventError(Exception):
    code = "event_error"


@dataclass
class Occurrence:
    """运营态目录中的一条排期（活动场次 / 接驳班次 / 商户营业段）。"""

    entity_id: str
    kind: str            # activity / transport / merchant
    area: str
    start_utc: str | None = None
    end_utc: str | None = None
    corridor: str | None = None       # 接驳走廊（如 口岸↔体育中心）
    tags: list[str] = field(default_factory=list)
    status: str = "open"              # open / cancelled / postponed / changed / closed
    capacity: int | None = None
    extra: dict = field(default_factory=dict)

    def day(self, at=None) -> str:
        from datetime import datetime
        if self.start_utc is None:
            # 商户/无固定场次的实体按服务时钟的口岸日历日归类。
            return str(portal_today(at))
        return str(datetime.fromisoformat(self.start_utc).astimezone(PORTAL_TZ).date())

    def to_dict(self, at=None) -> dict:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "area": self.area,
            "day": self.day(at),
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "corridor": self.corridor,
            "tags": self.tags,
            "status": self.status,
            "capacity": self.capacity,
        }


@dataclass
class Plan:
    """游客收藏的一条跨日安排（仅存哈希侧）。"""

    visitor_hash: str
    entity_id: str
    day: str

    def key(self) -> tuple:
        return self.visitor_hash, self.entity_id, self.day


@dataclass
class Notification:
    id: str
    visitor_hash: str
    event_seq: int
    entity_id: str
    kind: str
    day: str
    title_zh: str
    title_ru: str
    message_zh: str
    message_ru: str
    conflict: str
    alternatives: list[dict]
    created_utc: str
    read: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event_seq": self.event_seq,
            "entity_id": self.entity_id,
            "kind": self.kind,
            "day": self.day,
            "title_zh": self.title_zh,
            "title_ru": self.title_ru,
            "message_zh": self.message_zh,
            "message_ru": self.message_ru,
            "conflict": self.conflict,
            "alternatives": self.alternatives,
            "created_utc": self.created_utc,
            "read": self.read,
        }


class EventService:
    def __init__(self, clock: Clock, idgen: IdGen, bus: EventBus,
                 content: ContentService, audit: AuditLog):
        self.clock = clock
        self.idgen = idgen
        self.bus = bus
        self.content = content
        self.audit = audit
        self.catalog: dict[str, Occurrence] = {}
        self._plans: dict[tuple, Plan] = {}
        self._notifications: dict[str, list[Notification]] = {}
        # 联动部门处置记录：event_seq -> 记录列表（内部可见）。
        self.dispatches: dict[int, list[dict]] = {}

    # ---- 排期目录 ------------------------------------------------------------

    def register_occurrence(self, occurrence: Occurrence) -> Occurrence:
        self.catalog[occurrence.entity_id] = occurrence
        return occurrence

    def occurrence(self, entity_id: str) -> Occurrence | None:
        return self.catalog.get(entity_id)

    def public_occurrences(self, day: str | None = None) -> list[dict]:
        at = self.clock()
        out = []
        for occ in self.catalog.values():
            if day is not None and occ.day(at) != day:
                continue
            if occ.status in ("cancelled", "closed"):
                continue
            out.append(occ.to_dict(at))
        return out

    # ---- 收藏 / 跨日安排 -----------------------------------------------------

    def save_plan(self, visitor_token: str, entity_id: str, day: str) -> Plan:
        if entity_id not in self.catalog:
            raise EventError(f"未知实体: {entity_id}")
        plan = Plan(hash_visitor(visitor_token), entity_id, day)
        self._plans[plan.key()] = plan
        return plan

    def remove_plan(self, visitor_token: str, entity_id: str, day: str) -> None:
        self._plans.pop((hash_visitor(visitor_token), entity_id, day), None)

    def plans_of(self, visitor_token: str) -> list[dict]:
        h = hash_visitor(visitor_token)
        return [
            {"entity_id": p.entity_id, "day": p.day}
            for p in self._plans.values()
            if p.visitor_hash == h
        ]

    # ---- 事件接入 ------------------------------------------------------------

    def ingest(self, kind: str, entity_id: str, payload: dict, actor: str) -> dict:
        if kind not in EVENT_KINDS:
            raise EventError(f"未知事件类型: {kind}")
        occ = self.catalog.get(entity_id)
        if occ is None:
            raise EventError(f"事件指向未登记实体: {entity_id}")

        event = self.bus.publish(topic=self._topic(occ), kind=kind,
                                 entity_id=entity_id, payload=dict(payload))
        # 必须在应用状态前确定受影响安排：延期会改写场次日期，否则收藏旧日期的游客会漏通知。
        affected = self._affected_plans(occ, kind, payload)
        self._apply_state(occ, kind, payload)
        self.audit.record(actor, f"event.{kind}", entity_id,
                          {"seq": event.seq, **{k: v for k, v in payload.items()
                                                if k not in ("reason_zh", "reason_ru")}})

        notifications = self._notify(event, occ, kind, payload, affected)
        return {
            "event": event.to_dict(),
            "state": occ.to_dict(self.clock()),
            "notified": len(notifications),
        }

    @staticmethod
    def _topic(occ: Occurrence) -> str:
        return {
            "activity": "events.activities",
            "transport": "events.shuttles",
            "merchant": "events.merchants",
        }[occ.kind]

    def _apply_state(self, occ: Occurrence, kind: str, payload: dict) -> None:
        if kind == "capacity":
            occ.capacity = payload.get("capacity", occ.capacity)
            if payload.get("capacity") == 0 or payload.get("sold_out"):
                occ.status = "sold_out"
        elif kind == "cancel":
            occ.status = "cancelled"
            occ.extra["cancel_reason"] = payload
        elif kind == "postpone":
            occ.status = "postponed"
            occ.start_utc = payload.get("new_start_utc", occ.start_utc)
            occ.end_utc = payload.get("new_end_utc", occ.end_utc)
            occ.extra["postpone"] = payload
        elif kind == "shuttle_change":
            occ.status = payload.get("status", "changed")
            occ.extra["shuttle"] = payload
        elif kind == "merchant_hours":
            if payload.get("closed"):
                occ.status = "closed"
            else:
                occ.status = "changed"
            occ.extra["hours"] = payload

    # ---- 通知引擎 ------------------------------------------------------------

    def _affected_plans(self, occ: Occurrence, kind: str, payload: dict) -> list[Plan]:
        day = occ.day(self.clock())
        plans = [p for p in self._plans.values() if p.entity_id == occ.entity_id]
        if kind == "postpone" and payload.get("new_start_utc"):
            from datetime import datetime
            new_day = str(datetime.fromisoformat(payload["new_start_utc"])
                          .astimezone(PORTAL_TZ).date())
            plans = [p for p in plans if p.day in (day, new_day)]
        return plans

    def _notify(self, event, occ: Occurrence, kind: str, payload: dict,
                plans: list[Plan]) -> list[Notification]:
        created = []
        for plan in plans:
            conflict, msg_zh, msg_ru, alternatives = self._build_impact(occ, kind, payload)
            note = Notification(
                id=self.idgen.next("ntf"),
                visitor_hash=plan.visitor_hash,
                event_seq=event.seq,
                entity_id=occ.entity_id,
                kind=kind,
                day=plan.day,
                title_zh=self._title(occ.entity_id, "zh"),
                title_ru=self._title(occ.entity_id, "ru"),
                message_zh=msg_zh,
                message_ru=msg_ru,
                conflict=conflict,
                alternatives=alternatives,
                created_utc=self.clock().isoformat(),
            )
            self._notifications.setdefault(plan.visitor_hash, []).append(note)
            created.append(note)
        return created

    def _build_impact(self, occ: Occurrence, kind: str, payload: dict):
        """返回 (冲突类型, 中文说明, 俄文说明, 替代方案列表)。"""
        reason_zh = payload.get("reason_zh", "")
        reason_ru = payload.get("reason_ru", "")
        if kind == "cancel":
            alts = self._alternate_activities(occ)
            return (
                "cancelled",
                f"您安排的活动已临时取消。{reason_zh} 为您推荐同时段同片区的替代活动。",
                f"Мероприятие в вашем плане временно отменено. {reason_ru} "
                "Подобраны альтернативы в том же районе на это же время.",
                alts,
            )
        if kind == "postpone":
            new_day = occ.day(self.clock())
            cross_day = payload.get("cross_day", False)
            alts = self._alternate_activities(occ)
            conflict = "postponed_cross_day" if cross_day else "postponed"
            return (
                conflict,
                f"活动延期至 {new_day}（口岸当地时间），"
                + ("跨日安排与您的其他行程冲突。" if cross_day else "时刻已调整。")
                + " 可改选以下活动。",
                f"Мероприятие перенесено на {new_day} (местное время порта). "
                + ("Перенос через сутки конфликтует с другими пунктами плана. " if cross_day else "")
                + "Возможные замены:",
                alts,
            )
        if kind == "capacity":
            return (
                "sold_out",
                "活动容量已约满，您可能无法入场，建议改选同片区活动。",
                "Места закончились, вход может быть недоступен. Рекомендуем другую программу рядом.",
                self._alternate_activities(occ),
            )
        if kind == "shuttle_change":
            alt_trip = payload.get("alternative_trip")
            alts = ([self._shuttle_alternative(occ, alt_trip)] if alt_trip
                    else self._alternate_shuttles(occ))
            return (
                "shuttle_changed",
                f"接驳班次发生变化：{reason_zh or '原班次调整'}。请改乘备用班次。",
                f"Изменилось расписание шаттла: {reason_ru or 'рейс скорректирован'}. "
                "Воспользуйтесь резервным рейсом.",
                alts,
            )
        if kind == "merchant_hours":
            return (
                "merchant_closed",
                f"商户临时停业：{reason_zh or '营业时间调整'}。附近仍营业的商户：",
                f"Торговая точка временно закрыта: {reason_ru or 'изменение часов работы'}. "
                "Рядом открыты:",
                self._alternate_merchants(occ),
            )
        raise EventError(f"未实现的事件影响: {kind}")

    # ---- 替代方案 ------------------------------------------------------------

    def _public_card(self, entity_id: str) -> dict | None:
        """替代方案卡片：俄文标题取自现行译文；译文过期时回退中文并明确标注。"""
        try:
            item = self.content.get(entity_id)
        except Exception:
            return None
        card = {"id": entity_id, "area": self.catalog[entity_id].area}
        try:
            view = self.content.resolve(entity_id, lang="ru", at=self.clock())
            card["language"] = "ru"
            card["title"] = view["title"]
        except NotServable:
            try:
                view = self.content.resolve(entity_id, lang="zh", at=self.clock())
                card["language"] = "zh"
                card["title"] = view["title"]
                card["translation_pending"] = True
            except NotServable:
                return None
        occ = self.catalog.get(entity_id)
        if occ:
            card["day"] = occ.day(self.clock())
            card["start_utc"] = occ.start_utc
            card["status"] = occ.status
        return card

    def _alternate_activities(self, occ: Occurrence, limit: int = 3) -> list[dict]:
        same_day = occ.day(self.clock())
        candidates = []
        for other_id, other in self.catalog.items():
            if other_id == occ.entity_id or other.kind != "activity":
                continue
            if other.status in ("cancelled", "sold_out", "postponed"):
                continue
            if other.day(self.clock()) != same_day:
                continue
            card = self._public_card(other_id)
            if card is None:
                continue
            score = 0 if other.area == occ.area else 1
            if set(other.tags) & set(occ.tags):
                score -= 1
            candidates.append((score, card))
        candidates.sort(key=lambda pair: pair[0])
        return [card for _, card in candidates[:limit]]

    def _shuttle_alternative(self, occ: Occurrence, trip: str) -> dict:
        return {
            "id": occ.entity_id,
            "kind": "transport",
            "area": occ.area,
            "corridor": occ.corridor,
            "alternative_trip": trip,
            "language": "ru",
            "title": trip,
        }

    def _alternate_shuttles(self, occ: Occurrence, limit: int = 3) -> list[dict]:
        out = []
        for other_id, other in self.catalog.items():
            if other.kind != "transport" or other_id == occ.entity_id:
                continue
            if other.status in ("cancelled", "changed"):
                continue
            if other.corridor != occ.corridor:
                continue
            card = self._public_card(other_id)
            if card:
                card["corridor"] = other.corridor
                out.append(card)
        return out[:limit]

    def _alternate_merchants(self, occ: Occurrence, limit: int = 3) -> list[dict]:
        out = []
        for other_id, other in self.catalog.items():
            if other.kind != "merchant" or other_id == occ.entity_id:
                continue
            if other.status in ("closed",):
                continue
            if other.area != occ.area:
                continue
            card = self._public_card(other_id)
            if card:
                out.append(card)
        return out[:limit]

    def _title(self, entity_id: str, lang: str) -> str:
        try:
            view = self.content.resolve(entity_id, lang=lang, at=self.clock())
            return view["title"] or entity_id
        except NotServable:
            return entity_id

    # ---- 游客取通知 ----------------------------------------------------------

    def notifications_for(self, visitor_token: str, unread_only: bool = False) -> list[dict]:
        h = hash_visitor(visitor_token)
        notes = self._notifications.get(h, [])
        if unread_only:
            notes = [n for n in notes if not n.read]
        return [n.to_dict() for n in notes]

    def mark_read(self, visitor_token: str, notification_id: str) -> bool:
        h = hash_visitor(visitor_token)
        for note in self._notifications.get(h, []):
            if note.id == notification_id:
                note.read = True
                return True
        return False

    # ---- 联动部门处置 --------------------------------------------------------

    def record_dispatch(self, event_seq: int, dept: str, action: str, note: str) -> dict:
        entry = {
            "event_seq": event_seq,
            "dept": dept,
            "action": action,
            "note": note,
            "scope": Scope.INTERNAL_DISPATCH.value,
            "created_utc": self.clock().isoformat(),
        }
        self.dispatches.setdefault(event_seq, []).append(entry)
        self.audit.record(dept, "dispatch.record", f"event:{event_seq}", {"note": note})
        return entry

    def dispatches_for(self, event_seq: int) -> list[dict]:
        return list(self.dispatches.get(event_seq, []))

    # ---- 事件流读取 ----------------------------------------------------------

    def stream(self, after_seq: int, topics: list[str] | None = None,
               wait: float | None = None) -> dict:
        if wait is not None:
            self.bus.wait_for(after_seq, timeout=wait)
        events = self.bus.since(after_seq, topics=topics)
        return {
            "after": after_seq,
            "latest": self.bus.latest_seq,
            "events": [e.to_dict() for e in events],
        }
