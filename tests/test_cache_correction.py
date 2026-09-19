"""边缘缓存硬闸与紧急更正上线速度。

重点验证：
* 缓存节点即使持有旧政策报文，越过生效时刻也绝不返回报文体；
* 中文换版/复核/取消按实体标签清掉全部语言变体；
* 紧急更正从受理到游客可见的阶段耗时可度量，且终态前后“旧的拿不到、新的拿得到”。
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from domain.app import ServiceError
from domain.cache import CacheResult
from domain.pipeline import STAGES
from domain.timeutil import PORTAL_TZ, Window, local_dt

from tests.helpers import build_app

DAY = date(2026, 9, 20)
AT = lambda hm: local_dt(DAY, hm, PORTAL_TZ)


class EdgeCacheGateTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app(AT("09:00"))

    def test_cached_policy_suppressed_at_and_after_window_end(self):
        self.app.serve("pol_visa", "ru")
        self.app.serve("pol_visa", "zh")
        end = local_dt(date(2026, 10, 20), "23:59", PORTAL_TZ)

        # 窗口结束前一分钟：仍然命中。
        self.app.clock.set(end - timedelta(minutes=1))
        result, body = self.app.cache.get("pol_visa:ru")
        self.assertEqual(result, CacheResult.HIT)
        self.assertIn("Правила", body["title"])

        # 恰好到达窗口结束：报文体被抑制且物理删除。
        self.app.clock.set(end)
        result, body = self.app.cache.get("pol_visa:ru")
        self.assertEqual(result, CacheResult.EXPIRED_SUPPRESSED)
        self.assertIsNone(body)
        self.assertFalse(self.app.cache.has("pol_visa:ru"))

        # 即使源站再被请求，也不会“复活”旧报文，而是 410。
        with self.assertRaises(ServiceError) as ctx:
            self.app.serve("pol_visa", "ru")
        self.assertEqual(ctx.exception.http_status, 410)

    def test_cache_key_carries_hard_expiry_header(self):
        served = self.app.serve("pol_visa", "ru")
        self.assertIsNotNone(served.body["effective_until_utc"])
        entry = self.app.cache._store["pol_visa:ru"]
        self.assertEqual(entry.effective_until_utc, served.body["effective_until_utc"])

    def test_purge_hits_both_language_variants_on_zh_republish(self):
        self.app.serve("pol_visa", "ru")
        self.app.serve("pol_visa", "zh")
        zh2 = self.app.content.submit_zh("pol_visa", "边检站-王", {"title": "z2"})
        purged = self.app.publish_zh("pol_visa", zh2.no, "边检站-王")["purged_keys"]
        self.assertEqual(set(purged), {"pol_visa:ru", "pol_visa:zh"})
        self.assertEqual(self.app.cache.keys_for_entity("pol_visa"), [])

    def test_cancel_event_purges_activity_cache(self):
        self.app.serve("ev_sport", "ru")
        outcome = self.app.ingest_event(
            "cancel", "ev_sport", {"reason_zh": "故障"}, "organizer:张主任")
        self.assertIn("ev_sport:ru", outcome["purged_keys"])
        self.assertFalse(self.app.cache.has("ev_sport:ru"))


class UrgentCorrectionTest(unittest.TestCase):
    def setUp(self):
        # 每次读取前进 20 秒：管线 8 个阶段总耗时应为 7*20 秒（受理为 0）。
        self.app = build_app(AT("09:00"), tick=timedelta(seconds=20))

    def _correct(self):
        return self.app.urgent_correction(
            "pol_visa",
            reason="上级临时调整免签口径",
            zh_body={"title": "免签须知（紧急修订）", "summary": "团队名单制"},
            ru_body={"title": "Срочная правка", "summary": "въезд по спискам"},
            window=Window(AT("00:00"), local_dt(DAY + timedelta(days=7), "23:59", PORTAL_TZ)),
            opener="ops:值班长", unit_author="边检站-王",
            translator="译员-列娜", reviewer="边检总站-政策处",
        )

    def test_correction_runs_all_stages_and_measures_speed(self):
        out = self._correct()
        corr = out["correction"]
        names = [s["name"] for s in corr["stages"]]
        self.assertEqual(names, list(STAGES))
        self.assertTrue(corr["done"])
        # 受理点为 0 秒，之后各阶段相对受理严格递增、为正。
        elapsed = [s["elapsed_seconds"] for s in corr["stages"]]
        self.assertEqual(elapsed[0], 0)
        self.assertTrue(all(b > a for a, b in zip(elapsed, elapsed[1:])))
        self.assertEqual(corr["total_seconds"], elapsed[-1])

    def test_pipeline_elapsed_is_clock_accurate(self):
        """直接驱动管线：每阶段推进 30 秒，耗时必须精确等于 30 的倍数。"""
        from domain.pipeline import CorrectionPipeline
        from domain.store import AuditLog, Clock, IdGen

        clock = Clock(AT("09:00"))
        pipe = CorrectionPipeline(clock, IdGen(), AuditLog(clock))
        corr = pipe.open("pol_visa", "受理", "ops:值班长")
        for i, stage in enumerate(STAGES[1:], start=1):
            clock.set(clock() + timedelta(seconds=30))
            recorded = pipe.mark(corr.id, stage)
            self.assertEqual(recorded.elapsed_seconds, i * 30)
        self.assertEqual(pipe.get(corr.id).to_dict()["total_seconds"], 7 * 30)

    def test_old_content_gone_and_new_content_served(self):
        # 旧报文先在缓存里（两个语言变体）。
        old = self.app.serve("pol_visa", "ru").body
        self.app.serve("pol_visa", "zh")
        self.assertEqual(old["ru_version"], 1)
        out = self._correct()
        # 两个语言变体都被清除。
        self.assertEqual(set(out["purged_keys"]), {"pol_visa:ru", "pol_visa:zh"})
        for lang in ("ru", "zh"):
            self.assertFalse(self.app.cache.has(f"pol_visa:{lang}"))
        # 再取拿到新报文，俄文绑定新中文版，生效区间也是新的。
        served = self.app.serve("pol_visa", "ru")
        self.assertEqual((served.body["zh_version"], served.body["ru_version"]), (2, 2))
        self.assertEqual(served.body["title"], "Срочная правка")
        self.assertEqual(served.body["effective_until_utc"],
                         local_dt(DAY + timedelta(days=7), "23:59", PORTAL_TZ).isoformat())

    def test_policy_correction_without_review_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            self.app.urgent_correction(
                "pol_visa", reason="x", zh_body={"title": "z"}, ru_body={"title": "r"},
                window=None, opener="ops", unit_author="u",
                translator="t", reviewer=None)
        self.assertEqual(ctx.exception.http_status, 422)

    def test_non_policy_correction_skips_policy_stage(self):
        out = self.app.urgent_correction(
            "ev_sport", reason="嘉宾变动",
            zh_body={"title": "中俄体育大会（更新）"},
            ru_body={"title": "Спортфестиваль (обновлено)"},
            window=None, opener="ops:值班长", unit_author="org-sport-经办",
            translator="译员-列娜", reviewer=None)
        names = [s["name"] for s in out["correction"]["stages"]]
        self.assertNotIn("policy_approved", names)
        self.assertIn("publicly_visible", names)


if __name__ == "__main__":
    unittest.main()
