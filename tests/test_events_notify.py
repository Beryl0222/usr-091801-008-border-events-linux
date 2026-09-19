"""事件流接入、跨日收藏冲突与中俄双语替代方案通知。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from domain.timeutil import PORTAL_TZ, local_dt

from tests.helpers import build_app

DAY = date(2026, 9, 20)
AT = lambda hm: local_dt(DAY, hm, PORTAL_TZ)


class EventNotificationTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app(AT("09:00"))

    def test_cancel_notifies_savers_with_same_day_ru_alternative(self):
        self.app.events.save_plan("tok-ivan", "ev_sport", "2026-09-20")
        self.app.events.save_plan("tok-other", "ev_choir", "2026-09-21")  # 无关安排

        outcome = self.app.ingest_event(
            "cancel", "ev_sport",
            {"reason_zh": "场地设备故障", "reason_ru": "неисправность оборудования"},
            "organizer:张主任")
        self.assertEqual(outcome["notified"], 1)

        notes = self.app.events.notifications_for("tok-ivan")
        self.assertEqual(len(notes), 1)
        note = notes[0]
        self.assertEqual(note["kind"], "cancel")
        self.assertEqual(note["conflict"], "cancelled")
        self.assertIn("отменено", note["message_ru"])
        self.assertIn("取消", note["message_zh"])
        # 同日仍开放、且有现行俄文的博览会是替代项；次日活动不混入。
        alt_ids = [a["id"] for a in note["alternatives"]]
        self.assertIn("ev_expo", alt_ids)
        self.assertTrue(all(a["language"] == "ru" for a in note["alternatives"]))
        self.assertNotIn("ev_choir", alt_ids)
        # 无关游客不被打扰。
        self.assertEqual(self.app.events.notifications_for("tok-other"), [])

    def test_postpone_cross_day_flags_conflict(self):
        self.app.events.save_plan("tok-ivan", "ev_sport", "2026-09-20")
        new_start = local_dt(date(2026, 9, 22), "19:00", PORTAL_TZ)
        outcome = self.app.ingest_event(
            "postpone", "ev_sport",
            {"new_start_utc": new_start.isoformat(),
             "new_end_utc": local_dt(date(2026, 9, 22), "22:00", PORTAL_TZ).isoformat(),
             "cross_day": True,
             "reason_zh": "顺延两天", "reason_ru": "перенос на два дня"},
            "organizer:张主任")
        note = self.app.events.notifications_for("tok-ivan")[0]
        self.assertEqual(note["conflict"], "postponed_cross_day")
        self.assertIn("2026-09-22", note["message_zh"])
        self.assertEqual(self.app.events.occurrence("ev_sport").status, "postponed")
        self.assertEqual(outcome["event"]["seq"] >= 1, True)

    def test_capacity_sellout_offers_alternatives(self):
        self.app.events.save_plan("tok-ivan", "ev_sport", "2026-09-20")
        self.app.ingest_event("capacity", "ev_sport",
                              {"capacity": 0, "sold_out": True}, "organizer:张主任")
        note = self.app.events.notifications_for("tok-ivan")[0]
        self.assertEqual(note["conflict"], "sold_out")
        self.assertTrue(any(a["id"] == "ev_expo" for a in note["alternatives"]))

    def test_shuttle_change_names_backup_trip(self):
        self.app.events.save_plan("tok-ivan", "shuttle_port_sport", "2026-09-20")
        self.app.ingest_event(
            "shuttle_change", "shuttle_port_sport",
            {"trip": "15:30 班次", "corridor": "口岸↔体育中心", "status": "cancelled",
             "alternative_trip": "16:30 加班车",
             "reason_zh": "原班车检修", "reason_ru": "плановый ремонт"},
            "organizer:交通局")
        note = self.app.events.notifications_for("tok-ivan")[0]
        self.assertEqual(note["conflict"], "shuttle_changed")
        self.assertEqual(note["alternatives"][0]["alternative_trip"], "16:30 加班车")

    def test_merchant_closing_suggests_open_nearby_merchant(self):
        self.app.events.save_plan("tok-ivan", "shop_bakery", "2026-09-20")
        self.app.ingest_event(
            "merchant_hours", "shop_bakery",
            {"closed": True, "reason_zh": "食材售罄", "reason_ru": "продукты закончились"},
            "organizer:商户")
        note = self.app.events.notifications_for("tok-ivan")[0]
        self.assertEqual(note["conflict"], "merchant_closed")
        self.assertIn("shop_grill", [a["id"] for a in note["alternatives"]])

    def test_stream_is_cursor_based_and_topic_filterable(self):
        self.app.ingest_event("capacity", "ev_sport", {"capacity": 1200}, "o")
        page = self.app.events.stream(0, topics=["events.activities"])
        self.assertEqual(len(page["events"]), 1)
        seq = page["latest"]
        self.assertEqual(self.app.events.stream(seq)["events"], [])
        self.app.ingest_event(
            "shuttle_change", "shuttle_port_sport",
            {"trip": "t", "status": "changed"}, "o")
        page = self.app.events.stream(0, topics=["events.shuttles"])
        self.assertEqual({e["kind"] for e in page["events"]}, {"shuttle_change"})

    def test_notifications_are_isolated_per_visitor_token(self):
        self.app.events.save_plan("tok-ivan", "ev_sport", "2026-09-20")
        self.app.events.save_plan("tok-petr", "ev_sport", "2026-09-20")
        self.app.ingest_event("cancel", "ev_sport", {"reason_zh": "x", "reason_ru": "y"}, "o")
        self.assertEqual(len(self.app.events.notifications_for("tok-ivan")), 1)
        self.assertEqual(len(self.app.events.notifications_for("tok-petr")), 1)
        note_id = self.app.events.notifications_for("tok-ivan")[0]["id"]
        # 别的令牌无法标记/读到这条通知。
        self.assertFalse(self.app.events.mark_read("tok-petr", note_id))
        self.assertTrue(self.app.events.mark_read("tok-ivan", note_id))
        self.assertEqual(
            self.app.events.notifications_for("tok-ivan", unread_only=True), [])

    def test_plans_do_not_leak_across_tokens(self):
        self.app.events.save_plan("tok-ivan", "ev_sport", "2026-09-20")
        self.assertEqual(
            self.app.events.plans_of("tok-petr"), [])

    def test_open_ended_entity_day_follows_portal_clock(self):
        # 商户没有固定场次；归属日必须用服务时钟的口岸日历，而非真实系统日。
        self.assertEqual(
            self.app.events.occurrence("shop_bakery").day(self.app.clock()),
            "2026-09-20")
        # 莫斯科 9/19 21:00（UTC 18:00）= 口岸 9/20 02:00，仍归 9/20。
        from domain.timeutil import MOSCOW_TZ
        self.app.clock.set(local_dt(date(2026, 9, 19), "21:00", MOSCOW_TZ))
        self.assertEqual(
            self.app.events.occurrence("shop_bakery").day(self.app.clock()),
            "2026-09-20")
        # 推进到口岸次日零点后，归属日翻页。
        self.app.clock.set(local_dt(date(2026, 9, 21), "00:30", PORTAL_TZ))
        self.assertEqual(
            self.app.events.occurrence("shop_bakery").day(self.app.clock()),
            "2026-09-21")


if __name__ == "__main__":
    unittest.main()
