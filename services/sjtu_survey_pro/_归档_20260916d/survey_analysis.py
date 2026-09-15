#!/usr/bin/env python3
"""
Survey Result Analysis Engine
Calculates scores and provides interpretation for all survey scales.
Handles both simple question and matrix question formats.
"""

import json
import math
from datetime import datetime
from population_charts import make_bell_svg, make_debq_2d_svg, compute_percentile

# ── 修正版计分核心 ────────────────────────────────────────────────────────
# 各量表的官方规则算法集中在 survey_scoring.py；本模块保留原有函数名作为
# 兼容层，内部委托给 survey_scoring，避免历史调用方（feedback_server /
# email_feedback / survey_pipeline）需要改动。
import survey_scoring as SC

# 计分规则版本号（写入报告，便于追溯新旧分差异）
SCORING_VERSION = SC.__name__ + ":2.0-official-rules"

# ── Scale scoring definitions ──────────────────────────────────────────────

# DEBQ scoring: each subscale has a number of items, each rated 0-4
# Emotional Eating: 13 items, External Eating: 10 items, Restrained Eating: 10 items
DEBQ_EMOTIONAL = [
    "当你感到烦躁时，是否有进食的欲望",
    "当你无事可做时，是否有进食的欲望",
    "当你感到沮丧或气馁时，是否有进食的欲望",
    "当你感到孤独时，是否有进食的欲望",
    "当有人让你失望时，是否有进食的欲望",
    "当你生气时，是否有进食的欲望",
    "当你预感即将发生不愉快的事情时，是否有进食的欲望",
    "当你感到焦虑、担忧或紧张时，是否有进食的欲望",
    "当事情不顺利或出错时，是否有进食的欲望",
    "当你感到害怕时，是否有进食的欲望",
    "当你感到失望时，是否有进食的欲望",
    "当你情绪低落时，是否有进食的欲望",
    "当你感到无聊或不安时，是否有进食的欲望",
]

DEBQ_EXTERNAL = [
    "如果食物味道很好，你是否会吃得比平时多",
    "如果食物闻起来和看起来都很诱人，你是否会吃得比平时多",
    "如果你看到或闻到美味的食物，是否有进食的欲望",
    "如果你有美味的食物，是否会立刻吃掉",
    "如果你经过面包店，是否有购买美味食物的欲望",
    "如果你经过小吃店或咖啡馆，是否有购买美味食物的欲望",
    "如果你看到别人在吃东西，是否也会有进食的欲望",
    "你能否抵制美味食物的诱惑",
    "当你看到别人吃东西时，是否会吃得比平时多",
    "在准备餐食时，你是否会忍不住先吃点东西",
]

DEBQ_RESTRAINED = [
    "如果你体重增加了，你是否会吃得比平时少",
    "你是否会在用餐时故意吃得比你想吃的量少",
    "你是否会因担心体重而拒绝提供的食物或饮料",
    "你是否会严格控制自己的饮食",
    "你是否会刻意选择有助于减肥的食物",
    "当你吃得过多时，接下来的几天是否会减少食量",
    "你是否会为了不增重而刻意减少饮食",
    "你是否会因关注体重而避免在两餐之间进食",
    "你是否会因关注体重而在晚上尽量避免进食",
    "你在选择食物时是否会考虑体重因素",
]

DEBQ_OPTIONS = {"从不": 0, "很少": 1, "有时": 2, "经常": 3, "总是": 4}

GAD7_OPTIONS = {"没有或极少": 0, "有过几天（≤7天）": 1, "超过一半天数（＞7天）": 2, "几乎每天": 3}

PHQ9_OPTIONS = GAD7_OPTIONS

PSS_OPTIONS = {"从不": 0, "几乎没有": 1, "有时": 2, "经常": 3, "总是": 4}

GSRS_OPTIONS = {
    "无症状": 1,
    "轻度": 2, "轻度症状": 2,
    "中度": 3, "中度症状": 3,
    "重度": 4, "重度症状": 4,
    "极重度": 5, "极重度症状": 5,
}

VSI_OPTIONS = {
    "非常不符": 1,
    "比较不符": 2,
    "有点不符": 3,
    "有点符合": 4,
    "比较相符": 5,
    "非常相符": 6,
}

SCALE_DEFINITIONS = {
    "DEBQ": {
        "name_zh": "荷兰饮食行为问卷（DEBQ）",
        "subscales": {
            "情绪性饮食": {"keywords": DEBQ_EMOTIONAL, "n_items": 13},
            "外部性饮食": {"keywords": DEBQ_EXTERNAL, "n_items": 10},
            "限制性饮食": {"keywords": DEBQ_RESTRAINED, "n_items": 10},
        },
        "options": DEBQ_OPTIONS,
    },
    "GAD-7": {
        "name_zh": "广泛性焦虑障碍量表（GAD-7）",
        "n_items": 7,
        "max_score": 21,
    },
    "PHQ-9": {
        "name_zh": "患者健康问卷抑郁量表（PHQ-9）",
        "n_items": 9,
        "max_score": 27,
    },
    "PSQI": {
        "name_zh": "匹兹堡睡眠质量指数（PSQI）",
        "n_items": 19,
        "max_score": 21,
    },
    "PSS-14": {
        "name_zh": "感知压力量表（PSS-14）",
        "n_items": 14,
        "max_score": 56,
    },
    "GSRS": {
        "name_zh": "胃肠道症状评定量表（GSRS）",
        "n_items": 15,
        "max_score": 105,
    },
    "IPAQ-S": {
        "name_zh": "国际体力活动问卷（IPAQ-S）",
        "n_items": 7,
    },
    "VSI": {
        "name_zh": "内脏敏感指数（VSI）",
        "n_items": 15,
        "max_score": 75,
    },
    "WHOQOL-BREF": {
        "name_zh": "世界卫生组织生活质量简表（WHOQOL-BREF）",
        "n_items": 26,
    },
    "BMI": {
        "name_zh": "身体质量指数（BMI）",
        "n_items": 2,
    },
}


def find_scale_by_title(title):
    """[已弃用·仅供兼容] 依据题干判定量表。

    旧实现依赖关键词，实测漏识别严重（GSRS 2/15、IPAQ 2/7、PSS-14 部分、
    VSI/GAD-7 部分），且会把 PHQ-9 的睡眠/食欲/注意力题误归 PSQI/WHOQOL。
    现统一改用 survey_scoring.classify_item() 的显式条目库；此函数保留为兼容层，
    调用后返回同样的量表键（未识别返回 None）。
    """
    scale, _comp, _rev = SC.classify_item(title)
    if scale in ("_NON_SCORING", "_UNKNOWN"):
        return None
    return scale


def _find_scale_by_title_legacy(title):
    """[旧实现·保留仅供对照] Determine which scale a question belongs to based on its title."""
    t = title.strip().lower()

    # Exact/simple matches
    if "饮食行为" in t or "DEBQ" in t.upper():
        return "DEBQ"
    if "焦虑" in t and ("评估" in t or "GAD" in t.upper()):
        return "GAD-7"
    if "抑郁" in t:
        return "PHQ-9"
    if "睡眠" in t:
        return "PSQI"
    if "压力" in t:
        return "PSS-14"
    if "胃肠道" in t or "GSRS" in t.upper():
        return "GSRS"
    if "体力活动" in t or "IPAQ" in t.upper() or "剧烈" in t:
        return "IPAQ-S"
    if "内脏" in t or "VSI" in t.upper():
        return "VSI"
    if "生存质量" in t or "生活质量" in t or "WHOQOL" in t.upper():
        return "WHOQOL-BREF"
    if "满意度评估" in t or "整体评估" in t:
        return "WHOQOL-BREF"
    if any(kw in t for kw in ["疼痛", "医疗帮助", "充沛的精力", "行动的能力", "外形",
                              "消极感受", "安全", "钱够用", "信息都齐备",
                              "休闲活动", "家庭摩擦", "食欲", "生活有乐趣",
                              "生活有意义", "集中注意力", "健康状况满意"]):
        return "WHOQOL-BREF"

    # Matrix sub-question matching
    if "进食" in t or "饮食" in t or "食欲" in t:
        for kw in DEBQ_EMOTIONAL + DEBQ_EXTERNAL + DEBQ_RESTRAINED:
            if any(k[:8] in t for k in [kw]):
                return "DEBQ"
        if "情绪" in t or "外部" in t or "限制" in t:
            return "DEBQ"
    return None


def _match_debq_subscale(text):
    """Match a DEBQ question text to a subscale."""
    text = text.strip()
    for kw in DEBQ_EMOTIONAL:
        if kw[:8] in text or text[:8] in kw:
            return "情绪性饮食"
    for kw in DEBQ_EXTERNAL:
        if kw[:8] in text or text[:8] in kw:
            return "外部性饮食"
    for kw in DEBQ_RESTRAINED:
        if kw[:8] in text or text[:8] in kw:
            return "限制性饮食"
    return None


def _score_option(ans_text, options_map):
    """Score a single option text against a mapping, handling fuzzy matching."""
    ans_text = ans_text.strip().rstrip()
    for opt_text, score in options_map.items():
        if ans_text.startswith(opt_text.rstrip()) or opt_text.rstrip().startswith(ans_text):
            return score
        # Fuzzy match — try contains
        if len(opt_text) >= 4 and opt_text[:4] in ans_text:
            return score
        if len(ans_text) >= 4 and ans_text[:4] in opt_text:
            return score
    return None


def analyze_survey_responses(responses):
    """
    Analyze survey responses and produce a report.

    responses: dict with {'data': {'rows': [...]}} structure from wj.sjtu.edu.cn API
    """
    # Handle both direct {data: {rows: ...}} and {rows: ...}
    if isinstance(responses, dict):
        if "data" in responses:
            rows = responses["data"].get("rows", [])
        else:
            rows = responses.get("rows", [])
    elif isinstance(responses, list):
        rows = responses
    else:
        return {"error": "Invalid response format"}

    if not rows:
        return {"error": "No rows found in data"}

    # Use the first row (the user's own submission, typically)
    row = rows[0]
    answers = row.get("answers", [])

    report = {
        "timestamp": datetime.now().isoformat(),
        "scales": {},
        "scores": {},
        "basic_info": {},
    }

    # ── First pass: extract basic info ──────────────────────────────────
    BASIC_FIELDS = ["姓名", "学工号", "性别", "年级", "学院/单位", "邮箱",
                    "身高", "体重"]
    for item in answers:
        q = item.get("question", {})
        title = q.get("title", "")
        ans = item.get("answer", "")

        # Check if any basic field matches
        for field in BASIC_FIELDS:
            if field in title:
                if isinstance(ans, dict):
                    report["basic_info"][title] = ans.get("label", str(ans))
                else:
                    report["basic_info"][title] = str(ans)
                break

    # ── Second pass: organize by scale ──────────────────────────────────
    for item in answers:
        q = item.get("question", {})
        title = q.get("title", "")
        qtype = q.get("question_type", "")
        ans = item.get("answer", "")

        # 量表归属：使用修正版条目库（显式声明，不再依赖关键词猜测）
        scale_name, _component, _is_rev = SC.classify_item(title)
        if scale_name in ("_NON_SCORING", "_UNKNOWN"):
            scale_name = None

        if not scale_name and qtype == "矩阵单选题":
            # Try to identify from answer_name sub-questions
            ans_name = q.get("answer_name", [])
            if isinstance(ans_name, list) and len(ans_name) > 0:
                first_key = ""
                if isinstance(ans_name[0], dict):
                    first_key = ans_name[0].get("key", "")
                else:
                    first_key = str(ans_name[0])
                cand, _, _ = SC.classify_item(first_key)
                if cand not in ("_NON_SCORING", "_UNKNOWN"):
                    scale_name = cand

        if scale_name:
            if scale_name not in report["scales"]:
                report["scales"][scale_name] = []

            # For matrix questions, expand each sub-question
            if qtype == "矩阵单选题":
                ans_name = q.get("answer_name", [])
                if isinstance(ans, list):
                    for i, sub_ans in enumerate(ans):
                        sub_text = ""
                        if isinstance(sub_ans, dict):
                            sub_text = sub_ans.get("title", "")
                        else:
                            sub_text = str(sub_ans)

                        q_text = ""
                        if i < len(ans_name):
                            if isinstance(ans_name[i], dict):
                                q_text = ans_name[i].get("key", "")
                            else:
                                q_text = str(ans_name[i])

                        report["scales"][scale_name].append({
                            "question": q_text or title,
                            "answer_text": sub_text,
                            "type": "matrix_item",
                        })
                else:
                    report["scales"][scale_name].append({
                        "question": title,
                        "answer_text": str(ans),
                        "type": qtype,
                    })
            else:
                ans_text = ""
                if isinstance(ans, dict):
                    ans_text = ans.get("label", str(ans))
                else:
                    ans_text = str(ans)

                report["scales"][scale_name].append({
                    "question": title,
                    "answer_text": ans_text,
                    "type": qtype,
                })

    # ── Third pass: calculate scores ────────────────────────────────────
    # 统一交由修正版计分核心（survey_scoring.py）按官方规则计分。
    for scale_name, items in report["scales"].items():
        scorer = SC.SCORERS.get(scale_name)
        if scorer is None:
            continue
        res = scorer(items)
        # 兼容旧渲染层：把领域分/成分分映射到 subscales
        if scale_name == "WHOQOL-BREF" and res.get("domain_scores"):
            res["subscales"] = {
                name: {"mean": v.get("score_0_100"), "level": f"{v.get('score_0_100')}/100",
                       "n_items": v.get("n_items")}
                for name, v in res["domain_scores"].items()
            }
        elif scale_name == "PSQI" and res.get("components"):
            res["subscales"] = {
                k: {"mean": v, "level": f"{v}/3"}
                for k, v in res["components"].items()
                if v is not None and not k.endswith(("明细", "数据来源"))
            }
        elif scale_name == "GSRS" and res.get("subscales"):
            res["subscales"] = {
                k: {"mean": v.get("mean"), "level": f"{v.get('mean')}",
                    "n_items": v.get("n_items")}
                for k, v in res["subscales"].items()
            }
        report["scores"][scale_name] = res

    # ── BMI calculation (independent of scale classification) ────────
    bmi_result = _calculate_bmi(report["basic_info"])
    if bmi_result:
        report["scores"]["BMI"] = bmi_result
        report["scales"]["BMI"] = [
            {"question": "身高（cm）", "answer_text": str(report["basic_info"].get("身高（cm）", "")), "type": "text"},
            {"question": "体重（kg）", "answer_text": str(report["basic_info"].get("体重（kg）", "")), "type": "text"},
        ]

    report["scoring_version"] = SCORING_VERSION

    # ── Generate suggestions ─────────────────────────────────────────
    report["suggestions"] = generate_suggestions(report["scores"], report["basic_info"])

    if not report["scores"]:
        report["warning"] = "No scales were detected from the survey data. Check scale keywords."

    return report


def _score_debq(items):
    """[已修正] 委托至 survey_scoring.score_debq（官方 1–5 计分 + 外部性饮食反向条目）。"""
    return SC.score_debq(items)


def _score_debq_legacy(items):
    """旧实现（保留仅供对照，不再被调用）：0–4 计分、直接求和、无反向条目。"""
    result = {"total": None, "subscales": {}, "interpretation": {}}

    # Group matrix items by subscale
    subscale_groups = {"情绪性饮食": [], "外部性饮食": [], "限制性饮食": []}

    for item in items:
        q_text = item.get("question", "")
        ans_text = item.get("answer_text", "")

        sub = _match_debq_subscale(q_text)
        if sub and sub in subscale_groups:
            score = _score_option(ans_text, DEBQ_OPTIONS)
            if score is not None:
                subscale_groups[sub].append(score)

    total = 0
    for sub, scores in subscale_groups.items():
        if scores:
            s = sum(scores)
            n_items = len(scores)
            mean = s / n_items
            if mean <= 1.0:
                level = "低水平"
            elif mean <= 2.0:
                level = "中等水平"
            elif mean <= 3.0:
                level = "较高水平"
            else:
                level = "高水平"
            result["subscales"][sub] = {
                "raw": s,
                "mean": round(mean, 2),
                "level": level,
                "n_items": n_items,
            }
            # For DEBQ, higher scores on emotional/external = more problematic
            total += s

    result["total"] = total

    # Generate interpretation string
    parts = []
    for sub, val in result["subscales"].items():
        badge = {"低水平": "🟢", "中等水平": "🟡", "较高水平": "🟠", "高水平": "🔴"}.get(
            val["level"], "⚪")
        parts.append(f"{badge} {sub}: {val['level']} (均分={val['mean']})")

    result["interpretation"] = "\n".join(parts)
    return result


def _score_scale(items, options_map, max_score, ranges):
    """[已弃用·仅供兼容] 朴素加总。

    缺陷：不支持反向计分、不校验缺失条目、上界可写错导致分档不可达。
    现在所有量表均改用 survey_scoring 中按官方规则的算法；保留此函数只为
    避免历史外部调用报错。
    """
    result = {"total": 0, "subscales": {}, "interpretation": None, "n_items": 0}

    total = 0
    n_scored = 0
    for item in items:
        ans_text = item.get("answer_text", "")
        score = _score_option(ans_text, options_map)
        if score is not None:
            total += score
            n_scored += 1

    result["total"] = total
    result["n_items"] = n_scored

    for lo, hi, label in ranges:
        if lo <= total <= hi:
            result["interpretation"] = f"总分: {total}/{max_score} — {label}"
            break

    if not result["interpretation"]:
        result["interpretation"] = f"总分: {total}/{max_score}"

    return result


def _score_psqi(items):
    """[已修正] 委托至 survey_scoring.score_psqi（官方 7 成分算法）。"""
    return SC.score_psqi(items)


def _score_psqi_legacy(items):
    """
    [旧实现·保留仅供对照] Simplified PSQI scoring. Full PSQI has 7 component scores.
    This provides a basic approximation by scoring multiple question types.
    """
    result = {"total": 0, "subscales": {}, "interpretation": None, "n_items": 0}

    psqi_maps = [
        # Sleep quality
        {"非常好": 0, "较好": 1, "较差": 2, "很差": 3},
        # Sleep latency (minutes)
        {"≤15 分钟": 0, "16～30 分钟": 1, "31～60 分钟": 2, "> 60 分钟": 3,
         "少于 5 小时": 2, "5～6 小时": 1, "7～8 小时": 0, "> 8 小时": 0},
        # Sleep duration
        {"少于 5 小时": 3, "5～6 小时": 2, "7～8 小时": 1, "> 8 小时": 0},
        # Sleep efficiency
        {"> 85%": 0, "75～84%": 1, "65～74%": 2, "< 65%": 3},
        # Sleep disturbances (frequency)
        {"没有": 0, "每周 1-2 次": 1, "每周 3 次或更多": 2},
        # Bedtime
        {"晚上 9:00前": 0, "晚上 9:00—10:00": 0, "晚上 10:00—11:00": 0,
         "晚上 11:00—12:00": 1, "凌晨 0:00—1:00": 2, "凌晨 1:00后": 3},
        # Wake time
        {"早上 5:00前": 0, "早上 5:00-6:00": 0, "早上 6:00-7:00": 0,
         "早上 7:00-8:00": 1, "早上 8:00-9:00": 2, "早上 9:00后": 3},
    ]

    total = 0
    n_scored = 0
    for item in items:
        ans_text = item.get("answer_text", "")
        score = None
        for pmap in psqi_maps:
            score = _score_option(ans_text, pmap)
            if score is not None:
                break
        if score is not None:
            total += score
            n_scored += 1

    result["total"] = total
    result["n_items"] = n_scored
    if total > 7:
        result["interpretation"] = f"PSQI总分: {total}/21 — ⚠️ 睡眠质量差 (>7分提示睡眠障碍)"
    else:
        result["interpretation"] = f"PSQI总分: {total}/21 — ✅ 睡眠质量尚可"

    return result


def _score_ipaq(items):
    """[已修正] 委托至 survey_scoring.score_ipaq（官方 MET 系数与截断规则）。"""
    return SC.score_ipaq(items)


def _score_ipaq_legacy(items):
    """
    [旧实现·保留仅供对照] IPAQ-S: calculate MET-min/week.
    Walking = 3.3 METs, Moderate = 4.0 METs, Vigorous = 8.0 METs.
    """
    result = {"total": 0, "subscales": {}, "interpretation": None, "n_items": 0}

    vigorous_days = 0
    vigorous_min = 0
    moderate_days = 0
    moderate_min = 0
    walking_days = 0
    walking_min = 0

    for item in items:
        q_text = item.get("question", "")
        ans_text = item.get("answer_text", "")

        # Try to parse numbers
        try:
            val = int(ans_text) if ans_text.isdigit() else 0
        except:
            val = 0

        if "剧烈" in q_text or "vigorous" in q_text.lower():
            if "几天" in q_text:
                vigorous_days = val
            elif "分钟" in q_text:
                vigorous_min = val
        elif "适度" in q_text or "moderate" in q_text.lower():
            if "几天" in q_text:
                moderate_days = val
            elif "分钟" in q_text:
                moderate_min = val
        elif "步行" in q_text or "walk" in q_text.lower():
            if "几天" in q_text:
                walking_days = val
            elif "分钟" in q_text:
                walking_min = val
        elif "坐着" in q_text or "坐" in q_text:
            result["subscales"]["静坐时间"] = val

    met_min_week = (vigorous_days * vigorous_min * 8.0 +
                    moderate_days * moderate_min * 4.0 +
                    walking_days * walking_min * 3.3)
    met_min_week = round(met_min_week)

    result["total"] = met_min_week

    if met_min_week >= 3000:
        result["interpretation"] = f"MET-min/周: {met_min_week} — 💪 高体力活动水平"
    elif met_min_week >= 600:
        result["interpretation"] = f"MET-min/周: {met_min_week} — 🚶 中等体力活动水平"
    else:
        result["interpretation"] = f"MET-min/周: {met_min_week} — 🛋️ 低体力活动水平"

    return result


def _score_whoqol(items):
    """[已修正] 委托至 survey_scoring.score_whoqol（四领域分 + 反向条目）。"""
    return SC.score_whoqol(items)


def _score_whoqol_legacy(items):
    """
    [旧实现·保留仅供对照] Simplified WHOQOL-BREF scoring.
    """
    result = {"total": 0, "subscales": {}, "interpretation": {}, "n_items": 0}

    # Map WHOQOL-BREF items to domains
    # Domain 1: Physical (生理领域)
    # Domain 2: Psychological (心理领域)
    # Domain 3: Social (社会关系领域)
    # Domain 4: Environment (环境领域)
    # Plus 2 general items

    # Simple scoring for non-matrix items
    satisfaction_map = {
        "很差": 1, "差": 2, "一般": 3, "好": 4, "很好": 5,
        "不满意": 1, "较不满意": 2, "一般": 3, "满意": 4, "很满意": 5,
        "极少": 1, "很少": 2, "一般": 3, "多数": 4, "总是": 5,
        "极差": 1, "很差": 2, "不好也不差": 3, "较好": 4, "极好": 5,
    }

    negative_map = {
        "极多": 1, "很多": 2, "一般": 3, "很少": 4, "极少": 5,
        "非常有": 1, "较有": 2, "一般": 3, "很少有": 4, "极无": 5,
    }

    total = 0
    n_scored = 0

    # Score all items (including matrix items for WHOQOL-BREF)
    for item in items:
        q_text = item.get("question", "")
        ans_text = item.get("answer_text", "")

        # Try numeric scoring (for the total score question)
        score = None
        try:
            num = int(ans_text)
            if 0 <= num <= 100:
                score = num  # Direct numeric score
        except:
            pass

        if score is None:
            score = _score_option(ans_text, satisfaction_map)
        if score is None:
            score = _score_option(ans_text, negative_map)

        if score is not None:
            total += score
            n_scored += 1

    result["total"] = total
    result["n_items"] = n_scored

    # Determine general quality level
    if total >= 80:
        result["interpretation"] = f"综合评分: {total}/100 — 🟢 生活质量较好"
    elif total >= 60:
        result["interpretation"] = f"综合评分: {total}/100 — 🟡 生活质量中等偏上"
    elif total >= 40:
        result["interpretation"] = f"综合评分: {total}/100 — 🟠 生活质量中等"
    else:
        result["interpretation"] = f"综合评分: {total}/100 — 🔴 生活质量较差"

    return result


def _calculate_bmi(basic_info: dict) -> dict:
    """[已修正] 委托至 survey_scoring.compute_bmi（中国标准 WS/T 428-2013）。"""
    if not basic_info:
        return None
    return SC.compute_bmi(
        basic_info.get("身高（cm）", basic_info.get("身高", basic_info.get("height"))),
        basic_info.get("体重（kg）", basic_info.get("体重", basic_info.get("weight"))),
    )


def _calculate_bmi_legacy(basic_info: dict) -> dict:
    """
    [旧实现·保留仅供对照] Calculate BMI from height (cm) and weight (kg).
    """
    height_str = basic_info.get("身高（cm）", basic_info.get("身高", basic_info.get("height", "")))
    weight_str = basic_info.get("体重（kg）", basic_info.get("体重", basic_info.get("weight", "")))

    try:
        height_cm = float(height_str)
        weight_kg = float(weight_str)
    except (ValueError, TypeError):
        return None

    if height_cm <= 0 or weight_kg <= 0:
        return None

    height_m = height_cm / 100.0
    bmi = round(weight_kg / (height_m * height_m), 1)

    # Chinese BMI classification (WS/T 428-2013)
    if bmi < 18.5:
        category = "偏瘦"
        emoji = "🟡"
        detail = "(<18.5)"
    elif bmi < 24.0:
        category = "正常"
        emoji = "🟢"
        detail = "(18.5-23.9)"
    elif bmi < 28.0:
        category = "超重"
        emoji = "🟠"
        detail = "(24.0-27.9)"
    else:
        category = "肥胖"
        emoji = "🔴"
        detail = "(≥28.0)"

    interpretation = f"BMI: {bmi} — {emoji} 体重{category} {detail}"

    return {
        "total": bmi,
        "interpretation": interpretation,
        "category": category,
        "n_items": 2,
    }


def render_html_report(report, answer_id=None):
    """
    Render analysis report as an HTML page.
    answer_id: optional submission answer ID for individual page.
    """
    styles = """
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Helvetica Neue', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 800px; margin: 0 auto; }
        .header {
            background: white; border-radius: 20px; padding: 40px;
            margin-bottom: 24px; text-align: center;
            box-shadow: 0 10px 40px rgba(0,0,0,0.15);
        }
        .header h1 { font-size: 28px; color: #333; margin-bottom: 8px; }
        .header .subtitle { color: #666; font-size: 14px; }
        .header .sjtulogo { font-size: 12px; color: #999; margin-top: 16px; }
        .card {
            background: white; border-radius: 16px; padding: 28px;
            margin-bottom: 20px; box-shadow: 0 4px 20px rgba(0,0,0,0.08);
        }
        .card h2 {
            font-size: 18px; color: #333; margin-bottom: 16px;
            padding-bottom: 12px; border-bottom: 2px solid #f0f0f0;
        }
        .info-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 12px;
        }
        .info-item {
            text-align: center; padding: 12px;
            background: #f8f9ff; border-radius: 12px;
        }
        .info-item .label { font-size: 12px; color: #888; margin-bottom: 4px; }
        .info-item .value { font-size: 16px; font-weight: 500; color: #333; }
        .scale-card {
            overflow: hidden;
            background: white; border-radius: 16px; padding: 24px;
            margin-bottom: 16px; box-shadow: 0 4px 20px rgba(0,0,0,0.08);
            border-left: 4px solid #667eea;
        }
        .scale-card .scale-name {
            font-size: 16px; font-weight: 500; color: #333; margin-bottom: 8px;
        }
        .scale-card .scale-result {
            font-size: 14px; color: #555; line-height: 1.6;
        }
        .score-badge {
            display: inline-block; padding: 4px 12px;
            border-radius: 20px; font-size: 13px; font-weight: 500;
        }
        .badge-good { background: #d4edda; color: #155724; }
        .badge-warn { background: #fff3cd; color: #856404; }
        .badge-bad  { background: #f8d7da; color: #721c24; }
        .badge-info { background: #d1ecf1; color: #0c5460; }
        .footer {
            text-align: center; color: rgba(255,255,255,0.7);
            font-size: 12px; padding: 20px;
        }
        .progress-container { margin: 12px 0; }
        .progress-bar {
            height: 8px; background: #e9ecef;
            border-radius: 4px; overflow: hidden;
        }
        .progress-fill {
            height: 100%; border-radius: 4px; transition: width 1s ease;
        }
        .fill-good { background: linear-gradient(90deg, #28a745, #20c997); }
        .fill-warn { background: linear-gradient(90deg, #ffc107, #fd7e14); }
        .fill-bad  { background: linear-gradient(90deg, #dc3545, #e74c3c); }
        .print-btn {
            display: inline-block; padding: 10px 24px;
            background: #667eea; color: white; border: none;
            border-radius: 8px; font-size: 14px; cursor: pointer;
            margin-top: 16px; text-decoration: none;
        }
        .print-btn:hover { background: #5a67d8; }
        .highlight-box { padding: 12px 16px; border-radius: 12px; margin: 8px 0; font-size: 14px; }
        @media (max-width: 600px) {
            body { padding: 10px; }
            .header { padding: 24px 16px; }
            .card { padding: 20px 16px; }
            .scale-card { padding: 16px; }
        }
        .scale-card svg { max-width: 100%; height: auto; }
        .scales-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 14px;
        }
        @media (max-width: 750px) {
            .scales-grid { grid-template-columns: 1fr; }
        }
    </style>
    """

    # Basic info
    basic = report.get("basic_info", {})

    if not basic:
        info_rows = '<div class="info-item"><div class="value">用户已填写</div></div>'
    else:
        info_rows = "".join(
            f'<div class="info-item"><div class="label">{k}</div><div class="value">{v}</div></div>'
            for k, v in basic.items()
            if "邮箱" not in k and "email" not in k.lower()
        )

    # BMI in basic info
    bmi_d = report.get("scores", {}).get("BMI", {})
    if bmi_d and bmi_d.get("total") is not None:
        bv = round(bmi_d["total"], 1)
        bc = bmi_d.get("category", "")
        bi = bmi_d.get("interpretation", "")
        info_rows += ('<div class="info-item" style="grid-column:1/-1;background:linear-gradient(135deg,#f0f4ff,#e8f0fe);">'
                      '<div class="label">BMI 体质量指数</div>'
                      f'<div class="value" style="font-size:20px;font-weight:700;">{bv} <span style="font-size:13px;color:#888;">{bc}</span></div>'
                      f'<div style="font-size:11px;color:#888;margin-top:2px;">{bi}</div></div>')

    # Scale scores
    scale_cards = ""
    ipaq_card = ""
    highlights = []

    # Ordered list for display
    display_order = ["GAD-7", "PHQ-9", "PSQI", "PSS-14", "GSRS",
                     "VSI", "DEBQ", "WHOQOL-BREF"]

    MAX_SCORES = {
        "GAD-7": 21, "PHQ-9": 27, "PSQI": 21,
        "PSS-14": 56,
        # 以下两个量程在原实现中写错（GSRS 写 105、VSI 写 75）；
        # 已按官方规则修正：GSRS 15 题×1–5 = 75；VSI 15 题×1–6 = 90。
        "GSRS": 75, "VSI": 90,
    }

    for scale_key in display_order:
        score_data = report.get("scores", {}).get(scale_key)
        if not score_data:
            continue

        defn = SCALE_DEFINITIONS.get(scale_key, {})
        name_zh = defn.get("name_zh", scale_key)
        interpretation = score_data.get("interpretation")
        total = score_data.get("total")

        badge_class = "badge-info"

        # Color badge based on interpretation text (works for all scales, including BMI)
        interp_str = str(interpretation) if interpretation else ""
        if total is not None:
            if any(kw in interp_str for kw in ["💪", "✅", "🟢", "无明显", "低", "尚可", "较好", "正常"]):
                badge_class = "badge-good"
            elif any(kw in interp_str for kw in ["🟡", "中等", "⚠", "偏瘦"]):
                badge_class = "badge-warn"
            elif any(kw in interp_str for kw in ["🔴", "重度", "高压力", "差", "🟠", "超重", "肥胖"]):
                badge_class = "badge-bad"
    
        # Format interpretation
        interp_html = ""
        if isinstance(interpretation, str):
            # Replace emoji separators with badge spans
            display_text = interpretation
            interp_html = f'<div class="scale-result"><span class="score-badge {badge_class}">{display_text}</span></div>'

        # Subscale breakdown (DEBQ)
        sub_html = ""
        for sub_name, sub_val in score_data.get("subscales", {}).items():
            if isinstance(sub_val, dict):
                sub_badge = {"低水平": "badge-good", "中等水平": "badge-warn",
                             "较高水平": "badge-warn", "高水平": "badge-bad"}.get(
                    sub_val.get("level", ""), "badge-info")
                mean_or_score = sub_val.get("mean", sub_val.get("score", ""))
                sub_html += f'<div style="margin:4px 0;font-size:13px;color:#666;padding-left:12px;">└ {sub_name}: <span class="score-badge {sub_badge}">{sub_val.get("level", "")}</span> (均分: {mean_or_score})</div>'

        # SVG population distribution chart (replaces progress bar)
        chart_html = ""
        if total is not None and scale_key != "BMI":
            chart_html = make_bell_svg(total, name_zh, scale_key)


        # For DEBQ: 2D distribution chart (解除抑制 vs 抑制)
        debq_charts = ""
        if scale_key == "DEBQ":
            subs = score_data.get("subscales", {})
            em = subs.get("情绪性饮食", {})
            ex = subs.get("外部性饮食", {})
            re = subs.get("限制性饮食", {})
            if isinstance(em, dict) and isinstance(ex, dict) and isinstance(re, dict):
                em_mean = em.get("mean")
                ex_mean = ex.get("mean")
                re_mean = re.get("mean")
                if em_mean is not None and ex_mean is not None and re_mean is not None:
                    debq_charts = make_debq_2d_svg(em_mean, ex_mean, re_mean)

        scale_cards += f'''
        <div class="scale-card">
            <div class="scale-name">{name_zh}</div>
            {chart_html}
            {debq_charts}
            {interp_html}
            {sub_html}
        </div>
        '''

    # IPAQ-S solo card
    ipaq_data = report.get("scores", {}).get("IPAQ-S")
    if ipaq_data:
        ipaq_total = ipaq_data.get("total")
        ipaq_interp = str(ipaq_data.get("interpretation", ""))
        ipaq_subs_html = ""
        for sn, sv in ipaq_data.get("subscales", {}).items():
            ipaq_subs_html += f'<div style="margin:4px 0;font-size:13px;color:#666;">{sn}: {sv}</div>'
        ipaq_badge = "badge-info"
        if "高" in ipaq_interp: ipaq_badge = "badge-good"
        elif "低" in ipaq_interp: ipaq_badge = "badge-warn"
        ipaq_chart = make_bell_svg(ipaq_total, "IPAQ-S", "IPAQ-S") if ipaq_total else ""
        ipaq_card = ('<div class="scale-card" style="margin-top:20px;">'
                     f'<div class="scale-name">国际体力活动问卷 (IPAQ-S)</div>'
                     f'{ipaq_chart}'
                     f'<div class="scale-result"><span class="score-badge {ipaq_badge}">{ipaq_interp}</span></div>'
                     f'{ipaq_subs_html}</div>')

    # Highlight box
    highlights_html = ""
    if highlights:
        items_html = "".join(
            f'<div class="highlight-box" style="background:#fff8e1;border:1px solid #ffe082;margin:4px 0;">{h}</div>'
            for h in highlights[:5]
        )
        highlights_html = f'''
        <div class="card" style="border-left: 4px solid #ffc107;">
            <h2>🔔 重点关注</h2>
            {items_html}
        </div>
        '''

    timestamp = report.get("timestamp", "")
    try:
        ts = datetime.fromisoformat(timestamp)
        timestamp_str = ts.strftime("%Y年%m月%d日 %H:%M")
    except:
        timestamp_str = str(timestamp)

    # ── Suggestions ────────────────────────────────────────────────────
    suggestions = report.get("suggestions", [])
    suggestions_html = ""
    if suggestions:
        items = "".join(
            f'''<div class="suggestion-item">
                <div class="suggestion-header">
                    <span class="score-badge {('badge-bad' if s["severity"]=="高" else "badge-warn")}">{s["scale"]} (severity={s["severity"]})</span>
                </div>
                <div class="suggestion-text">{s["suggestion"]}</div>
            </div>'''
            for s in suggestions
        )
        suggestions_html = f'''
        <div class="card" style="border-left: 4px solid #28a745;">
            <h2>💡 个性化建议</h2>
            <div class="suggestions-container">{items}</div>
        </div>
        '''
        # Add suggestion styles
        styles = styles.replace("</style>", '''
        .suggestion-item { padding: 12px 0; border-bottom: 1px solid #f0f0f0; }
        .suggestion-item:last-child { border-bottom: none; }
        .suggestion-header { margin-bottom: 6px; }
        .suggestion-text { font-size: 14px; color: #555; line-height: 1.8; padding-left: 4px; }
    </style>''')

    # Answer ID tag
    answer_tag = f'<div class="sjtulogo" style="margin-top:4px;font-size:11px;">提交编号: {answer_id}</div>' if answer_id else ""

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>问卷结果分析报告</title>
    {styles}
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📋 问卷结果分析报告</h1>
            <div class="subtitle">上海交通大学健康评估问卷</div>
            <div class="sjtulogo">报告生成时间: {timestamp_str}</div>
            {answer_tag}
            <a href="#" class="print-btn" onclick="window.print()">🖨️ 打印 / 保存 PDF</a>
        </div>

        <div class="card">
            <h2>👤 基本信息</h2>
            <div class="info-grid">{info_rows}</div>
        </div>

        {highlights_html}

        <div class="card">
            <h2>📊 量表评估结果</h2>
            <div class="scales-grid">
            {scale_cards if scale_cards else '<div style="color:#888;text-align:center;padding:20px;grid-column:1/-1;">暂无量表数据</div>'}
            </div>
        </div>

        {ipaq_card}
        {suggestions_html}

        <div class="card">
            <h2>📝 免责声明</h2>
            <div style="font-size:13px;color:#888;line-height:1.8;">
                本报告基于问卷填写结果自动生成，仅供参考，不构成任何医疗建议。
            </div>
        </div>
        <div class="footer">
            
        </div>
    </div>
</body>
</html>'''
    return html


# ── Suggestions Generation ───────────────────────────────────────────────

def generate_suggestions(scores: dict, basic_info: dict) -> list:
    """
    Generate personalized health suggestions based on scale scores.
    Returns a list of dicts: {scale, severity, suggestion}
    """
    suggestions = []

    scale_names = {
        "GAD-7": "焦虑", "PHQ-9": "抑郁", "PSQI": "睡眠",
        "PSS-14": "压力", "GSRS": "胃肠道", "VSI": "内脏敏感",
        "DEBQ": "饮食行为", "IPAQ-S": "体力活动", "WHOQOL-BREF": "生活质量",
    }

    # GAD-7
    gad = scores.get("GAD-7", {})
    if gad.get("total") is not None:
        t = gad["total"]
        if t >= 15:
            suggestions.append({"scale": "GAD-7", "severity": "高",
                "suggestion": "焦虑水平较高。建议学习腹式呼吸或正念冥想等放松技巧，必要时可到校心理咨询中心（54747174）寻求专业帮助。"})
        elif t >= 10:
            suggestions.append({"scale": "GAD-7", "severity": "中",
                "suggestion": "存在一定焦虑情绪。建议规律作息，适当运动，尝试每日10分钟冥想练习。"})
        elif t >= 5:
            suggestions.append({"scale": "GAD-7", "severity": "轻",
                "suggestion": "偶有焦虑感属正常现象。保持社交活动、合理规划学习时间有助于缓解。"})

    # PHQ-9
    phq = scores.get("PHQ-9", {})
    if phq.get("total") is not None:
        t = phq["total"]
        if t >= 15:
            suggestions.append({"scale": "PHQ-9", "severity": "高",
                "suggestion": "抑郁情绪较明显。请务必与辅导员或心理咨询师沟通，不要独自承受。学校心理中心可提供免费咨询。"})
        elif t >= 10:
            suggestions.append({"scale": "PHQ-9", "severity": "中",
                "suggestion": "情绪偏低落。建议增加户外活动时间，保持与朋友家人的联系，尝试记录每日积极小事。"})

    # PSQI
    psqi = scores.get("PSQI", {})
    if psqi.get("total") is not None and psqi["total"] > 7:
        suggestions.append({"scale": "PSQI", "severity": "高",
            "suggestion": "睡眠质量需要改善。建议：①固定就寝和起床时间 ②睡前1小时避免电子屏幕 ③卧室保持黑暗安静 ④午睡不超过30分钟 ⑤避免睡前摄入咖啡因。"})

    # PSS-14
    pss = scores.get("PSS-14", {})
    if pss.get("total") is not None:
        t = pss["total"]
        if t >= 27:
            suggestions.append({"scale": "PSS-14", "severity": "高",
                "suggestion": "压力水平较高。建议学习时间管理技巧，合理分配学习与休息时间，必要时寻求心理咨询。"})
        elif t >= 14:
            suggestions.append({"scale": "PSS-14", "severity": "中",
                "suggestion": "有一定压力。建议每天安排30分钟运动（跑步、游泳、球类等），运动是最佳减压方式之一。"})

    # GSRS
    gsrs = scores.get("GSRS", {})
    if gsrs.get("total") is not None:
        t = gsrs["total"]
        if t >= 41:
            suggestions.append({"scale": "GSRS", "severity": "高",
                "suggestion": "胃肠道症状较明显。建议：①少食多餐 ②避免辛辣油腻食物 ③记录饮食日记找出触发食物 ④如持续不适请到校医院消化科就诊。"})
        elif t >= 26:
            suggestions.append({"scale": "GSRS", "severity": "轻",
                "suggestion": "偶有胃肠道不适。注意饮食规律，避免暴饮暴食，减少咖啡和碳酸饮料摄入。"})

    # VSI
    vsi = scores.get("VSI", {})
    if vsi.get("total") is not None:
        t = vsi["total"]
        if t >= 51:
            suggestions.append({"scale": "VSI", "severity": "高",
                "suggestion": "对胃肠不适较为敏感。过度关注可能加重不适感，建议练习放松训练，减少对身体的过度关注。"})

    # DEBQ
    debq = scores.get("DEBQ", {})
    if debq.get("subscales"):
        emo = debq["subscales"].get("情绪性饮食", {})
        if emo.get("mean", 0) >= 2.0:
            suggestions.append({"scale": "DEBQ", "severity": "中",
                "suggestion": "情绪性饮食倾向较高，心情波动时容易通过进食缓解。建议寻找替代性情绪调节方式，如听音乐、写日记、运动等。"})
        ext = debq["subscales"].get("外部性饮食", {})
        if ext.get("mean", 0) >= 2.0:
            suggestions.append({"scale": "DEBQ", "severity": "中",
                "suggestion": "容易受美食诱惑。建议：①购物前列清单 ②不囤积零食 ③用水果替代高热量零食。"})

    # IPAQ-S
    ipaq = scores.get("IPAQ-S", {})
    if ipaq.get("total") is not None:
        t = ipaq["total"]
        if t < 600:
            suggestions.append({"scale": "IPAQ-S", "severity": "低",
                "suggestion": "体力活动偏少。建议每周至少进行150分钟中等强度运动（快走、骑车），可利用校内体育设施如南区体育馆、光明体育场。"})

    # WHOQOL-BREF
    whoqol = scores.get("WHOQOL-BREF", {})
    if whoqol.get("total") is not None:
        t = whoqol["total"]
        if t < 40:
            suggestions.append({"scale": "WHOQOL-BREF", "severity": "低",
                "suggestion": "生活质量自评较低。建议从改善睡眠、增加运动和社交活动入手，逐步提升生活满意度。"})

    # BMI
    bmi = scores.get("BMI", {})
    if bmi.get("total") is not None:
        cat = bmi.get("category", "")
        t = bmi["total"]
        if cat == "偏瘦":
            suggestions.append({"scale": "BMI", "severity": "中",
                "suggestion": f"BMI={t}，体重偏瘦。建议适当增加营养摄入，可咨询校医院营养科制定健康的增重计划。注意均衡饮食，增加优质蛋白质和碳水化合物摄入。"})
        elif cat == "超重":
            suggestions.append({"scale": "BMI", "severity": "中",
                "suggestion": f"BMI={t}，超重。建议：①控制饮食热量，减少高糖高脂食物 ②增加有氧运动（每周至少150分钟） ③规律作息，避免熬夜导致的代谢紊乱。"})
        elif cat == "肥胖":
            suggestions.append({"scale": "BMI", "severity": "高",
                "suggestion": f"BMI={t}，肥胖。建议：①到校医院或营养科做全面评估 ②制定科学减重计划，每周减重0.5-1kg为宜 ③增加体育锻炼，改善饮食结构 ④必要时咨询专业医师。"})

    return suggestions
