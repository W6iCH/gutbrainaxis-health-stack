# 问卷反馈系统 — 服务器部署技术说明

> 文档版本: 2.6  
> 部署服务器: `127.0.0.1` (ecs-aab7, Ubuntu 22.04 aarch64)  
> 部署目录: `/opt/sjtu_survey_pro`  
> 最后更新: 2026-07-04  
> 维护者: 维护者 (operator@example.edu)

---

## 一、系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    调查表 (wj.sjtu.edu.cn)                   │
│  用户提交问卷 → 跳转到 http://127.0.0.1:8000/report      │
│  公共 API: /api/v1/public/result/{token}/json               │
└──────────┬──────────────────────────────────────┬───────────┘
           │  HTTP redirect (user/quest/answer)   │  定时拉取 (15分钟)
           ▼                                      ▼
┌─────────────────────────┐    ┌──────────────────────────────┐
│   反馈服务器 (8000) │    │   同步定时任务 (cron)         │
│   feedback_server.py    │    │   survey_sync_cron.py        │
├─────────────────────────┤    ├──────────────────────────────┤
│  /report → 数据库→渲染  │    │  ① 调用 WJX 公共 API        │
│  /health → 健康检查     │    │  ② 对比数据库已有 answerId   │
│  /ping   → 连通测试     │    │  ③ 新数据→分析→存储→发邮件   │
│  /api/report → JSON    │    │  ④ 已有数据→跳过             │
│  触发同步 (cache-miss)  │    │                              │
└─────────────────────────┘    └──────────────────────────────┘
           │                              │
           ▼                              ▼
┌─────────────────────────────────────────────────────────────┐
│              SQLite 数据库 v2 (survey_data.db)               │
├─────────────────────────────────────────────────────────────┤
│  submissions 表: 每道题独立存储 + 10 量表分值 + BMI 单独列   │
│  email_log 表:   邮件发送记录                                 │
│  查询: get_latest_submission_by_answer() → 按 answerId 倒序  │
└─────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────┐
│           邮件发送队列 (email_feedback.py daemon)            │
├─────────────────────────────────────────────────────────────┤
│  即时发送 → 失败入队 → 指数退避重试(×10) → 通知管理员      │
└─────────────────────────────────────────────────────────────┘
```

### 数据流核心逻辑

```
每次提交问卷后:
  answer=4969681 跳转到 /report?answer=4969681
  ↓
  feedback_server 检查数据库中是否有 redirect_answer=4969681 的记录
  ↓
  有 → 展示那条记录自己的分析结果 ✅ (一一对应)
  无 → ① 立即触发后台同步 (trigger_sync_async)
       ② 展示"分析中..."自动刷新页面
  ↓
  后台同步线程:
    立即从 WJX API 拉取全部记录
     → 对比 answerId → 新回答 → 分析 → 存入 DB → 发送邮件
  ↓
  cron 定时任务每15分钟执行一次 (兜底):
    ① 从 WJX API 拉取全部记录
    ② 逐个 answerId 与数据库对比
    ③ 新回答 → 分析 + 存入 DB + 发送邮件
       存储时自动提取每题答案 (responses JSON) + 各量表总分 + 子量表分
    ④ 已有 → 跳过
  ↓
  下次刷新 /report?answer=4969681 时 → 展示该条数据 ✅
```

---

## 二、文件清单

| 文件 | 说明 |
|---|---|
| `survey_analysis.py` | **分析引擎** — 解析问卷数据，计算 10 量表评分 + BMI |
| `feedback_server.py` | **网页反馈服务器** — HTTP 服务，监听 8000，从数据库读取最新数据 |
| `email_feedback.py` | **邮件反馈系统** — SMTP 发送 + 持久队列 + 失败通知管理员 |
| `survey_database.py` | **数据库管理 v2** — SQLite，量表分值单独列，每道题独立 JSON 存储 |
| `survey_pipeline.py` | **流水线编排** (备用) — 手动触发: 加载 JSON → 分析 → 存储 → 发送 |
| `survey_sync_cron.py` | **定时同步任务** — 从 WJX 公共 API 拉取数据，增量更新数据库 |
| `fetch_survey_results.py` | **数据拉取** (备用) — 通过 Playwright 浏览器从 WJX 管理员页面拉取 |
| `run_all.sh` | **一键启动脚本** |
| `TECHNICAL.md` | **本文档** — 技术说明 |
| `survey_data.db` | SQLite 数据库文件 (v2: 52 列) |
| `migrate_db_v2.py` | 数据库迁移脚本 (v1→v2，运行后可删除) |
| `survey_data.db.v1.bak` | v1 数据库备份 (迁移前自动创建) |
| `survey_database.py.v1.bak` | v1 database.py 备份 |
| `sjtu_survey_data_page1.json` | 问卷原始数据缓存 (静态备份) |
| `.email_queue/` | 邮件队列目录 (pending/sent/failed) |
| `feedback_trigger.log` | 用户访问/同步触发记录日志 |

---

## 三、启动与停止

### 启动所有服务
```bash
cd /opt/sjtu_survey_pro
bash run_all.sh all
```

### 单独启动组件
```bash
bash run_all.sh server        # 网页反馈服务器 (8000)
bash run_all.sh email         # 邮件守护进程
bash run_all.sh bot           # 自动填卷机器人 (本地 Mac)
```

### 查看状态
```bash
bash run_all.sh status                    # 邮件队列状态
python3 email_feedback.py queue           # 待发送队列
python3 email_feedback.py status          # 详细统计
python3 survey_sync_cron.py              # 手动触发一次同步
```

### 停止服务
```bash
pkill -f feedback_server.py
pkill -f email_feedback.py
```

---

## 四、数据同步机制 (核心)

### 触发方式
| 方式 | 说明 | 频率 |
|---|---|---|
| 用户访问触发 | 访问 `/report?answer=xxx` 且数据未就绪 → 后台立即同步 | 按访问 (去重) |
| 系统 cron | 服务器定时任务 (兜底) | 每 15 分钟 (整点) |
| 手动触发 | `python3 survey_sync_cron.py` | 按需 |
| 强制重处理 | `python3 survey_sync_cron.py --force` | 按需 |

### 同步流程 (survey_sync_cron.py)
```
① GET https://wj.sjtu.edu.cn/api/v1/public/result/{token}/json
   ↓
② 遍历返回的 rows (每个 row 含 answerId)
   ↓
③ 检查 answerId 是否已在数据库 submissions.redirect_answer 中
   ↓
   ├─ 已存在 → log "跳过" → 继续下一条
   └─ 不存在 → ④ 分析 (10 量表评分 + BMI)
               ⑤ 存入数据库
                  - 原始数据存入 raw_data + analysis
                  - **每道题答案拆分存入 responses JSON**
                  - **各量表总分/子量表分/BMI写入独立列**
               ⑥ 发送邮件 (send_immediate)
               ⑦ log "新增"
   ↓
④ 统计汇总: API_rows, new, skipped, errors
```

### 即时触发同步 (feedback_server.py)
```
用户访问 /report?answer=xxx → 数据库无此数据
  ↓
  trigger_sync_async(answer_id)  // 在后台 daemon 线程运行
  ↓
  ① 检查该 answerId 是否已有同步任务在途中 → 有则跳过 (去重)
  ② 在后台线程调用 survey_sync_cron.sync()
  ③ sync 从 WJX API 拉取所有新数据并处理
  ④ 完成后自动返回 (不阻塞用户请求)
  ↓
  用户页面每 5 秒自动刷新 → 下次刷新即可看到报告 ✅
```

### 公共 API 配置
```python
API_URL = (
    "https://wj.sjtu.edu.cn/api/v1/public/result/"
    "<WJX_SURVEY_TOKEN>/"
    "json?pageSize=50&pageNum=1"
)
```
- `<WJX_SURVEY_TOKEN>` 是 WJX 公共访问令牌
- 无需认证，可直接调用
- 返回所有提交记录的完整答卷数据
- 每条记录含 `id` (answerId)、`answers` (完整回答)、`submitted_at` (时间戳) 等字段

### 增量判断依据
数据库 `submissions.redirect_answer` 字段与 API 返回的 `row["id"]` 做精确匹配。
仅当 `redirect_answer` 中不存在该 answerId 时，才作为新数据处理。

---

## 五、数据库结构 v2 (SQLite) — 49 列

### submissions 表 — 存储每次问卷提交

#### 基础信息
| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增主键 |
| `student_id` | TEXT NOT NULL | 学号 (索引) |
| `submission_id` | TEXT UNIQUE | WJX 流水号 (可空) |
| `name` | TEXT | 姓名 |
| `email` | TEXT | 邮箱 |

#### 题目级数据 (每个答题作为单独变量)
| 字段 | 类型 | 说明 |
|---|---|---|
| `responses` | TEXT (JSON) | **每道题的问答对: {"题目1": "答案1", ...}** |
| `item_scores` | TEXT (JSON) | 每题分值 (预留) |
| `raw_data` | TEXT (JSON) | 原始 API 响应 (完整保留) |
| `analysis` | TEXT (JSON) | 分析报告 (完整保留) |

#### 重定向追踪
| 字段 | 类型 | 说明 |
|---|---|---|
| `redirect_user` | TEXT | 问卷用户标识 |
| `redirect_quest` | TEXT | 问卷 ID |
| `redirect_answer` | TEXT | **WJX answerId** (用于匹配跳转参数) |

#### DEBQ (荷兰饮食行为问卷) — 33项, 3子量表
| 字段 | 类型 | 说明 |
|---|---|---|
| `debq_emotional_score` | REAL | 情绪性饮食总分 (0-52) |
| `debq_external_score` | REAL | 外部性饮食总分 (0-40) |
| `debq_restrained_score` | REAL | 限制性饮食总分 (0-40) |
| `debq_emotional_mean` | REAL | 情绪性饮食均分 |
| `debq_external_mean` | REAL | 外部性饮食均分 |
| `debq_restrained_mean` | REAL | 限制性饮食均分 |
| `debq_emotional_level` | TEXT | 低/中/较高/高 |
| `debq_external_level` | TEXT | 低/中/较高/高 |
| `debq_restrained_level` | TEXT | 低/中/较高/高 |
| `debq_total` | REAL | 3子量表原始分之和 |
| `debq_interpretation` | TEXT | 文字解读 |

#### GAD-7 (广泛性焦虑障碍量表) — 7项
| 字段 | 类型 | 说明 |
|---|---|---|
| `gad7_score` | REAL | 总分 (0-21) |
| `gad7_n_items` | INTEGER | 实际答题数 |
| `gad7_interpretation` | TEXT | 无/轻/中/重度 |

#### PHQ-9 (患者健康问卷抑郁量表) — 9项
| 字段 | 类型 | 说明 |
|---|---|---|
| `phq9_score` | REAL | 总分 (0-27) |
| `phq9_n_items` | INTEGER | 实际答题数 |
| `phq9_interpretation` | TEXT | 无/轻/中/中重/重度 |

#### PSQI (匹兹堡睡眠质量指数) — 19项
| 字段 | 类型 | 说明 |
|---|---|---|
| `psqi_score` | REAL | 总分 (0-21, >7提示障碍) |
| `psqi_n_items` | INTEGER | 实际答题数 |
| `psqi_interpretation` | TEXT | 文字解读 |

#### PSS-14 (感知压力量表) — 14项
| 字段 | 类型 | 说明 |
|---|---|---|
| `pss14_score` | REAL | 总分 (0-56) |
| `pss14_n_items` | INTEGER | 实际答题数 |
| `pss14_interpretation` | TEXT | 低/中/较高/高压力 |

#### GSRS (胃肠道症状评定量表) — 15项
| 字段 | 类型 | 说明 |
|---|---|---|
| `gsrs_score` | REAL | 总分 (15-105) |
| `gsrs_n_items` | INTEGER | 实际答题数 |
| `gsrs_interpretation` | TEXT | 无/轻/中/重度 |

#### IPAQ-S (国际体力活动问卷短版) — 7项
| 字段 | 类型 | 说明 |
|---|---|---|
| `ipaq_met_min_week` | REAL | MET-min/周 |
| `ipaq_sedentary_min` | REAL | 每日静坐时间(分钟) |
| `ipaq_interpretation` | TEXT | 低/中/高活动水平 |

#### VSI (内脏敏感指数) — 15项
| 字段 | 类型 | 说明 |
|---|---|---|
| `vsi_score` | REAL | 总分 (15-105) |
| `vsi_n_items` | INTEGER | 实际答题数 |
| `vsi_interpretation` | TEXT | 低/轻/中/重度焦虑 |

#### WHOQOL-BREF (世界卫生组织生活质量简表) — 26项
| 字段 | 类型 | 说明 |
|---|---|---|
| `whoqol_score` | REAL | 综合评分 (0-100) |
| `whoqol_n_items` | INTEGER | 实际答题数 |
| `whoqol_interpretation` | TEXT | 文字解读 |

#### BMI (身体质量指数)
| 字段 | 类型 | 说明 |
|---|---|---|
| `bmi_score` | REAL | BMI 值 (体重kg / 身高m²) |
| `bmi_category` | TEXT | 分类: 偏瘦/正常/超重/肥胖 |
| `bmi_interpretation` | TEXT | 文字解读 |

#### 时间戳
| 字段 | 类型 | 说明 |
|---|---|---|
| `submitted_at` | TEXT | 提交时间 |
| `created_at` | TEXT | 记录创建时间 (自动) |

### 关键查询

```sql
-- 获取最新的提交 (按 answerId 数值倒序)
SELECT * FROM submissions
WHERE redirect_answer IS NOT NULL AND redirect_answer != ''
ORDER BY CAST(redirect_answer AS INTEGER) DESC
LIMIT 1;

-- 按 answerId 精确匹配
SELECT * FROM submissions WHERE CAST(redirect_answer AS INTEGER) = 4969681;

-- 查某次提交的具体题目答案 (JSON 提取)
SELECT json_extract(responses, '$."姓名"') AS 姓名,
       json_extract(responses, '$."当你感到烦躁时..."') AS 情绪饮食_第一题
FROM submissions WHERE id = 35;

-- 筛选焦虑总分 >= 10 的记录 (中重度焦虑)
SELECT student_id, name, gad7_score, gad7_interpretation
FROM submissions WHERE gad7_score >= 10
ORDER BY gad7_score DESC;

-- 查 DEBQ 情绪性饮食高分者
SELECT student_id, name, debq_emotional_score, debq_emotional_level
FROM submissions
WHERE debq_emotional_level IN ('较高水平', '高水平')
ORDER BY debq_emotional_score DESC;

-- 查睡眠障碍记录
SELECT student_id, name, psqi_score, psqi_interpretation
FROM submissions WHERE psqi_score > 7;

-- 统计各量表均值
SELECT AVG(gad7_score) AS avg_gad7, AVG(phq9_score) AS avg_phq9,
       AVG(psqi_score) AS avg_psqi, AVG(debq_total) AS avg_debq
FROM submissions;

-- 查 BMI 超重/肥胖的学生
SELECT student_id, name, bmi_score, bmi_category
FROM submissions
WHERE bmi_category IN ('超重', '肥胖')
ORDER BY bmi_score DESC;

-- 查所有记录的 BMI 分布
SELECT bmi_category, COUNT(*) AS count,
       ROUND(AVG(bmi_score), 1) AS avg_bmi
FROM submissions
WHERE bmi_category IS NOT NULL
GROUP BY bmi_category
ORDER BY avg_bmi;
```

### Python 查询单题答案
```python
from survey_database import get_response_value
val = get_response_value(35, "姓名")
print(val)  # → "维护者"
```

### email_log 表 — 邮件发送记录
| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增主键 |
| `submission_id` | TEXT | 关联提交 |
| `recipient` | TEXT NOT NULL | 收件人 |
| `status` | TEXT | pending/sent/failed |
| `attempts` | INTEGER | 尝试次数 |
| `fallback_sent` | INTEGER | 是否已通知管理员 |
| `last_error` | TEXT | 最后一次错误信息 |

---

## 六、邮件系统说明

### 配置 (email_feedback.py)
```python
SMTP_HOST = "mail.sjtu.edu.cn"
SMTP_PORT = 465
USERNAME = "operator"
PASSWORD = "***"           # 实际密码在部署文件中
SENDER_EMAIL = "operator@example.edu"
ADMIN_EMAIL = "operator@example.edu"  # 失败通知接收
```

### 发送流程
```
send_immediate(recipient, submission_id)
    │
    ├── 成功 → 归档到 sent/
    │
    └── 失败 → 写入 pending/ 队列
                │
                守护进程每 30s 检查
                │  (指数退避: 1min→2min→5min→...→24h)
                │
                ├── 成功 → 移至 sent/
                │
                └── 已达 10 次上限 →
                    移至 failed/
                    + 发送失败通知 → operator@example.edu
```

### 手动操作
```bash
python3 email_feedback.py send user@example.com    # 立即发送
python3 email_feedback.py process                  # 强制处理队列
python3 email_feedback.py resend                   # 重试失败的邮件
python3 email_feedback.py queue                    # 查看队列
```

---

## 七、问卷分析支持的量表

| 量表 | 全称 | 项目数 | 评分方式 |
|---|---|---|---|
| DEBQ | 荷兰饮食行为问卷 | 33 (3子量表) | 均分分级: 低/中/较高/高 |
| GAD-7 | 广泛性焦虑障碍量表 | 7 | 0-21 → 无/轻/中/重度 |
| PHQ-9 | 患者健康问卷抑郁量表 | 9 | 0-27 → 无/轻/中/中重/重度 |
| PSQI | 匹兹堡睡眠质量指数 | 19 | ＞7分提示睡眠障碍 |
| PSS-14 | 感知压力量表 | 14 | 0-56 → 低/中/较高/高压力 |
| GSRS | 胃肠道症状评定量表 | 15 | 15-105 → 无/轻/中/重度 |
| IPAQ-S | 国际体力活动问卷 (短版) | 7 | MET-min/周分级 |
| VSI | 内脏敏感指数 | 15 | 15-105 → 低/轻/中/重度焦虑 |
| WHOQOL-BREF | 世界卫生组织生活质量简表 | 26 | 综合 0-100 分 |
| BMI | 身体质量指数 | 2 (身高+体重) | 中国标准: <18.5偏瘦/18.5-23.9正常/24.0-27.9超重/≥28.0肥胖 |

---

## 八、故障排查

### 页面空白 / 无法访问
```bash
# 1. 检查服务是否运行
ps aux | grep feedback_server
curl http://localhost:8000/health

# 2. 检查纯文本连通性
curl http://127.0.0.1:8000/ping

# 3. 检查端口
ss -tlnp | grep .8000.

# 4. 查看日志
cat /opt/sjtu_survey_pro/server.log
tail -50 /opt/sjtu_survey_pro/.email_queue/email_worker.log
```

### 页面卡在"分析中..."不刷新
```bash
# 1. 检查 trigger 日志，查看是否触发了同步
tail -20 /opt/sjtu_survey_pro/feedback_trigger.log
# 正常应有: "sync triggered by user visit (answer=xxx)"

# 2. 手动触发同步
python3 survey_sync_cron.py

# 3. 查看最新数据
python3 -c "
import sys; sys.path.insert(0, '/opt/sjtu_survey_pro')
from survey_database import get_conn
conn = get_conn()
rows = conn.execute('SELECT id, redirect_answer, bmi_score, bmi_category, name FROM submissions ORDER BY id DESC LIMIT 5').fetchall()
for r in rows: print(f'id={r[0]} answer={r[1]} BMI={r[2]} cat={r[3]} name={r[4]}')
conn.close()
"

# 4. 检查同步日志
cat /opt/sjtu_survey_pro/.email_queue/sync_cron.log
```

### 页面显示的不是最新数据
```bash
# 1. 检查数据库最新记录 (v2 可直接查量表分)
python3 -c "
import sys; sys.path.insert(0, '/opt/sjtu_survey_pro')
from survey_database import get_latest_submission_by_answer
s = get_latest_submission_by_answer()
print(f'学生: {s[\"name\"]} | GAD-7: {s[\"gad7_score\"]} | PHQ-9: {s[\"phq9_score\"]}')
"

# 2. 手动触发同步
python3 survey_sync_cron.py

# 3. 确认数据库中有正确的 answerId
python3 -c "
import sys; sys.path.insert(0, '/opt/sjtu_survey_pro')
from survey_database import get_conn
conn = get_conn()
rows = conn.execute('SELECT id, redirect_answer, gad7_score, created_at FROM submissions ORDER BY id DESC LIMIT 5').fetchall()
for r in rows: print(r['id'], r['redirect_answer'], r['gad7_score'], r['created_at'])
conn.close()
"

# 4. 查单题答案
python3 -c "
from survey_database import get_response_value
val = get_response_value(35, '姓名')
print(f'姓名: {val}')
"
```

### 邮件发送失败
```bash
python3 email_feedback.py queue     # 查看失败原因
python3 email_feedback.py resend    # 移回重试队列
python3 email_feedback.py process   # 立即处理
tail -30 .email_queue/email_worker.log  # 查看日志
```

### 定时任务不执行
```bash
crontab -l                          # 检查 cron 任务是否存在
grep sync_cron /var/log/syslog      # 查看 cron 执行日志
python3 survey_sync_cron.py         # 手动执行测试
```

### 数据库迁移相关
```bash
# 如需回退到 v1
cp survey_data.db survey_data.db.v2.bak          # 备份 v2
cp survey_data.db.v1.bak survey_data.db          # 恢复 v1
cp survey_database.py.v1.bak survey_database.py  # 恢复代码

# 重新运行 v2 迁移 (会跳过已有列)
python3 migrate_db_v2.py
```

---

## 九、维护规范

1. **修改代码后** → 更新本文档对应章节
2. **修改量表评分规则** → 更新 `survey_analysis.py` 中的评分函数
3. **修改邮件模板** → 更新 `survey_analysis.py` 中的 `render_html_report()`
4. **新增量表** → 在 `survey_analysis.py` 添加评分函数 + `find_scale_by_title` 关键字
5. **新增简单计算指标（如BMI）** → 添加计算函数 + `analyze_survey_responses()` 中调用 + `SCALE_DEFINITIONS` + `display_order` + `generate_suggestions()`
6. **数据库迁移** → SQLite `CREATE TABLE IF NOT EXISTS` + `init_db()` 内置 ALTER TABLE 迁移逻辑
7. **新增分值列** → 修改 `survey_database.py` 中的 `_extract_scale_scores()` + `init_db()` 的建表和 ALTER 循环
8. **更新 API 令牌** → 修改 `survey_sync_cron.py` 中的 `API_URL`
9. **徽章颜色逻辑** → `render_html_report()` 中的徽章由 `interp_str` 关键字匹配决定（在 `MAX_SCORES` 外执行），新增指标需确保 interpretation 包含对应 emoji/关键字
10. **定期维护** → 每月 `sqlite3 survey_data.db VACUUM` 压缩数据库
11. **同步时机** →
    - 用户访问触发：数据未就绪时后台立即同步（`feedback_server.py` `trigger_sync_async()`）
    - 定时任务：cron 每15分钟兜底
    - 手动：`python3 survey_sync_cron.py`
12. **内容对应规则** → `/report?answer=4969681` 展示的是 **answerId=4969681 这条记录自己的分析结果**，非最新提交的快照

---

> 文档存放位置: `/opt/sjtu_survey_pro/TECHNICAL.md`  
> 每次方案更新后请同步修改本文件

---

## 十一、人口基线统计图表系统 (v2.5 新增)

### 数据来源
`population_stats.json` — 56 名大学生人群各量表均值与标准差。

### 图表类型

| 量表 | 图表类型 | 说明 |
|---|---|---|
| GAD-7, PHQ-9, PSQI, PSS-14, GSRS, VSI, WHOQOL-BREF | 正态分布曲线 (SVG) | 钟形曲线 + 用户红点 + 百分位 + Z值 |
| DEBQ (荷兰饮食行为问卷) | 二维 KDE 分布图 (SVG) | X轴=抑制(限制性饮食), Y轴=解除抑制((情绪+外部)/2), 展示4个人群点+用户标记 |
| IPAQ-S (国际体力活动问卷) | 正态分布曲线 (SVG) | 页面底部单栏展示 |
| BMI | 文本展示 | 嵌入基本信息卡片 |

### 布局
- BMI：置于基本信息区域，独占一行
- 8 个量表（GAD-7 ~ WHOQOL-BREF）：双列网格排版
- IPAQ-S：页面末尾单栏独占一行

### 技术实现
- 纯 SVG 内嵌渲染，无外部图片依赖，单图约 1~3 KB
- 正态分布使用 erf 近似函数，百分位精确到 0.1%
- DEBQ 二维 KDE 使用 60×60 网格 + 高斯核密度估计
- 响应式布局：>750px 双栏，≤750px 单栏

### 展示信息
每个统计图表显示：
1. 用户得分在正态分布曲线上的位置（红色标记点）
2. 人群均值参考线（虚线）
3. 百分位排名（如"高于/低于 X% 同龄人"）
4. 标准分数（Z值）

---

## 十二、维护规范（续）

13. **更新人口基线数据** → 修改 `population_stats.json`
14. **调整图表样式** → 修改 `population_charts.py` 中的 SVG 生成函数
15. **定时任务同步频率** → `crontab -e`，当前为 `*/15 * * * *`（每15分钟）
