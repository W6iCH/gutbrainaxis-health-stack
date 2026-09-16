# gutbrainaxis-health-stack

> 上海交通大学「个性化饮食干预」课题 —— 服务器端问卷 / 饮食记录 / 数据看板 / 管理控制台
> **一键部署包**（Ubuntu 22.04 / 24.04，aarch64 / x86_64）

本仓库把课题原先散落在服务器 `/opt` 下的若干 Python 服务整理为一个**可一键安装、可被管理控制台统一运维**的部署包。
仓库内**不含任何真实答卷、日志、凭据**：数据库只有空 schema，所有敏感配置一律占位符。

---

## 目录

- [1. 这套系统做什么](#1-这套系统做什么)
- [2. 服务清单与端口](#2-服务清单与端口)
- [3. 三步安装](#3-三步安装)
- [4. 目录结构](#4-目录结构)
- [5. 管理控制台](#5-管理控制台)
- [6. 评分逻辑修复](#6-评分逻辑修复)
- [7. 安全与合规](#7-安全与合规)
- [8. 文档索引](#8-文档索引)

---

## 1. 这套系统做什么

课题需要对大学生（肠-脑轴相关研究）做多量表心理健康与饮食/运动行为评估，并把结果**自动反馈给被试本人**、同时**给研究者一个数据看板**。系统由四条流水线组成：

| 流水线 | 输入 | 处理 | 输出 |
|---|---|---|---|
| 问卷评估 | wj.sjtu.edu.cn 问卷提交 + 重定向 | 拉取 → 计分（10 量表 + BMI）→ 入库 → 发邮件 | 个人 HTML 报告 + 邮件 |
| 饮食记录 | 微信/Webhook 触发 | LLM 营养分析 → 队列 → 反馈 | 每日饮食建议 + 流水报告 |
| 运动问卷 | 定时同步 | 增量同步 → 入库 | 运动数据 |
| 数据看板 | 上述三库 | 聚合统计 | 研究者 Web 看板 |

## 2. 服务清单与端口

| 服务 | 目录 | 入口 | 默认端口 | 说明 |
|---|---|---|---|---|
| 问卷反馈 | `services/sjtu_survey_pro` | `feedback_server.py` | 8000 / 8080 | 计分 + 个人报告 |
| 问卷同步 | `services/sjtu_survey_pro` | `survey_sync_cron.py` | — (timer) | 每小时增量同步 |
| 问卷邮件 | `services/sjtu_survey_pro` | `email_feedback.py` | — (内联) | 队列 + 退避重试 |
| 饮食反馈 | `services/diet_survey` | `diet_feedback_server.py` | 8001 | 饮食建议报告 |
| 饮食同步 | `services/diet_survey` | `diet_sync_cron.py` | — (timer) | **本轮新增排程**，每 15 分钟 |
| 饮食 Webhook | `services/diet_survey` | `webhook_listener.py` | 9876 | 接收提交事件 |
| 饮食 LLM | `services/diet_survey` | `diet_llm_queue.py` | — (worker) | 营养分析队列 |
| 运动同步 | `services/exercise_survey` | `exercise_sync_cron.py` | — (timer) | 每 15 分钟增量同步 |
| 数据看板 | `services/data_dashboard` | `app.py` | 8090 | 研究者看板 |
| 每日报告 | `services/feedback` | `daily_report.py` | — (timer) | **本轮新增排程**，默认 08:30 |
| 二期菌群 | `services/microbiome` | `import_microbiome.py` | — (timer) | 宏基因组/SCFA/炎症因子导入 |
| 管理控制台 | `services/admin_console` | `app.py` | 9000 | 含「自检」页 |

> 单元总数：**13 个 service + 7 个 timer**，逐条清单见 [`功能与进程清单.md`](功能与进程清单.md)。
> 端口都可在统一配置 `config/app.yaml`（→ `/etc/research-app/app.yaml`）或控制台界面里改，
> 控制台会做**冲突检测**并重新生成 Nginx 配置。

## 3. 三步安装

```bash
# 1) 克隆
git clone <YOUR_REPO_URL> gutbrainaxis-health-stack
cd gutbrainaxis-health-stack

# 2) 预览将要执行的动作（不改系统）
sudo bash install.sh --dry-run

# 3) 真正安装
sudo bash install.sh
```

安装完成后：

- 安装日志：`$INSTALL_ROOT/logs/install.log`
- 控制台初始口令：`$INSTALL_ROOT/config/initial_password.txt`（权限 600，**首次登录后请立即修改**）
- 自检：`sudo bash verify.sh`

**域名配置（部署时自行设置）**：仓库内一律以 `example.com` 作占位。安装后到「管理控制台 → 端口映射」把各服务映射改成你的真实域名（或编辑 `/etc/research-app/env` 的 `SITE_DOMAIN=`，再在控制台点「应用」重新生成 Nginx 配置并 reload）。前置条件：各子域名的 A 记录已指向本机。

卸载 / 回滚见 [`docs/升级备份与回滚.md`](docs/升级备份与回滚.md)。

## 4. 目录结构

```
.
├── install.sh              # 一键安装（幂等）
├── uninstall.sh            # 卸载（默认保留数据）
├── verify.sh               # 自检
├── services/               # 五套服务源码
│   ├── sjtu_survey_pro/    #   问卷评估 + 计分核心 survey_analysis.py
│   ├── diet_survey/        #   饮食记录
│   ├── exercise_survey/    #   运动问卷
│   ├── data_dashboard/     #   数据看板
│   └── admin_console/      #   管理控制台
├── systemd/                # systemd 单元与 timer
├── nginx/                  # 反向代理站点模板
├── config/                 # *.example 配置模板（真实配置安装时生成）
├── scripts/                # 建库 / 备份 / 告警 / 健康巡检
├── tools/                  # 发布前脱敏扫描器
├── docs/                   # 使用说明（见 §8）
└── 01_评分修复/            # 计分逻辑审计报告 / 测试 / 旧新分回归
```

## 5. 管理控制台

安装后浏览器打开 `http://<服务器内网IP>:9000`（默认只监听本机/内网，需登录）。

| 模块 | 能力 |
|---|---|
| 配置管理 | 读写在统一配置 `app.yaml` 下派生的控制台配置，端口/域名/路径/邮箱告警/API key，带校验与「恢复默认」 |
| 进程开关 | 一键 start / stop / restart / enable / disable，实时状态（systemd 或 timer） |
| 端口映射 | 可视化改端口与反代映射 → 变更预览 → `nginx -t` → reload，冲突检测 + 自动回滚 |
| 自检面板 | 服务探活、日志尾查看、数据库可读写检查 |
| **自检**（本轮新增） | `/selfcheck` 页 + `/api/selfcheck`，复用 `tools/selfcheck.py` 同一后端 |
| 审计 | 所有写操作记入审计日志 |

技术选型、鉴权方式、接口清单见 [`02_控制台/设计说明.md`](02_控制台/设计说明.md) 与
[`services/admin_console/README.md`](services/admin_console/README.md)。

## 6. 评分逻辑修复

原始计分核心 `survey_analysis.py` 存在多处与国际量表官方规则不符的问题（PSS-14 反向计分缺失、
VSI/GSRS 量程错误、PSQI 成分算法不符、WHOQOL-BREF 量纲混用等）。

**2026-09-16 起，计分核心已整体替换为「原生编码口径」**（`SCORING_VERSION =
`survey_scoring:3.0-native-options``，此前 `2.0-official-rules` 已废止）：矩阵题按问卷平台
自带 `optionN` 序号计分，单选题按**实测真实 label** 显式映射，**未识别即报错**（不再静默丢弃）。
详见 [`services/sjtu_survey_pro/TECHNICAL.md`](services/sjtu_survey_pro/TECHNICAL.md) §二·A 与
[`14_评分标准/03_条目库_修正/与已发布版本差异.md`](../14_评分标准/03_条目库_修正/与已发布版本差异.md)。

本仓库还提供：

- [`01_评分修复/审计报告.md`](01_评分修复/审计报告.md) —— 逐条「原逻辑 / 新逻辑 / 影响 / 依据」
- [`01_评分修复/旧新分回归对照.md`](01_评分修复/旧新分回归对照.md) —— 旧分 vs 新分差异统计（**仅统计量，无个人信息**）
- [`01_评分修复/tests/`](01_评分修复/tests) —— 逐量表单元测试（反向极值 / 缺项 / 边界）

## 7. 安全与合规

- **零真实数据**：仓库内无 `.db` / `.log` / 队列目录 / 数据导出；安装脚本只建**空 schema**。
- **零凭据**：SMTP 密码、API key、rclone token、cloudflared 凭据一律占位符，安装时从环境变量/交互输入注入。
- **可移植**：不写死绝对路径，统一由 `INSTALL_ROOT` 与环境变量驱动。
- **发布前自检**：

  ```bash
  python3 tools/scan_sensitive.py            # 0 阻断项才可推送
  ```

- 控制台默认仅本机/内网可访问，**不要直接暴露公网**。
- 量表条目文本与计分规则各有其使用条款，发布前请确认授权（见 `LICENSE` 说明）。

## 8. 默认人群数据（本轮新增）

个人报告里的「该分数在同侨人群中的位置」正态曲线与百分位，需要一个**人群基线**。
此前仓库只提供一个数值全为占位（`mean=0, sd=1`）的示例文件，图表实际无意义。
本轮把基线改为**内置默认数据**：

| 项 | 值 |
|---|---|
| 文件 | `services/sjtu_survey_pro/population_stats.default.json` |
| 来源 | `13_问卷原始数据_20260916/raw/量表问卷_226份.json`（226 份 / 67 名学生） |
| 口径 | **原生编码口径（Edition B）**，条目库 `14_评分标准/03_条目库_修正/survey_scoring_native.py`（只读引用） |
| 规模 | 16 个量表键 × 全样本 + 三个时点（T0/T1/T2 = 74/74/78）；每单元含 `n / mean / sd / min / max / median / q05…q95` |
| 隐私 | **只含聚合统计，绝不含任何个体作答行**；样本量 < 5 的单元整体不输出 |
| 可复现 | `python3 tools/build_population_stats.py`（只读原始问卷 + 权威条目库） |

程序读取顺序（`population_charts._load()`）：

1. `services/sjtu_survey_pro/population_stats.json` —— **本地覆盖**（部署方用真实基线替换）
2. `services/sjtu_survey_pro/population_stats.default.json` —— 内置默认（本文件）

**计分口径一致性**：基线文件同时包含 `2.0-official-rules` 与 `3.0-native-ordinal` 两个口径块，
程序按 `app.scoring_version` 选择**与自身计分相同的块**，避免「分数一套口径、基线另一套口径」
导致的百分位失真。**默认推荐 `3.0-native-options`**（与 `3.0-native-ordinal` 同义，均指原生编码口径）。

**替换为你自己的基线**：

```bash
# ① 用你自己的原始问卷生成
python3 tools/build_population_stats.py --raw <你的问卷.json> --out population_stats.json
# ② 或直接手写一个同结构 JSON 放到 services/sjtu_survey_pro/population_stats.json
#    必须含：source / n / computed_with / scales.<量表>.{mean,sd,min,max}
```

> ⚠️ 基线必须用与程序**同一版计分规则**计算，否则百分位失真。
> 若两个文件都不存在，图表会自动跳过（返回空串），不影响计分与报告生成。

---

## 9. 统一配置（本轮新增）

所有可配置项集中在**一个文件**：`config/app.yaml`（安装后为 `/etc/research-app/app.yaml`），
由 JSON Schema `config/app.schema.json` 做**启动即校验**。

覆盖范围：端口 / 域名 / DNS / 反向代理、各服务目录与入口、数据库路径、SMTP 与告警、
LLM（主+备）、三个问卷平台 Token、调度周期、日志级别、备份策略、自检开关、二期菌群存储。

**密钥分离**：`app.yaml` 里只写 `"${SMTP_PASSWORD}"` 这类引用；真实值放在
`/etc/research-app/secrets.env`（0600）。**仓库内零密钥**。

```bash
PY=~/.openclaw/workspace/.venv-diet/bin/python3

# 校验（缺项 / 类型错 / 越界 / 未替换占位符 → 逐条报出键位置，退出码 1）
$PY tools/appconfig.py --config /etc/research-app/app.yaml --check

# 渲染 systemd 环境文件（由 app.yaml 生成，不要手改 /etc/research-app/env）
$PY tools/appconfig.py --config /etc/research-app/app.yaml --render-env /etc/research-app/env

# 渲染 timer 周期覆盖片段（来自 schedule.*）
$PY tools/appconfig.py --config /etc/research-app/app.yaml --render-timers /etc/systemd/system
```

安装脚本会自动完成上述渲染；你只需改 `app.yaml`（并在 `secrets.env` 填密钥）后重启服务。

---

## 10. 自检（本轮新增）

统一的系统自检，**命令行与管理控制台共用同一后端**（`tools/selfcheck.py`）。

```bash
PY=~/.openclaw/workspace/.venv-diet/bin/python3
sudo bash verify.sh                      # 安装后自检（封装 selfcheck.py）
$PY tools/selfcheck.py                   # 全部检查
$PY tools/selfcheck.py --json            # 机器可读
$PY tools/selfcheck.py --only config,database
$PY tools/selfcheck.py --no-external     # 离线环境（不探外网）
$PY tools/selfcheck.py --report logs/selfcheck.json
```

**覆盖 12 组**：平台与环境 / 配置齐备性 / 目录与权限 / systemd 单元 /
systemd timer / 端口监听 / HTTP 探活 / 数据库（可读・可写・schema）/
Nginx（`-t` + 站点启用）/ 外部依赖（问卷 API・SMTP・LLM，**串行限流**）/
磁盘余量 / 人口基线 + 模拟数据残留 + 日志轮转。

**输出语义**：每项为 `通过 / 失败 / 跳过`，失败项附**修复建议**；
跳过项给出原因（如非 Linux、未安装 nginx）。**退出码** 0=全过 / 1=有失败 / 2=致命，可直接用于 CI。

控制台入口：浏览器打开 `http://<内网IP>:9000/selfcheck`，或调 `GET /api/selfcheck`。
每个 HTTP 服务另提供统一探针 `GET /healthz`（含数据库可读性检查）。

---

## 11. 模拟数据测试（本轮新增）

用于在**不接触真实数据**的前提下验证全部功能链路，测后**必须清除**。

```bash
PY=~/.openclaw/workspace/.venv-diet/bin/python3

# 注入模拟数据并端到端验证（问卷收数→计分→入库→邮件队列→LLM 队列→看板→控制台→自检）
$PY tools/seed_mock_data.py  --root /tmp/gba-test --n 3 --report /tmp/seed.json

# 清除全部模拟数据并**校验清除干净**（输出前后对照 + 残留复查 + 证据 JSON）
$PY tools/clear_mock_data.py --root /tmp/gba-test --report /tmp/clear.json
```

* 模拟数据一律带 `MOCK-` 前缀；`clear_mock_data.py` 退出码 0 才代表清干净。
* 清除含：库记录、LLM 队列任务、含标记的日志行，并做 `WAL checkpoint + VACUUM`。
* 完整测试报告与证据： [`测试报告_模拟数据.md`](测试报告_模拟数据.md)、`_测试证据_20260916/`。

---

## 12. 二期菌群数据（本轮新增，预留）

宏基因组 / SCFA / 炎症因子有独立库与导入接口，与一期数据解耦。

```bash
# 把 CSV 放进投递目录（每小时由 timer 自动导入）
sudo cp your_data.csv /opt/gutbrainaxis/microbiome/import/
# 或手动导入
$PY services/microbiome/import_microbiome.py --file your_data.csv --omics-type scfa
```

* 支持**长表**（`sample_id,…,feature,value,unit`）与**宽表**（指标为列名）。
* 幂等：同文件 sha256 去重；`(sample_id, omics_type)` / `(sample_id, feature, method)` 业务主键 upsert。
* 未知列自动进 `extras` JSON，**不丢数据**；导入留痕在 `omics_import_log`。
* 字段定义与保留策略： [`docs/数据模型与存储.md`](docs/数据模型与存储.md)。

---

## 13. 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/安装说明.md`](docs/安装说明.md) | 前置条件、分步安装、幂等与 dry-run |
| [`docs/控制台使用手册.md`](docs/控制台使用手册.md) | 登录、配置、进程、端口映射、自检 |
| [`docs/端口与反向代理配置.md`](docs/端口与反向代理配置.md) | 端口规划、Nginx 模板、改端口流程 |
| [`docs/常见故障排查.md`](docs/常见故障排查.md) | 症状 → 定位 → 处置 |
| [`docs/升级备份与回滚.md`](docs/升级备份与回滚.md) | 升级、备份、回滚、卸载 |
| [`docs/数据模型与存储.md`](docs/数据模型与存储.md) | 逐库逐表字段、存放路径、保留策略 |
| [`功能与进程清单.md`](功能与进程清单.md) | 全部功能与 systemd 单元/端口/落点 |
| [`设计复查_问题清单.md`](设计复查_问题清单.md) | 本轮复查发现的问题、严重度、处理情况 |
| [`变更清单.md`](变更清单.md) | 本轮设计与功能改动逐条 |
| [`测试报告_模拟数据.md`](测试报告_模拟数据.md) | 模拟数据测试范围、结果、清除证据 |
| [`02_控制台/设计说明.md`](02_控制台/设计说明.md) | 控制台选型与设计 |
| [`01_评分修复/审计报告.md`](01_评分修复/审计报告.md) | 计分逻辑完整审计 |

---

## License

见 [`LICENSE`](LICENSE)（当前为**占位**，发布前需权利人确认）。

---

## 附：推送到 GitHub 前的自检清单

本仓库已按「零真实数据 / 零凭据 / 可移植」构建，推送前请再核一遍：

```bash
# ① 脱敏扫描：必须 0 阻断项（退出码 0）
python3 tools/scan_sensitive.py

# ② 评分修复自测：必须 111 项全通过
python3 01_评分修复/tests/test_scoring.py

# ③ 安装脚本语法与幂等（不改系统）
bash -n install.sh uninstall.sh verify.sh
bash install.sh --dry-run --prefix=/tmp/dryrun-gba
bash install.sh --dry-run --prefix=/tmp/dryrun-gba   # 再跑一次，输出应完全一致

# ④ 统一配置校验（应报出未替换的占位符）
python3 tools/appconfig.py --config config/app.yaml.example --check || true

# ⑤ 人群基线可重建（原生口径未映射应为 0）
python3 tools/build_population_stats.py --dry-run

# ⑥ 模拟数据残留检查（应无输出）
grep -rl "MOCK-" services scripts tools 2>/dev/null | grep -v "\.py$" || echo "✅ 无残留"
```

清单（逐条确认）：

- [ ] 无 `*.db` / `*.db-wal` / `*.log` / `.llm_queue/` / `.email_queue/` / `logs/`
- [ ] 无真实 SMTP 密码 / API key / rclone token / cloudflared 凭据（一律占位符）
- [ ] 无姓名 / 学号 / 邮箱 / 提交编号（含数据分析导出）
- [ ] 无 `venv/` / `__pycache__/` / `*.bak` / `.DS_Store`
- [ ] **无硬编码问卷 Token**（应为 `${WJX_*_TOKEN}` 或环境变量）
- [ ] **无模拟数据残留**（`MOCK-` 在非源码文件中零命中）
- [ ] `config/*.example` 与 `config/app.yaml.example` + `app.schema.json` 已随仓库发布
- [ ] `population_stats.json` **未**入库；仅 `population_stats.default.json`（**匿名聚合**）随包发布
- [ ] `secrets.env` / `app.yaml` 实体 **未**入库
- [ ] 已确认仓库可见性（建议 **private**）与 `LICENSE` 授权

> 说明：`00_上线与决策.md`、`00_评分错误审计_初查.md` 属内部工作留档（含本地绝对路径），
> 已由 `.gitignore` 排除，不会随仓库上传；它们在磁盘上保留以备追溯。
