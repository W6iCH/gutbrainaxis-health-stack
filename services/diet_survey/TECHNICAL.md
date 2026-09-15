# 饮食记录回调系统 v2 — 服务器部署技术说明

> 文档版本: 2.1  
> 部署服务器: `127.0.0.1` (ecs-aab7, Ubuntu 22.04 aarch64)  
> 部署目录: `/opt/diet_survey`  
> 最后更新: 2026-07-04  
> 维护者: 维护者 (operator@example.edu)

---

## 一、系统架构

```
┌─────────────────────────────────────────────────────────────┐
│              饮食记录问卷 (wj.sjtu.edu.cn)                   │
│  用户提交 → POST 回调到监听服务器                           │
│  提交后 → 跳转到反馈页面 /report?user=X&quest=Y&answer=Z    │
│  公共 API: /api/v1/public/result/{token}/json               │
└──────────┬──────────────────────────┬───────────────────────┘
           │  POST callback           │  redirect + API poll
           ▼                          ▼
┌─────────────────────────┐  ┌──────────────────────────────────┐
│  Webhook 监听 (:9876)   │  │  反馈服务器 (:8001) + 同步 cron  │
│  webhook_listener.py    │  │  diet_feedback_server.py         │
├─────────────────────────┤  │  diet_sync_cron.py               │
│  ① 接收回调 → 存库     │  ├──────────────────────────────────┤
│  ② 后台 LLM 分析       │  │  /report → 数据库 → 渲染页面     │
│  ③ 生成建议 → 发邮件   │  │  /health → 健康检查              │
│  首次提交也生成建议     │  │  定时拉取 API → 处理 → 存库     │
└──────────┬──────────────┘  └──────────────────────────────────┘
           │                              │
           ▼                              ▼
┌─────────────────────────────────────────────────────────────┐
│              SQLite 数据库 v2 (diet_data.db)                 │
├─────────────────────────────────────────────────────────────┤
│  submissions 表: 每题独立存储 + 饮食建议 + 落实报告           │
│  +dietary_advice (LLM 建议) +fulfillment_report (落实报告)   │
└─────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────┐
│    邮件发送队列 (diet_email_feedback.py daemon)              │
├─────────────────────────────────────────────────────────────┤
│  即时发送 → 失败入队 → 指数退避重试(×10) → 通知管理员      │
└─────────────────────────────────────────────────────────────┘
```

### 数据流核心逻辑

```
用户在饮食问卷提交每日饮食记录后:
  ↓
  【方式 A: Webhook 回调】
  问卷系统向监听地址 POST 回调 (JSON)
  → webhook_listener 接收 → 解析 → 存库 (含 redirect_answer)
  → 后台线程 → LLM 分析 → 存建议 → 发邮件
  → 首次提交：生成建议，但无落实情况章节
  → 非首次：生成建议 + 对照上次建议逐条评价落实情况
  ↓
  【方式 B: 定时同步 + 跳转触发】
  cron 每 60 分钟 + 用户访问触发
  → 从公共 API 拉取全部记录
  → 逐条对比 answerId
  → 新数据 → 存库 → LLM 分析 → 存建议 → 发邮件
  ↓
  【反馈页面】
  提交后跳转到 /report?user=X&quest=Y&answer=Z
  → 查询数据库中对应 answerId 的记录 (redirect_answer 或 submission_id)
  → 有建议 → 展示饮食建议页面 ✅
  → 无数据/建议未生成 → 展示"分析中..."自动刷新
```

---

## 二、文件清单

| 文件 | 说明 |
|---|---|
| `diet_database.py` | **数据库管理 v2** — SQLite，含 dietary_advice 和 fulfillment_report 列 |
| `diet_llm.py` | **LLM 分析引擎** — DeepSeek Reasoner，双 Key 轮换，温度 0.1 |
| `webhook_listener.py` | **Webhook 监听** — 接收 POST 回调，存库 + 后台 LLM + 邮件 |
| `diet_feedback_server.py` | **反馈页面服务器** — HTTP 服务 (8001)，展示饮食建议报告 |
| `diet_email_feedback.py` | **邮件反馈系统** — SMTP 发送 + 持久队列 + 失败通知管理员 |
| `diet_sync_cron.py` | **定时同步任务** — 从 WJX 公共 API 拉取，增量更新 |
| `run_all.sh` | **一键启动脚本** |
| `diet_data.db` | SQLite 数据库文件 (v2) |
| `food_nutrition.csv` | 食物营养成分表（参考数据） |
| `dish_nutrition_calculated.csv` | 菜肴营养成分计算表（参考数据） |
| `TECHNICAL.md` | **本文档** — 技术说明 |

---

## 三、数据库结构 v2 (diet_data.db)

### submissions 表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增主键 |
| `student_id` | TEXT NOT NULL | 学号 (索引) |
| `submission_id` | TEXT UNIQUE | 答卷 ID |
| `name` | TEXT | 姓名 |
| `email` | TEXT | 邮箱 |
| `responses` | TEXT (JSON) | 每道题的问答对 |
| `raw_data` | TEXT (JSON) | 原始回调/API JSON |
| `record_date` | TEXT | 记录日期 |
| `diet_description` | TEXT | 饮食描述全文 |
| `meal_count` | INTEGER | 大致餐次数 |
| `organization` | TEXT | 所属组织/学院 |
| `dietary_advice` | TEXT | **LLM 生成的饮食建议** |
| `fulfillment_report` | TEXT | **上次建议落实情况** |
| `redirect_user` | TEXT | 问卷用户标识 |
| `redirect_quest` | TEXT | 问卷 ID |
| `redirect_answer` | TEXT | 答卷 answerId |
| `submitted_at` | TEXT | 提交时间 |
| `created_at` | TEXT | 记录创建时间 (自动) |

---

## 四、LLM 饮食建议生成

### 配置
```python
BASE_URL = "https://models.sjtu.edu.cn/api/v1"
MODEL_NAME = "deepseek-reasoner"
# 双 Key 轮换，含限额控制
API_KEY_1 = "sk-h2X…LvaQ"
API_KEY_2 = "sk-_0i…2OLg"
# 限额（每个 Key）
RPM = 10       # 每分钟请求数
TPM = 100000   # 每分钟 token 消耗
Weekly = 1B    # 每周 token 总量
# 温度
TEMPERATURE = 0.1  # 低温度保证输出一致性
MAX_TOKENS = 1500  # 控制输出长度
```

### 建议格式

```
## 📊 今日饮食评估
[1-2句话概述今日饮食的优点和不足]

## 🎯 明日饮食建议
[一段总结：2-3句话概述明天的整体饮食方向]

最需改进：
1. [单一动作，如"早餐把油条换成燕麦粥"]
2. [单一动作]
3. [单一动作，可选]

（每条严格一个动词一个宾语，禁止"A和B""A并B""A搭配B"等并列结构）

## 📝 上次建议落实情况
[仅当有上次建议时输出。逐条对照评价：
✅ 已完成 / ⚠️ 部分做到 / ❌ 未做到]

## 💡 小贴士
[1条实用小知识]
```

### 关键行为规则

| 场景 | 行为 |
|---|---|
| 首次提交 | 生成建议，**不输出**落实情况章节 |
| 非首次提交 | 生成建议 + 逐条对照上次建议评价完成情况 |
| 无上次建议（DB 有记录但无 advice） | 视为首次，不输出落实情况 |

**实现方式**：代码层面检测 `prev_advice` 是否为空，为空时在 prompt 中强调"严禁输出落实情况章节"。

---

## 五、问卷跳转链接配置

问卷系统提交后跳转链接：

```
http://127.0.0.1:8001/report?user={{.User}}&quest={{.QuestID}}&answer={{.AnswerID}}
```

| 参数 | 模板变量 | 说明 |
|---|---|---|
| `user` | `{{.User}}` | 用户账号 |
| `quest` | `{{.QuestID}}` | 问卷 ID |
| `answer` | `{{.AnswerID}}` | 答卷 ID（匹配数据库 redirect_answer/submission_id） |

---

## 六、邮件系统

### 配置（与 sjtu_survey_pro 一致）
```python
SMTP_HOST = "mail.sjtu.edu.cn"
SMTP_PORT = 465
USERNAME = "operator"
SENDER_EMAIL = "operator@example.edu"
ADMIN_EMAIL = "operator@example.edu"
```

### 发送流程
```
send_immediate(recipient, submission_id, record)
    ├── 成功 → 归档到 sent/
    └── 失败 → 写入 pending/ 队列
                │
                守护进程每 30s 检查
                │  (指数退避: 1min→2min→5min→...→24h)
                │
                ├── 成功 → 移至 sent/
                └── 已达 10 次上限 → 移至 failed/ + 通知管理员
```

---

## 七、数据同步机制

| 触发方式 | 说明 | 频率 |
|---|---|---|
| Webhook 回调 | 问卷系统直接 POST 回调 | 每次提交 |
| 用户访问触发 | 访问 /report 且数据未就绪 | 按访问 (去重) |
| 系统 cron | 服务器定时任务 (兜底) | 每 60 分钟 |
| 手动触发 | `python3 diet_sync_cron.py` | 按需 |

### 公共 API
```python
API_URL = (
    "https://wj.sjtu.edu.cn/api/v1/public/result/"
    "<WJX_DIET_TOKEN>/json?pageSize=10&pageNum=1"
)
```

### 增量判断
数据库 `redirect_answer` 字段与 API 返回的 `row["id"]` 精确匹配。仅不存在时处理。

---

## 八、部署与管理

### 启动
```bash
cd /opt/diet_survey
bash run_all.sh all        # 启动所有
bash run_all.sh webhook    # 仅 Webhook (9876)
bash run_all.sh server     # 仅反馈页面 (8001)
bash run_all.sh email      # 仅邮件守护进程
bash run_all.sh sync       # 手动同步
bash run_all.sh status     # 查看状态
bash run_all.sh stop       # 停止所有
```

### 邮件队列
```bash
python3 diet_email_feedback.py queue
python3 diet_email_feedback.py status
```

### 测试 LLM
```bash
python3 diet_llm.py <student_id> "<diet_description>"
```

### Cron
```bash
# 当前配置：每小时第15分钟
15 * * * * cd /opt/diet_survey && python3 diet_sync_cron.py >> .email_queue/sync_cron.log 2>&1
```

---

## 九、与 sjtu_survey_pro 对比

| 特性 | `sjtu_survey_pro` | `diet_survey` v2 |
|---|---|---|
| 数据来源 | WJX 公共 API 定时拉取 + 跳转回调 | 问卷系统 POST 回调 + API 拉取 |
| 分析引擎 | 10 个量表评分 (DEBQ, GAD-7, PHQ-9 ...) | DeepSeek Reasoner LLM 饮食建议 |
| 建议格式 | 量表解读 + 分级建议 + 进度条 | 总结 + 1~3条改进 + 落实情况 + 小贴士 |
| 每题独立存储 | `responses` JSON | ✅ 同样实现 |
| 邮件通知 | ✅ Email 队列 | ✅ Email 队列 (相同配置) |
| 反馈页面 | :8000 / :8080 | :8001 |
| Webhook 监听 | ❌ | ✅ :9876 |
| LLM 配置 | ❌ | DeepSeek Reasoner, 双 Key 轮换, T=0.1 |
| 首次提交行为 | 分析所有提交 | 生成建议，无落实情况章节 |
| 落实情况 | ❌ | ✅ 逐条对照 ✅/⚠️/❌ |
| 人口基线对比 | ✅ SVG 正态分布图 | ❌ (不适用) |
| BMI 交叉引用 | 自身计算 | 引用 sjtu_survey_pro 数据库 |

---

## 十、维护规范

1. **修改代码后** → 更新本文档
2. **修改 LLM 提示词** → 更新 `diet_llm.py` 中 `SYSTEM_PROMPT`
3. **更换 API Key** → 更新 `diet_llm.py` 中 `API_KEYS`
4. **数据库迁移** → `python3 diet_database.py` 自动检测并添加新列
5. **定期维护** → 每月 `sqlite3 /opt/diet_survey/diet_data.db VACUUM`

---

> 文档存放位置: `/opt/diet_survey/TECHNICAL.md`

---

## 十一、LLM 任务队列 (v2.1 新增)

### 目的
确保高并发时 LLM 任务排队完成不遗漏，建议生成完毕后才显示报告和发送邮件。

### 文件
| 文件 | 说明 |
|---|---|
| `diet_llm_queue.py` | 持久化任务队列 worker |
| `.llm_queue/pending/` | 待处理任务 |
| `.llm_queue/done/` | 已完成任务 |
| `.llm_queue/failed/` | 失败任务 (5次重试后) |

### 启动
```bash
cd /opt/diet_survey
nohup python3 diet_llm_queue.py > logs/llm_queue.log 2>&1 &
```

### 备用 LLM API
主 API (models.sjtu.edu.cn) 失败时自动切换：
- 地址: https://api.deepseek.com
- 模型: deepseek-v4-flash
- 触发: 主 API 3次重试均失败 → 切换备用 2次尝试

### Webhook 流程变更
v2 → v2.1: 后台线程 → 持久化任务队列
- 接收回调 → 存库 → 入队 → 返回 OK
- Worker 顺序处理队列 → 生成建议 → 发邮件

