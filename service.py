"""口岸文旅双语运营运行入口。

默认启动完整双语信息服务（内容版本/政策复核、事件流、边缘缓存硬闸、通知、隐私账）。
``--check`` 仅校验服务身份；``--seed`` 在启动时写入样例数据，便于演示与手测。

向后兼容基线契约：``Handler`` / ``SERVICE_ID`` / ``SERVICE_NAME`` / ``health_payload``
仍可从本模块直接导入，``GET /health`` 载荷保持不变。
"""

from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer

from domain.api import make_handler
from domain.app import PortalApp
from domain.meta import SERVICE_ID, SERVICE_NAME, health_payload
from domain.store import Clock

# 进程级应用装配（真实时钟）；测试可自行构造 PortalApp 与 Handler。
app = PortalApp(clock=Clock())
Handler = make_handler(app)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true",
                        help="只做服务身份基础检查")
    parser.add_argument("--seed", action="store_true",
                        help="启动时写入样例数据（活动/政策/接驳/商户/联系人）")
    args = parser.parse_args()

    if args.check:
        assert SERVICE_ID == health_payload()["service"]
        print("基础检查通过")
        return

    if args.seed:
        from domain.seed import seed
        seed(app)
        print("样例数据已载入")

    print(f"{SERVICE_NAME} 监听 0.0.0.0:{args.port}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
