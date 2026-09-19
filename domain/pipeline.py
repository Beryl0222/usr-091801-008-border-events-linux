"""紧急更正上线管线。

把“中文事实源更新 → 旧俄文失效 → 新俄文上线 → 政策复核 → 缓存清除 → 游客可见”
作为一条可计时、可审计的管线。运营重点检查的“紧急更正上线速度”直接来自各阶段
相对受理时刻的秒数；任何阶段缺失（例如跳过复核）管线不会走到“对外可见”。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .store import AuditLog, Clock

# 有序阶段。``publicly_visible`` 是终态。
STAGES = (
    "received",          # 运营中心受理紧急更正
    "zh_submitted",      # 责任单位中文事实源新版本进入系统
    "zh_published",      # 中文新版本发布（旧译文此刻起 stale）
    "ru_submitted",      # 译员提交绑定新版本的俄文
    "ru_published",      # 俄文新版本上线
    "policy_approved",   # 政策复核通过并带新生效区间（仅通关类）
    "cache_purged",      # 边缘节点旧报文全部清除
    "publicly_visible",  # 游客拿到新内容（且拿不到旧内容）
)


@dataclass
class Stage:
    name: str
    at_utc: str
    elapsed_seconds: int

    def to_dict(self) -> dict:
        return {"name": self.name, "at_utc": self.at_utc,
                "elapsed_seconds": self.elapsed_seconds}


@dataclass
class Correction:
    id: str
    item_id: str
    reason: str
    actor: str
    stages: list[Stage] = field(default_factory=list)
    done: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "item_id": self.item_id,
            "reason": self.reason,
            "actor": self.actor,
            "done": self.done,
            "stages": [s.to_dict() for s in self.stages],
            "total_seconds": self.stages[-1].elapsed_seconds if self.stages else None,
        }


class CorrectionPipeline:
    def __init__(self, clock: Clock, idgen, audit: AuditLog):
        self.clock = clock
        self.idgen = idgen
        self.audit = audit
        self._items: dict[str, Correction] = {}

    def open(self, item_id: str, reason: str, actor: str) -> Correction:
        corr = Correction(id=self.idgen.next("corr"), item_id=item_id,
                          reason=reason, actor=actor)
        self._mark(corr, "received")
        self._items[corr.id] = corr
        self.audit.record(actor, "correction.open", item_id, {"corr": corr.id, "note": reason})
        return corr

    def mark(self, correction_id: str, stage: str) -> Stage:
        corr = self._items[correction_id]
        return self._mark(corr, stage)

    def get(self, correction_id: str) -> Correction:
        return self._items[correction_id]

    def _mark(self, corr: Correction, stage: str) -> Stage:
        if stage not in STAGES:
            raise ValueError(f"未知管线阶段: {stage}")
        existing = {s.name for s in corr.stages}
        if stage in existing:
            return next(s for s in corr.stages if s.name == stage)
        start = corr.stages[0].at_utc if corr.stages else None
        now = self.clock()
        from datetime import datetime
        elapsed = int((now - datetime.fromisoformat(start)).total_seconds()) if start else 0
        recorded = Stage(name=stage, at_utc=now.isoformat(), elapsed_seconds=elapsed)
        corr.stages.append(recorded)
        corr.done = stage == "publicly_visible"
        self.audit.record(corr.actor, "correction.stage", corr.item_id,
                          {"corr": corr.id, "stage": stage, "elapsed_seconds": elapsed})
        return recorded
