"""口岸当地时区与生效区间工具。

所有对外接收到的时间必须携带时区偏移，内部统一换算为 UTC 存储；
仅在为运营人员展示时换算回口岸当地时区，避免跨时区发布产生歧义。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PORT_TZ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def parse_instant(text: str) -> datetime:
    """解析带时区偏移的 ISO 时间并统一为 UTC；裸时间一律拒绝。"""
    instant = datetime.fromisoformat(text)
    if instant.tzinfo is None:
        raise ValueError("时间必须携带时区偏移，禁止裸时间")
    return instant.astimezone(UTC)


@dataclass(frozen=True)
class EffectiveWindow:
    """生效区间 [starts_at, ends_at)，内部一律保存 UTC 时刻。"""

    starts_at: datetime
    ends_at: datetime

    def __post_init__(self):
        for value in (self.starts_at, self.ends_at):
            if value.tzinfo is None:
                raise ValueError("生效区间必须使用带时区的时间")
        if not self.starts_at < self.ends_at:
            raise ValueError("生效区间起点必须早于终点")

    @classmethod
    def from_text(cls, starts_at: str, ends_at: str) -> "EffectiveWindow":
        return cls(parse_instant(starts_at), parse_instant(ends_at))

    def contains(self, instant: datetime) -> bool:
        return self.starts_at <= instant < self.ends_at

    def port_local(self) -> tuple[str, str]:
        """以口岸当地时区展示，便于运营人员核对。"""
        return (
            self.starts_at.astimezone(PORT_TZ).isoformat(),
            self.ends_at.astimezone(PORT_TZ).isoformat(),
        )
