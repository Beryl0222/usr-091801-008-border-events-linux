"""测试共享辅助：每次读取自动前进的时钟与应用装配。"""

from __future__ import annotations

from datetime import timedelta

from domain.app import PortalApp
from domain.store import Clock


class StepClock(Clock):
    """每被读取一次，时钟前进固定步长——用于测量更正管线各阶段耗时。"""

    def __init__(self, start, step: timedelta = timedelta(seconds=30)):
        super().__init__(start)
        self.step = step

    def __call__(self):
        self._fixed += self.step
        return self._fixed


def build_app(start, *, k_threshold: int = 3, tick: timedelta | None = None,
              seeded: bool = True, base_day=None):
    from datetime import date
    from domain.seed import seed

    clock = StepClock(start, tick) if tick is not None else Clock(start)
    app = PortalApp(clock=clock, k_threshold=k_threshold)
    if seeded:
        seed(app, base_day or date(2026, 9, 20))
    return app
