# OpenClaw 健康评估系统 — 技术文档 v2

> 最后更新：2026-07-05 15:30  
> 服务器：10.116.4.156:9000 (管理控制台)  
> 本文档涵盖所有修复、架构变更和使用指南

---

## 一、系统架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                     /opt 目录结构                            │
├─────────────────────────────────────────────────────────────┤
│  /opt/run_all.sh              ← 统一启动/停止/状态脚本       │
│  /opt/admin_console.py        ← 远程管理控制台 (端口 9000)   │
│  /opt/TECHNICAL.md            ← 本文档                      │
│  /opt/logs/                   ← 控制台日志                   │
│                                                              │
│  /opt/sjtu_survey_pro/        ← 问卷评估系统                 │
│    feedback_server.py         → 网页反馈 (端口 8000/8080)    │
│    survey_analysis.py         → 量表分析引擎                 │
│    survey_database.py         → SQLite 数据库操作            │
│    survey_sync_cron.py        → 定时同步 (cron)              │
│    population_charts.py       → SVG 统计图生成               │
│    survey_data.db             → 问卷数据 (SQLite)            │
│                                                              │
│  /opt/diet_survey/            ← 饮食记录系统                 │
│    webhook_listener.py        → Webhook 接收 (端口 9876)     │
│    diet_feedback_server.py    → 反馈页面 (端口 8001)         │
│    diet_llm.py                → LLM 饮食建议生成             │
│    diet_llm_queue.py          → LLM 任务队列（持久化）       │
│    diet_database.py           → SQLite 数据库操作            │
│    diet_data.db               → 饮食数据 (SQLite)            │
└─────────────────────────────────────────────────────────────┘
```

### 服务端口

| 端口 | 服务 | 进程 | 说明 |
|------|------|------|------|
| 8000 | feedback_server | python3 feedback_server.py 8000 8080 | 问卷结果报告页面 |
| 8001 | diet_feedback_server | python3 diet_feedback_server.py 8001 | 饮食建议报告页面 |
| 9000 | admin_console | python3 admin_console.py 9000 | 🆕 远程管理控制台 v2 |
| 9876 | webhook_listener | python3 webhook_listener.py 9876 | 饮食问卷 Webhook 接收 |
| - | diet_llm_queue | python3 diet_llm_queue.py | LLM 任务队列后台处理 |

---

## 二、2026-07-05 完整修复记录

### 修复 1：IPAQ-S 重复显示

**问题**：量表评估结果页面中，国际体力活动问卷（IPAQ-S）结果显示了两次。

**根因**：`survey_analysis.py` 的 `display_order` 列表包含 `IPAQ-S`，同时代码又单独渲染 `ipaq_card`。

**修复** (`/opt/sjtu_survey_pro/survey_analysis.py`)：
- 从 `display_order` 中移除 `IPAQ-S`
- 保留独立 `ipaq_card`（IPAQ 的 MET-min/week 格式特殊）

### 修复 2：统计图宽度溢出

**问题**：双列排版时 SVG 统计图（340px）超出容器宽度。

**修复** (`/opt/sjtu_survey_pro/survey_analysis.py`)：
- 添加 CSS：`.scale-card svg { max-width: 100%; height: auto; }`
- 添加 `overflow: hidden` 到 `.scale-card`

### 修复 3：饮食建议生成失败（NameError 致命 Bug）

**问题**：提交饮食问卷后，页面永久显示"正在生成饮食建议"。

**根因**：`diet_llm.py` 中 `result[tokens_used]` 缺少引号，应为 `result['tokens_used']`。
Python 将 `tokens_used` 作为变量名解析，导致 NameError。**LLM API 实际调用成功**，但日志行崩溃使流程进入异常路径。

**修复** (`/opt/diet_survey/diet_llm.py`)：
- L302: `result[tokens_used]` → `result['tokens_used']`
- L325: `result[tokens_used]` → `result['tokens_used']`
- 备份：`diet_llm.py.bak`

**影响范围**：3 个卡住的任务（db_id=40, 41, 42）在修复后 30 秒内成功完成。

### 修复 4：数据库查询增强

**问题**：`get_submission_by_answer_id` 仅查询 `redirect_answer` 字段，部分记录可能存在 `submission_id` 字段中。

**修复** (`/opt/diet_survey/diet_database.py`)：
- 新增 `submission_id` 字段查询（整数 + 字符串两种方式）

### 修复 5：邮件流程移除

**变更**：
- 停止 3 个邮件 daemon 进程
- `survey_sync_cron.py` — 移除 `send_immediate` 调用
- `diet_llm_queue.py` — 移除邮件发送块

### 新增：远程管理控制台 v2

**访问地址**：`http://10.116.4.156:9000`

**六大功能模块**：

| 页面 | 路径 | 功能 |
|------|------|------|
| 📊 首页 | `/` | 系统概览、实时统计、服务状态、服务器信息 |
| 📋 问卷数据 | `/survey` | 搜索/浏览/查看/编辑/删除问卷提交，支持统计视图 |
| 🥗 饮食数据 | `/diet` | 搜索/浏览/查看/编辑/删除饮食记录，含 LLM 建议预览，支持统计视图 |
| 💾 数据导出 | `/export` | CSV/JSON 格式下载，/opt 文件列表 |
| 📁 文件管理 | `/files` | 浏览 /opt 目录树，在线查看文本文件 |
| ⚙️ 系统管理 | `/system` | 服务状态、重启控制、LLM 队列管理、实时日志 |

**控制台特性**：
- 全中文界面
- 响应式设计（适配桌面和移动端）
- 搜索筛选 + 分页（可调每页条数）
- 详细记录视图（含 JSON 格式化显示）
- 数据统计视图（量表均值、每日趋势、学生排行）
- 在线编辑（支持 JSON 文本字段）
- 一键服务重启
- LLM 队列手动处理 / 清除失败任务

---

## 三、服务管理命令

### 统一管理

```bash
bash /opt/run_all.sh start     # 启动所有服务
bash /opt/run_all.sh stop      # 停止所有服务
bash /opt/run_all.sh restart   # 重启所有服务
bash /opt/run_all.sh status    # 查看状态
```

### 子系统管理

```bash
# 问卷系统
bash /opt/sjtu_survey_pro/run_all.sh {all|server|status|fetch}

# 饮食系统
bash /opt/diet_survey/run_all.sh {all|webhook|server|llm|sync|status|stop}
```

### 通过管理控制台

访问 `http://10.116.4.156:9000/system`：
- 查看所有服务运行状态
- 一键重启单个或全部服务
- 查看实时日志尾部
- 手动处理 LLM 队列
- 清除失败任务

---

## 四、数据流程

### 问卷评估流程 (sjtu_survey_pro)

```
用户提交问卷 (wj.sjtu.edu.cn)
    ↓ [每 60 分钟]
survey_sync_cron.py
    ├── 拉取 WJX API 数据
    ├── 分析量表得分 (survey_analysis.py)
    ├── 存入 SQLite (survey_database.py)
    └── (邮件流程已移除 → 通过控制台管理)
    ↓
用户访问 /report?user=X&quest=Y&answer=Z (端口 8000)
    ├── feedback_server.py 查数据库
    ├── 有数据 → 返回 HTML 分析报告
    └── 无数据 → 显示 loading + 触发即时同步
```

### 饮食建议流程 (diet_survey)

```
用户提交饮食问卷 (wj.sjtu.edu.cn)
    ↓ [实时]
webhook_listener.py (端口 9876)
    ├── 存储记录到 SQLite (diet_database.py)
    └── 任务入队 (.llm_queue/pending/)
    ↓
diet_llm_queue.py (持续后台处理)
    ├── 调用 LLM 生成饮食建议 (diet_llm.py)
    ├── 主 API: models.sjtu.edu.cn (双 Key 轮换)
    ├── 备用 API: api.deepseek.com (deepseek-v4-flash)
    ├── 失败任务指数退避重试（最多 5 次）
    └── 写回数据库
    ↓
用户访问 /report?user=X&quest=Y&answer=Z (端口 8001)
    ├── diet_feedback_server.py 查数据库
    ├── 有建议 → 返回 HTML 饮食建议报告
    └── 无建议 → 显示 loading (5 秒自动刷新)
```

---

## 五、LLM API 配置

| 项目 | 主 API | 备用 API |
|------|--------|----------|
| URL | `https://models.sjtu.edu.cn/api/v1` | `https://api.deepseek.com` |
| Model | `deepseek-reasoner` | `deepseek-v4-flash` |
| Keys | 2 个，自动轮换 | 1 个 |
| RPM | 10 | - |
| TPM | 100,000 | - |

### 容错机制
- 双 Key 轮换（避免单 Key 限流）
- 主 API 失败 → 自动切备用 API
- 备用 API 也失败 → 指数退避重试（1min/2min/5min/10min/30min/1h/...）
- 5 次重试后永久标记失败
- 文件持久化（进程重启不丢任务）

---

## 六、数据库 Schema

### survey_data.db

```sql
CREATE TABLE submissions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id            TEXT NOT NULL,
    submission_id         TEXT UNIQUE,
    name                  TEXT,
    email                 TEXT,
    responses             TEXT,         -- JSON
    item_scores           TEXT,         -- JSON
    raw_data              TEXT,
    analysis              TEXT,         -- JSON
    redirect_user         TEXT,
    redirect_quest        TEXT,
    redirect_answer       TEXT,
    -- DEBQ (6 fields)
    debq_emotional_score  REAL,
    debq_external_score   REAL,
    debq_restrained_score REAL,
    debq_emotional_mean   REAL,
    debq_external_mean    REAL,
    debq_restrained_mean  REAL,
    debq_emotional_level  TEXT,
    debq_external_level   TEXT,
    debq_restrained_level TEXT,
    debq_total            REAL,
    debq_interpretation   TEXT,
    -- GAD-7 (3), PHQ-9 (3), PSQI (3), PSS-14 (3), GSRS (3)
    -- IPAQ-S (3), VSI (3), WHOQOL-BREF (3), BMI (3)
    created_at            TEXT DEFAULT (datetime('now'))
);
```

### diet_data.db

```sql
CREATE TABLE submissions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id            TEXT NOT NULL,
    submission_id         TEXT UNIQUE,
    name                  TEXT,
    email                 TEXT,
    responses             TEXT,         -- JSON
    raw_data              TEXT,
    record_date           TEXT,
    diet_description      TEXT,
    meal_count            INTEGER,
    organization          TEXT,
    dietary_advice        TEXT,         -- LLM 生成的饮食建议
    fulfillment_report    TEXT,         -- 上次建议落实情况
    redirect_user         TEXT,
    redirect_quest        TEXT,
    redirect_answer       TEXT,
    submitted_at          TEXT,
    created_at            TEXT DEFAULT (datetime('now'))
);
```

---

## 七、日志文件

| 路径 | 内容 |
|------|------|
| `/opt/diet_survey/logs/llm_queue.log` | LLM 队列处理日志 |
| `/opt/diet_survey/logs/diet_llm.log` | LLM API 调用日志 |
| `/opt/diet_survey/logs/webhook.log` | Webhook 接收日志 |
| `/opt/diet_survey/logs/feedback_server.log` | 饮食反馈服务日志 |
| `/opt/sjtu_survey_pro/server.log` | 问卷反馈服务日志 |
| `/opt/sjtu_survey_pro/.email_queue/sync_cron.log` | 数据同步日志 |
| `/opt/logs/admin_console.log` | 管理控制台日志 |

---

## 八、故障排查

| 症状 | 排查步骤 |
|------|----------|
| 饮食建议一直 loading | 1. 控制台 → 系统管理 查看 LLM 队列状态<br>2. 检查 LLM Worker 是否运行<br>3. 点击"手动处理队列"<br>4. 查看 LLM 日志是否有 NameError<br>5. 清除失败任务后重新提交 |
| 问卷报告无数据 | 1. `cd /opt/sjtu_survey_pro && python3 survey_sync_cron.py`<br>2. 查看 sync_cron.log<br>3. 检查 WJX API 可访问性 |
| 控制台无法访问 | 1. `curl http://localhost:9000/health`<br>2. `bash /opt/run_all.sh restart` |
| LLM API 全部失败 | 1. 检查 Key 余额<br>2. 检查网络：`curl https://models.sjtu.edu.cn/api/v1/models`<br>3. 备份 API Key 是否有效 |

---

## 九、文件变更清单

### 新增
| 文件 | 说明 |
|------|------|
| `/opt/admin_console.py` | 管理控制台 v2（全中文、全功能） |
| `/opt/run_all.sh` | 统一管理脚本 |
| `/opt/logs/` | 控制台日志目录 |
| `/opt/TECHNICAL.md` | 本文档 |

### 修改
| 文件 | 变更 |
|------|------|
| `diet_llm.py` | NameError 修复 |
| `survey_analysis.py` | IPAQ 重复 + SVG 溢出修复 |
| `survey_sync_cron.py` | 移除邮件发送 |
| `diet_llm_queue.py` | 移除邮件发送 |
| `diet_database.py` | 增强 answer_id 查找 |
| `run_all.sh` (×3) | 更新启动脚本 |

### 备份
| 文件 | 说明 |
|------|------|
| `diet_llm.py.bak` | 修复前代码 |
| `survey_analysis.py.bak3` | 修复前代码 |

---

## 十、2026-07-05 第二次更新 (15:41)

### 更新 1：Loading 页面重新设计

**旧版**：简单转圈动画 + "正在处理中，5 秒后刷新"

**新版**：
- 动态进度条（渐变色流动动画）
- 预估等待时长："预计等待 30~60 秒"
- 邮箱提示卡片："如加载时间超过 2 分钟，分析结果将自动发送至您填写的邮箱，请注意查收"
- 自动刷新间隔改为 8 秒

修改文件：
- `/opt/sjtu_survey_pro/feedback_server.py`
- `/opt/diet_survey/diet_feedback_server.py`

### 更新 2：邮件通知防护机制

为确保每条数据都能成功分析并通知用户，恢复了邮件发送功能作为防护：

- **问卷系统**：`survey_sync_cron.py` 在每条新数据成功分析后自动发送邮件
- **饮食系统**：`diet_llm_queue.py` 在 LLM 建议生成成功后自动发送邮件
- **邮件守护进程**：两个 email daemon 均已启动，负责重试失败的邮件

邮件流程：
1. 提交数据 → 分析成功 → 立即尝试发送邮件
2. 发送失败 → 进入持久化队列 → daemon 指数退避重试（最多 10 次）
3. 最终失败 → 通知管理员 operator@example.edu


---

## 十一、2026-07-05 第三次更新 (15:52) — 异常监控系统

### 新增：任务异常监控 (`health_monitor.py`)

**文件**：`/opt/health_monitor.py`

**功能**：每 30 分钟自动检查所有系统组件，发现异常立即发送告警邮件至 `operator@example.edu`。

**检查项**：

| 类别 | 检查内容 | 异常阈值 |
|------|----------|----------|
| 问卷系统 | 数据库可访问性、记录总数、未分析记录 | 超过 120 分钟未分析 |
| 饮食系统 | 数据库可访问性、未生成建议记录、建议覆盖率 | 超过 30 分钟未生成建议 |
| LLM 队列 | 待处理/处理中/失败任务数、积压 | 积压 > 10 个 |
| 邮件队列 | 待发送/失败邮件数、积压 | 积压 > 5 封 |
| 服务进程 | 6 个关键进程是否在运行 | 任一进程缺失 |
| 同步日志 | 最近同步时间、同步结果 | 超过 120 分钟未同步 |

**使用方法**：
```bash
python3 health_monitor.py           # 运行检查 + 发送报告
python3 health_monitor.py --test    # 发送测试邮件
python3 health_monitor.py --quiet   # 仅异常时告警（cron 模式）
```

**Cron 配置**：
```
*/30 * * * * cd /opt && python3 health_monitor.py --quiet
```

### 新增：定时同步 Cron

```
0  * * * *  cd /opt/sjtu_survey_pro && python3 survey_sync_cron.py   # 问卷同步（每小时整点）
30 * * * *  cd /opt/diet_survey && python3 diet_sync_cron.py          # 饮食同步（每小时半点）
```

### 验证

测试邮件已成功发送至 operator@example.edu ✅


---

## 十二、2026-07-05 第四次更新 (16:21) — 进度条修复

### 修复：进度条改为单向递增

**旧版问题**：CSS `@keyframes progressAnim` 动画循环 5% → 75% → 5%，进度条来回跳动。

**新版**：JavaScript 驱动，从 0% 单向递增至 90%，永不回退。
- 递增速度逐渐减慢（初期快，后期慢，模拟真实加载）
- 到达 90% 后停留在 90%，等待页面自动刷新（8 秒）
- 文字提示同步更新

### 修改：邮箱提示文案

**旧版**：「如加载时间超过 2 分钟，分析结果将自动发送至您填写的邮箱，请注意查收」

**新版**：「分析完成后，结果将自动发送至您的邮箱。您可以关闭此页面，稍后查收邮件即可」

明确告知用户：无论页面是否加载成功，邮件都会发送，可以放心关闭页面。


---

## 十三、2026-07-05 第五次更新 (16:25) — 简化加载标识

### 变更：进度条 → 旋转加载图标

由于页面每 8 秒刷新一次会重置 JavaScript 状态，进度条无法正常递增。

**新版**：简洁的 CSS 旋转圆圈（spinner），配合文字提示，适配任何刷新频率。

页面现在显示：
- 🔄 旋转加载圆圈
- "预计需要 30~60 秒，页面将自动刷新"
- 📧 邮箱提示（可关闭页面，结果会发邮件）
- 提交编号


---

## 十四、2026-07-05 第六次更新 (16:38) — 监控系统 v2

### 新增特性

**1. 唯一告警编号**
每一条告警分配唯一编号，格式：`ALT-YYYYMMDD-HHMMSS-xxxx`
- 便于在邮件中搜索和引用
- 便于追踪告警生命周期
- 状态文件 `/opt/.health_state.json` 记录所有活跃告警

**2. 自动告警消除**
当异常恢复后，自动发送「告警消除通知」邮件：
- 列出已恢复的告警编号
- 显示触发时间和恢复时间
- 邮件标题用绿色 ✅ 标识

**3. 抖动过滤**
同一告警在 15 分钟内不重复发送，避免服务重启等短暂中断造成邮件轰炸。

**4. 定期健康报告**
系统正常时，每 2 小时发送一次「一切正常」确认邮件。

### 使用

```bash
python3 health_monitor.py --status   # 查看当前活跃告警
python3 health_monitor.py --quiet    # cron 模式
python3 health_monitor.py --test     # 测试邮件
```

### 状态文件

`/opt/.health_state.json` 记录：
- 当前活跃告警列表（id, text, opened_at, last_seen）
- 已消除告警历史

