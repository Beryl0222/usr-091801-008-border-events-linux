"""政策生命周期、中俄文版本追溯与生效区间闸门。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from domain.app import ServiceError
from domain.content import InvalidState, NotServable, ReviewStatus, RuStatus, ZhStatus
from domain.timeutil import PORTAL_TZ, Window, local_dt

from tests.helpers import build_app

DAY = date(2026, 9, 20)
AT = lambda hm: local_dt(DAY, hm, PORTAL_TZ)


class PolicyLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app(AT("12:00"))

    def test_initial_pol_versions_are_traceable(self):
        lineage = self.app.content.lineage("pol_visa")
        self.assertEqual(len(lineage["zh_versions"]), 1)
        zh1 = lineage["zh_versions"][0]
        self.assertEqual(zh1["status"], ZhStatus.PUBLISHED.value)
        self.assertEqual([r["no"] for r in zh1["ru_versions"]], [1])
        self.assertEqual([r["zh_no"] for r in zh1["ru_versions"]], [1])
        self.assertEqual(len(zh1["reviews"]), 1)
        view = self.app.content.resolve("pol_visa", lang="ru")
        self.assertEqual((view["zh_version"], view["ru_version"], view["ru_bound_zh"]),
                         (1, 1, 1))

    def test_new_zh_without_ru_makes_old_ru_stale_and_blocks_serving(self):
        # 责任单位发布中文 v2，译员尚未跟进。
        zh2 = self.app.content.submit_zh("pol_visa", "边检站-王", {"title": "新口径"}, note="v2")
        self.app.publish_zh("pol_visa", zh2.no, "边检站-王")

        # 旧俄文立刻 stale，俄文侧不得返回（宁可缺译文也不给旧信息）。
        stale = self.app.content.get("pol_visa").ru[0]
        self.assertEqual(stale.status, RuStatus.STALE)
        with self.assertRaises(NotServable) as ctx:
            self.app.content.resolve("pol_visa", lang="ru")
        self.assertEqual(ctx.exception.code, "ru_stale")
        # 中文 v2 同样要等政策复核：通关页在复核通过前整页不外发。
        with self.assertRaises(NotServable) as ctx:
            self.app.content.resolve("pol_visa", lang="zh")
        self.assertEqual(ctx.exception.code, "policy_review_required")

    def test_ru_cannot_republish_against_superseded_zh(self):
        zh2 = self.app.content.submit_zh("pol_visa", "边检站-王", {"title": "新口径"})
        self.app.publish_zh("pol_visa", zh2.no, "边检站-王")
        # 试图把新译文绑到旧中文 v1：禁止上线，只能留档。
        ru = self.app.content.submit_ru("pol_visa", 1, "译员-列娜", {"title": "x"})
        with self.assertRaises(InvalidState):
            self.app.publish_ru("pol_visa", ru.no)

    def test_new_ru_rebinds_and_lineage_keeps_full_chain(self):
        zh2 = self.app.content.submit_zh("pol_visa", "边检站-王", {"title": "新口径"})
        self.app.publish_zh("pol_visa", zh2.no, "边检站-王")
        # 新政策还需复核 v2 才能对外（见下条）；先验证绑定关系。
        ru2 = self.app.content.submit_ru("pol_visa", 2, "译员-列娜", {"title": "новый"})
        self.app.publish_ru("pol_visa", ru2.no)
        lineage = self.app.content.lineage("pol_visa")
        ru_by_zh = {v["no"]: [r["zh_no"] for r in v["ru_versions"]]
                    for v in lineage["zh_versions"]}
        self.assertEqual(ru_by_zh, {1: [1], 2: [2]})
        self.assertEqual(self.app.content.get("pol_visa").ru[0].status, RuStatus.STALE)
        self.assertEqual(self.app.content.get("pol_visa").ru[1].status, RuStatus.PUBLISHED)

    def test_policy_requires_review_window(self):
        self.app.content.register("policy", 0, item_id="pol_tmp")
        self.app.content.submit_zh("pol_tmp", "u", {"title": "t"}, publish=True)
        self.app.content.submit_ru("pol_tmp", 1, "tr", {"title": "t"}, publish=True)
        with self.assertRaises(NotServable) as ctx:
            self.app.content.resolve("pol_tmp", lang="ru")
        self.assertEqual(ctx.exception.code, "policy_review_required")

        # 生效区间在未来：尚不能返回。
        self.app.approve_policy(
            "pol_tmp", "复核", Window(AT("18:00"), local_dt(DAY + timedelta(days=1), "18:00", PORTAL_TZ)))
        with self.assertRaises(ServiceError) as ctx:
            self.app.serve("pol_tmp", "ru")
        self.assertEqual(ctx.exception.code, "policy_not_yet_effective")
        self.assertEqual(ctx.exception.http_status, 425)

    def test_expired_policy_hard_gated_even_when_cached(self):
        # 先把俄文报文缓存到边缘节点。
        served = self.app.serve("pol_visa", "ru")
        self.assertEqual(served.cache_status, "miss")
        self.assertTrue(self.app.cache.has("pol_visa:ru"))

        # 越过生效区间结束时刻（样例窗口到 10-20 23:59 口岸时间）。
        self.app.clock.set(local_dt(date(2026, 10, 21), "00:00", PORTAL_TZ))
        with self.assertRaises(ServiceError) as ctx:
            self.app.serve("pol_visa", "ru")
        self.assertEqual(ctx.exception.code, "policy_expired")
        self.assertEqual(ctx.exception.http_status, 410)
        # 报文已物理清除，不会被后续请求“复活”。
        self.assertFalse(self.app.cache.has("pol_visa:ru"))
        with self.assertRaises(ServiceError):
            self.app.serve("pol_visa", "ru")

    def test_revoke_kills_active_policy_immediately(self):
        self.app.serve("pol_visa", "ru")
        self.app.revoke_policy("pol_visa", "边检总站-政策处", "上级叫停旧口径")
        with self.assertRaises(NotServable) as ctx:
            self.app.content.resolve("pol_visa", lang="ru")
        self.assertEqual(ctx.exception.code, "policy_revoked")
        self.assertFalse(self.app.cache.has("pol_visa:ru"))

    def test_window_is_half_open_and_timezone_aware(self):
        # 口岸时间 9/20 全天窗口 = UTC 9/19 16:00 至 9/20 16:00。
        win = Window(AT("00:00"), local_dt(DAY + timedelta(days=1), "00:00", PORTAL_TZ))
        self.assertTrue(win.contains(local_dt(DAY, "00:00", PORTAL_TZ)))
        self.assertFalse(win.contains(local_dt(DAY + timedelta(days=1), "00:00", PORTAL_TZ)))
        # 莫斯科时间 9/19 21:00 == UTC 18:00 == 口岸 9/20 02:00，仍在窗口内。
        from domain.timeutil import MOSCOW_TZ
        self.assertTrue(win.contains(local_dt(date(2026, 9, 19), "21:00", MOSCOW_TZ)))


if __name__ == "__main__":
    unittest.main()
