"""三类可见范围与“不留可还原个人行程轨迹”的隐私约束。"""

from __future__ import annotations

import unittest
from datetime import date

from domain.app import ServiceError
from domain.privacy import hash_visitor, redact
from domain.timeutil import PORTAL_TZ, local_dt
from domain.visibility import Scope, can_read

from tests.helpers import build_app

DAY = date(2026, 9, 20)
AT = lambda hm: local_dt(DAY, hm, PORTAL_TZ)


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app(AT("10:00"))

    def test_role_scope_matrix(self):
        checks = [
            ("visitor", Scope.PUBLIC, True),
            ("visitor", Scope.ORGANIZER_CONTACTS, False),
            ("visitor", Scope.INTERNAL_DISPATCH, False),
            ("organizer", Scope.PUBLIC, True),
            ("organizer", Scope.INTERNAL_DISPATCH, False),
            ("reviewer", Scope.INTERNAL_DISPATCH, True),
        ]
        for role, scope, expected in checks:
            self.assertEqual(can_read(role, scope), expected, (role, scope))

    def test_organizer_contact_row_level_isolation(self):
        sport = self.app.list_contacts("organizer", "org-sport")
        self.assertEqual([c["id"] for c in sport], ["cnt_sport"])
        # 别的承办方只看到自己的联系人；游客一个都看不到。
        self.assertEqual(
            [c["id"] for c in self.app.list_contacts("organizer", "org-culture")],
            ["cnt_culture"])
        self.assertEqual(self.app.list_contacts("visitor"), [])
        # 运营中心可见全部。
        self.assertEqual(len(self.app.list_contacts("ops")), 4)

    def test_dispatch_records_internal_only(self):
        outcome = self.app.ingest_event(
            "cancel", "ev_sport", {"reason_zh": "x", "reason_ru": "y"}, "organizer:张主任")
        seq = outcome["event"]["seq"]
        self.app.events.record_dispatch(seq, "应急局", "分流", "加开两班接驳")
        with self.assertRaises(ServiceError) as ctx:
            self.app.list_dispatches(seq, "visitor")
        self.assertEqual(ctx.exception.http_status, 403)
        with self.assertRaises(ServiceError):
            self.app.list_dispatches(seq, "organizer")
        self.assertEqual(len(self.app.list_dispatches(seq, "ops")), 1)

    def test_visitor_cannot_read_internal_scoped_content(self):
        # 构造一条内部可见的内容（例如内部处置通报）。
        item = self.app.content.register("activity", Scope.INTERNAL_DISPATCH.value,
                                         organizer_id="org-sport", item_id="ev_internal")
        self.app.content.submit_zh(item.id, "ops", {"title": "内部通报"}, publish=True)
        with self.assertRaises(ServiceError) as ctx:
            self.app.serve(item.id, "zh", role="visitor")
        self.assertEqual(ctx.exception.http_status, 403)
        self.app.serve(item.id, "zh", role="ops")  # 运营可读


class PrivacyTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app(AT("10:00"), k_threshold=3)

    def test_token_is_hashed_irreversibly_and_stable(self):
        h1 = hash_visitor("tourist-secret")
        h2 = hash_visitor("tourist-secret")
        self.assertEqual(h1, h2)
        self.assertNotIn("tourist-secret", h1)
        # 服务端收藏与通知存储里只有哈希。
        self.app.events.save_plan("tourist-secret", "ev_sport", "2026-09-20")
        stored = [p.visitor_hash for p in self.app.events._plans.values()]
        self.assertEqual(stored, [h1])
        self.assertTrue(all("tourist-secret" not in s for s in stored))

    def test_conversion_suppressed_below_k(self):
        for token in ("a", "b"):
            self.app.record_view(token, "ev_sport")
        self.assertIn("suppressed", self.app.conversion("ev_sport", "ops"))
        # 游客/承办方无权看转化。
        with self.assertRaises(ServiceError):
            self.app.conversion("ev_sport", "visitor")

    def test_conversion_released_at_k_without_individual_rows(self):
        for token, arrived in (("a", True), ("b", False), ("c", True)):
            self.app.record_view(token, "ev_sport")
            if arrived:
                self.app.record_arrival(token, "ev_sport")
        metric = self.app.conversion("ev_sport", "ops")
        self.assertEqual(metric["views"], 3)
        self.assertEqual(metric["arrivals"], 2)
        self.assertAlmostEqual(metric["rate"], 2 / 3, places=2)
        # 聚合账不提供逐条轨迹导出。
        self.assertFalse(hasattr(self.app.privacy, "export_rows"))
        self.assertNotIn("_arrivals", self.app.conversion("ev_sport", "ops"))

    def test_redaction_strips_pii_from_free_text(self):
        text = redact("联系 13812345678 / a.b@example.ru / 护照 E12345678")
        self.assertNotIn("13812345678", text)
        self.assertNotIn("a.b@example.ru", text)
        self.assertNotIn("E12345678", text)

    def test_audit_never_holds_visitor_tokens_or_pii(self):
        self.app.ingest_event("cancel", "ev_sport",
                              {"reason_zh": "联系 13812345678 改期",
                               "reason_ru": "y"}, "organizer:张主任")
        self.app.events.save_plan("tourist-secret", "ev_sport", "2026-09-20")
        blob = repr(self.app.audit.entries_for())
        self.assertNotIn("tourist-secret", blob)
        self.assertNotIn("13812345678", blob)


if __name__ == "__main__":
    unittest.main()
