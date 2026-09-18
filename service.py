"""口岸文旅双语运营的基础运行入口。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_ID = "border-events"
SERVICE_NAME = "口岸文旅双语运营"


def health_payload():
    """构造服务身份数据。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """提供健康检查响应。"""

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(health_payload(), ensure_ascii=False).encode()
        self.send_response(200)
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
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
