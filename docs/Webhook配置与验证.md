# Webhook 配置与验证

> 对象：量表问卷链路（`research-survey-webhook`，默认 `:9877`）
> 相关：饮食链路同为 Webhook 接收（`research-diet-webhook`，默认 `:9876`），机制一致。
> 代码：`services/sjtu_survey_pro/survey_webhook_listener.py`

---

## 1. 它解决什么问题

量表链路原有两条时序：

| 通道 | 触发 | 最长滞后 | 作用 |
|---|---|---|---|
| **Webhook（本服务）** | 问卷平台提交事件回调 | **秒级** | 主通道：提交后立即计分 + 入邮件队列 |
| **定时拉取**（`research-survey-sync.timer`） | 每小时（`schedule.survey_sync_minutes`） | 60 分钟 | **兜底**：补漏、平台不支持回调时唯一通道 |

两条通道**共用同一份收数/计分/入库/邮件实现**
（`services/sjtu_survey_pro/survey_sync_cron.py` 的 `process_row()`），
因此不会出现「webhook 一套口径、定时另一套口径」。

**幂等**：写入层 `submissions.submission_id` 为 UNIQUE，
`save_submission()` 命中已存在则直接返回既有行 id。
webhook 与定时同时到达、或平台重复投递，都只会写一行。

---

## 2. 平台是否支持回调？

问卷平台（wj.sjtu.edu.cn）的「数据推送 / Webhook」能力**按平台版本与账号权限而异**：

| 情形 | 表现 | 系统行为 |
|---|---|---|
| 平台支持回调 | 平台侧能配置推送地址并成功投递 | `webhook` 模式；数据秒级到达 |
| 平台不支持回调 | 平台侧没有该选项，或投递始终失败 | `timer_only` 模式；**完全依赖定时拉取**，数据不丢 |
| 平台支持但回调中断 | 曾收到过，之后长时间无回调 | `degraded_timer_fallback`；自动回落定时拉取并告警 |

**关键设计**：本服务**不假设**平台一定支持回调。
`GET /healthz` 的 `webhook.mode` 字段直接告诉你当前处于哪个模式，
`webhook.timer_only_after_minutes`（默认 180 分钟）是判定阈值，
`tools/selfcheck.py` 的 `webhook.*` 检查项会同步报告。

---

## 3. 如何启用

### 3.1 服务器侧（必须）

1. **确认服务在跑**
   ```bash
   systemctl status research-survey-webhook
   curl -s http://127.0.0.1:9877/healthz | python3 -m json.tool
   ```
   期望：`"webhook": {"mode": "timer_only", ...}`（尚未收到回调属正常起始态）。

2. **配置共享密钥**（强烈建议）
   编辑 `/etc/research-app/secrets.env`（0600）：
   ```ini
   WEBHOOK_SECRET=<一串足够长的随机字符串>
   ```
   `app.yaml` 中 `webhook.secret: ${WEBHOOK_SECRET}` 已就位；改完重启：
   ```bash
   systemctl restart research-survey-webhook
   ```
   未配置密钥时服务**仍可运行**，但任何来源都能触发拉取（仅做限流），
   生产环境**必须**配置。

3. **开通反向代理路径**
   `nginx/research-app.conf` 已内置：
   ```nginx
   # api.example.com
   location = /survey-webhook { proxy_pass http://127.0.0.1:9877; client_max_body_size 1m; }
   ```
   另有独立域名 `survey-hook.example.com` → `:9877`（平台只允许填域名时用）。
   改完：
   ```bash
   nginx -t && systemctl reload nginx
   ```
   ⚠️ 若改动端口，请同步 `services.survey_webhook.port`，再
   `python3 tools/appconfig.py --render-env /etc/research-app/env` 并重启服务。

### 3.2 平台侧

1. 登录问卷平台 → 打开**量表问卷** → 「设置」→「数据推送 / Webhook（或"回调"）」；
2. 推送地址填（二选一，取决于平台允许填什么）：
   * **URL 形式**：`https://api.<你的域名>/survey-webhook`
   * **仅域名形式**：`https://survey-hook.<你的域名>/`
3. 若平台支持自定义 Header / 密钥，填 Header `X-Webhook-Token` = `WEBHOOK_SECRET` 的值；
   若只支持在 URL 带参数，用 `...?token=<WEBHOOK_SECRET>`；
   若只支持在 body 里带，服务同样会识别 `secret` / `token` / `webhook_token` 字段。

---

## 4. 如何验证（4 步，逐步可判定）

```bash
PY=~/.openclaw/workspace/.venv-diet/bin/python3   # 服务器上用 venv 的 python3
PORT=$(python3 -c "import yaml;print(yaml.safe_load(open('/etc/research-app/app.yaml'))['services']['survey_webhook']['port'])")
```

**① 健康端点通**
```bash
curl -s "http://127.0.0.1:${PORT}/healthz" | python3 -m json.tool
```
期望：HTTP 200，含 `service=survey_webhook`、`webhook.mode`、`webhook.received_total`。

**② 密钥生效（未带令牌应被拒）**
```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST "http://127.0.0.1:${PORT}/" \
     -H 'Content-Type: application/json' -d '{"id":"0"}'
```
- 配了密钥 → 期望 **401**
- 没配密钥 → 期望 200/200 系列（此时建议去把密钥配上）

**③ 模拟一次回调，确认「立刻拉取」**
```bash
curl -s -X POST "http://127.0.0.1:${PORT}/" \
     -H 'Content-Type: application/json' \
     -H "X-Webhook-Token: ${WEBHOOK_SECRET}" \
     -d '{"id":"<一个真实 answerId>"}' | python3 -m json.tool
```
期望：`{"status":"ok","accepted":1,"new":1,...}`；
若该条已入库则 `new=0, skipped=1`（**幂等正确**）。
再查：
```bash
curl -s "http://127.0.0.1:${PORT}/healthz" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['webhook'])"
```
期望 `last_received_at` 已刷新、`received_total` +1、`mode` 变为 `webhook`。

**④ 平台侧真实提交**
在平台上**真填一份测试答卷**，随后：
```bash
journalctl -u research-survey-webhook -n 50 --no-pager | grep 收到回调
python3 tools/selfcheck.py --only webhook
```
期望：日志出现「收到回调 ip=… 候选 answerId=[…]」，自检 `webhook.stale` 为 ✅。

---

## 5. 平台不支持回调时（回落逻辑）

**无需任何额外操作**。系统自动回落，证据链如下：

1. `GET /healthz` → `webhook.mode = "timer_only"`，`hint` 写明「量表数据完全由定时拉取提供」；
2. `research-survey-sync.timer` **始终启用**（`install.sh` 会 enable + start），
   按 `schedule.survey_sync_minutes`（默认 60）兜底；
3. `tools/selfcheck.py --only webhook` → `webhook.state` 报 `skip` 并提示
   「尚未收到过回调（依赖定时拉取，属正常起始态）」；
4. `health_monitor.py` 的 `check_webhook_health()` 在超过
   `webhook.timer_only_after_minutes` 时发 **warning**（不是 critical）——
   因为这只是「没走快通道」，不是故障；
5. 看板「队列运行状况」页直接展示 `webhook.survey_mode`。

> **结论**：回调能力是**增强**而非**依赖**。任何时刻失去回调，数据仍由定时拉取补齐。

---

## 6. 失败重试与限流

| 机制 | 行为 | 可配置键 |
|---|---|---|
| 单 IP 限流 | 滑动窗口，超限返回 429 | `webhook.rate_limit_per_minute` |
| 请求体上限 | 超限返回 413 | `webhook.max_body_bytes` |
| 载荷校验 | 非 JSON / 超深 / 超大 → 400 | `webhook.max_body_bytes` |
| 本地重试队列 | `.webhook_queue/retry/`，指数退避，超次数转 `.dead` | `webhook.retry_max_attempts`、`webhook.retry_backoff_seconds`、`webhook.retry_poll_seconds` |
| 平台重投 | 服务**总是返回 200**（避免平台风暴），失败靠本地队列 + 定时兜底 | — |
| 定时金兜底 | 无论 webhook 成败，每小时拉取都会补齐 | `schedule.survey_sync_minutes` |

---

## 7. 排障速查

| 现象 | 排查 |
|---|---|
| `curl :9877/healthz` 连不上 | `systemctl status research-survey-webhook`；端口是否被占 `ss -ltnp \| grep 9877` |
| 平台投递总失败 | 平台能否访问 `https://api.<域名>/survey-webhook`；Nginx `access_log` 是否见到请求；`client_max_body_size` 是否过小 |
| 401 频繁 | 平台侧密钥与 `WEBHOOK_SECRET` 不一致 |
| 429 频繁 | 平台在重投风暴；调大 `webhook.rate_limit_per_minute` 或修平台回调配置 |
| `mode` 长时间 `degraded_timer_fallback` | 检查 Nginx 反代与平台回调配置；数据仍在由定时拉取补齐，**不会丢** |
| 回调到了但没新数据 | 该 answerId 已入库（幂等正常）；或平台首页未包含该条 → 服务会自动退回全量比对 |

---

## 8. 相关配置项速查

见 `docs/时间项配置总表.md` 的「Webhook 重试与限流」分组，共 7 项：
`webhook.enabled`、`webhook.max_body_bytes`、`webhook.rate_limit_per_minute`、
`webhook.retry_max_attempts`、`webhook.retry_backoff_seconds`、
`webhook.retry_poll_seconds`、`webhook.timer_only_after_minutes`。
端口相关：`services.survey_webhook.port`、`services.diet_webhook.port`。
