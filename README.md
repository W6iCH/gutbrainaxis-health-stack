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
| 问卷同步 | `services/sjtu_survey_pro` | `survey_sync_cron.py` | — (timer) | 15 分钟增量同步 |
| 问卷邮件 | `services/sjtu_survey_pro` | `email_feedback.py` | — (daemon) | 队列 + 退避重试 |
| 饮食反馈 | `services/diet_survey` | `diet_feedback_server.py` | 8001 | 饮食建议报告 |
| 饮食 Webhook | `services/diet_survey` | `webhook_listener.py` | 9876 | 接收提交事件 |
| 饮食 LLM | `services/diet_survey` | `diet_llm_queue.py` | — (worker) | 营养分析队列 |
| 运动同步 | `services/exercise_survey` | `exercise_sync_cron.py` | — (timer) | 15 分钟增量同步 |
| 数据看板 | `services/data_dashboard` | `app.py` | 8090 | 研究者看板 |
| 管理控制台 | `services/admin_console` | `app.py` | 9000 | **本轮新增**，见 §5 |

> 端口都可在 `config/console.env` 或控制台界面里改，控制台会做**冲突检测**并重新生成 Nginx 配置。

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
| 配置管理 | 读写 `.env`，端口/域名/路径/邮箱告警/API key，带校验与「恢复默认」 |
| 进程开关 | 一键 start / stop / restart / enable / disable，实时状态（systemd） |
| 端口映射 | 可视化改端口与反代映射 → 变更预览 → `nginx -t` → reload，冲突检测 + 自动回滚 |
| 自检面板 | 服务探活、日志尾查看、数据库可读写检查 |
| 审计 | 所有写操作记入审计日志 |

技术选型、鉴权方式、接口清单见 [`02_控制台/设计说明.md`](02_控制台/设计说明.md) 与
[`services/admin_console/README.md`](services/admin_console/README.md)。

## 6. 评分逻辑修复

原始计分核心 `survey_analysis.py` 存在多处与国际量表官方规则不符的问题（PSS-14 反向计分缺失、
VSI/GSRS 量程错误、PSQI 成分算法不符、WHOQOL-BREF 量纲混用等）。本仓库提供：

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

## 8. 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/安装说明.md`](docs/安装说明.md) | 前置条件、分步安装、幂等与 dry-run |
| [`docs/控制台使用手册.md`](docs/控制台使用手册.md) | 登录、配置、进程、端口映射、自检 |
| [`docs/端口与反向代理配置.md`](docs/端口与反向代理配置.md) | 端口规划、Nginx 模板、改端口流程 |
| [`docs/常见故障排查.md`](docs/常见故障排查.md) | 症状 → 定位 → 处置 |
| [`docs/升级备份与回滚.md`](docs/升级备份与回滚.md) | 升级、备份、回滚、卸载 |
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
```

清单（逐条确认）：

- [ ] 无 `*.db` / `*.db-wal` / `*.log` / `.llm_queue/` / `.email_queue/` / `logs/`
- [ ] 无真实 SMTP 密码 / API key / rclone token / cloudflared 凭据（一律占位符）
- [ ] 无姓名 / 学号 / 邮箱 / 提交编号（含数据分析导出）
- [ ] 无 `venv/` / `__pycache__/` / `*.bak` / `.DS_Store`
- [ ] `config/*.example` 已随仓库发布（模板可被跟踪）
- [ ] `population_stats.json` **未**入库，仅提供 `.example` 模板
- [ ] 已确认仓库可见性（建议 **private**）与 `LICENSE` 授权

> 说明：`00_上线与决策.md`、`00_评分错误审计_初查.md` 属内部工作留档（含本地绝对路径），
> 已由 `.gitignore` 排除，不会随仓库上传；它们在磁盘上保留以备追溯。
