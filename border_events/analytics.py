"""浏览与到场转化的隐私保护统计。

只保存按（日期, 内容, 指标）聚合的计数，不记录任何游客标识，
因此存储中不存在可还原个人行程的轨迹；导出时支持小单元格抑制，
避免稀疏数据被反推出个体行为。
"""

from __future__ import annotations

from collections import Counter
from datetime import date

DEFAULT_MIN_COUNT = 5


class Analytics:
    def __init__(self):
        self._counters: Counter[tuple[date, str, str]] = Counter()

    def record_view(self, content_id: str, *, day: date) -> None:
        """记录一次浏览。注意：签名中不接受任何游客标识。"""
        self._counters[(day, content_id, "view")] += 1

    def record_arrival(self, content_id: str, *, day: date) -> None:
        """记录一次到场转化。同样不携带游客标识，无法与浏览拼接成行程。"""
        self._counters[(day, content_id, "arrival")] += 1

    def export(self, *, min_count: int = DEFAULT_MIN_COUNT) -> list[dict]:
        """导出聚合报表，低于阈值的单元格被抑制。"""
        rows = [
            {"day": day.isoformat(), "content_id": cid, "metric": metric, "count": count}
            for (day, cid, metric), count in self._counters.items()
            if count >= min_count
        ]
        return sorted(rows, key=lambda row: (row["day"], row["content_id"], row["metric"]))
