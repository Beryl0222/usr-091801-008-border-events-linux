"""HTTP 适配层：把 :class:`~domain.app.PortalApp` 暴露为 JSON API。

鉴权用最小请求头模拟：``X-Role``（visitor/organizer/reviewer/ops）、
``X-Organizer-Id``（承办方行级隔离）、``X-Visitor-Token``（游客不透明令牌）。
游客令牌只作为隐私哈希的输入，从不落日志。

为便于跨时区与过期场景的端到端验证，运营角色可通过 ``POST /admin/clock``
把时钟固定到任意 UTC 时刻。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .app import PortalApp, ServiceError
from .content import ContentError, NotFound
from .timeutil import local_dt
from .events import EventError as _EventError
from .meta import health_payload


def parse_window(data: dict | None):
    """请求体 window: {start:"YYYY-MM-DD HH:MM", end:..., timezone?} -> Window。"""
    if not data:
        return None
    from .timeutil import Window
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(data.get("timezone", "Asia/Shanghai"))
    start_day, start_hm = data["start"].split(" ", 1)
    end_day, end_hm = data["end"].split(" ", 1)
    start = local_dt(date.fromisoformat(start_day), start_hm, zone)
    end = local_dt(date.fromisoformat(end_day), end_hm, zone)
    return Window(start, end, zone_name=data.get("timezone", "Asia/Shanghai"))


def make_handler(app: PortalApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BorderEvents/1.0"

        # ---- 框架 ------------------------------------------------------------

        def _send(self, status: int, payload, extra_headers=None):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ServiceError("bad_json", f"请求体不是合法 JSON: {exc}", 400)

        @property
        def role(self) -> str:
            return self.headers.get("X-Role", "visitor")

        @property
        def organizer_id(self) -> str | None:
            return self.headers.get("X-Organizer-Id")

        @property
        def visitor_token(self) -> str | None:
            return self.headers.get("X-Visitor-Token")

        def _require_role(self, *roles: str):
            if self.role not in roles:
                raise ServiceError("forbidden",
                                   f"该操作需要角色: {', '.join(roles)}", 403)

        def log_message(self, *_args):
            return

        # ---- 路由 ------------------------------------------------------------

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _dispatch(self, method: str):
            parsed = urlparse(self.path)
            path = parsed.path.strip("/")
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            segments = path.split("/") if path else []
            try:
                self._route(method, segments, query)
            except ServiceError as exc:
                self._send(exc.http_status, {"error": exc.code, "message": str(exc)})
            except NotFound as exc:
                self._send(404, {"error": "not_found", "message": str(exc)})
            except (ContentError, _EventError, ValueError) as exc:
                code = getattr(exc, "code", "invalid_request")
                self._send(400, {"error": code, "message": str(exc)})

        def _route(self, method: str, segments: list[str], query: dict):
            # /health 保持基线契约
            if segments == ["health"] and method == "GET":
                self._send(200, health_payload())
                return

            if segments == ["content"] and method == "GET":
                kind = query.get("kind")
                self._send(200, {"ids": app.content.list_ids(kind)})
                return

            if len(segments) == 2 and segments[0] == "content" and method == "GET":
                self._serve_content(segments[1], query)
                return

            if len(segments) == 2 and segments[0] == "lineage" and method == "GET":
                self._require_role("reviewer", "ops")
                self._send(200, app.content.lineage(segments[1]))
                return

            if segments == ["occurrences"] and method == "GET":
                self._send(200, {"occurrences": app.events.public_occurrences(
                    query.get("day"))})
                return

            if segments == ["events"] and method == "GET":
                after = int(query.get("after", 0))
                topics = query.get("topics", "").split(",") if query.get("topics") else None
                wait = float(query["wait"]) if query.get("wait") else None
                self._send(200, app.events.stream(after, topics, wait))
                return

            if segments == ["events"] and method == "POST":
                self._require_role("organizer", "ops", "reviewer")
                data = self._read_json()
                outcome = app.ingest_event(
                    data["kind"], data["entity_id"], data.get("payload", {}),
                    actor=self._actor())
                self._send(201, outcome)
                return

            if segments == ["corrections"] and method == "POST":
                self._require_role("ops", "reviewer")
                self._open_correction(self._read_json())
                return

            if len(segments) == 2 and segments[0] == "corrections" and method == "GET":
                self._require_role("ops", "reviewer")
                self._send(200, app.pipeline.get(segments[1]).to_dict())
                return

            if (len(segments) == 3 and segments[0] == "policy"
                    and segments[2] == "revoke" and method == "POST"):
                self._require_role("reviewer", "ops")
                data = self._read_json()
                self._send(200, app.revoke_policy(
                    segments[1], self._actor(), data.get("reason", "")))
                return

            if (len(segments) == 3 and segments[0] == "content"
                    and segments[2] == "zh" and method == "POST"):
                self._require_role("organizer", "ops")
                data = self._read_json()
                version = app.content.submit_zh(
                    segments[1], data.get("author", self._actor()),
                    data["body"], data.get("note", ""))
                if data.get("publish"):
                    app.publish_zh(segments[1], version.no, version.author)
                self._send(201, version.to_dict())
                return
            if (len(segments) == 3 and segments[0] == "content"
                    and segments[2] == "ru" and method == "POST"):
                self._require_role("organizer", "ops")
                data = self._read_json()
                version = app.content.submit_ru(
                    segments[1], int(data["zh_no"]),
                    data.get("translator", self._actor()),
                    data["body"], data.get("note", ""))
                if data.get("publish"):
                    app.publish_ru(segments[1], version.no, version.translator)
                self._send(201, version.to_dict())
                return
            if (len(segments) == 3 and segments[0] == "content"
                    and segments[2] == "publish" and method == "POST"):
                self._require_role("organizer", "reviewer", "ops")
                data = self._read_json()
                if data["lang"] == "zh":
                    out = app.publish_zh(segments[1], int(data["no"]), self._actor())
                else:
                    out = app.publish_ru(segments[1], int(data["no"]), self._actor())
                self._send(200, out)
                return
            if (len(segments) == 3 and segments[0] == "policy"
                    and segments[2] == "approve" and method == "POST"):
                self._require_role("reviewer", "ops")
                data = self._read_json()
                window = parse_window(data.get("window"))
                if window is None:
                    raise ServiceError("window_required", "复核必须带生效区间", 422)
                out = app.approve_policy(
                    segments[1], data.get("reviewer", self._actor()), window,
                    zh_no=data.get("zh_no"), note=data.get("note", ""))
                self._send(200, out)
                return

            if segments == ["plans"] and method in ("GET", "POST", "DELETE"):
                self._plans(method, query)
                return

            if segments == ["notifications"] and method == "GET":
                token = self._token(query)
                notes = app.events.notifications_for(
                    token, unread_only=query.get("unread") == "1")
                self._send(200, {"notifications": notes})
                return

            if len(segments) == 3 and segments[0] == "notifications" and segments[2] == "read":
                token = self._token(query)
                ok = app.events.mark_read(token, segments[1])
                self._send(200 if ok else 404, {"updated": ok})
                return

            if segments == ["contacts"] and method == "GET":
                self._send(200, {"contacts": app.list_contacts(
                    self.role, self.organizer_id)})
                return

            if segments == ["dispatches"]:
                if method == "GET":
                    self._send(200, {"dispatches": app.list_dispatches(
                        int(query["event_seq"]), self.role)})
                else:
                    self._require_role("ops", "reviewer")
                    data = self._read_json()
                    entry = app.events.record_dispatch(
                        int(data["event_seq"]), data.get("dept", self._actor()),
                        data.get("action", ""), data.get("note", ""))
                    self._send(201, entry)
                return

            if segments == ["metrics", "views"] and method == "POST":
                data = self._read_json()
                app.record_view(self._token_from(data), data["entity_id"])
                self._send(202, {"recorded": True})
                return
            if segments == ["metrics", "arrivals"] and method == "POST":
                data = self._read_json()
                app.record_arrival(self._token_from(data), data["entity_id"])
                self._send(202, {"recorded": True})
                return
            if (len(segments) == 3 and segments[0] == "metrics"
                    and segments[1] == "conversion" and method == "GET"):
                self._send(200, app.conversion(segments[2], self.role))
                return

            if segments == ["admin", "seed"] and method == "POST":
                self._require_role("ops")
                from .seed import seed
                day_str = self._read_json().get("day")
                day = date.fromisoformat(day_str) if day_str else None
                self._send(201, {"ids": seed(app, day)})
                return
            if segments == ["admin", "clock"] and method == "POST":
                self._require_role("ops")
                data = self._read_json()
                app.clock.set(datetime.fromisoformat(data["utc"]))
                self._send(200, {"now_utc": app.clock().isoformat()})
                return

            self._send(404, {"error": "not_found",
                            "message": f"无此路由: /{'/'.join(segments)}"})

        # ---- 具体处理 --------------------------------------------------------

        def _serve_content(self, item_id: str, query: dict):
            lang = query.get("lang", "ru")
            if lang not in ("zh", "ru"):
                raise ServiceError("bad_lang", "lang 只能是 zh 或 ru", 400)
            served = app.serve(item_id, lang, self.role, self.organizer_id)
            headers = {"X-Cache": served.cache_status}
            if served.body.get("effective_until_utc"):
                headers["Edge-Effective-Until"] = served.body["effective_until_utc"]
                headers["Cache-Control"] = "must-revalidate"
            self._send(200, served.body, headers)

        def _open_correction(self, data: dict):
            outcome = app.urgent_correction(
                data["item_id"],
                reason=data.get("reason", ""),
                zh_body=data["zh_body"],
                ru_body=data["ru_body"],
                window=parse_window(data.get("window")),
                opener=self._actor(),
                unit_author=data.get("unit_author", "责任单位"),
                translator=data.get("translator", "译员"),
                reviewer=data.get("reviewer"),
            )
            self._send(201, outcome)

        def _plans(self, method: str, query: dict):
            if method == "GET":
                self._send(200, {"plans": app.events.plans_of(self._token(query))})
                return
            data = self._read_json() if method == "POST" else query
            token = self._token_from(data, query)
            if method == "POST":
                plan = app.events.save_plan(token, data["entity_id"], data["day"])
                self._send(201, {"saved": {"entity_id": plan.entity_id, "day": plan.day}})
            else:
                app.events.remove_plan(token, data["entity_id"], data["day"])
                self._send(200, {"deleted": True})

        # ---- 辅助 ------------------------------------------------------------

        def _actor(self) -> str:
            return f'{self.role}:{self.headers.get("X-Actor-Name", "anonymous")}'

        def _token(self, query: dict) -> str:
            return self._token_from(query, query)

        def _token_from(self, data: dict, fallback: dict | None = None) -> str:
            token = (data or {}).get("visitor_token") or (fallback or {}).get("token") \
                or self.visitor_token
            if not token:
                raise ServiceError("visitor_token_required",
                                   "需要 X-Visitor-Token 或 visitor_token 字段", 401)
            return token

    return Handler
