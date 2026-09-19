# 口岸文旅双语运营

面向中俄互免签证后口岸城市外籍游客的**双语活动与出行信息服务**。中文事实源由责任单位提交并版本化，俄文由译员绑定到具体中文版本；通关类页面必须通过政策复核并带生效区间，过期内容即使仍在缓存节点也不会对外返回。活动容量、临时取消、接驳班次、商户营业变化经事件流汇入，收藏跨日安排的游客在冲突时收到中俄双语、带替代方案的通知。

## 核心规则（不变量）

- **版本链可追溯**：中文事实源 `zh v1/v2…`；每个俄文版本都绑定一个具体中文版号（`ru.zh_no`）。中文版换版后，旧俄文立即转为 `stale`，不再对外；新译文只能绑定最新中文版，不能借旧版复活。
- **政策复核 + 生效区间**：通关页每个中文版本都要有一次 `approved` 复核和半开区间 `[start, end)`，区间用口岸时区（Asia/Shanghai）描述、内部按 UTC 比较。未复核（409）、未生效（425）、已过期/已废止（410）都不返回。
- **缓存硬闸**：边缘缓存写入时携带 `effective_until_utc`；读取时刻一旦到达边界，报文体物理删除且不外泄，源站不可达也不存在“旧政策顶着”的路径。换版/复核/取消按实体标签同时清除 `zh`/`ru` 变体。
- **事件驱动通知**：容量、取消、延期（含跨日）、接驳、商户事件进入游标式事件流，更新运营态并扫描受影响的跨日收藏，生成中俄双语通知与替代方案（同日同片区活动、同走廊备用班次、邻近营业商户）。
- **三类可见范围**：公开信息（visitor+）、承办方联系人（本活动承办方行级隔离 + 运营）、联动部门处置记录（仅运营/复核/联动部门）。
- **不可还原轨迹**：服务端只存游客令牌的加盐哈希；浏览/到场按“实体×口岸日历日”聚合，达到 k 匿名阈值（默认 5）才输出转化率；审计与日志对手机/邮箱/证件号脱敏，且不含游客令牌。

## 目录结构

```
domain/
  timeutil.py    时区、生效区间 Window（半开、UTC 比较、口岸/莫斯科渲染）
  privacy.py     令牌哈希、PII 脱敏、k-匿名转化账
  visibility.py  角色与三类可见范围、行级隔离
  store.py       可注入时钟、ID、游标事件总线、脱敏审计日志
  content.py     中俄文版本、翻译绑定、政策复核、对外解析与追溯 lineage
  events.py      事件接入、运营态目录、跨日收藏、替代方案通知、处置记录
  cache.py       边缘缓存：硬过期闸门与按标签清除
  pipeline.py    紧急更正阶段时间线（受理→…→对外可见，秒级计时）
  app.py         门面：发布联动清缓存、紧急更正全链路、角色裁剪
  seed.py        样例数据（体育大会/合唱交流/贸易博览会/夜间市集/通关政策/接驳/商户/联系人）
  api.py         JSON HTTP 适配层
tests/           策略生命周期、缓存与更正、事件通知、可见性与隐私、HTTP 端到端
scripts/operator_check.py  运营核验脚本（三项重点检查）
```

## 运行

```bash
python3 service.py --check            # 服务身份基础检查
python3 service.py --port 8000 --seed # 启动并载入样例数据
node test_service.js                  # 运行全部 48 个测试
python3 scripts/operator_check.py     # 运营三项重点核验（紧急更正速度/追溯/旧政策缓存）
```

## 主要 HTTP 接口

鉴权用请求头模拟：`X-Role`（visitor/organizer/reviewer/ops）、`X-Organizer-Id`、`X-Visitor-Token`。

| 方法与路径 | 说明 |
|---|---|
| `GET /health` | 服务身份（基线契约，载荷稳定） |
| `GET /content/{id}?lang=ru\|zh` | 游客读取；带 `X-Cache` 与 `Edge-Effective-Until` 头 |
| `POST /content/{id}/zh` · `/ru` | 责任单位/译员提交版本（可 `publish:true`） |
| `POST /content/{id}/publish` | 发布指定版本（联动清缓存） |
| `POST /policy/{id}/approve` · `/revoke` | 政策复核（带生效区间）/ 紧急废止 |
| `GET /lineage/{id}` | 中俄文版本与复核追溯（reviewer/ops） |
| `GET /events?after=&topics=&wait=` | 游标式事件流（支持长轮询） |
| `POST /events` | 承办方汇入容量/取消/延期/接驳/商户事件 |
| `GET|POST|DELETE /plans` | 游客跨日收藏（按令牌哈希存储） |
| `GET /notifications` | 中俄双语通知与替代方案 |
| `GET /contacts` | 承办方联系人（行级隔离） |
| `GET|POST /dispatches` | 联动部门处置记录（内部可见） |
| `POST /metrics/views` · `/arrivals`；`GET /metrics/conversion/{id}` | k-匿名浏览/到场转化 |
| `POST /corrections` | 紧急更正快速通道（一次走完中俄文+复核+清缓存+可见校验） |
| `POST /admin/clock` | 固定时钟，便于跨时区/过期场景验证（ops） |

## 对外状态语义

政策类不可返回时的状态码：`409` 未复核/俄文滞后、`425` 未到生效时刻、`410` 已过生效区间或被紧急废止（缓存节点同样返回 410，旧报文不复活）。
