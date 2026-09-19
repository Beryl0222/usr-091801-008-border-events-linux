"""口岸文旅双语运营的基础运行入口。

中文事实源由责任单位提交，俄文译稿锚定具体中文版本；通关类内容
须经政策复核并带生效区间，过期内容即使缓存仍在也不对外返回。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from border_events import (
    ContentKind,
    ContentService,
    EffectiveWindow,
    ReviewDecision,
    Role,
    Schedule,
    parse_instant,
)

SERVICE_ID = "border-events"
SERVICE_NAME = "口岸文旅双语运营"


def health_payload():
    """构造服务身份数据。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_demo_service() -> ContentService:
    """构造演示数据：一场体育大会与一条经复核的通关提示。"""
    service = ContentService()
    service.submit(
        id="sports-meet",
        kind=ContentKind.EVENT,
        source_org="文旅局",
        title_zh="中俄体育大会",
        body_zh="口岸体育馆举行，凭票入场。",
        schedule=Schedule(
            starts_at=parse_instant("2026-10-01T10:00:00+08:00"),
            ends_at=parse_instant("2026-10-01T18:00:00+08:00"),
            capacity=500,
            location="口岸体育馆",
        ),
    )
    service.publish("sports-meet")
    service.add_translation(
        "sports-meet",
        lang="ru",
        translator="译员A",
        title="Спортивные игры",
        body="Билеты обязательны.",
    )
    service.submit(
        id="visa-notice",
        kind=ContentKind.POLICY,
        source_org="边检站",
        title_zh="互免签证通关提示",
        body_zh="持有效普通护照可免签入境，停留期以公告为准。",
    )
    service.review_policy(
        "visa-notice",
        reviewer="政策岗",
        decision=ReviewDecision.APPROVED,
        window=EffectiveWindow.from_text(
            "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00"
        ),
    )
    service.publish("visa-notice")
    service.add_translation(
        "visa-notice",
        lang="ru",
        translator="译员B",
        title="Безвизовый въезд",
        body="Въезд по общегражданскому паспорту.",
    )
    return service


class Handler(BaseHTTPRequestHandler):
    """只读对外接口：健康检查与公开内容（按语言与可见范围过滤）。"""

    contents = build_demo_service()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._respond(200, health_payload())
            return
        if parsed.path.startswith("/public/content/"):
            content_id = parsed.path.rsplit("/", 1)[-1]
            lang = parse_qs(parsed.query).get("lang", ["zh"])[0]
            payload = self.contents.serve(content_id, lang=lang, role=Role.VISITOR)
            if payload is None:
                self._respond(404, {"error": "unavailable"})
            else:
                self._respond(200, payload)
            return
        self._respond(404, {"error": "not found"})

    def _respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert SERVICE_ID == health_payload()["service"]
        demo = build_demo_service()
        assert demo.serve("visa-notice", lang="ru") is not None
        assert demo.serve("visa-notice", lang="en") is None
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
