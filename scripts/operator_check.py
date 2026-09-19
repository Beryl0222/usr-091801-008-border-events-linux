#!/usr/bin/env python3
"""运营核验脚本：用样例数据跑一遍三个重点检查项。

  1) 紧急更正上线速度：受理 → 中俄文 → 复核 → 清缓存 → 对外可见，打印各阶段耗时；
  2) 中俄文追溯关系：每个俄文版本必须能指回具体中文版本与复核记录；
  3) 缓存节点是否仍错误返回旧政策：越过生效时刻后必须 410 且报文物理消失。

另含跨时区读取与“取消 → 带替代方案通知”两条辅助检查。

用法::

    python3 scripts/operator_check.py

退出码 0 表示全部通过。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from domain.app import ServiceError
from domain.seed import seed
from domain.store import Clock
from domain.timeutil import MOSCOW_TZ, PORTAL_TZ, Window, local_dt

DAY = date(2026, 9, 20)
AT = lambda hm: local_dt(DAY, hm, PORTAL_TZ)

_failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def main() -> int:
    # 每次读取前进 10 秒的时钟，模拟“受理后逐项流转”，使阶段耗时可观察。
    from tests.helpers import StepClock
    clock = StepClock(AT("08:00"), timedelta(seconds=10))
    from domain.app import PortalApp
    app = PortalApp(clock=clock)
    seed(app, DAY)

    print("检查一：紧急更正上线速度")
    app.serve("pol_visa", "ru")  # 旧报文在缓存
    app.serve("pol_visa", "zh")
    clock.set(AT("08:10"))
    out = app.urgent_correction(
        "pol_visa", reason="口岸夜间紧急公告",
        zh_body={"title": "免签通关须知（紧急修订）", "summary": "团体凭名单通关"},
        ru_body={"title": "Срочная правка правил въезда", "summary": "группы по спискам"},
        window=Window(AT("00:00"), local_dt(DAY + timedelta(days=7), "23:59", PORTAL_TZ)),
        opener="ops:值班长", unit_author="边检站-王",
        translator="译员-列娜", reviewer="边检总站-政策处")
    for stage in out["correction"]["stages"]:
        print(f"      {stage['name']:<18} T+{stage['elapsed_seconds']}s")
    check("终态为 publicly_visible", out["correction"]["done"])
    check("旧缓存（中俄文）全部清除",
          set(out["purged_keys"]) == {"pol_visa:ru", "pol_visa:zh"},
          str(out["purged_keys"]))
    new_view = app.serve("pol_visa", "ru").body
    check("游客立即拿到新俄文且绑定新中文版",
          (new_view["zh_version"], new_view["ru_version"]) == (2, 2))

    print("检查二：中俄文追溯关系")
    lineage = app.content.lineage("pol_visa")
    chains = []
    for zh in lineage["zh_versions"]:
        for ru in zh["ru_versions"]:
            chains.append((zh["no"], ru["no"], ru["zh_no"],
                           [r["status"] for r in zh["reviews"]]))
    ok_chain = all(ru_bound == zh_no for zh_no, _, ru_bound, _ in chains)
    check("每个俄文版本都指回其来源中文版本", ok_chain, str(chains))
    v1 = lineage["zh_versions"][0]
    check("旧俄文在中文版换版后标记为 stale",
          v1["ru_versions"][0]["status"] == "stale",
          v1["ru_versions"][0]["status"])
    check("政策复核记录可追溯到具体中文版本",
          any(r["zh_no"] == 2 for zh in lineage["zh_versions"] for r in zh["reviews"]))

    print("检查三：缓存节点不得错误返回旧政策")
    # 新窗口 9/27 23:59 口岸时间结束。
    edge_end = local_dt(DAY + timedelta(days=7), "23:59", PORTAL_TZ)
    clock.set(edge_end - timedelta(minutes=1))
    before = app.serve("pol_visa", "ru")
    check("窗口结束前一分钟仍正常返回", before.http_status == 200)
    clock.set(edge_end)
    suppressed = False
    try:
        app.serve("pol_visa", "ru")
    except ServiceError as exc:
        suppressed = exc.http_status == 410
    check("到达生效结束时刻返回 410", suppressed)
    check("报文体已从缓存物理清除", not app.cache.has("pol_visa:ru"))
    clock.set(edge_end + timedelta(hours=6))
    still_gone = False
    try:
        app.serve("pol_visa", "ru")
    except ServiceError as exc:
        still_gone = exc.http_status == 410
    check("六小时后旧政策仍不会复活", still_gone)

    print("辅助检查：跨时区发布")
    # 莫斯科晚上 21:00（UTC 18:00）= 口岸次日 03:00；政策按口岸日历日仍有效。
    moscow_evening = local_dt(date(2026, 9, 19), "21:00", MOSCOW_TZ)
    # 此时新更正窗口尚未开始（窗口 9/20 00:00 口岸 = UTC 9/19 16:00，已开始），
    # 但上面已把时钟推到 9/27 之后，这里用独立解析在指定时刻验证窗口语义。
    review = app.content.get("pol_visa").effective_review(moscow_evening)
    check("莫斯科 9/19 21:00 落在口岸政策窗口内", review is not None)

    print("辅助检查：临时取消 → 跨日安排通知带替代方案")
    clock.set(AT("09:00"))
    app.events.save_plan("tourist-demo", "ev_sport", "2026-09-20")
    app.ingest_event("cancel", "ev_sport",
                     {"reason_zh": "设备故障", "reason_ru": "поломка оборудования"},
                     "organizer:张主任")
    notes = app.events.notifications_for("tourist-demo")
    if notes:
        n = notes[0]
        check("收到中俄双语取消通知", bool(n["message_zh"] and n["message_ru"]))
        check("通知附带同时段替代方案",
              any(a["id"] == "ev_expo" for a in n["alternatives"]),
              str([a["id"] for a in n["alternatives"]]))
    else:
        check("收到中俄双语取消通知", False)

    print()
    if _failures:
        print(f"未通过 {len(_failures)} 项：{', '.join(_failures)}")
        return 1
    print("全部核验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
