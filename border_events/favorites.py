"""游客收藏与冲突通知：跨日安排被事件流打乱时，给出带替代方案的通知。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .content import ContentService
from .models import ContentItem, ContentStatus, Visibility
from .timeutil import PORT_TZ


@dataclass
class Notification:
    """发给游客的变更通知，必须附带可执行的替代方案。"""

    visitor_token: str
    content_id: str
    reason: str
    alternatives: list[str]
    created_at: datetime


class FavoriteService:
    """按游客匿名令牌维护收藏，并在内容变更时检测跨日冲突。"""

    def __init__(self, contents: ContentService, clock):
        self._contents = contents
        self._clock = clock
        self._favorites: dict[str, set[str]] = {}
        self._outbox: dict[str, list[Notification]] = {}

    def add(self, visitor_token: str, content_id: str) -> None:
        if self._contents.get(content_id) is None:
            raise KeyError(f"内容不存在: {content_id}")
        self._favorites.setdefault(visitor_token, set()).add(content_id)

    def remove(self, visitor_token: str, content_id: str) -> None:
        self._favorites.get(visitor_token, set()).discard(content_id)

    def favorites_of(self, visitor_token: str) -> set[str]:
        return set(self._favorites.get(visitor_token, set()))

    def affected_visitors(self, content_id: str) -> list[str]:
        return [token for token, ids in self._favorites.items() if content_id in ids]

    def conflicts_of(self, visitor_token: str) -> list[tuple[str, str]]:
        """检测收藏行程中两两时间重叠的条目（跨日安排改期后最易触发）。"""
        items = [
            self._contents.get(cid)
            for cid in self._favorites.get(visitor_token, set())
        ]
        scheduled = [
            item
            for item in items
            if item is not None
            and item.status is ContentStatus.PUBLISHED
            and item.schedule is not None
        ]
        scheduled.sort(key=lambda item: item.id)
        conflicts = []
        for i, left in enumerate(scheduled):
            for right in scheduled[i + 1 :]:
                if left.schedule.overlaps(right.schedule):
                    conflicts.append((left.id, right.id))
        return conflicts

    def notify_change(self, content_id: str, reason: str) -> list[Notification]:
        """内容变更后通知收藏者，并附上同口岸当日的替代方案。"""
        sent = []
        for token in self.affected_visitors(content_id):
            notification = Notification(
                visitor_token=token,
                content_id=content_id,
                reason=reason,
                alternatives=self._alternatives(token, content_id),
                created_at=self._clock(),
            )
            self._outbox.setdefault(token, []).append(notification)
            sent.append(notification)
        return sent

    def inbox_of(self, visitor_token: str) -> list[Notification]:
        return list(self._outbox.get(visitor_token, []))

    def _alternatives(self, visitor_token: str, content_id: str) -> list[str]:
        """替代方案：同类型、公开、仍在发布、口岸当地同日且不与行程冲突。"""
        original = self._contents.get(content_id)
        if original is None or original.schedule is None:
            return []
        original_day = original.schedule.starts_at.astimezone(PORT_TZ).date()
        taken = self._favorites.get(visitor_token, set()) | {content_id}
        candidates = []
        for item in self._contents.iter_items():
            if (
                item.id in taken
                or item.kind is not original.kind
                or item.visibility is not Visibility.PUBLIC
                or item.status is not ContentStatus.PUBLISHED
                or item.schedule is None
            ):
                continue
            if item.schedule.capacity is not None and item.schedule.capacity <= 0:
                continue
            if item.schedule.starts_at.astimezone(PORT_TZ).date() != original_day:
                continue
            candidates.append(item)
        candidates.sort(
            key=lambda item: abs(
                item.schedule.starts_at - original.schedule.starts_at
            )
        )
        return [item.id for item in candidates]
