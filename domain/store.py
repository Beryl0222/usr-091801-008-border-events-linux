"""存储内核：可注入时钟、ID 生成、事件总线与审计日志。

刻意做成内存实现，方便测试用例把时钟拨到任意时区时刻；生产环境替换为持久化
实现时保持相同接口即可。
"""

from __future__ import annotations

import itertools
import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterable

from .timeutil import now_utc
from .privacy import redact


class Clock:
    """可替换的时钟。默认走真实时间；测试可固定或手动推进。"""

    def __init__(self, fixed=None):
        self._fixed = fixed

    def __call__(self):
        return self._fixed or now_utc()

    def set(self, utc_dt) -> None:
        self._fixed = utc_dt


class IdGen:
    """带前缀、进程内唯一的短 ID（``ev_0001`` 形式），便于样例与断言阅读。"""

    def __init__(self):
        self._counters: dict[str, itertools.count] = {}
        self._lock = threading.Lock()

    def next(self, prefix: str) -> str:
        with self._lock:
            counter = self._counters.setdefault(prefix, itertools.count(1))
            return f"{prefix}_{next(counter):04d}"


@dataclass
class Event:
    seq: int
    topic: str
    kind: str
    entity_id: str
    payload: dict
    created_utc: str

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "topic": self.topic,
            "kind": self.kind,
            "entity_id": self.entity_id,
            "payload": self.payload,
            "created_utc": self.created_utc,
        }


class EventBus:
    """线程安全的追加式事件流。

    事件获得全局单调递增 ``seq``，消费端按游标（last seen seq）拉取增量，
    既支持实时订阅者，也支持断线后从旧游标补齐。
    """

    def __init__(self, clock: Clock, idgen: IdGen, maxlen: int = 10_000):
        self._clock = clock
        self._idgen = idgen
        self._events: Deque[Event] = deque(maxlen=maxlen)
        self._cond = threading.Condition()
        self._seq = 0

    @property
    def latest_seq(self) -> int:
        with self._cond:
            return self._seq

    def publish(self, topic: str, kind: str, entity_id: str, payload: dict) -> Event:
        with self._cond:
            self._seq += 1
            event = Event(
                seq=self._seq,
                topic=topic,
                kind=kind,
                entity_id=entity_id,
                payload=payload,
                created_utc=self._clock().isoformat(),
            )
            self._events.append(event)
            self._cond.notify_all()
        return event

    def since(self, after_seq: int, topics: Iterable[str] | None = None,
              limit: int = 200) -> list[Event]:
        wanted = set(topics) if topics is not None else None
        with self._cond:
            out = [
                e for e in self._events
                if e.seq > after_seq and (wanted is None or e.topic in wanted)
            ]
        return out[:limit]

    def wait_for(self, after_seq: int, timeout: float = 15.0) -> int:
        """阻塞直到出现比 ``after_seq`` 更新的事件，返回最新 seq；超时返回当前 seq。"""
        with self._cond:
            self._cond.wait_for(lambda: self._seq > after_seq, timeout=timeout)
            return self._seq


@dataclass
class AuditEntry:
    seq: int
    actor: str
    action: str
    target: str
    detail: dict
    created_utc: str


class AuditLog:
    """仅追加的操作审计，自由文本字段落盘前脱敏。

    审计记录的是“谁在何时对哪个版本做了什么”，不记录任何游客令牌或浏览轨迹。
    """

    SENSITIVE_TEXT_KEYS = ("note", "reason", "detail_text", "message")

    def __init__(self, clock: Clock):
        self._clock = clock
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()
        self._seq = 0

    def record(self, actor: str, action: str, target: str, detail: dict | None = None) -> AuditEntry:
        clean = dict(detail or {})
        for key in self.SENSITIVE_TEXT_KEYS:
            if isinstance(clean.get(key), str):
                clean[key] = redact(clean[key])
        with self._lock:
            self._seq += 1
            entry = AuditEntry(
                seq=self._seq,
                actor=actor,
                action=action,
                target=target,
                detail=clean,
                created_utc=self._clock().isoformat(),
            )
            self._entries.append(entry)
        return entry

    def entries_for(self, target: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._entries)
        if target is not None:
            items = [e for e in items if e.target == target]
        return [
            {
                "seq": e.seq,
                "actor": e.actor,
                "action": e.action,
                "target": e.target,
                "detail": e.detail,
                "created_utc": e.created_utc,
            }
            for e in items
        ]
