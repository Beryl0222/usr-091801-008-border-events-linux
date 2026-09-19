"""隐私防护：不落可还原个人行程的轨迹。

游客用不透明令牌（仅存在于游客自己的设备上）标识收藏与通知订阅。服务端只保存
令牌的加盐哈希；浏览/到场先按“实体 × 当地日历日”汇入哈希集合，只有在达到
匿名阈值后才对外输出转化率，任何单条浏览、到场或收藏关系都不写日志、不进导出。
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import date

from .timeutil import portal_today

# 一个转化桶至少覆盖多少互不相同的游客，才允许输出聚合指标。
K_ANONYMITY = 5

_PATTERNS = (
    (re.compile(r"\b1[3-9]\d{9}\b"), "<手机已脱敏>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<邮箱已脱敏>"),
    # 护照号风格：一位字母加八位数字，或九位数字。
    (re.compile(r"\b[A-Za-z]\d{8}\b|\b\d{9}\b"), "<证件号已脱敏>"),
)


def pepper() -> bytes:
    """部署级加盐；未配置时使用固定开发盐并在启动检查中提示。"""
    return os.environ.get("BILINGUAL_PEPPER", "dev-pepper").encode()


def hash_visitor(token: str) -> str:
    """把游客令牌变成服务端可存储但不可还原的标识。"""
    if not token:
        raise ValueError("游客令牌不能为空")
    digest = hashlib.sha256(pepper() + b"|" + token.encode("utf-8"))
    return "v_" + digest.hexdigest()[:32]


def redact(text: str) -> str:
    """对自由文本做脱敏，供日志与跨部门记录导出使用。"""
    cleaned = text
    for pattern, replacement in _PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


class PrivacyLedger:
    """浏览与到场的 k-匿名聚合账。

    保存的是按桶划分的游客哈希集合（内存态、随进程消失），只通过
    :meth:`conversion` 输出达标桶的比率；刻意不提供逐条枚举接口。
    """

    def __init__(self, threshold: int = K_ANONYMITY):
        self._threshold = threshold
        self._views: dict[tuple, set[str]] = {}
        self._arrivals: dict[tuple, set[str]] = {}

    @staticmethod
    def _bucket(entity_id: str, day: date | None) -> tuple:
        return entity_id, day or portal_today()

    def record_view(self, visitor_hash: str, entity_id: str, day: date | None = None) -> None:
        self._views.setdefault(self._bucket(entity_id, day), set()).add(visitor_hash)

    def record_arrival(self, visitor_hash: str, entity_id: str, day: date | None = None) -> None:
        bucket = self._bucket(entity_id, day)
        self._arrivals.setdefault(bucket, set()).add(visitor_hash)
        # 到场必然产生过浏览，计入分母避免低估。
        self._views.setdefault(bucket, set()).add(visitor_hash)

    def conversion(self, entity_id: str, day: date | None = None) -> dict | None:
        """达标返回 ``{views, arrivals, rate, k}``，不足阈值返回 ``None``（抑制，而非四舍五入到 0）。"""
        bucket = self._bucket(entity_id, day)
        viewers = self._views.get(bucket, set())
        if len(viewers) < self._threshold:
            return None
        arrivals = self._arrivals.get(bucket, set()) & viewers
        rate = len(arrivals) / len(viewers)
        return {
            "day": str(bucket[1]),
            "views": len(viewers),
            "arrivals": len(arrivals),
            "rate": round(rate, 3),
            "k": self._threshold,
        }
