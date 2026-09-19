"""领域层验收测试：对应运营人员的跨时区发布检查清单。

重点覆盖：紧急更正上线速度、中俄文追溯关系、缓存节点不得返回旧政策，
以及可见范围、收藏冲突通知与隐私聚合。
"""

import unittest
from datetime import date, datetime, timedelta

from border_events import (
    Analytics,
    ChangeEvent,
    ChangeKind,
    ContentKind,
    ContentService,
    ContentStatus,
    EffectiveWindow,
    EventStream,
    FavoriteService,
    ReviewDecision,
    Role,
    Schedule,
    Visibility,
    parse_instant,
)

T0 = parse_instant("2026-09-19T08:00:00+08:00")


def make_clock(start=T0):
    state = {"now": start}

    def clock():
        return state["now"]

    clock.advance = lambda **kw: state.update(now=state["now"] + timedelta(**kw))
    clock.set = lambda instant: state.update(now=instant)
    return clock


def make_window(starts="2026-09-19T00:00:00+08:00", ends="2026-09-26T00:00:00+08:00"):
    return EffectiveWindow.from_text(starts, ends)


def make_event_schedule(starts="2026-09-20T10:00:00+08:00", hours=2, capacity=100):
    start = parse_instant(starts)
    return Schedule(starts_at=start, ends_at=start + timedelta(hours=hours), capacity=capacity)


class PolicyReviewTest(unittest.TestCase):
    def setUp(self):
        self.clock = make_clock()
        self.svc = ContentService(clock=self.clock)

    def test_policy_requires_approved_review_before_publish(self):
        self.svc.submit(
            id="visa-notice",
            kind=ContentKind.POLICY,
            source_org="边检站",
            title_zh="互免签证通关提示",
            body_zh="持普通护照可免签停留……",
        )
        with self.assertRaises(PermissionError):
            self.svc.publish("visa-notice")

        self.svc.review_policy(
            "visa-notice",
            reviewer="政策岗",
            decision=ReviewDecision.REJECTED,
            window=make_window(),
        )
        with self.assertRaises(PermissionError):
            self.svc.publish("visa-notice")

        self.svc.review_policy(
            "visa-notice",
            reviewer="政策岗",
            decision=ReviewDecision.APPROVED,
            window=make_window(),
        )
        self.svc.publish("visa-notice")
        self.assertIsNotNone(self.svc.serve("visa-notice"))

    def test_cross_timezone_window_is_normalized_and_shown_in_port_tz(self):
        # 运营人员在莫斯科（UTC+3）录入生效区间
        window = EffectiveWindow.from_text(
            "2026-10-01T00:00:00+03:00", "2026-10-08T00:00:00+03:00"
        )
        start_local, end_local = window.port_local()
        self.assertEqual(start_local, "2026-10-01T05:00:00+08:00")
        self.assertEqual(end_local, "2026-10-08T05:00:00+08:00")
        # 口岸时间 10-01 04:59 尚未生效，05:00 已生效
        self.assertFalse(window.contains(parse_instant("2026-10-01T04:59:59+08:00")))
        self.assertTrue(window.contains(parse_instant("2026-10-01T05:00:00+08:00")))

    def test_naive_time_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_instant("2026-10-01T00:00:00")

    def test_expired_policy_is_not_served_even_when_cache_still_holds_it(self):
        self.svc.submit(
            id="visa-notice",
            kind=ContentKind.POLICY,
            source_org="边检站",
            title_zh="通关提示",
            body_zh="旧政策",
        )
        self.svc.review_policy(
            "visa-notice",
            reviewer="政策岗",
            decision=ReviewDecision.APPROVED,
            window=make_window(ends="2026-09-19T12:00:00+08:00"),
        )
        self.svc.publish("visa-notice")
        self.assertIsNotNone(self.svc.serve("visa-notice"))
        # 缓存节点仍物理持有旧政策字节
        self.assertIsNotNone(self.svc.cache.peek(("visa-notice", "zh")))
        # 生效区间一过，对外一律不得返回
        self.clock.advance(hours=5)
        self.assertIsNone(self.svc.serve("visa-notice"))


class EmergencyCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.clock = make_clock()
        self.svc = ContentService(clock=self.clock)

    def test_correction_is_live_immediately_and_cache_purged(self):
        self.svc.submit(
            id="night-market",
            kind=ContentKind.EVENT,
            source_org="市集承办方",
            title_zh="夜间市集",
            body_zh="周五晚开市",
            schedule=make_event_schedule(),
        )
        self.svc.publish("night-market")
        self.assertEqual(self.svc.serve("night-market")["body"], "周五晚开市")

        self.svc.emergency_correct(
            "night-market",
            title_zh="夜间市集",
            body_zh="因天气原因提前一小时闭市",
            reviewer="值班运营",
        )
        # 调用返回后任何读路径都不得再出现旧文案
        served = self.svc.serve("night-market")
        self.assertEqual(served["body"], "因天气原因提前一小时闭市")
        self.assertEqual(served["version"], 2)
        # 缓存节点上不存在旧版本条目（更正时已同步清除，重新缓存的是新版本）
        cached = self.svc.cache.peek(("night-market", "zh"))
        self.assertEqual(cached["version"], 2)
        self.assertEqual(cached["payload"]["body"], "因天气原因提前一小时闭市")

    def test_policy_correction_keeps_review_trail_and_new_window(self):
        self.svc.submit(
            id="visa-notice",
            kind=ContentKind.POLICY,
            source_org="边检站",
            title_zh="通关提示",
            body_zh="旧政策",
        )
        self.svc.review_policy(
            "visa-notice",
            reviewer="政策岗",
            decision=ReviewDecision.APPROVED,
            window=make_window(),
        )
        self.svc.publish("visa-notice")

        new_window = make_window(ends="2026-10-07T00:00:00+08:00")
        self.svc.emergency_correct(
            "visa-notice",
            title_zh="通关提示",
            body_zh="口岸临时调整通关时间",
            reviewer="政策岗",
            window=new_window,
        )
        item = self.svc.get("visa-notice")
        self.assertEqual(item.version, 2)
        self.assertTrue(item.reviews[-1].expedited)
        self.assertEqual(item.reviews[-1].window, new_window)
        self.assertEqual(self.svc.serve("visa-notice")["body"], "口岸临时调整通关时间")

    def test_policy_correction_without_window_is_rejected(self):
        self.svc.submit(
            id="visa-notice",
            kind=ContentKind.POLICY,
            source_org="边检站",
            title_zh="通关提示",
            body_zh="旧政策",
        )
        with self.assertRaises(ValueError):
            self.svc.emergency_correct(
                "visa-notice",
                title_zh="通关提示",
                body_zh="新政",
                reviewer="政策岗",
            )


class TranslationTraceTest(unittest.TestCase):
    def setUp(self):
        self.clock = make_clock()
        self.svc = ContentService(clock=self.clock)
        self.svc.submit(
            id="choir",
            kind=ContentKind.EVENT,
            source_org="文旅局",
            title_zh="合唱交流",
            body_zh="中俄合唱团交流演出",
            schedule=make_event_schedule(),
        )
        self.svc.publish("choir")
        self.svc.add_translation(
            "choir", lang="ru", translator="译员A", title="Хор", body="Обмен"
        )

    def test_translation_is_anchored_to_source_version(self):
        served = self.svc.serve("choir", lang="ru")
        self.assertEqual(
            served["trace"],
            {
                "source_version": 1,
                "current_version": 1,
                "stale": False,
                "translator": "译员A",
            },
        )

    def test_stale_translation_is_flagged_until_retranslated(self):
        self.svc.revise("choir", title_zh="合唱交流", body_zh="演出改到周六")
        self.svc.publish("choir")
        served = self.svc.serve("choir", lang="ru")
        self.assertTrue(served["trace"]["stale"])
        self.assertEqual(served["trace"]["source_version"], 1)
        self.assertEqual(served["trace"]["current_version"], 2)

        self.svc.add_translation(
            "choir", lang="ru", translator="译员A", title="Хор", body="В субботу"
        )
        served = self.svc.serve("choir", lang="ru")
        self.assertFalse(served["trace"]["stale"])
        self.assertEqual(served["body"], "В субботу")

    def test_stale_policy_translation_is_not_served(self):
        self.svc.submit(
            id="visa-notice",
            kind=ContentKind.POLICY,
            source_org="边检站",
            title_zh="通关提示",
            body_zh="旧政策",
        )
        self.svc.review_policy(
            "visa-notice",
            reviewer="政策岗",
            decision=ReviewDecision.APPROVED,
            window=make_window(),
        )
        self.svc.publish("visa-notice")
        self.svc.add_translation(
            "visa-notice", lang="ru", translator="译员B", title="Виза", body="Старое"
        )
        self.assertIsNotNone(self.svc.serve("visa-notice", lang="ru"))

        # 中文事实源更新后，落后的俄文政策译稿不得对外返回
        self.svc.emergency_correct(
            "visa-notice",
            title_zh="通关提示",
            body_zh="新政策",
            reviewer="政策岗",
            window=make_window(),
        )
        self.assertIsNone(self.svc.serve("visa-notice", lang="ru"))
        self.assertEqual(self.svc.serve("visa-notice", lang="zh")["body"], "新政策")

    def test_language_without_translation_is_not_served(self):
        self.assertIsNone(self.svc.serve("choir", lang="en"))


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self.clock = make_clock()
        self.svc = ContentService(clock=self.clock)
        for cid, visibility in [
            ("expo", Visibility.PUBLIC),
            ("organizer-contact", Visibility.ORGANIZER),
            ("incident-record", Visibility.INTERNAL),
        ]:
            self.svc.submit(
                id=cid,
                kind=ContentKind.EVENT,
                visibility=visibility,
                source_org="文旅局",
                title_zh=cid,
                body_zh=cid,
            )
            self.svc.publish(cid)

    def test_public_visible_to_everyone(self):
        for role in (Role.VISITOR, Role.ORGANIZER, Role.OPERATOR):
            self.assertIsNotNone(self.svc.serve("expo", role=role))

    def test_organizer_contact_hidden_from_visitor(self):
        self.assertIsNone(self.svc.serve("organizer-contact", role=Role.VISITOR))
        self.assertIsNotNone(self.svc.serve("organizer-contact", role=Role.ORGANIZER))

    def test_internal_record_only_for_operator(self):
        self.assertIsNone(self.svc.serve("incident-record", role=Role.VISITOR))
        self.assertIsNone(self.svc.serve("incident-record", role=Role.ORGANIZER))
        self.assertIsNotNone(self.svc.serve("incident-record", role=Role.OPERATOR))


class EventStreamAndFavoritesTest(unittest.TestCase):
    def setUp(self):
        self.clock = make_clock()
        self.svc = ContentService(clock=self.clock)
        self.favorites = FavoriteService(self.svc, clock=self.clock)
        self.stream = EventStream(self.svc, self.favorites)

        def add_event(cid, starts, capacity=100):
            self.svc.submit(
                id=cid,
                kind=ContentKind.EVENT,
                source_org="文旅局",
                title_zh=cid,
                body_zh=cid,
                schedule=make_event_schedule(starts, capacity=capacity),
            )
            self.svc.publish(cid)

        add_event("sports-meet", "2026-09-20T10:00:00+08:00")
        add_event("choir", "2026-09-21T19:00:00+08:00")
        add_event("backup-show", "2026-09-20T14:00:00+08:00")
        self.favorites.add("visitor-1", "sports-meet")
        self.favorites.add("visitor-1", "choir")

    def ingest(self, kind, target, **data):
        return self.stream.ingest(
            ChangeEvent(kind=kind, target_id=target, occurred_at=self.clock(), data=data)
        )

    def test_cancellation_notifies_with_alternatives(self):
        sent = self.ingest(ChangeKind.CANCELLED, "sports-meet")
        self.assertEqual(len(sent), 1)
        notice = sent[0]
        self.assertEqual(notice.reason, "cancelled")
        self.assertEqual(notice.alternatives, ["backup-show"])  # 同口岸当日、不冲突
        self.assertIsNone(self.svc.serve("sports-meet"))  # 已取消内容不再对外返回

    def test_reschedule_into_cross_day_conflict_triggers_notice(self):
        self.assertEqual(self.favorites.conflicts_of("visitor-1"), [])
        # 合唱交流改期到与体育大会同一时段，跨日安排出现冲突
        sent = self.ingest(
            ChangeKind.RESCHEDULED,
            "choir",
            starts_at="2026-09-20T10:30:00+08:00",
            ends_at="2026-09-20T12:30:00+08:00",
        )
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            self.favorites.conflicts_of("visitor-1"), [("choir", "sports-meet")]
        )
        # 替代方案不含已收藏或冲突项
        notice = self.favorites.inbox_of("visitor-1")[-1]
        self.assertNotIn("sports-meet", notice.alternatives)
        self.assertNotIn("choir", notice.alternatives)

    def test_capacity_shuttle_and_merchant_events(self):
        sent = self.ingest(ChangeKind.CAPACITY_REDUCED, "sports-meet", capacity=20)
        self.assertEqual(self.svc.get("sports-meet").schedule.capacity, 20)
        self.assertEqual(sent[0].reason, "capacity_reduced")

        self.svc.submit(
            id="shuttle-1",
            kind=ContentKind.SHUTTLE,
            source_org="交通公司",
            title_zh="接驳车",
            body_zh="口岸—会场",
            schedule=make_event_schedule("2026-09-20T09:00:00+08:00", hours=1),
        )
        self.svc.publish("shuttle-1")
        self.ingest(
            ChangeKind.SHUTTLE_CHANGED,
            "shuttle-1",
            starts_at="2026-09-20T09:30:00+08:00",
            ends_at="2026-09-20T10:30:00+08:00",
        )
        self.assertEqual(
            self.svc.serve("shuttle-1")["schedule"]["starts_at"],
            "2026-09-20T01:30:00+00:00",
        )

        self.svc.submit(
            id="cafe",
            kind=ContentKind.MERCHANT,
            source_org="商户联合会",
            title_zh="俄货商店",
            body_zh="正常营业",
        )
        self.svc.publish("cafe")
        self.ingest(ChangeKind.MERCHANT_CLOSED, "cafe")
        self.assertIsNone(self.svc.serve("cafe"))
        self.assertEqual(self.svc.get("cafe").status, ContentStatus.RETRACTED)


class AnalyticsPrivacyTest(unittest.TestCase):
    def test_only_aggregates_are_stored(self):
        analytics = Analytics()
        day = date(2026, 9, 20)
        for _ in range(6):
            analytics.record_view("expo", day=day)
        analytics.record_arrival("expo", day=day)

        rows = analytics.export(min_count=1)
        self.assertIn(
            {"day": "2026-09-20", "content_id": "expo", "metric": "view", "count": 6},
            rows,
        )
        # 存储与导出中不存在任何可关联个人的字段
        for row in rows:
            self.assertEqual(set(row), {"day", "content_id", "metric", "count"})
        self.assertFalse(any("visitor" in attr for attr in vars(analytics)))

    def test_sparse_cells_are_suppressed(self):
        analytics = Analytics()
        analytics.record_view("quiet-page", day=date(2026, 9, 20))
        self.assertEqual(analytics.export(), [])  # 默认阈值抑制小单元格


if __name__ == "__main__":
    unittest.main()
