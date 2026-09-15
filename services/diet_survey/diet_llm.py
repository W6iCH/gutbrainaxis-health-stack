#!/usr/bin/env python3
"""
Diet LLM Analysis — 基于中国居民膳食指南的饮食建议生成
========================================================
调用 DeepSeek Reasoner API，基于每日饮食描述 + 历史记录 + BMI 
+ 膳食指南，生成固定格式的短期饮食建议 + 上次建议落实情况。

API Key 轮换机制：两个 Key 按 token 消耗轮流使用。
"""

import json
import os
import time
import threading
import logging
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ── LLM 配置 ─────────────────────────────────────────────────────────────

BASE_URL = os.environ.get("LLM_BASE_URL", "https://models.sjtu.edu.cn/api/v1")
MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "deepseek-reasoner")

# 备用 API (DeepSeek 官方, OpenAI 兼容)
BACKUP_BASE_URL = os.environ.get("LLM_BACKUP_BASE_URL", "https://api.deepseek.com")
BACKUP_MODEL_NAME = os.environ.get("LLM_BACKUP_MODEL_NAME", "deepseek-v4-flash")
BACKUP_API_KEY = os.environ.get("LLM_BACKUP_API_KEY", "")

# 两个 API Key（主，支持轮换）
# 主 API Key 列表（逗号分隔），通过环境变量 LLM_API_KEYS 注入
API_KEYS = [k.strip() for k in os.environ.get("LLM_API_KEYS", "").split(",") if k.strip()]

# Token 限额（每个 Key）
RPM_LIMIT = 10          # 每分钟请求数
TPM_LIMIT = 100000      # 每分钟 token 消耗
WEEKLY_TOKEN_LIMIT = 1000000000  # 每周 token 总量

# ── 日志 ─────────────────────────────────────────────────────────────────

log = logging.getLogger("diet_llm")
log.setLevel(logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "logs", "diet_llm.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(fh)

# ── API Key 管理器 ────────────────────────────────────────────────────────

class APIKeyManager:
    """管理多个 API Key 的轮换和限流。"""

    def __init__(self, keys):
        self.keys = keys
        self.index = 0
        self.lock = threading.Lock()
        # 每个 key 的统计数据
        self.stats = {
            i: {
                "requests_this_minute": 0,
                "tokens_this_minute": 0,
                "tokens_this_week": 0,
                "minute_start": time.time(),
                "week_start": time.time(),
                "last_error": None,
                "consecutive_errors": 0,
            }
            for i in range(len(keys))
        }

    def _reset_if_needed(self, ki):
        s = self.stats[ki]
        now = time.time()
        if now - s["minute_start"] >= 60:
            s["requests_this_minute"] = 0
            s["tokens_this_minute"] = 0
            s["minute_start"] = now
        if now - s["week_start"] >= 7 * 86400:
            s["tokens_this_week"] = 0
            s["week_start"] = now

    def get_key(self):
        """获取可用的 API Key（轮换）。"""
        with self.lock:
            original = self.index
            for _ in range(len(self.keys)):
                ki = self.index % len(self.keys)
                self._reset_if_needed(ki)
                s = self.stats[ki]

                # 检查是否可用
                if s["requests_this_minute"] >= RPM_LIMIT:
                    self.index += 1
                    continue
                if s["tokens_this_minute"] >= TPM_LIMIT:
                    self.index += 1
                    continue
                if s["tokens_this_week"] >= WEEKLY_TOKEN_LIMIT:
                    self.index += 1
                    continue

                self.index += 1
                return self.keys[ki], ki

            # 所有 key 都受限，等一等
            log.warning("All API keys at limit, waiting...")
            time.sleep(10)
            self.index = (original + 1) % len(self.keys)
            return self.keys[self.index % len(self.keys)], self.index % len(self.keys)

    def record_usage(self, ki, tokens_used):
        with self.lock:
            s = self.stats[ki]
            s["requests_this_minute"] += 1
            s["tokens_this_minute"] += tokens_used
            s["tokens_this_week"] += tokens_used
            s["consecutive_errors"] = 0

    def record_error(self, ki):
        with self.lock:
            s = self.stats[ki]
            s["consecutive_errors"] += 1
            s["last_error"] = time.time()

    def is_healthy(self, ki):
        with self.lock:
            s = self.stats[ki]
            return s["consecutive_errors"] < 3


# 全局实例
key_manager = APIKeyManager(API_KEYS)


# ── 膳食指南摘要（注入 prompt）─────────────────────────────────────────

DIETARY_GUIDELINES = """
## 中国居民膳食指南（2022）核心参考指标

### 每日推荐摄入量（成年人）

| 类别 | 每日推荐量 | 要点 |
|------|-----------|------|
| 谷类 | 200~300g | 全谷物和杂豆 50~150g |
| 薯类 | 50~100g | 可替代部分主食 |
| 蔬菜 | 300~500g | 深色蔬菜占 1/2 |
| 水果 | 200~350g | 果汁不可代替鲜果 |
| 奶及奶制品 | 300~500g 液态奶当量 | |
| 大豆及坚果 | 大豆 15~25g + 坚果 10g | |
| 动物性食物 | 120~200g | 鱼禽蛋瘦肉，每周蛋 300-350g |
| 烹调油 | 25~30g | 植物油为主，多种轮换 |
| 食盐 | < 5g | 警惕隐形盐（酱油、零食等） |
| 添加糖 | < 50g（最好<25g） | 不喝含糖饮料 |
| 水 | 1500~1700ml | 少量多次，白开水为主 |

### 准则概览
- 准则一：食物多样，合理搭配 — 每天 12 种以上食物，每周 25 种以上
- 准则三：多吃蔬果、奶类、全谷、大豆 — 餐餐有蔬菜，天天有水果
- 准则四：适量吃鱼、禽、蛋、瘦肉 — 每天 120~200g，少吃加工肉
- 准则五：少盐少油，控糖限酒 — 盐<5g，油 25-30g，糖<25-50g
- 准则六：规律进餐，足量饮水 — 早 25-30%，午 30-40%，晚 30-35%
- 准则七：会烹会选，会看标签 — 蒸煮炖 > 煎炸烤

### 素食人群补充
- 谷豆必须同食（蛋白质互补）
- 全素者须摄入发酵豆制品补充维生素 B12
- 多吃坚果、菌菇海藻类补充微量元素
"""


# ── 获取历史数据和 BMI ─────────────────────────────────────────────────

def get_student_history(student_id: str, current_submission_id: str = None):
    """
    获取某学生的历史饮食记录（排除当前提交）。
    返回格式化的历史摘要。
    """
    import sys
    sys.path.insert(0, BASE_DIR)
    from diet_database import get_conn

    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT id, record_date, diet_description, meal_count,
                   dietary_advice, fulfillment_report, created_at
            FROM submissions
            WHERE student_id = ?
              AND (submission_id != ? OR submission_id IS NULL OR ? IS NULL)
            ORDER BY created_at ASC
        """, (student_id, current_submission_id or '', current_submission_id)).fetchall()

        if not rows:
            return None, None

        history_parts = []
        prev_advice = None

        for i, row in enumerate(rows):
            date = row["record_date"] or f"记录{i+1}"
            desc = row["diet_description"] or "无描述"
            history_parts.append(f"### {date}\n{desc}")
            # 最后一次历史记录的 advice 作为"上次建议"
            advice = row["dietary_advice"]
            if advice:
                prev_advice = advice

        return "\n\n".join(history_parts), prev_advice
    finally:
        conn.close()


def get_student_bmi(student_id: str):
    """
    从 sjtu_survey_pro 数据库查询该学生的 BMI 数据。
    返回 BMI 值、类别、解读。
    """
    import sys
    sys.path.insert(0, os.environ.get("SURVEY_APP_DIR", os.path.dirname(os.path.abspath(__file__))))
    try:
        from survey_database import get_conn as get_survey_conn
        conn = get_survey_conn()
        try:
            row = conn.execute("""
                SELECT bmi_score, bmi_category, bmi_interpretation,
                       name, created_at
                FROM submissions
                WHERE student_id = ?
                  AND bmi_score IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
            """, (student_id,)).fetchone()
            if row and row["bmi_score"] is not None:
                return {
                    "bmi": row["bmi_score"],
                    "category": row["bmi_category"],
                    "interpretation": row["bmi_interpretation"],
                }
            return None
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"Failed to fetch BMI from sjtu_survey_pro for {student_id}: {e}")
        return None


# ── LLM 调用 ─────────────────────────────────────────────────────────────

def _try_api_call(api_key: str, base_url: str, model: str,
                  system_prompt: str, user_prompt: str,
                  max_tokens: int, timeout: int = 120) -> dict:
    """调用单个 API 端点。返回结果或引发异常。"""
    api_key_safe = api_key.encode("ascii", errors="replace").decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"{base_url}/chat/completions"
    req = Request(url, data=data, headers={
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {api_key_safe}",
        "Accept": "application/json",
    })
    with urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    content = result["choices"][0]["message"]["content"]
    usage = result.get("usage", {})
    return {
        "content": content,
        "tokens_used": usage.get("total_tokens", max_tokens // 2),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }


def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 4000) -> dict:
    """
    调用 LLM API。主 API (models.sjtu.edu.cn) 失败时自动切换到备用 API (api.deepseek.com)。
    返回 {"content": str, "tokens_used": int} 或 {"error": str}。
    """
    max_retries = 3

    # Phase 1: 主 API (双 Key 轮换)
    for attempt in range(max_retries):
        api_key, ki = key_manager.get_key()
        try:
            result = _try_api_call(api_key, BASE_URL, MODEL_NAME,
                                   system_prompt, user_prompt, max_tokens)
            key_manager.record_usage(ki, result["tokens_used"])
            log.info(f"LLM primary OK (key {ki}, {result['tokens_used']} tokens)")
            return result
        except HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            log.warning(f"LLM primary HTTP {e.code} (key {ki}, attempt {attempt+1}): {err_body[:150]}")
            key_manager.record_error(ki)
            if e.code == 429:
                time.sleep(15 * (attempt + 1))
            elif e.code >= 500:
                time.sleep(5 * (attempt + 1))
            else:
                break
        except (URLError, Exception) as e:
            log.warning(f"LLM primary error (key {ki}, attempt {attempt+1}): {type(e).__name__}")
            key_manager.record_error(ki)
            time.sleep(5 * (attempt + 1))

    # Phase 2: 备用 API (DeepSeek 官方)
    log.info(f"Primary API exhausted, switching to backup ({BACKUP_MODEL_NAME})...")
    for attempt in range(2):
        try:
            result = _try_api_call(BACKUP_API_KEY, BACKUP_BASE_URL, BACKUP_MODEL_NAME,
                                   system_prompt, user_prompt, max_tokens)
            log.info(f"LLM backup OK ({BACKUP_MODEL_NAME}, {result['tokens_used']} tokens)")
            return result
        except HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            log.warning(f"LLM backup HTTP {e.code} (attempt {attempt+1}): {err_body[:150]}")
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
            else:
                break
        except (URLError, Exception) as e:
            log.warning(f"LLM backup error (attempt {attempt+1}): {type(e).__name__}")
            time.sleep(5 * (attempt + 1))

    return {"error": "All LLM attempts exhausted (primary + backup)"}



# ── 饮食建议生成 ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是一位专业的注册营养师，严格遵循《中国居民膳食指南（2022）》为咨询者提供饮食建议。

你的任务：
1. 评估用户今日的饮食状况
2. 给出简短具体的明日饮食建议
3. 如果是第2次及以上提交，评价上次建议的落实情况；如果是首次提交，不输出落实情况章节

输出要求：
- 使用中文
- 格式固定（严格遵守下面的模板）
- 建议简短具体，每条1-2句话，直接说吃什么、吃多少、怎么吃
- ⚠️「最需改进」每条严格只描述一个具体动作。禁止用「和」「并」「搭配」连接多个建议。例如✅"午餐加一份菠菜" ❌"早餐换水煮蛋和豆浆" ❌"减半面条并加黄瓜"
- 语气专业但温暖，有鼓励性
- 如果用户有 BMI 数据，结合 BMI 给出针对性建议

请严格按照以下格式输出（不要添加额外章节）：

## 📊 今日饮食评估
[1-2句话概述今日饮食的优点和不足]

## 🎯 明日饮食建议

[一段总结：2-3句话概述明天的整体饮食方向]

**最需改进：**
1. [单一动作，如"早餐把油条换成燕麦粥"，不要写"换成A和B"]
2. [单一动作]
3. [单一动作，可选]

（每条严格只有一个动词一个宾语，禁止"A和B""A并B""A搭配B"等并列结构）

## 📝 上次建议落实情况
[仅当有历史数据和上次建议时输出此章节。逐条对照上次的改进建议，评价完成情况：
✅ 已完成：[具体哪条做到了]
⚠️ 部分做到：[哪条做了一半]
❌ 未做到：[哪条没做]
没有上次建议时完全不要输出此章节。]

## 💡 小贴士
[1条实用小知识]
"""


def generate_dietary_advice(student_id: str, current_diet_desc: str,
                             current_record_date: str,
                             submission_id: str = None) -> dict:
    """
    生成饮食建议。
    返回 {"advice": str, "fulfillment": str} 或 {"error": str}。
    """
    # 获取历史数据
    history_text, prev_advice = get_student_history(student_id, submission_id)
    is_first_submission = not history_text

    # 获取 BMI
    bmi_info = get_student_bmi(student_id)

    # 判断是否有可对比的上次建议
    has_prev_advice = bool(prev_advice)

    # 构建 user prompt
    prompt_parts = [
        f"## 用户信息",
        f"- 学号：{student_id}",
        f"- 记录日期：{current_record_date or '未指定'}",
    ]

    if not has_prev_advice:
        prompt_parts.append(f"- ⚠️ 这是首次生成饮食建议（或之前无可用建议），绝对不要输出「上次建议落实情况」章节")

    if bmi_info:
        prompt_parts.append(f"\n### 身体质量指数（BMI）")
        prompt_parts.append(f"- BMI 值：{bmi_info['bmi']:.1f}")
        prompt_parts.append(f"- 分类：{bmi_info['category']}")
        prompt_parts.append(f"- 解读：{bmi_info.get('interpretation', '未提供')}")

    prompt_parts.append(f"\n## 今日饮食描述")
    prompt_parts.append(current_diet_desc or "（无饮食描述）")

    if history_text:
        prompt_parts.append(f"\n## 历史饮食记录（仅供参考饮食习惯）\n{history_text}")

    if prev_advice:
        prompt_parts.append(f"\n## 上次给出的饮食建议\n{prev_advice}")

    prompt_parts.append(f"\n## 参考：中国居民膳食指南（2022）\n{DIETARY_GUIDELINES}")

    if has_prev_advice:
        prompt_parts.append(f"\n请基于以上信息给出本次饮食建议，并务必逐条对照「上次给出的饮食建议」中的改进条目，评价完成情况（✅已完成 / ⚠️部分做到 / ❌未做到）。")
    else:
        prompt_parts.append(f"\n请基于以上信息，按照指定格式给出本次的饮食建议。")
        prompt_parts.append(f"\n⚠️ 再次强调：没有上次建议可供对比，严禁输出「📝 上次建议落实情况」章节。")

    user_prompt = "\n".join(prompt_parts)

    log.info(f"Generating advice for student={student_id}, "
             f"is_first={is_first_submission}, has_bmi={bmi_info is not None}")

    result = call_llm(SYSTEM_PROMPT, user_prompt, max_tokens=1500)

    if "error" in result:
        log.error(f"LLM failed for {student_id}: {result['error']}")
        return result

    content = result["content"]
    log.info(f"Advice generated for {student_id}: "
             f"{len(content)} chars, {result.get('tokens_used', '?')} tokens")

    # 分割 advice 和 fulfillment
    # 根据输出格式，advice 包含所有内容
    # fulfillment 单独提取 "上次建议落实情况" 部分
    fulfillment = ""
    if "## 📝 上次建议落实情况" in content:
        parts = content.split("## 📝 上次建议落实情况", 1)
        fulfillment_section = parts[1] if len(parts) > 1 else ""
        # 截取到下一个 ## 之前
        if "\n## " in fulfillment_section:
            fulfillment_section = fulfillment_section.split("\n## ", 1)[0]
        fulfillment = "## 📝 上次建议落实情况" + fulfillment_section

    return {
        "advice": content,
        "fulfillment": fulfillment,
        "tokens_used": result.get("tokens_used", 0),
    }


def update_submission_with_advice(db_id: int, advice_data: dict):
    """将 LLM 生成的建议写回数据库。"""
    import sys
    sys.path.insert(0, BASE_DIR)
    from diet_database import get_conn

    conn = get_conn()
    try:
        conn.execute("""
            UPDATE submissions
            SET dietary_advice = ?,
                fulfillment_report = ?
            WHERE id = ?
        """, (
            advice_data.get("advice"),
            advice_data.get("fulfillment"),
            db_id,
        ))
        conn.commit()
        log.info(f"Advice stored for submission db_id={db_id}")
        return True
    except Exception as e:
        log.error(f"Failed to store advice for db_id={db_id}: {e}")
        return False
    finally:
        conn.close()


# ── 测试入口 ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python3 diet_llm.py <student_id> <diet_description>")
        sys.exit(1)

    sid = sys.argv[1]
    desc = sys.argv[2]
    result = generate_dietary_advice(sid, desc, datetime.now().strftime("%Y-%m-%d"))
    if "error" in result:
        print(f"Error: {result['error']}")
    else:
        print(result["advice"])
        print(f"\n---\nTokens: {result.get('tokens_used', '?')}")
