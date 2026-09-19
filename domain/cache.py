"""边缘缓存节点：硬过期闸门与按标签清除。

这是对靠近游客的缓存节点的模拟。核心安全约束：

* 政策报文写入缓存时带 ``effective_until``（生效区间结束时刻）；
* 读取时只要当前时刻到达/越过该时刻，**报文体绝不出缓存模块**——即使源站不可达、
  即使仍有客户端在请求，也只能返回"已抑制"信号（对外映射为 410），从不存在
  "旧政策先顶着"的路径；
* 中文事实源换版、政策紧急复核通过、活动取消时，源站按实体标签清除，所有语言
  变体（``zh``/``ru``）同时失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .store import AuditLog, Clock


class CacheResult(str, Enum):
    HIT = "hit"
    MISS = "miss"
    # 命中了旧报文但已过硬过期：被抑制，报文体不可外泄。
    EXPIRED_SUPPRESSED = "expired_suppressed"
    # 命中的报文被源站主动失效（换版/取消/紧急更正）。
    PURGED = "purged"


@dataclass
class CacheEntry:
    body: dict
    tags: frozenset[str]
    effective_until_utc: str | None
    cached_utc: str

    def expired(self, now) -> bool:
        if self.effective_until_utc is None:
            return False
        from datetime import datetime
        return now >= datetime.fromisoformat(self.effective_until_utc)


class EdgeCache:
    def __init__(self, clock: Clock, audit: AuditLog, node: str = "edge-primary"):
        self.clock = clock
        self.audit = audit
        self.node = node
        self._store: dict[str, CacheEntry] = {}
        self.stats = {"hits": 0, "misses": 0, "suppressed": 0, "purges": 0}

    # ---- 写入 ---------------------------------------------------------------

    def put(self, key: str, body: dict, tags: set[str],
            effective_until_utc: str | None = None) -> None:
        self._store[key] = CacheEntry(
            body=body,
            tags=frozenset(tags),
            effective_until_utc=effective_until_utc,
            cached_utc=self.clock().isoformat(),
        )
        self.audit.record(self.node, "cache.put", key,
                          {"tags": sorted(tags), "effective_until_utc": effective_until_utc})

    # ---- 读取 ---------------------------------------------------------------

    def get(self, key: str) -> tuple[CacheResult, dict | None]:
        """返回 (结果, 报文)。任何非 HIT 结果下报文恒为 ``None``。"""
        entry = self._store.get(key)
        if entry is None:
            self.stats["misses"] += 1
            return CacheResult.MISS, None
        if entry.expired(self.clock()):
            # 硬闸：立即物理删除，报文体不返回给任何调用方。
            del self._store[key]
            self.stats["suppressed"] += 1
            self.audit.record(self.node, "cache.suppress_expired", key, {})
            return CacheResult.EXPIRED_SUPPRESSED, None
        self.stats["hits"] += 1
        return CacheResult.HIT, dict(entry.body)

    # ---- 失效 ---------------------------------------------------------------

    def purge_tags(self, tags: set[str], actor: str = "origin") -> list[str]:
        removed = []
        for key, entry in list(self._store.items()):
            if entry.tags & tags:
                del self._store[key]
                removed.append(key)
        self.stats["purges"] += len(removed)
        if removed:
            self.audit.record(actor, "cache.purge", ",".join(sorted(tags)),
                              {"keys": removed})
        return removed

    def purge_entity(self, entity_id: str, actor: str = "origin") -> list[str]:
        return self.purge_tags({f"entity:{entity_id}"}, actor=actor)

    def has(self, key: str) -> bool:
        return key in self._store

    def keys_for_entity(self, entity_id: str) -> list[str]:
        tag = f"entity:{entity_id}"
        return [k for k, e in self._store.items() if tag in e.tags]
