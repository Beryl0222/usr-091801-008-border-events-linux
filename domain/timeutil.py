"""时区与生效区间工具。

所有时刻在系统内部统一以带时区的 UTC ``datetime`` 表示；面向中俄用户展示时，
再按各自时区渲染。政策类内容的生效区间使用口岸当地时区（Asia/Shanghai）描述，
避免“按本地日历理解有效期”造成的歧义。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

PORTAL_TZ = ZoneInfo("Asia/Shanghai")
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
UTC = timezone.utc

# 游客侧“即将开始 / 刚刚结束”的判定半径。
SOFT_WINDOW = timedelta(minutes=15)


def now_utc() -> datetime:
    """当前 UTC 时刻（aware）。"""
    return datetime.now(UTC)


def portal_today(utc_dt: datetime | None = None) -> date:
    """口岸当地日历日，跨时区发布按此对齐。"""
    return (utc_dt or now_utc()).astimezone(PORTAL_TZ).date()


def local_dt(day: date, hour_minute: str, zone: ZoneInfo) -> datetime:
    """把 ``YYYY-MM-DD`` 与 ``HH:MM``（当地时间）解释为 aware UTC。"""
    hour, minute = (int(part) for part in hour_minute.split(":"))
    wall = datetime.combine(day, time(hour, minute), tzinfo=zone)
    return wall.astimezone(UTC)


def to_local(utc_dt: datetime, zone: ZoneInfo) -> datetime:
    return utc_dt.astimezone(zone)


def format_local(utc_dt: datetime, zone: ZoneInfo, with_time: bool = True) -> str:
    """渲染给运营复核界面使用的当地时间字符串。"""
    wall = utc_dt.astimezone(zone)
    pattern = "%Y-%m-%d %H:%M %Z" if with_time else "%Y-%m-%d"
    return wall.strftime(pattern)


@dataclass(frozen=True)
class Window:
    """半开生效区间 ``[start, end)``，内部以 UTC 存储，记录来源时区。"""

    start: datetime
    end: datetime
    zone_name: str = "Asia/Shanghai"

    def __post_init__(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("窗口边界必须带时区")
        if self.end <= self.start:
            raise ValueError("生效区间结束必须晚于开始")

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.zone_name)

    def contains(self, utc_dt: datetime) -> bool:
        return self.start <= utc_dt < self.end

    def overlaps(self, other: "Window") -> bool:
        return self.start < other.end and other.start < self.end

    def to_dict(self) -> dict:
        zone = self.zone
        start_wall = self.start.astimezone(zone)
        end_wall = self.end.astimezone(zone)
        return {
            "timezone": self.zone_name,
            "start": start_wall.strftime("%Y-%m-%d %H:%M"),
            "end": end_wall.strftime("%Y-%m-%d %H:%M"),
            "start_utc": self.start.isoformat(),
            "end_utc": self.end.isoformat(),
        }


def minutes_between(later: datetime, earlier: datetime) -> int:
    return int((later - earlier).total_seconds() // 60)
