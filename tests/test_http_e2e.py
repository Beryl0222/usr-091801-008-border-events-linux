"""跨时区 HTTP 端到端：发布滞后、政策过期、紧急更正、事件通知、角色与隐私。"""

from __future__ import annotations

import json
import threading
import unittest
from datetime import date
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from domain.api import make_handler
from domain.timeutil import PORTAL_TZ, local_dt

from tests.helpers import build_app

DAY = date(2026, 9, 20)
AT = lambda hm: local_dt(DAY, hm, PORTAL_TZ)


class HttpCase(unittest.TestCase):
    def setUp(self):
        # 每个用例独立装配，避免版本号/时钟在用例间相互耦合。
        self.app = build_app(AT("09:00"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.base + path, data=data, method=method, headers={
            "Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(req, timeout=3) as resp:
                payload = json.load(resp)
                return resp.status, dict(resp.headers), payload
        except HTTPError as exc:
            return exc.code, dict(exc.headers), json.load(exc)

    def set_clock(self, utc_iso):
        status, _, payload = self.call("POST", "/admin/clock", {"utc": utc_iso},
                                       {"X-Role": "ops"})
        self.assertEqual(status, 200)

    # ---- 基线与跨时区读取 ---------------------------------------------------

    def test_health_contract_unchanged(self):
        status, headers, payload = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload,
                         {"status": "ok", "service": "border-events",
                          "name": "口岸文旅双语运营"})

    def test_policy_served_with_hard_expiry_header_in_utc(self):
        self.set_clock(AT("10:00").isoformat())
        status, headers, payload = self.call("GET", "/content/pol_visa?lang=ru")
        self.assertEqual(status, 200)
        self.assertEqual(payload["language"], "ru")
        # 口岸 10-20 23:59 == UTC 10-20 15:59；头里的硬过期是绝对 UTC 时刻。
        self.assertEqual(headers["Edge-Effective-Until"], "2026-10-20T15:59:00+00:00")
        self.assertEqual(headers["X-Cache"], "miss")
        # 第二次命中缓存。
        _, headers2, _ = self.call("GET", "/content/pol_visa?lang=ru")
        self.assertEqual(headers2["X-Cache"], "hit")

    def test_moscow_evening_still_within_portal_day_window(self):
        # 莫斯科时间 9/19 21:00（UTC 18:00）已是口岸 9/20 凌晨，政策应可读。
        self.set_clock("2026-09-19T18:00:00+00:00")
        # 换新实体缓存场景：先清缓存视角直接请求。
        status, _, payload = self.call("GET", "/content/pol_visa?lang=ru")
        self.assertEqual(status, 200)

    # ---- 中文先发、俄文滞后、复核 -------------------------------------------

    def test_zh_first_then_ru_lag_returns_409_until_translated(self):
        self.set_clock(AT("11:00").isoformat())
        # 缓存清掉旧报文。
        status, _, _ = self.call(
            "POST", "/content/pol_visa/zh",
            {"author": "边检站-王", "body": {"title": "免签须知 v2"}, "publish": True},
            {"X-Role": "organizer", "X-Actor-Name": "officer-wang"})
        self.assertEqual(status, 201)
        # 俄文立即不可返回：旧译文 stale。
        status, _, payload = self.call("GET", "/content/pol_visa?lang=ru")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "ru_stale")
        # 中文 v2 也尚待政策复核。
        status, _, payload = self.call("GET", "/content/pol_visa?lang=zh")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "policy_review_required")

        # 译员补译文并发布，复核员给新生效区间。
        status, _, _ = self.call(
            "POST", "/content/pol_visa/ru",
            {"zh_no": 2, "translator": "译员-列娜",
             "body": {"title": "Безвизовый режим v2"}, "publish": True},
            {"X-Role": "organizer", "X-Actor-Name": "translator-lena"})
        self.assertEqual(status, 201)
        status, _, _ = self.call(
            "POST", "/policy/pol_visa/approve",
            {"reviewer": "边检总站-政策处",
             "window": {"start": "2026-09-20 11:00", "end": "2026-11-30 23:59"}},
            {"X-Role": "reviewer"})
        self.assertEqual(status, 200)
        status, _, payload = self.call("GET", "/content/pol_visa?lang=ru")
        self.assertEqual(status, 200)
        self.assertEqual(payload["title"], "Безвизовый режим v2")
        self.assertEqual((payload["zh_version"], payload["ru_version"]), (2, 2))

    # ---- 过期缓存硬闸 -------------------------------------------------------

    def test_cache_node_returns_410_after_window_end(self):
        self.set_clock(AT("12:00").isoformat())
        self.call("GET", "/content/pol_visa?lang=ru")  # 填热缓存
        # 跳到新窗口结束之后（2026-11-30 口岸 23:59 = UTC 15:59）。
        self.set_clock("2026-12-01T00:00:00+00:00")
        status, headers, payload = self.call("GET", "/content/pol_visa?lang=ru")
        self.assertEqual(status, 410)
        self.assertEqual(payload["error"], "policy_expired")
        # 再次请求仍然 410，旧报文不会复活。
        status2, _, _ = self.call("GET", "/content/pol_visa?lang=ru")
        self.assertEqual(status2, 410)

    # ---- 紧急更正速度 -------------------------------------------------------

    def test_urgent_correction_endpoint_timeline(self):
        self.set_clock(AT("13:00").isoformat())
        self.call("GET", "/content/pol_visa?lang=ru")
        status, _, payload = self.call(
            "POST", "/corrections",
            {"item_id": "pol_visa", "reason": "夜间紧急公告",
             "zh_body": {"title": "免签须知（紧急）"},
             "ru_body": {"title": "Срочное обновление"},
             "window": {"start": "2026-09-20 13:00", "end": "2026-10-01 23:59"},
             "unit_author": "边检站-王", "translator": "译员-列娜",
             "reviewer": "边检总站-政策处"},
            {"X-Role": "ops"})
        self.assertEqual(status, 201)
        stages = [s["name"] for s in payload["correction"]["stages"]]
        self.assertEqual(stages[-1], "publicly_visible")
        self.assertIn("pol_visa:ru", payload["purged_keys"])
        # 更正后游客立即拿到新报文。
        _, _, view = self.call("GET", "/content/pol_visa?lang=ru")
        self.assertEqual(view["title"], "Срочное обновление")
        self.assertEqual(view["ru_version"], 2)
        self.assertEqual(view["zh_version"], 2)

    def test_correction_forbidden_for_visitor(self):
        status, _, _ = self.call("POST", "/corrections",
                                 {"item_id": "ev_sport", "zh_body": {}, "ru_body": {}},
                                 {"X-Role": "visitor"})
        self.assertEqual(status, 403)

    # ---- 事件 → 通知 全链路 -------------------------------------------------

    def test_event_then_visitor_notification_with_alternatives(self):
        self.set_clock(AT("14:00").isoformat())
        token = {"X-Visitor-Token": "http-tourist-1"}
        self.call("GET", "/content/ev_sport?lang=ru")  # 先填热缓存，验证取消联动清除
        status, _, _ = self.call("POST", "/plans",
                                 {"entity_id": "ev_sport", "day": "2026-09-20"}, token)
        self.assertEqual(status, 201)

        status, _, payload = self.call(
            "POST", "/events",
            {"kind": "cancel", "entity_id": "ev_sport",
             "payload": {"reason_zh": "设备故障", "reason_ru": "поломка"}},
            {"X-Role": "organizer", "X-Actor-Name": "zhang"})
        self.assertEqual(status, 201)
        self.assertEqual(payload["notified"], 1)
        self.assertIn("ev_sport:ru", payload["purged_keys"])

        status, _, payload = self.call("GET", "/notifications", headers=token)
        self.assertEqual(status, 200)
        note = payload["notifications"][0]
        self.assertEqual(note["kind"], "cancel")
        self.assertTrue(note["alternatives"])
        # 通知不含任何游客令牌明文。
        self.assertNotIn("http-tourist-1", json.dumps(note, ensure_ascii=False))

    # ---- 角色可见范围 -------------------------------------------------------

    def test_contacts_and_dispatches_scope_over_http(self):
        _, _, contacts = self.call("GET", "/contacts")
        self.assertEqual(contacts["contacts"], [])
        _, _, contacts = self.call("GET", "/contacts", headers={
            "X-Role": "organizer", "X-Organizer-Id": "org-sport"})
        self.assertEqual([c["id"] for c in contacts["contacts"]], ["cnt_sport"])

        _, _, event_out = self.call(
            "POST", "/events",
            {"kind": "cancel", "entity_id": "ev_choir",
             "payload": {"reason_zh": "x", "reason_ru": "y"}},
            {"X-Role": "ops"})
        seq = event_out["event"]["seq"]
        status, _, _ = self.call("POST", "/dispatches",
                                 {"event_seq": seq, "dept": "应急局",
                                  "action": "分流", "note": "加开车次"},
                                 {"X-Role": "ops"})
        self.assertEqual(status, 201)
        status, _, payload = self.call("GET", f"/dispatches?event_seq={seq}")
        self.assertEqual(status, 403)
        status, _, payload = self.call("GET", f"/dispatches?event_seq={seq}",
                                       headers={"X-Role": "reviewer"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["dispatches"][0]["dept"], "应急局")

    # ---- 隐私：转化抑制 -----------------------------------------------------

    def test_conversion_suppressed_until_k(self):
        status, _, payload = self.call("GET", "/metrics/conversion/ev_market",
                                       headers={"X-Role": "ops"})
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("suppressed"))
        for i in range(5):
            self.call("POST", "/metrics/views",
                      {"entity_id": "ev_market", "visitor_token": f"mkt-{i}"})
        for i in range(2):
            self.call("POST", "/metrics/arrivals",
                      {"entity_id": "ev_market", "visitor_token": f"mkt-{i}"})
        status, _, payload = self.call("GET", "/metrics/conversion/ev_market",
                                       headers={"X-Role": "ops"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["views"], 5)
        self.assertEqual(payload["arrivals"], 2)
        # 游客角色无权访问。
        status, _, _ = self.call("GET", "/metrics/conversion/ev_market")
        self.assertEqual(status, 403)

    # ---- 追溯关系 -----------------------------------------------------------

    def test_lineage_endpoint_reconstructs_zh_ru_chain(self):
        status, _, payload = self.call("GET", "/lineage/pol_visa",
                                       headers={"X-Role": "reviewer"})
        self.assertEqual(status, 200)
        zh_versions = payload["zh_versions"]
        # 经过前序用例更正后，至少有多个中文版本，每个俄文版本都能指回中文版号。
        for zh in zh_versions:
            for ru in zh["ru_versions"]:
                self.assertEqual(ru["zh_no"], zh["no"])
        # 游客拿不到追溯视图。
        status, _, _ = self.call("GET", "/lineage/pol_visa")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
