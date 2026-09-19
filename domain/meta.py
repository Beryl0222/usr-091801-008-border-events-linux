"""稳定的服务身份信息（健康检查与监控共用）。"""

SERVICE_ID = "border-events"
SERVICE_NAME = "口岸文旅双语运营"


def health_payload() -> dict:
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}
