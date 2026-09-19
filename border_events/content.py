"""内容发布与对外服务：版本化中文事实源、译稿追溯、政策复核与缓存再验证。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from .models import (
    ContentItem,
    ContentKind,
    ContentStatus,
    PolicyReview,
    ReviewDecision,
    Role,
    Schedule,
    Translation,
    Visibility,
    visible_to,
)
from .timeutil import UTC, EffectiveWindow

CACHE_TTL = timedelta(minutes=10)


class CacheNode:
    """模拟边缘缓存节点。

    缓存条目自带内容版本与有效截止时刻：即便节点仍持有旧字节，
    服务层在返回前必须再验证，过期或版本落后的条目不得对外返回。
    """

    def __init__(self):
        self._entries: dict[tuple[str, str], dict] = {}
        self.purge_count = 0

    def get(self, key: tuple[str, str]) -> dict | None:
        return self._entries.get(key)

    def put(self, key: tuple[str, str], entry: dict) -> None:
        self._entries[key] = entry

    def purge(self, content_id: str) -> None:
        doomed = [key for key in self._entries if key[0] == content_id]
        for key in doomed:
            del self._entries[key]
        self.purge_count += len(doomed)

    def peek(self, key: tuple[str, str]) -> dict | None:
        """仅用于检查节点上是否仍物理持有旧条目（测试与巡检用）。"""
        return self._entries.get(key)


class ContentService:
    """中文事实源的提交、修订、复核、发布与多语言对外服务。"""

    def __init__(self, clock: Callable[[], datetime] | None = None):
        self._items: dict[str, ContentItem] = {}
        self._clock = clock or (lambda: datetime.now(UTC))
        self.cache = CacheNode()

    # ---- 中文事实源 ----

    def submit(
        self,
        *,
        id: str,
        kind: ContentKind,
        source_org: str,
        title_zh: str,
        body_zh: str,
        visibility: Visibility = Visibility.PUBLIC,
        schedule: Schedule | None = None,
    ) -> ContentItem:
        if id in self._items:
            raise ValueError(f"内容已存在: {id}")
        item = ContentItem(
            id=id,
            kind=kind,
            visibility=visibility,
            source_org=source_org,
            title_zh=title_zh,
            body_zh=body_zh,
            schedule=schedule,
        )
        self._items[id] = item
        return item

    def get(self, content_id: str) -> ContentItem | None:
        return self._items.get(content_id)

    def iter_items(self):
        """只读遍历全部条目，供替代方案检索等内部场景使用。"""
        return iter(self._items.values())

    def revise(self, content_id: str, *, title_zh: str, body_zh: str) -> ContentItem:
        """责任单位修订中文事实源：版本号递增并回到草稿，既有译稿随之过期。"""
        item = self._require(content_id)
        item.title_zh = title_zh
        item.body_zh = body_zh
        item.version += 1
        item.status = ContentStatus.DRAFT
        self.cache.purge(content_id)
        return item

    # ---- 政策复核与发布 ----

    def review_policy(
        self,
        content_id: str,
        *,
        reviewer: str,
        decision: ReviewDecision,
        window: EffectiveWindow,
        expedited: bool = False,
    ) -> PolicyReview:
        item = self._require(content_id)
        review = PolicyReview(
            version=item.version,
            reviewer=reviewer,
            decision=decision,
            reviewed_at=self._clock(),
            window=window,
            expedited=expedited,
        )
        item.reviews.append(review)
        return review

    def publish(self, content_id: str) -> ContentItem:
        """发布当前版本；通关类必须先通过政策复核并取得生效区间。"""
        item = self._require(content_id)
        if item.kind is ContentKind.POLICY and item.approved_window(self._clock()) is None:
            raise PermissionError("通关类内容未经政策复核通过，不得发布")
        item.status = ContentStatus.PUBLISHED
        self.cache.purge(content_id)
        return item

    def retract(self, content_id: str) -> ContentItem:
        item = self._require(content_id)
        item.status = ContentStatus.RETRACTED
        self.cache.purge(content_id)
        return item

    def emergency_correct(
        self,
        content_id: str,
        *,
        title_zh: str,
        body_zh: str,
        reviewer: str,
        window: EffectiveWindow | None = None,
    ) -> ContentItem:
        """紧急更正：一次调用完成修订、加急复核与发布，并同步清除缓存。

        通关类内容即使在紧急通道下也必须留下复核记录与生效区间，
        调用返回后任何读路径都不得再返回旧版本。
        """
        item = self._require(content_id)
        item.title_zh = title_zh
        item.body_zh = body_zh
        item.version += 1
        if item.kind is ContentKind.POLICY:
            if window is None:
                raise ValueError("通关类紧急更正必须给出新的生效区间")
            self.review_policy(
                content_id,
                reviewer=reviewer,
                decision=ReviewDecision.APPROVED,
                window=window,
                expedited=True,
            )
        item.status = ContentStatus.PUBLISHED
        self.cache.purge(content_id)
        return item

    # ---- 译稿 ----

    def add_translation(
        self,
        content_id: str,
        *,
        lang: str,
        translator: str,
        title: str,
        body: str,
    ) -> Translation:
        """译员基于当前中文版本提交译稿，记录源版本形成追溯关系。"""
        item = self._require(content_id)
        translation = Translation(
            lang=lang,
            source_version=item.version,
            translator=translator,
            title=title,
            body=body,
            updated_at=self._clock(),
        )
        item.translations[lang] = translation
        self.cache.purge(content_id)
        return translation

    # ---- 对外服务 ----

    def serve(
        self,
        content_id: str,
        *,
        lang: str = "zh",
        role: Role = Role.VISITOR,
        now: datetime | None = None,
    ) -> dict | None:
        """按可见范围与时效对外提供内容；不满足条件一律返回 None。"""
        now = now or self._clock()
        item = self._items.get(content_id)
        if item is None or item.status is not ContentStatus.PUBLISHED:
            return None
        if not visible_to(item.visibility, role):
            return None
        window = None
        if item.kind is ContentKind.POLICY:
            window = item.approved_window(now)
            if window is None or not window.contains(now):
                # 过期或复核失效的政策内容，即使缓存仍在也不得返回
                return None
        return self._render(item, lang, now, window)

    def _render(
        self,
        item: ContentItem,
        lang: str,
        now: datetime,
        window: EffectiveWindow | None,
    ) -> dict | None:
        key = (item.id, lang)
        cached = self.cache.get(key)
        if (
            cached is not None
            and cached["version"] == item.version
            and cached["valid_until"] > now
        ):
            return cached["payload"]

        if lang == "zh":
            title, body, trace = item.title_zh, item.body_zh, None
        else:
            translation = item.translations.get(lang)
            if translation is None:
                return None  # 没有译稿的语言不对外提供
            stale = translation.source_version != item.version
            if stale and item.kind is ContentKind.POLICY:
                return None  # 政策类译稿落后于中文版本时不得对外返回
            title, body = translation.title, translation.body
            trace = {
                "source_version": translation.source_version,
                "current_version": item.version,
                "stale": stale,
                "translator": translation.translator,
            }

        payload = {
            "id": item.id,
            "kind": item.kind.value,
            "lang": lang,
            "title": title,
            "body": body,
            "version": item.version,
            "source_org": item.source_org,
            "trace": trace,
            "window": window.port_local() if window else None,
            "schedule": (
                {
                    "starts_at": item.schedule.starts_at.isoformat(),
                    "ends_at": item.schedule.ends_at.isoformat(),
                    "capacity": item.schedule.capacity,
                    "location": item.schedule.location,
                }
                if item.schedule
                else None
            ),
        }
        valid_until = now + CACHE_TTL
        if window is not None:
            valid_until = min(valid_until, window.ends_at)
        self.cache.put(key, {"version": item.version, "valid_until": valid_until, "payload": payload})
        return payload

    def _require(self, content_id: str) -> ContentItem:
        item = self._items.get(content_id)
        if item is None:
            raise KeyError(f"内容不存在: {content_id}")
        return item
