# CHANGELOG

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格。
版本号用于**部署包发布标识**（非语义化 API 承诺）。

---

## [未发布] — 2026-09-16 · 追加约束补做（计分一致性 + 可配置总清单）

### 新增（Added）
* `tools/verify_scoring_parity.py` —— 计分一致性验证：同一份输入双跑「部署包实现 ⟷ 课题权威
  条目库」，**L1 条目级 / L2 量表级 / L3 常量级 / L4 缺失与严格模式** 四层逐条断言，
  任意差异即失败并打印明细（量表 / 提交 / 字段 / 两侧取值）。**1771 项断言全等**；
  已并入 `tools/selfcheck.py` 的 `parity` 组（CI/自检门禁）。
* `tools/audit_config.py` —— 部署个性化项**穷尽审计**扫描器（扫描 105 文件；待外提 0）。
* `tools/gen_config_doc.py` + `docs/可配置项总清单.md` —— **180 项**配置总清单，
  含 `键名 / 类型 / 默认值 / 单位 / 作用 / 是否必填 / 生效方式 / 所属分组`，`--check` 防漂移。
* `docs/分析口径一致性核对.md` —— 计分 / 饮食抽取 / 完成度依从性 / 统计口径的逐项比对与结论。
* `config/study_calendar.example.yaml`、`tests/test_parity_and_config.py`。

### 修复（Fixed）
* **「装上即坏」：** `tools/verify_scoring_parity.py` 按**仓库布局**定位计分模块，而
  `install.sh` 会把 `services/*` 抹平复制到安装根 → 装到服务器后 `parity` 自检恒报「模块不存在」。
  改为**双布局探测**，真实沙箱布局下实测通过。
* `services/common/lib_study.py`：完成度注释与取值不一致（注释「量表 3 次×3 分」vs 取值 1 分/次、
  上限 10 分）——修正注释并注明「仅供控制台显示，科研口径另见口径核对文档」。

### 变更（Changed）
* 端口外提：`services/{diet_survey,sjtu_survey_pro}/run_all.sh` 改为 `${PORT_*:-默认值}`。
* 邮件域名 / 问卷平台 API base 外提（`email.*`、`survey_platforms.*.api_base` 真正被服务读取）。

### 验证（Verified）
* `selfcheck`（真实沙箱布局）：**34 通过 / 0 失败 / 8 跳过**（含 parity、config.env_bridge）。
* `bash -n` 9/9；`install.sh --dry-run` 退出 0 且幂等；schema ↔ example **180 键**一致。
* 「改配置即生效」：`--render-timers` 实测 `*:0/60 → *:0/20`、`*:0/30 → *:0/10`、
  `RandomizedDelaySec 60 → 90`（证据 `_测试证据_20260916g/改配置即生效/`）。

---

## [未发布] — 2026-09-16 · 商用级完善

标题：**量表 webhook 链路 + LLM 分析可配置时段 + 时间项统一可配 + 看板四缺口 + 结构与安全保障**

### 新增（Added）

* **量表问卷 Webhook（A）**
  * `services/sjtu_survey_pro/survey_webhook_listener.py`：接收问卷平台提交事件，
    立即拉取该条答卷 → 计分 → 入邮件队列；**与每小时定时拉取并存**
    （webhook 为主、定时兜底），幂等去重（`submissions.submission_id` UNIQUE）。
  * `systemd/research-survey-webhook.service`（`Type=simple`，端口 `services.survey_webhook.port`，默认 9877）。
  * 共享密钥校验（Header / Query / Body 三种携带方式）、请求体上限、单 IP 限流、
    失败重试队列（`.webhook_queue/retry/`）、`/healthz` 暴露 `webhook.mode`。
  * 「平台不支持回调」自动回落：`timer_only` / `degraded_timer_fallback` 模式
    + 自检 `webhook.*` 检查项 + 看板展示。
  * 文档：`docs/Webhook配置与验证.md`（启用 3 步 + 验证 4 步 + 回落证据链）。
* **LLM 分析可配置时段（B）**
  * `analysis.mode`：`realtime`（默认，等价旧行为）/ `offpeak`（仅窗口内消费）/
    `hybrid`（窗口内全速 + 窗口外按日额度限量）。
  * 窗口可配：`analysis.window_start/end`（支持跨天）、`analysis.timezone`、
    `analysis.workdays`、`analysis.throttle_seconds`、`analysis.concurrency`、
    `analysis.max_per_window`、`analysis.hybrid_daytime_max`、
    `analysis.idle_sleep_seconds`、`analysis.window_check_seconds`。
  * 幂等安全：原子领取（`os.rename`）、崩溃恢复（`processing/` 回 `pending/`）、
    窗口关闭「处理完当前任务再停」、跨天窗口由 `in_time_window` 统一处理。
  * `.llm_queue/state.json` 暴露 `mode / waiting_window / window_consumed /
    day_consumed / reason`，看板与控制台可见。
* **时间项统一可配（C）**
  * `config/app.schema.json` / `config/app.yaml.example` 扩展到 **151 键**，
    其中**时间项 55 个**（3 个拉取间隔、6+ 个 `schedule.*`、告警冷却、自检探活、
    LLM 窗口与节流、webhook 重试、备份时刻、每日报告时刻、各 timer 静默时段）。
  * `tools/appconfig.py`：`--render-timers` 渲染 `OnCalendar` /
    `RandomizedDelaySec` / `AccuracySec` / `Persistent`；
    `--render-env` 渲染端口与窗口；新增 `--check-example-keys`（悬空键门禁）与
    `--show-window`；时间窗口逻辑下沉到 `services/common/lib_schedule.py`（**单一真源**）。
  * 控制台新增「**调度**」页（`/api/schedule`）：分组展示全部时间项
    （键名/当前值/单位/作用/生效方式），支持修改 + 变更预览 + 写前自动备份。
  * 文档：`docs/时间项配置总表.md`（由 `tools/gen_time_config_doc.py` 从配置生成）。
  * **各 timer 静默时段**：`schedule.quiet_hours_*` 生效于告警（warning 级静默、
    critical 始终发送）与定时拉取（静默跳过，**webhook 不受影响**）。
* **数据看板补全（D）** — 4 个新视图 + 4 个导出端点
  * `队列运行状况` `/queues`：LLM 队列（pending/processing/done/failed/等待窗口）、
    量表与饮食邮件队列（pending/sent/failed）、Webhook 回调健康与重试积压。
  * `依从性时间序列` `/adherence`：每人逐日提交热力图 + 分链路完成率趋势 + 整体趋势。
  * `数据质量` `/quality`：缺失项、异常值（超出量程）、重复提交、未映射条目、模拟残留。
  * `系统健康` `/system`：聚合全部服务 `/healthz` + 端口监听 + 数据库状态。
  * 导出：`/api/export/{queues,adherence,quality,system}`（CSV/JSON），
    并入「数据导出」中心（第 5 类数据集）。
* **公共库（结构优化，E）** `services/common/`
  * `lib_schedule.py`：时间窗口/时区/静默时段（唯一真源）
  * `lib_queue.py`：持久化文件队列（原子领取、幂等 upsert、崩溃恢复、死信）
  * `lib_ratelimit.py`：滑动窗口限流 + **持久化**登录锁定 + 口令强度
  * `lib_validate.py`：输入校验与净化（HH:MM、JSON 深度/大小、日志注入防护）
  * `lib_errors.py`：统一错误模型与**对外安全**的 HTTP 错误响应
  * `lib_runtime.py`（已有）：优雅停机 / `/healthz` / 结构化日志
* **质量**
  * `tests/`：50 项单元/集成测试（窗口调度 23 项、webhook 20 项、配置一致性 7 项），
    含**真实 HTTP 集成**（起 webhook 服务、注入 stub 拉取模块、断言 200/401/413/429）。
  * `ci.sh`：7 步门禁（bash -n / py_compile / 测试 / 悬空键 / 示例校验 / 脱敏 / 可选 dry-run）。
  * `CONTRIBUTING.md`、`docs/运维手册.md`、`CHANGELOG.md`。

### 变更（Changed）

* **端口全部改为配置驱动**：`services.*.port` → `appconfig.to_env()` 渲染
  `PORT_*` 环境变量；各服务入口读环境变量而非硬编码（survey/diet feedback、
  diet/survey webhook、data dashboard），systemd 单元移除硬编码端口参数。
* `scripts/health_monitor.py`：阈值改为读 `alert.*` / `analysis.*`；
  告警分级（critical/warning）+ 静默时段 + 冷却；新增量表 Webhook 与
  数据看板进程检查；修正 `admin_console.py` → `admin_console/app.py` 笔误。
* `scripts/send_alert.py`：支持 `--severity` / `--force`，静默时段按级别分流，
  发送失败按 `smtp.send_retry_*` 重试，静默期告警落 `logs/alerts_quiet.log`。
* `services/admin_console/auth.py`：口令强度校验、**持久化**登录锁定、
  API Key 常量时间比较。
* `services/admin_console/app.py`：Cookie 安全属性、CSRF 校验、登录锁定审计、
  调度页 API。
* `services/sjtu_survey_pro/survey_sync_cron.py`：抽出
  `process_row()/sync_rows()/sync(only_answer_ids=)` 供 webhook 复用（行为不变）。
* `services/diet_survey/webhook_listener.py`：改为多线程 + 请求体上限 + 限流。
* `nginx/research-app.conf`：新增 `location = /survey-webhook` 与
  `survey-hook.<域名>` server。
* `install.sh`：`secrets.env` 增加 `WEBHOOK_SECRET`；修正「初始口令被写回发布包
  `config/`」的工作区污染 bug。
* `tools/selfcheck.py`：新增 `webhook` 检查组、`config.example_keys`（悬空键）、
  `config.time_format`（HH:MM）检查；服务单元列表加入 `research-survey-webhook`。
* `.gitignore`：新增 `.webhook_queue/`、`login_lockout.json`、`.health_state.json`、
  `alerts_quiet.log`、`app.yaml.bak.*`。

### 修复（Fixed）

* 登录失败计数仅存内存 → 进程重启即绕过锁定（改为持久化）。
* `admin_console` 初始口令会写回**发布包内的 `config/`**，污染仓库工作区。
* `health_monitor.py` 检查的进程名 `admin_console.py` 与实际入口不符（永远告警）。
* 各服务端口硬编码，`services.*.port` 改了也不生效（与「改配置即改行为」冲突）。
* `diet_llm_queue` 失败重试的 `time.sleep` 不可被信号打断（停机时卡住）→ 改为
  `graceful_sleep`。

### 测试与验证

* `tests/` 50 项全通过；`ci.sh` 全绿。
* `tools/scan_sensitive.py` 阻断项 **0**。
* `tools/selfcheck.py` 在隔离沙箱全通过（占位符已替换）。
* 模拟数据端到端测试后**已彻底清除**并留证据（见 `测试报告_模拟数据.md`）。
* 验收：上期已移除模块的残留关键词（排除归档）命中 **0**（检查由 `ci.sh` 内置）；全脚本 `bash -n` 通过；
  `install.sh --dry-run` 退出 0 且两次输出一致。

### 追加约束执行（同日续）

* **约束① 分析口径一致性（新增可验证机制）**
  * `tools/verify_scoring_parity.py`：同一份输入同时跑程序内实现与权威条目库，
    四层断言（L1 条目级 / L2 量表级 / L3 常量级 / L4 缺失与严格模式）逐条相同。
    **实测 1771 项断言全等**；另有 4 条 PSQI `max_score` 元数据差异（不影响任何分数）
    被显式标注。
  * 纳入 `tools/selfcheck.py` 新增 `parity` 组（生产无权威库时优雅 skip）。
  * 核对中发现并记录：PSQI `睡眠问题` 题组实为 **13** 个子题（含 C2B/C6/C7A/C7B）；
    PSS-14 反向 6 条为矩阵子题；问卷另有 6 个食物频率矩阵（99 子题）属**非计分**采集。
* **约束② 可配置范围扩展到全部部署个性化项**
  * 新增配置：`study.calendar_path` / `study.planned_id_prefixes` /
    `study.total_planned_fallback`、`survey_platforms.*.api_base` / `.survey_id`、
    `site.subdomain_*`（7 项）、`logging.rotate_days` / `.format`、
    `llm.primary.temperature` / `.max_tokens` / `llm.backup.temperature` / `llm.prompt_file`、
    `scoring.reverse_items_enabled` / `.custom_bands_path` / `.strict`、
    `email.mx_domain` / `.mx_host` / `.message_id_domain`、`export.salt`。
    配置键总数 **151 → 180**。
  * **干预时间轴/轮次窗口/周次切点/目标完成度/名册规则外提**为
    `config/study_calendar.example.yaml` + `services/common/lib_study.py`
    （内置默认值 = 原硬编码值，行为不变；缺文件不影响启动）。
  * 移除硬编码：`data_dashboard/data.py` 的轮次日期、`248`、学号前缀规则；
    `app.py` 的 2026-07-06 起 24 天；三个 sync cron 的问卷平台 URL；
    `diet_llm.py` 的 `temperature=0.1`；`logrotate` 的 `rotate 14`；
    `fetch_survey_results.py` 的问卷 ID。
  * 新增 `tools/gen_config_doc.py` → `docs/可配置项总清单.md`（180 项，
    键名/类型/默认值/作用/必填/生效方式/注入环境变量名；`--check` 防漂移）。
  * 控制台新增「**应用配置**」页（`/api/appconfig`）：全量键可视化查看/修改/
    预览/恢复默认/写前备份/审计；**密钥类只显示是否已配置**。
  * 三方一致门禁：`--check-example-keys`（180 键无悬空）、`config.env_bridge`
    （151 个已声明环境变量覆盖全部 82 个服务侧读取，0 未声明）、
    `gen_config_doc.py --check`。
  * `install.sh`：安装 `study_calendar.yaml`；`secrets.env` 增加 `EXPORT_SALT`；
    logrotate 保留份数改为按 `logging.rotate_days` 渲染。
  * 测试 50 → **63 项**（新增 parity 3 项、三方一致 7 项、完备性 3 项）。

### 待用户决策（未决）

* 量表严格计分模式下「问卷改版导致未登记选项」会直接报错（`UnmappedAnswer`），
  是否需在上线前用最新批次真实答卷干跑一次。
* `LICENSE` 仍为占位。
* 本地改动**未 commit / 未 push**。
