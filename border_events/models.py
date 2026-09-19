"""内容、译稿与政策复核的领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .timeutil import EffectiveWindow


class ContentKind(Enum):
    EVENT = "event"  # 体育大会、合唱交流、贸易博览会、夜间市集等活动
    SHUTTLE = "shuttle"  # 接驳班次
    MERCHANT = "merchant"  # 商户营业信息
    POLICY = "policy"  # 通关条件与边检提示


class Visibility(Enum):
    PUBLIC = "public"  # 公开信息
    ORGANIZER = "organizer"  # 承办方联系人
    INTERNAL = "internal"  # 联动部门处置记录


class Role(Enum):
    VISITOR = "visitor"
    ORGANIZER = "organizer"
    OPERATOR = "operator"


class ContentStatus(Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETRACTED = "retracted"


class ReviewDecision(Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


_CLEARANCE = {Role.VISITOR: 0, Role.ORGANIZER: 1, Role.OPERATOR: 2}
_SENSITIVITY = {Visibility.PUBLIC: 0, Visibility.ORGANIZER: 1, Visibility.INTERNAL: 2}


def visible_to(visibility: Visibility, role: Role) -> bool:
    """可见范围：公开 < 承办方 < 内部处置，逐级收紧。"""
    return _CLEARANCE[role] >= _SENSITIVITY[visibility]


@dataclass
class Schedule:
    """活动或班次的时刻与容量（UTC 存储）。"""

    starts_at: datetime
    ends_at: datetime
    capacity: int | None = None
    location: str = ""

    def overlaps(self, other: "Schedule") -> bool:
        return self.starts_at < other.ends_at and other.starts_at < self.ends_at


@dataclass
class Translation:
    """译员提交的译稿，必须锚定到具体中文版本，形成中俄文追溯关系。"""

    lang: str
    source_version: int
    translator: str
    title: str
    body: str
    updated_at: datetime


@dataclass
class PolicyReview:
    """通关类内容的政策复核记录，复核通过且带生效区间才可发布。"""

    version: int
    reviewer: str
    decision: ReviewDecision
    reviewed_at: datetime
    window: EffectiveWindow
    expedited: bool = False  # 紧急更正走加急复核，仍留痕


@dataclass
class ContentItem:
    """责任单位提交的中文事实源，俄文译稿与复核记录挂在条目上。"""

    id: str
    kind: ContentKind
    visibility: Visibility
    source_org: str
    title_zh: str
    body_zh: str
    version: int = 1
    status: ContentStatus = ContentStatus.DRAFT
    schedule: Schedule | None = None
    translations: dict[str, Translation] = field(default_factory=dict)
    reviews: list[PolicyReview] = field(default_factory=list)

    def approved_window(self, now: datetime) -> EffectiveWindow | None:
        """当前版本最近一次复核通过的生效区间。"""
        for review in reversed(self.reviews):
            if review.version == self.version and review.decision is ReviewDecision.APPROVED:
                return review.window
        return None
