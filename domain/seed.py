"""样例数据：体育大会、合唱交流、贸易博览会、夜间市集，通关政策、接驳与商户。

内容体为双语字典（``title`` / ``fields`` 等），中文事实源由责任单位提交，
俄文由译员单独提交并绑定版本；通关政策带口岸时区生效区间与复核记录。
"""

from __future__ import annotations

from datetime import date, timedelta

from .app import PortalApp
from .timeutil import PORTAL_TZ, Window, local_dt
from .visibility import Scope
from .events import Occurrence


def _day(base: date, offset: int) -> date:
    return base + timedelta(days=offset)


def seed(app: PortalApp, base_day: date | None = None) -> dict:
    """写入全套样例，返回关键 ID 映射，便于测试与演示引用。"""
    base_day = base_day or date(2026, 9, 20)
    ids: dict[str, str] = {}

    # ---- 通关政策（必须复核 + 生效区间）------------------------------------
    visa = app.content.register("policy", Scope.PUBLIC.value, item_id="pol_visa")
    ids["pol_visa"] = visa.id
    app.content.submit_zh(
        visa.id, author="边检站-王",
        body={
            "title": "中俄互免签证通关须知",
            "summary": "持有效普通护照的中俄双方公民可免签入境，单次停留不超过15日。",
            "items": [
                "护照有效期需超过6个月",
                "单次免签停留自入境次日起不超过15日",
                "可经绥芬河等开放口岸客运通道通行",
            ],
            "hotline": "12367",
        },
        note="2026 秋季免签口径初版",
        publish=True,
    )
    app.content.submit_ru(
        visa.id, zh_no=1, translator="译员-列娜",
        body={
            "title": "Правила безвизового въезда (Китай–Россия)",
            "summary": "Граждане России и Китая с обычным паспортом могут въезжать "
                       "без визы на срок до 15 дней.",
            "items": [
                "Срок действия паспорта — не менее 6 месяцев",
                "Безвизовое пребывание — до 15 дней со следующих суток после въезда",
                "Проход через открытые пункты, например Суйфэньхэ",
            ],
            "hotline": "12367",
        },
        note="绑定中文 v1",
        publish=True,
    )
    app.approve_policy(
        visa.id, reviewer="边检总站-政策处",
        window=Window(
            local_dt(base_day, "00:00", PORTAL_TZ),
            local_dt(_day(base_day, 30), "23:59", PORTAL_TZ),
        ),
        note="秋季免签政策复核通过",
    )

    # ---- 四类活动 -----------------------------------------------------------
    activities = [
        {
            "id": "ev_sport", "organizer": "org-sport", "area": "体育中心",
            "tags": ["sports", "family"],
            "zh": {"title": "中俄体育大会", "summary": "篮球、摔跤与趣味田径，凭电子票入场。",
                   "venue": "市体育中心", "price": "免费–80元"},
            "ru": {"title": "Российско-китайский спортивный фестиваль",
                   "summary": "Баскетбол, борьба и лёгкая атлетика. Вход по электронному билету.",
                   "venue": "Городской спортцентр", "price": "бесплатно–80 юаней"},
        },
        {
            "id": "ev_choir", "organizer": "org-culture", "area": "大剧院",
            "tags": ["music", "culture"],
            "zh": {"title": "中俄合唱交流音乐会", "summary": "两地合唱团联演，含中文与俄语经典曲目。",
                   "venue": "市大剧院", "price": "免费预约"},
            "ru": {"title": "Концерт хорового обмена Китая и России",
                   "summary": "Совместное выступление хоров: классика на китайском и русском.",
                   "venue": "Городской театр оперы и балета", "price": "бесплатно по регистрации"},
        },
        {
            "id": "ev_expo", "organizer": "org-trade", "area": "会展中心",
            "tags": ["trade", "business"],
            "zh": {"title": "中俄边境贸易博览会", "summary": "农产品、机电与跨境电商展区，设俄语洽谈区。",
                   "venue": "边境会展中心", "price": "专业观众登记"},
            "ru": {"title": "Российско-китайская приграничная торговая ярмарка",
                   "summary": "Сельхозпродукция, машиностроение, кросс-бордер e-commerce. "
                              "Есть зона переговоров на русском.",
                   "venue": "Экспоцентр приграничной торговли", "price": "регистрация профи"},
        },
        {
            "id": "ev_market", "organizer": "org-commerce", "area": "河畔夜市",
            "tags": ["food", "night", "family"],
            "zh": {"title": "中俄风味夜间市集", "summary": "俄式烘焙、东北烧烤与手作摊位，营业至23:00。",
                   "venue": "河畔步行街", "price": "免费入场"},
            "ru": {"title": "Ночной рынок вкусов Китая и России",
                   "summary": "Русская выпечка, дунбэйский гриль и ремесленные лавки, до 23:00.",
                   "venue": "Набережная, пешеходная улица", "price": "вход свободный"},
        },
    ]
    for i, spec in enumerate(activities):
        item = app.content.register("activity", Scope.PUBLIC.value,
                                    organizer_id=spec["organizer"], item_id=spec["id"])
        ids[spec["id"]] = item.id
        day = _day(base_day, i % 2)  # 体育/博览会首日，合唱/夜市次日
        app.content.submit_zh(item.id, author=f'{spec["organizer"]}-经办人',
                              body=spec["zh"], note="活动公告 v1", publish=True)
        app.content.submit_ru(item.id, zh_no=1, translator="译员-列娜",
                              body=spec["ru"], note="活动公告译文 v1", publish=True)
        app.events.register_occurrence(Occurrence(
            entity_id=item.id, kind="activity", area=spec["area"],
            start_utc=local_dt(day, "18:00", PORTAL_TZ).isoformat(),
            end_utc=local_dt(day, "21:00", PORTAL_TZ).isoformat(),
            tags=spec["tags"], capacity=2000,
        ))

    # ---- 接驳班次（不同走廊） -----------------------------------------------
    shuttles = [
        ("shuttle_port_sport", "口岸↔体育中心", "体育中心", "15:30", "ev_sport"),
        ("shuttle_port_theatre", "口岸↔大剧院", "大剧院", "17:00", "ev_choir"),
        ("shuttle_port_expo", "口岸↔会展中心", "会展中心", "08:30", "ev_expo"),
    ]
    for sid, corridor, area, depart, linked in shuttles:
        item = app.content.register("transport", Scope.PUBLIC.value,
                                    organizer_id="org-transit", item_id=sid)
        ids[sid] = item.id
        title_zh = f"接驳班车 {corridor}"
        app.content.submit_zh(item.id, author="交通局-调度",
                              body={"title": title_zh, "summary": f"口岸发车 {depart}",
                                    "corridor": corridor},
                              note="班次 v1", publish=True)
        app.content.submit_ru(item.id, zh_no=1, translator="译员-伊万",
                              body={"title": f"Шаттл {corridor.replace('↔', '–')}",
                                    "summary": f"Отправление от порта в {depart}",
                                    "corridor": corridor},
                              note="班次译文 v1", publish=True)
        app.events.register_occurrence(Occurrence(
            entity_id=item.id, kind="transport", area=area, corridor=corridor,
            start_utc=local_dt(base_day, depart, PORTAL_TZ).isoformat(),
        ))

    # ---- 夜间市集商户 -------------------------------------------------------
    merchants = [
        ("shop_bakery", "河畔夜市", "俄式烘焙坊", "Русская пекарня"),
        ("shop_grill", "河畔夜市", "东北烧烤摊", "Дунбэйский гриль"),
    ]
    for mid, area, zh_title, ru_title in merchants:
        item = app.content.register("merchant", Scope.PUBLIC.value,
                                    organizer_id="org-commerce", item_id=mid)
        ids[mid] = item.id
        app.content.submit_zh(item.id, author="商户自律联盟",
                              body={"title": zh_title, "summary": "17:00–23:00 营业"},
                              note="营业信息 v1", publish=True)
        app.content.submit_ru(item.id, zh_no=1, translator="译员-列娜",
                              body={"title": ru_title, "summary": "Открыто 17:00–23:00"},
                              note="营业信息译文 v1", publish=True)
        app.events.register_occurrence(Occurrence(
            entity_id=item.id, kind="merchant", area=area,
        ))

    # ---- 承办方联系人（仅本承办方 + 运营可见） -------------------------------
    contacts = [
        ("cnt_sport", "org-sport", "张主任", "13900001111", "体育大会总协调"),
        ("cnt_culture", "org-culture", "刘团长", "13900002222", "合唱交流艺术统筹"),
        ("cnt_trade", "org-trade", "赵经理", "13900003333", "博览会招商负责人"),
        ("cnt_commerce", "org-commerce", "孙会长", "13900004444", "夜市与商户管理"),
    ]
    for cid, org, name, phone, title in contacts:
        app.add_contact(cid, org, name, phone, title)

    return ids
