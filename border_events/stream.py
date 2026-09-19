"""变化事件流：容量、取消、改期、接驳与商户营业变化统一汇入。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .content import ContentService
from .favorites import FavoriteService, Notification
from .timeutil import parse_instant


class ChangeKind(Enum):
    CANCELLED = "cancelled"  # 临时取消
    RESCHEDULED = "rescheduled"  # 延期/改期
    CAPACITY_REDUCED = "capacity_reduced"  # 容量调整
    SHUTTLE_CHANGED = "shuttle_changed"  # 接驳班次变化
    MERCHANT_CLOSED = "merchant_closed"  # 商户临时停业


@dataclass
class ChangeEvent:
    kind: ChangeKind
    target_id: str
    occurred_at: datetime
    data: dict = field(default_factory=dict)


class EventStream:
    """把承办方与联动部门的变化事件应用到内容，并触发收藏通知。"""

    def __init__(self, contents: ContentService, favorites: FavoriteService):
        self._contents = contents
        self._favorites = favorites
        self._log: list[ChangeEvent] = []

    def ingest(self, event: ChangeEvent) -> list[Notification]:
        item = self._contents.get(event.target_id)
        if item is None:
            raise KeyError(f"事件目标不存在: {event.target_id}")
        self._log.append(event)

        if event.kind is ChangeKind.CANCELLED or event.kind is ChangeKind.MERCHANT_CLOSED:
            self._contents.retract(item.id)
        elif event.kind is ChangeKind.RESCHEDULED:
            item.schedule.starts_at = parse_instant(event.data["starts_at"])
            item.schedule.ends_at = parse_instant(event.data["ends_at"])
            self._contents.cache.purge(item.id)
        elif event.kind is ChangeKind.CAPACITY_REDUCED:
            item.schedule.capacity = int(event.data["capacity"])
            self._contents.cache.purge(item.id)
        elif event.kind is ChangeKind.SHUTTLE_CHANGED:
            item.schedule.starts_at = parse_instant(event.data["starts_at"])
            item.schedule.ends_at = parse_instant(event.data["ends_at"])
            self._contents.cache.purge(item.id)
        else:  # pragma: no cover - 防御未知事件类型
            raise ValueError(f"未知事件类型: {event.kind}")

        return self._favorites.notify_change(item.id, event.kind.value)

    @property
    def log(self) -> list[ChangeEvent]:
        return list(self._log)
