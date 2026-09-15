#!/usr/bin/env python3
"""
问卷计分核心（修正版） — survey_scoring.py
============================================================
本模块依据各量表的**官方计分规则**实现计分，替代原 `survey_analysis.py` 中
的近似算法。所有量表算法均为纯函数（输入条目列表，输出分值与解释），便于单测。

设计要点
--------
1. **条目库（ITEM_BANK）**：以本课题问卷的**实际题干文本**为键，显式声明每条
   题目属于哪个量表、哪个成分/维度、是否反向计分。
   原实现的 `find_scale_by_title()` 依赖关键词，导致大量条目静默漏计
   （实测：GSRS 只认出 2/15 题、IPAQ 只认出 2/7 题、PSS-14 只认出 ~2/14 题）。
2. **匹配策略**：精确文本 → 归一化文本（去空白/标点/全半角）→ 前缀模糊。
   三级匹配都失败才返回 None，并记录为「未归类条目」以便审计。
3. **缺项策略（MISSING_POLICY）**：显式定义。
   - 单条缺失：该条不计入分子，但计入分母（按题数折算），并累计 missing 计数；
   - 缺失超过 20%：整表标记 invalid，不给出分档结论；
   - 全表缺失：total=None。
4. **反向计分**：反向条目按 `max_option + min_option - raw` 转换，绝不漏项。
5. **量纲**：每个量表显式声明 min/max，解释区间一律以官方切点为准；
   无官方切点的量表（GSRS/VSI/PSS 四档）明确标注为「项目自定义操作性分档」。

官方规则依据
------------
- PSS-14：Cohen S, Kamarck T, Mermelstein R. J Health Soc Behav. 1983;24(4):385-96.
- GAD-7：Spitzer RL et al. Arch Intern Med. 2006;166(10):1092-7.
- PHQ-9：Kroenke K et al. J Gen Intern Med. 2001;16(9):606-13.
- PSQI：Buysse DJ et al. Psychiatry Res. 1989;28(2):193-213.（7 成分，各 0–3，合计 0–21，>7 提示睡眠质量差）
- WHOQOL-BREF：WHO, 1996.（26 题，4 领域；Q3/Q4/Q26 反向；领域分 = 条目均分 × 4）
- DEBQ：Van Strien T et al. 1986.（33 题，1–5 计分，三亚量表取均分）
- GSRS：Svedlund J et al. Dig Dis Sci. 1988;33(2):129-34.（15 题，1–5，总分 15–75）
- VSI：Labus JS et al. Aliment Pharmacol Ther. 2007.（15 题，0–6，总分 0–90）
- IPAQ-S：IPAQ Guidelines for Data Processing and Analysis, 2005.
- BMI：中国成人体重判定 WS/T 428-2013.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime

# ════════════════════════════════════════════════════════════════════════════
#  通用工具
# ════════════════════════════════════════════════════════════════════════════

MISSING_RATIO_INVALID = 0.20  # 缺失比例阈值，超过则整表 invalid


def _norm(text: str) -> str:
    """归一化题干：全角→半角、去空白、去标点。"""
    if text is None:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    t = re.sub(r"[\s\u3000]+", "", t)
    t = re.sub(r"[，。？！、；：,.?!;:（）()\[\]【】“”\"'‘’—\-_/\\]", "", t)
    return t


def _to_number(text):
    """把答案文本解析成数值；无法解析返回 None（不再静默归 0）。"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = unicodedata.normalize("NFKC", str(text)).strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _option_score(answer, options, fuzzy=True):
    """在选项映射中查分。返回 None 表示无法判定（含未作答）。"""
    if answer is None:
        return None
    a = unicodedata.normalize("NFKC", str(answer)).strip()
    if not a:
        return None
    a_norm = _norm(a)
    if a_norm in options:
        return options[a_norm]
    # 归一化后精确匹配
    for k, v in options.items():
        if _norm(k) == a_norm:
            return v
    if fuzzy:
        # 取最长匹配，避免「很差」被「差」抢先命中
        best = None
        best_len = 0
        for k, v in options.items():
            kn = _norm(k)
            if not kn or len(kn) < 2:
                continue
            if kn in a_norm or a_norm in kn:
                if len(kn) > best_len:
                    best, best_len = v, len(kn)
        if best is not None:
            return best
    return None


def _reverse(raw, options):
    """反向计分：min+max-raw。"""
    if raw is None or not options:
        return None
    vals = list(options.values())
    return min(vals) + max(vals) - raw


# ── 通用选项映射 ──────────────────────────────────────────────────────────

FREQ_0_4 = {"从不": 0, "几乎没有": 1, "有时": 2, "经常": 3, "总是": 4}

OPT_0_3_GAD = {"没有或极少": 0, "有过几天（≤7天）": 1, "超过一半天数（＞7天）": 2, "几乎每天": 3}

OPT_1_5_DEBQ = {"从不": 1, "很少": 2, "有时": 3, "经常": 4, "总是": 5}

OPT_1_5_GSRS = {
    "无症状": 1, "轻度": 2, "轻度症状": 2, "中度": 3, "中度症状": 3,
    "重度": 4, "重度症状": 4, "极重度": 5, "极重度症状": 5,
}

OPT_1_6_VSI = {
    "非常不符": 1, "比较不符": 2, "有点不符": 3,
    "有点符合": 4, "比较相符": 5, "非常相符": 6,
}

OPT_1_5_QUALITY5 = {"很差": 1, "差": 2, "一般": 3, "好": 4, "很好": 5}
OPT_1_5_SATISFIED5 = {"很不满意": 1, "较不满意": 2, "一般": 3, "较满意": 4, "很满意": 5}

# 睡眠障碍频率选项。
# 本课题问卷为 3 级（没有 / 每周 1-2 次 / 每周 3 次或更多），
# 官方 PSQI 为 4 级（无 / <1 次周 / 1-2 次周 / ≥3 次周）。
# 为保持各成分的 0–3 分域不失真，此处将 3 级按「最接近的官方档位」映射：
#   没有 → 0；每周 1-2 次 → 2（对应官方 1-2 次/周）；每周 3 次或更多 → 3。
# 已在本模块 docstring 与审计报告中标注为「层级压缩适配」。
OPT_0_3_FREQ3 = {"没有": 0, "每周 1-2 次": 2, "每周 3 次或更多": 3}
OPT_0_3_FREQ4 = {"没有": 0, "每周<1次": 1, "每周 1-2 次": 2, "每周≥3次": 3}
# 兼容旧名
OPT_0_2_FREQ3 = OPT_0_3_FREQ3
OPT_0_2_FREQ3_ALT = OPT_0_3_FREQ4

OPT_PSQI_QUALITY = {"非常好": 0, "较好": 1, "较差": 2, "很差": 3}
OPT_PSQI_LATENCY = {"少于 15 分钟": 0, "16～30 分钟": 1, "31～60 分钟": 2, "> 60 分钟": 3,
                    "少于15分钟": 0, "16-30分钟": 1, "31-60分钟": 2, "大于60分钟": 3}
OPT_PSQI_DURATION = {"大于 7 小时": 0, "6～7 小时": 1, "5～6 小时": 2, "少于 5 小时": 3,
                     "7～8 小时": 1, "> 8 小时": 0}
OPT_PSQI_EFFICIENCY = {
    "大于 85%": 0, "大于85%": 0, ">85%": 0, "> 85%": 0, "85%以上": 0,
    "75～84%": 1, "75%-84%": 1, "75-84%": 1, "75%～84%": 1, "75%到84%": 1,
    "65～74%": 2, "65%-74%": 2, "65-74%": 2, "65%～74%": 2, "65%到74%": 2,
    "小于 65%": 3, "小于65%": 3, "< 65%": 3, "<65%": 3, "65%以下": 3,
}


# ════════════════════════════════════════════════════════════════════════════
#  条目库：本课题问卷实际题干 → (量表, 成分/维度, 是否反向)
# ════════════════════════════════════════════════════════════════════════════

_PSS_NONREV = [
    "在过去一个月里，你因为意外发生的事情而感到不安。",
    "在过去一个月里，你觉得自己无法控制生活中的重要事情。",
    "在过去一个月里，你处于疲惫的状态。",
    "在过去一个月里，你感到紧张和压力。",
    "在过去一个月里，你感觉自己无法完成必须做的事情。",
    "在过去一个月里，你感到困难的事情堆积如山，以至于你无法处理。",
    "在过去一个月里，你因为你无法控制的事情而生气。",
    "在过去一个月里，你经常想到有些事情是自己必须完成的。",
]
_PSS_REV = [
    "在过去一个月里，你感到能有效地应对生活中不断发生的变化。",
    "在过去一个月里，你感觉自己处于领先地位。",
    "在过去一个月里，你感到事情顺心如意。",
    "在过去一个月里，你感觉自己可以成功地处理生活琐事和烦恼。",
    "在过去一个月里，你能掌控你的时间管理方式。",
    "在过去一个月里，你对自己处理个人问题的能力感到自信。",
]

_GAD7_ITEMS = [
    "过去两周内，您是否感到不安、焦虑或烦躁？",
    "过去两周内，您是否难以停止或无法控制担忧？",
    "过去两周内，您是否对各种各样的事情过度担忧？",
    "过去两周内，您是否很难放松下来？",
    "过去两周内，您是否感到坐立不安或心神不定？",
    "过去两周内，您是否容易烦恼或急躁？",
    "过去两周内，您是否会害怕将有可怕的事发生？",
]

_PHQ9_ITEMS = [
    "过去两周，您是否做事时提不起劲或没有兴趣？",
    "过去两周，您是否感到情绪低落、沮丧或无望？",
    "过去两周，您是否入睡困难、睡不安稳或睡眠过多？",
    "过去两周，您是否感到疲倦或没有活力？",
    "过去两周，您是否有食欲不振或过度进食（吃太多）？",
    "过去两周，您是否觉得自己很糟，或觉得自己很失败，或让自己或家人失望？",
    "过去两周内，您是否对事物专注有困难，例如看书或看电视不能集中注意力？",
    "过去两周内，您是否动作或说话速度变慢到别人已经觉察，或者相反，烦躁或坐立不安、动来动去的情况更胜于平常？",
    "过去两周内，您是否有不如死掉或用某种方式伤害自己的念头？",
]

_PSQI_C1 = "总的来说，您认为自己近一个月的睡眠质量如何？"
_PSQI_C2A = "近一个月，您从上床到入睡通常需要多少分钟？"
_PSQI_C2B = "近一个月，您是否因入睡困难（无法在 30 分钟内入睡）而影响睡眠？"
_PSQI_C3 = "近一个月，您每晚实际睡眠时间（非卧床时间）大约是多少？"
_PSQI_C3_NUM = "近一个月，您每晚实际睡了几个小时（平均值）？(注意：实际睡眠时间不等于卧床时间，填写具体小时数，如：6.5 小时)"
_PSQI_C4 = "请计算一下您的睡眠效率（睡眠效率 = 实际睡眠时间 /(起床时间 - 上床时间)×100%。实际睡眠时间占床上时间的比例）："
_PSQI_C5_ITEMS = [
    "近一个月，您是否因夜间易醒或早醒而影响睡眠？",
    "近一个月，您是否因夜间去厕所而影响睡眠？",
    "近一个月，您是否因呼吸不畅而影响睡眠？",
    "近一个月，您是否因咳嗽或打鼾而影响睡眠？",
    "近一个月，您是否因感觉太冷而影响睡眠？",
    "近一个月，您是否因感觉太热而影响睡眠？",
    "近一个月，您是否因做噩梦而影响睡眠？",
    "近一个月，您是否因疼痛不适而影响睡眠？",
    "近一个月，您是否有其他事情影响睡眠？",
]
_PSQI_C6 = "近一个月，您是否使用药物帮助睡眠？"
_PSQI_C7_A = "近一个月，您白天是否感到困倦或精力不足？"
_PSQI_C7_B = "近一个月，您的生活或工作效率是否因睡眠问题受影响？"
_PSQI_EXTRA = [
    "近一个月，您晚上上床睡觉的时间通常是几点几分？",
    "近一个月，每天早上通常几点起床？",
]

_IPAQ_V_DAYS = "最近 7 天内，您有几天做了剧烈的体育活动，像是提重物、挖掘、有氧运动或是快速骑车？"
_IPAQ_V_MIN = "在这其中一天您通常会花多少分钟在剧烈的体育活动上？（单位：分钟）"
_IPAQ_M_DAYS = "最近 7 天内，您有几天做了适度的体育活动，像是提轻的物品、以平常的速度骑车或打双人网球？请不要包括走路。"
_IPAQ_M_MIN = "在这其中一天您通常会花多少时间在适度的体育活动上？（单位：分钟）"
_IPAQ_W_DAYS = "最近 7 天内，您有几天是步行，且一次步行至少 10 分钟？"
_IPAQ_W_MIN = "在这其中一天您通常花多少时间在步行上？（单位：分钟）"
_IPAQ_SIT = "最近七天内，工作日您有多久时间是坐着的？（单位：分钟）"

# WHOQOL-BREF：题号 → 题干（官方 26 题 + 本问卷的附加总体自评题）
_WHOQOL_ITEMS = {
    1: "您如何评价您的生存质量？",
    2: "您对自己的健康状况满意吗？",
    3: "您觉得疼痛妨碍您去做自己需要做的事情吗？",          # 反向
    4: "您是否需要依靠医疗帮助进行日常活动？",              # 反向
    5: "您觉得生活有乐趣吗？",
    6: "您觉得自己的生活有意义吗？",
    7: "您能集中注意力吗？",
    8: "日常生活中您感觉安全吗？",
    9: "您的生活环境对健康好吗？",
    10: "您有充沛的精力去应付日常生活吗？",
    11: "您认为自己的外形过得去吗？",
    12: "您的钱够用吗？",
    13: "在日常生活中您需要的信息都齐备吗？",
    14: "您有机会进行休闲活动吗？",
    15: "您行动的能力如何？",
    16: "您对自己的睡眠情况满意吗？",
    17: "您对自己做日常生活事情的能力满意吗？",
    18: "您对自己的工作能力满意吗？",
    19: "您对自己满意吗？",
    20: "您对自己的人际关系满意吗？",
    21: "您对家人满意吗？",
    22: "您对自己从朋友那里得到的支持满意吗？",
    23: "您对自己居住地的条件满意吗？",
    24: "您对得到卫生保健服务的方便程度满意吗？",
    25: "您对自己的交通情况满意吗？",
    26: "您有消极感受吗（如情绪低落、绝望、焦虑、忧郁）？",   # 反向
}
_WHOQOL_REVERSE = {3, 4, 26}
_WHOQOL_EXTRA = ["家庭摩擦影响您的生活吗？"]
_WHOQOL_TOTAL_ITEM = (
    "如果让您综合以上各方面（生理健康、心理健康、社会关系和周围环境等方面）"
    "给自己的生存质量打一个总分，您打多少分？（满分为 100 分，请填写 0-100 分之间)"
)
# 官方领域归属（WHOQOL-BREF）
_WHOQOL_DOMAINS = {
    "生理领域": [3, 4, 10, 15, 16, 17, 18],
    "心理领域": [5, 6, 7, 11, 19, 26],
    "社会关系领域": [20, 21, 22],
    "环境领域": [8, 9, 12, 13, 14, 23, 24, 25],
}
_WHOQOL_DOMAIN_ZH = {
    "生理领域": "生理领域（Physical）",
    "心理领域": "心理领域（Psychological）",
    "社会关系领域": "社会关系领域（Social）",
    "环境领域": "环境领域（Environment）",
}

# GSRS 15 题 → 5 个症状维度（官方维度归属）
_GSRS_SUB = {
    "腹痛": ["上腹部疼痛（胃疼）", "下腹部疼痛（肚脐周围绞痛）"],
    "反流": ["烧心（胃灼热，胸骨后灼热）", "反酸（喉咙酸味感）"],
    "消化不良": ["腹胀（餐后饱胀，如 \"吃一顿管一天\"）", "打嗝或嗳气", "恶心", "呕吐"],
    "腹泻": ["腹泻", "稀便 / 水样便", "便急（突然强烈便意）"],
    "便秘": ["排便费力", "排便不净感", "大便干硬", "便秘"],
}
_GSRS_FLAT = [q for v in _GSRS_SUB.values() for q in v]

# VSI 15 题；"我相信偶尔的肠胃问题不会影响长期健康。" 为正向条目，需反向计分
_VSI_ITEMS = [
    "我常常为肠胃道问题而担心和焦虑。",
    "我容易注意到轻微的胃胀或胃肠道不适感。",
    "餐后若出现肠胃不适（如打嗝或腹胀），我会反复回想是否吃错了东西。",
    "若腹泻持续两天，我会感到恐慌并立刻就医。",
    "突然的肠胃不适（如腹痛）会让我担心自己得了重病。",
    "我经常担心自己的消化系统会出问题。",
    "即使轻微肠胃不适，我也会在寝室休息而非去上课或朋友聚会。",
    "为了避免肠胃不适，我尽量不吃辛辣或油炸食品。",
    "我尝试通过运动（如跑步、球类）缓解肠胃不适。",
    "当我感觉到腹部不适时，就会觉得很沮丧。",
    "我很难享受生活，因为无法摆脱腹部不适的困扰。",
    "肠胃不适时，我很难集中精力完成作业或实验。",
    "我相信偶尔的肠胃问题不会影响长期健康。",   # 反向
    "压力大时（如考试期间），我常会感到肠胃不适。",
    "我认为压力过大是学生群体肠胃不适的主要原因。",
]
_VSI_REVERSE = {"我相信偶尔的肠胃问题不会影响长期健康。"}

# DEBQ 33 题三亚量表；"你能否抵制美味食物的诱惑？" 为反向条目
_DEBQ_EMOTIONAL = [
    "当你感到烦躁时，是否有进食的欲望？",
    "当你无事可做时，是否有进食的欲望？",
    "当你感到沮丧或气馁时，是否有进食的欲望？",
    "当你感到孤独时，是否有进食的欲望？",
    "当有人让你失望时，是否有进食的欲望？",
    "当你生气时，是否有进食的欲望？",
    "当你预感即将发生不愉快的事情时，是否有进食的欲望？",
    "当你感到焦虑、担忧或紧张时，是否有进食的欲望？",
    "当事情不顺利或出错时，是否有进食的欲望？",
    "当你感到害怕时，是否有进食的欲望？",
    "当你感到失望时，是否有进食的欲望？",
    "当你情绪低落时，是否有进食的欲望？",
    "当你感到无聊或不安时，是否有进食的欲望？",
]
_DEBQ_EXTERNAL = [
    "如果食物味道很好，你是否会吃得比平时多？",
    "如果食物闻起来和看起来都很诱人，你是否会吃得比平时多？",
    "如果你看到或闻到美味的食物，是否有进食的欲望？",
    "如果你有美味的食物，是否会立刻吃掉？",
    "如果你经过面包店，是否有购买美味食物的欲望？",
    "如果你经过小吃店或咖啡馆，是否有购买美味食物的欲望？",
    "如果你看到别人在吃东西，是否也会有进食的欲望？",
    "你能否抵制美味食物的诱惑？",                     # 反向
    "当你看到别人吃东西时，是否会吃得比平时多？",
    "在准备餐食时，你是否会忍不住先吃点东西？",
]
_DEBQ_RESTRAINED = [
    "如果你体重增加了，你是否会吃得比平时少？",
    "你是否会在用餐时故意吃得比你想吃的量少？",
    "你是否会因担心体重而拒绝提供的食物或饮料？",
    "你是否会严格控制自己的饮食？",
    "你是否会刻意选择有助于减肥的食物？",
    "当你吃得过多时，接下来的几天是否会减少食量？",
    "你是否会为了不增重而刻意减少饮食？",
    "你是否会因关注体重而避免在两餐之间进食？",
    "你是否会因关注体重而在晚上尽量避免进食？",
    "你在选择食物时是否会考虑体重因素？",
]
_DEBQ_REVERSE = {"你能否抵制美味食物的诱惑？"}

# 非计分条目（人口学 / 饮食频率 / 病史 等），显式登记以免被误计入量表
NON_SCORING = {
    "姓名", "性别", "学工号", "年级", "学院/单位", "邮箱", "身高（cm）", "体重（kg）",
    "日常饮食习惯", "是否有胃肠道疾病史（如果有请注明疾病类型）", "是否有饮酒史 (有少量饮酒的情况也计入饮酒史)：",
    "是否经临床诊断为脂肪肝：", "当前脂肪肝发病阶段", "自我报告压力水平",
}


def _build_item_bank():
    bank = {}

    def add(text, scale, component=None, reverse=False):
        bank[_norm(text)] = {"text": text, "scale": scale,
                             "component": component, "reverse": reverse}

    for q in _PSS_NONREV:
        add(q, "PSS-14", "PSS-14", False)
    for q in _PSS_REV:
        add(q, "PSS-14", "PSS-14", True)
    for q in _GAD7_ITEMS:
        add(q, "GAD-7", "GAD-7", False)
    for q in _PHQ9_ITEMS:
        add(q, "PHQ-9", "PHQ-9", False)

    add(_PSQI_C1, "PSQI", "C1")
    add(_PSQI_C2A, "PSQI", "C2a")
    add(_PSQI_C2B, "PSQI", "C2b")
    add(_PSQI_C3, "PSQI", "C3")
    add(_PSQI_C3_NUM, "PSQI", "C3num")
    add(_PSQI_C4, "PSQI", "C4")
    for q in _PSQI_C5_ITEMS:
        add(q, "PSQI", "C5")
    add(_PSQI_C6, "PSQI", "C6")
    add(_PSQI_C7_A, "PSQI", "C7")
    add(_PSQI_C7_B, "PSQI", "C7")
    for q in _PSQI_EXTRA:
        add(q, "PSQI", "extra")

    for q in [_IPAQ_V_DAYS, _IPAQ_V_MIN, _IPAQ_M_DAYS, _IPAQ_M_MIN,
              _IPAQ_W_DAYS, _IPAQ_W_MIN, _IPAQ_SIT]:
        add(q, "IPAQ-S", "IPAQ-S")

    for n, q in _WHOQOL_ITEMS.items():
        add(q, "WHOQOL-BREF", n, n in _WHOQOL_REVERSE)
    for q in _WHOQOL_EXTRA:
        add(q, "WHOQOL-BREF", "extra")
    add(_WHOQOL_TOTAL_ITEM, "WHOQOL-BREF", "overall100")

    for q in _GSRS_FLAT:
        add(q, "GSRS", "GSRS")
    for q in _VSI_ITEMS:
        add(q, "VSI", "VSI", q in _VSI_REVERSE)
    for q in _DEBQ_EMOTIONAL:
        add(q, "DEBQ", "情绪性饮食")
    for q in _DEBQ_EXTERNAL:
        add(q, "DEBQ", "外部性饮食", q in _DEBQ_REVERSE)
    for q in _DEBQ_RESTRAINED:
        add(q, "DEBQ", "限制性饮食")
    for q in NON_SCORING:
        add(q, "_NON_SCORING", None)
    return bank


ITEM_BANK = _build_item_bank()


def classify_item(title):
    """
    返回 (scale, component, reverse)。
    未命中时返回 ("_UNKNOWN", None, False) —— 不再静默丢弃，便于审计告警。
    """
    key = _norm(title)
    hit = ITEM_BANK.get(key)
    if hit:
        return hit["scale"], hit["component"], hit["reverse"]
    # 前缀模糊：仅当题干足够长（≥10 字）且唯一命中时才接受，
    # 避免短词（如食物频率表里的"咖啡"）误命中长题干。
    if len(key) >= 10:
        cands = [v for k, v in ITEM_BANK.items() if k and (k[:14] in key or key[:14] in k)]
        if len(cands) == 1:
            v = cands[0]
            return v["scale"], v["component"], v["reverse"]
    return "_UNKNOWN", None, False


# ════════════════════════════════════════════════════════════════════════════
#  分档定义
# ════════════════════════════════════════════════════════════════════════════

def _band(value, bands, default="未分类"):
    for lo, hi, label in bands:
        if lo <= value <= hi:
            return label
    return default


# 官方切点
GAD7_BANDS = [(0, 4, "无焦虑症状"), (5, 9, "轻度焦虑"), (10, 14, "中度焦虑"), (15, 21, "重度焦虑")]
PHQ9_BANDS = [(0, 4, "无抑郁症状"), (5, 9, "轻度抑郁"), (10, 14, "中度抑郁"),
              (15, 19, "中重度抑郁"), (20, 27, "重度抑郁")]
# 项目自定义操作性分档（无量表官方切点，标注为自定义）
PSS14_BANDS = [(0, 18, "低压力"), (19, 37, "中等压力"), (38, 56, "高压力")]
GSRS_BANDS_CUSTOM = [(15, 29, "无明显胃肠道症状"), (30, 44, "轻度胃肠道症状"),
                     (45, 59, "中度胃肠道症状"), (60, 75, "重度胃肠道症状")]
VSI_BANDS_CUSTOM = [(15, 39, "低内脏敏感"), (40, 59, "中度内脏敏感"), (60, 90, "高内脏敏感")]
DEBQ_LEVELS = [(1, 2.0, "低水平"), (2.01, 3.0, "中等水平"), (3.01, 4.0, "较高水平"), (4.01, 5.0, "高水平")]


# ════════════════════════════════════════════════════════════════════════════
#  基础计分器
# ════════════════════════════════════════════════════════════════════════════

def _score_items(items, options, *, min_score=0, max_score=None,
                 reverse=False, scale_name="", reverse_set=(), fuzzy=True):
    """对一组成对条目计分，返回 (total, n_scored, n_missing, raw_values)。

    反向计分来源（任一成立即反向）：显式 reverse 参数、条目库中的 reverse 标记、reverse_set。
    条目的反向标记来自 ITEM_BANK，因此无需调用方重复声明。
    """
    total = 0
    n_scored = 0
    n_missing = 0
    vals = []
    rev_norm = {_norm(x) for x in reverse_set}
    for it in items:
        text = it.get("question", "")
        raw = _option_score(it.get("answer_text"), options, fuzzy=fuzzy)
        if raw is None:
            n_missing += 1
            continue
        _, _, bank_rev = classify_item(text)
        is_rev = bool(reverse or bank_rev or (_norm(text) in rev_norm))
        if is_rev:
            raw = _reverse(raw, options)
        vals.append(raw)
        total += raw
        n_scored += 1
    return total, n_scored, n_missing, vals


def _result(scale, name_zh, total, n_scored, n_missing, expected_n,
            max_score, interpretation, extra=None):
    """统一结果结构。

    n_missing 统一按「期望题数 − 实际可计分题数」计算，
    这样既覆盖「答案无法解析」也覆盖「该题根本没问/没答」两种情况。
    """
    n_missing = max(n_missing, max(0, expected_n - n_scored))
    missing_ratio = (n_missing / expected_n) if expected_n else 0.0
    need_ratio = (n_scored / expected_n) if expected_n else 1.0
    valid = (n_scored > 0) and (missing_ratio <= MISSING_RATIO_INVALID)
    if n_scored == 0:
        warning = "该量表未采集到可计分条目"
    elif missing_ratio > MISSING_RATIO_INVALID:
        warning = (f"缺失条目比例 {missing_ratio:.0%} 超过 {MISSING_RATIO_INVALID:.0%}，"
                   f"结论不可靠（可计分 {n_scored}/{expected_n}）")
    elif n_missing:
        warning = (f"缺失 {n_missing} 条（未计入分子，按可计分条目呈现；"
                   f"结论基于 {n_scored}/{expected_n} 条）")
    else:
        warning = None
    res = {
        "scale": scale, "name_zh": name_zh,
        "total": total, "max_score": max_score,
        "n_items": n_scored, "n_expected": expected_n, "n_missing": n_missing,
        "missing_ratio": round(missing_ratio, 3),
        "coverage": round(need_ratio, 3),
        "valid": valid, "warning": warning,
        "interpretation": interpretation,
        "subscales": {}, "components": {},
    }
    if extra:
        res.update(extra)
    return res


# ── PSS-14 ────────────────────────────────────────────────────────────────

def score_pss14(items):
    """PSS-14：14 题，0–4 计分；反向条目 min+max-raw；总分 0–56。"""
    total, n, miss, _ = _score_items(items, FREQ_0_4, scale_name="PSS-14")
    band = _band(total, PSS14_BANDS) if n else "—"
    interp = (f"PSS-14 总分 {total}/56 — {band}（项目自定义分档：0–18 低 / 19–37 中 / 38–56 高）"
              if n else "PSS-14 无可计分条目")
    return _result("PSS-14", "感知压力量表（PSS-14）", total if n else None, n, miss, 14, 56, interp)


# ── GAD-7 / PHQ-9 ─────────────────────────────────────────────────────────

def score_gad7(items):
    total, n, miss, _ = _score_items(items, OPT_0_3_GAD, scale_name="GAD-7")
    band = _band(total, GAD7_BANDS) if n else "—"
    interp = f"GAD-7 总分 {total}/21 — {band}（官方切点）" if n else "GAD-7 无可计分条目"
    return _result("GAD-7", "广泛性焦虑障碍量表（GAD-7）", total if n else None, n, miss, 7, 21, interp)


def score_phq9(items):
    total, n, miss, _ = _score_items(items, OPT_0_3_GAD, scale_name="PHQ-9")
    band = _band(total, PHQ9_BANDS) if n else "—"
    interp = f"PHQ-9 总分 {total}/27 — {band}（官方切点）" if n else "PHQ-9 无可计分条目"
    return _result("PHQ-9", "患者健康问卷抑郁量表（PHQ-9）", total if n else None, n, miss, 9, 27, interp)


# ── PSQI：官方 7 成分 ─────────────────────────────────────────────────────

def _psqi_c5_component(freq_sum, n_items_scored):
    """
    C5 睡眠障碍：官方为 9 小项各 0–3 求和后归并：
      0 → 0；1–9 → 1；10–18 → 2；19–27 → 3。
    此处按「实际可计分小项数」等比归一，使答案不完整时仍落在 0–3；
    完整 9 小项时与官方档位完全一致。
    """
    cap = 3 * max(1, n_items_scored)
    if freq_sum <= 0:
        return 0
    if freq_sum <= cap / 3.0:
        return 1
    if freq_sum <= cap * 2 / 3.0:
        return 2
    return 3


def _psqi_c7_component(a, b):
    """C7 日间功能障碍：官方两小项（各 0–3）之和归并 0→0、1–2→1、3–4→2、5–6→3。"""
    s = a + b
    if s <= 0:
        return 0
    if s <= 2:
        return 1
    if s <= 4:
        return 2
    return 3


def score_psqi(items):
    """PSQI：官方 7 成分，各 0–3，合计 0–21；>7 提示睡眠质量差。"""
    comp = {}
    notes = []
    missing = 0

    by_comp = {}
    for it in items:
        title = it.get("question", "")
        _, component, _ = classify_item(title)
        by_comp.setdefault(component, []).append(it)

    def first_score(component, options):
        for it in by_comp.get(component, []):
            s = _option_score(it.get("answer_text"), options)
            if s is not None:
                return s
        return None

    # C1 主观睡眠质量
    c1 = first_score("C1", OPT_PSQI_QUALITY)
    if c1 is None and by_comp.get("C1"):
        missing += 1
    comp["C1_主观睡眠质量"] = c1

    # C2 睡眠潜伏期 = 入睡耗时档分 + 入睡困难频率档分，再归并 0–3
    lat = first_score("C2a", OPT_PSQI_LATENCY)
    dis = first_score("C2b", OPT_0_3_FREQ4)
    if dis is None:
        dis = first_score("C2b", OPT_0_3_FREQ3)
    if lat is None and dis is None:
        if by_comp.get("C2a") or by_comp.get("C2b"):
            missing += 1
        comp["C2_睡眠潜伏期"] = None
    else:
        sub = (lat or 0) + (dis or 0)
        # 官方 C2：两小项之和 0→0, 1–2→1, 3–4→2, 5–6→3（3 级频率时上界为 5）
        if sub <= 0:
            c2 = 0
        elif sub <= 2:
            c2 = 1
        elif sub <= 4:
            c2 = 2
        else:
            c2 = 3
        comp["C2_睡眠潜伏期"] = c2
        comp["C2_明细"] = {"入睡耗时档分": lat, "入睡困难频率档分": dis}

    # C3 睡眠时长：优先用档位题，否则用数值小时
    c3 = first_score("C3", OPT_PSQI_DURATION)
    if c3 is None:
        num = None
        for it in by_comp.get("C3num", []):
            num = _to_number(it.get("answer_text"))
            if num is not None:
                break
        if num is not None:
            if num > 7:
                c3 = 0
            elif num >= 6:
                c3 = 1
            elif num >= 5:
                c3 = 2
            else:
                c3 = 3
            comp["C3_数据来源"] = "数值小时题"
    if c3 is None and (by_comp.get("C3") or by_comp.get("C3num")):
        missing += 1
    comp["C3_睡眠时长"] = c3

    # C4 睡眠效率
    c4 = first_score("C4", OPT_PSQI_EFFICIENCY)
    if c4 is None and by_comp.get("C4"):
        missing += 1
    comp["C4_睡眠效率"] = c4

    # C5 睡眠障碍：9 小项频率求和后归并
    freq_sum = 0
    n_c5 = 0
    for it in by_comp.get("C5", []):
        s = _option_score(it.get("answer_text"), OPT_0_3_FREQ4)
        if s is None:
            s = _option_score(it.get("answer_text"), OPT_0_3_FREQ3)
        if s is None:
            missing += 1
            continue
        freq_sum += s
        n_c5 += 1
    comp["C5_睡眠障碍"] = _psqi_c5_component(freq_sum, n_c5) if n_c5 else None
    comp["C5_明细"] = {"频率和": freq_sum, "计分小项数": n_c5}

    # C6 催眠药物
    c6 = first_score("C6", OPT_0_3_FREQ4)
    if c6 is None:
        c6 = first_score("C6", OPT_0_3_FREQ3)
    if c6 is None and by_comp.get("C6"):
        missing += 1
    comp["C6_催眠药物"] = c6

    # C7 日间功能障碍 = 日间困倦 + 效率受影响 归并 0–3
    c7_items = by_comp.get("C7", [])
    vals = []
    for it in c7_items:
        s = _option_score(it.get("answer_text"), OPT_0_3_FREQ4)
        if s is None:
            s = _option_score(it.get("answer_text"), OPT_0_3_FREQ3)
        if s is None:
            missing += 1
            continue
        vals.append(s)
    comp["C7_日间功能障碍"] = _psqi_c7_component(*vals) if len(vals) == 2 else (
        _psqi_c7_component(vals[0], 0) if len(vals) == 1 else None)

    # 总分：仅累加可得成分
    component_keys = ["C1_主观睡眠质量", "C2_睡眠潜伏期", "C3_睡眠时长",
                      "C4_睡眠效率", "C5_睡眠障碍", "C6_催眠药物", "C7_日间功能障碍"]
    available = [comp[k] for k in component_keys if comp.get(k) is not None]
    total = sum(available) if available else None
    n_comp = len(available)

    if total is None:
        interp = "PSQI 成分不足，无法计分"
    else:
        if total > 7:
            interp = (f"PSQI 总分 {total}/21 — ⚠️ 睡眠质量差（>7 分提示睡眠障碍）；"
                      f"已完成 {n_comp}/7 成分")
        else:
            interp = f"PSQI 总分 {total}/21 — ✅ 睡眠质量尚可（已完成 {n_comp}/7 成分）"
        if n_comp < 7:
            interp += "；缺失成分以 0 计入，结果为下限估计"

    res = _result("PSQI", "匹兹堡睡眠质量指数（PSQI）", total, n_comp, missing, 7, 21, interp)
    res["components"] = {k: v for k, v in comp.items()}
    return res


# ── IPAQ-S ────────────────────────────────────────────────────────────────

def _days_min(items, days_key, min_key):
    days = mins = None
    for it in items:
        t = it.get("question", "")
        if days_key in t:
            days = _to_number(it.get("answer_text"))
        elif min_key in t:
            mins = _to_number(it.get("answer_text"))
    return days, mins


def score_ipaq(items):
    """
    IPAQ-S：MET-min/周 = 剧烈 8.0 + 中度 4.0 + 步行 3.3（天 × 分钟 × MET）。
    截断规则：天数上限 7；单次时长上限 180 分钟（IPAQ 处理指南）。
    """
    by_q = {it.get("question", ""): it.get("answer_text") for it in items}

    def lookup(key):
        """返回 (是否存在该题, 数值或 None)。"""
        for t, a in by_q.items():
            if key in t:
                return True, _to_number(a)
        return False, None

    v_present, v_days = lookup(_IPAQ_V_DAYS[:20])
    _, v_min = lookup(_IPAQ_V_MIN[:20])
    m_present, m_days = lookup(_IPAQ_M_DAYS[:20])
    _, m_min = lookup(_IPAQ_M_MIN[:20])
    w_present, w_days = lookup(_IPAQ_W_DAYS[:20])
    _, w_min = lookup(_IPAQ_W_MIN[:20])
    _, sit = lookup(_IPAQ_SIT[:20])

    def clip(days, mins):
        d = max(0.0, min(days or 0.0, 7.0))
        m = max(0.0, min(mins or 0.0, 180.0))
        return d, m

    v_d, v_m = clip(v_days, v_min)
    m_d, m_m = clip(m_days, m_min)
    w_d, w_m = clip(w_days, w_min)

    met = v_d * v_m * 8.0 + m_d * m_m * 4.0 + w_d * w_m * 3.3
    met = round(met)

    # IPAQ 官方三分级（简化版：以 MET-min/周 切点）
    if met >= 3000:
        level = "高体力活动水平"
    elif met >= 600:
        level = "中等体力活动水平"
    else:
        level = "低体力活动水平"

    # IPAQ 处理指南：若某类活动天数为 0/未答，则其时长条件题不适用，按 0 计。
    # 区分两种情况：
    #   structural（该题在本版问卷里根本没采集）——不计入缺失，仅备注；
    #   missing（题目存在但答案无法解析）——计入缺失。
    expected = 7
    structural_gap = sum(1 for p in [m_present, w_present] if not p)
    missing = 0
    if v_present and v_days is None:
        missing += 1
    note = ""
    if structural_gap:
        note = (f"；{structural_gap} 个条目在本版问卷未采集"
                f"（问卷版本差异，未答按 0 计）")
    interp = (f"MET-min/周 {met} — {level}（MET 系数 步行3.3/中等4.0/剧烈8.0；"
              f"天数≤7、单日≤180 分钟截断）{note}")
    res = _result("IPAQ-S", "国际体力活动问卷（IPAQ-S）", met, expected - structural_gap,
                  missing, expected - structural_gap, None, interp)
    res["structural_gap"] = structural_gap
    res["subscales"] = {
        "剧烈活动(MET-min/周)": round(v_d * v_m * 8.0),
        "中等活动(MET-min/周)": round(m_d * m_m * 4.0),
        "步行(MET-min/周)": round(w_d * w_m * 3.3),
    }
    res["sedentary_min"] = sit
    res["activity_level"] = level
    return res


# ── WHOQOL-BREF ───────────────────────────────────────────────────────────

_WHOQOL_OPT_MAIN = {  # 5 级：能力/频率/评价类
    "根本没有": 1, "很少": 2, "一般": 3, "较多": 4, "完全有": 5,
    "极不满意": 1, "很不满意": 1, "较不满意": 2, "满意": 4, "较满意": 4, "很满意": 5,
    "极差": 1, "很差": 1, "差": 2, "不好也不差": 3, "好": 4, "很好": 5,
    "极好": 5,
    "极不妨碍": 5, "根本不妨碍": 5, "有点妨碍": 4, "比较妨碍": 2, "极妨碍": 1,
    "极不需要": 5, "根本不需要": 5, "很少需要": 4, "比较需要": 2, "极需要": 1,
    "极无乐趣": 1, "无乐趣": 1, "有点乐趣": 3, "比较有乐趣": 4, "极有乐趣": 5,
    "极无意义": 1, "无意义": 1, "有点意义": 3, "比较有意义": 4, "极有意义": 5,
    "极不能": 1, "不能": 1, "有点能": 3, "比较能": 4, "极能": 5,
    "完全过得去": 5, "过得去": 4, "有点过不去": 3, "比较过不去": 2, "极过不去": 1,
    "极不安全": 1, "不安全": 1, "有点安全": 3, "比较安全": 4, "极安全": 5,
    "完全不够用": 1, "不够用": 1, "有点够用": 3, "比较够用": 4, "完全够用": 5,
    "完全欠缺": 1, "欠缺": 1, "有点齐备": 3, "比较齐备": 4, "完全齐备": 5,
    "完全没有机会": 1, "没有机会": 1, "有点机会": 3, "比较有机会": 4, "完全有机会": 5,
    "极差 ": 1,
    "有极大影响": 1, "有较大影响": 2, "有中等影响": 3, "有较小影响": 4, "没有影响": 5,
    "总是有": 1, "经常有": 1, "有时有": 2, "较少有": 4, "没有": 5,
    "完全有精力": 5, "很有精力": 4, "一般": 3, "很少精力": 2, "没有精力": 1,
}


def score_whoqol(items):
    """
    WHOQOL-BREF：26 题，5 级计分；Q3/Q4/Q26 反向。
    领域分 = 该领域条目均分 × 4（范围 4–20），并给出 0–100 转换分。
    总体题（生存质量、健康满意度）与 0–100 总分自评单独呈现，不混入领域分。
    """
    by_q = {}
    for it in items:
        title, ans = it.get("question", ""), it.get("answer_text")
        scale, component, reverse = classify_item(title)
        if scale == "WHOQOL-BREF":
            by_q[component] = {"answer": ans, "reverse": reverse, "text": title, "raw": it}

    domain_scores = {}
    domain_detail = {}
    missing = 0
    for dname, nums in _WHOQOL_DOMAINS.items():
        vals = []
        for n in nums:
            rec = by_q.get(n)
            if not rec:
                continue
            s = _option_score(rec["answer"], _WHOQOL_OPT_MAIN)
            if s is None:
                missing += 1
                continue
            if n in _WHOQOL_REVERSE:
                s = _reverse(s, {k: i for i, k in enumerate(sorted(set(_WHOQOL_OPT_MAIN.values())), start=1)})
            vals.append(s)
        if vals:
            mean = sum(vals) / len(vals)
            domain_scores[dname] = {
                "mean": round(mean, 2),
                "score_4_20": round(mean * 4, 2),
                "score_0_100": round((mean * 4 - 4) * 100 / 16, 1),
                "n_items": len(vals),
                "n_expected": len(nums),
            }
        domain_detail[dname] = vals

    # 总体题
    overall = {}
    for n, label in [(1, "总体生存质量"), (2, "总体健康状况")]:
        rec = by_q.get(n)
        if rec:
            overall[label] = _option_score(rec["answer"], _WHOQOL_OPT_MAIN)
    # 0–100 自评总分（独立呈现，不混入领域分）
    overall_100 = None
    for rec in by_q.values():
        if isinstance(rec, dict) and rec.get("text") == _WHOQOL_TOTAL_ITEM:
            overall_100 = _to_number(rec.get("answer"))
    rec100 = None
    for it in items:
        if classify_item(it.get("question", ""))[1] == "overall100":
            rec100 = _to_number(it.get("answer_text"))
    if rec100 is not None:
        overall_100 = rec100

    n_scored = sum(len(v) for v in domain_detail.values())
    if domain_scores:
        parts = [f"{_WHOQOL_DOMAIN_ZH[d][:4]}:{v['score_0_100']}" for d, v in domain_scores.items()]
        life = "；".join(parts)
        interp = (f"WHOQOL-BREF 四领域分（0–100 转换）：{life}"
                  + (f"；总体自评 {overall_100}/100" if overall_100 is not None else ""))
    else:
        interp = "WHOQOL-BREF 无可计分条目"

    res = _result("WHOQOL-BREF", "世界卫生组织生活质量简表（WHOQOL-BREF）",
                  None, n_scored, missing, 24, None, interp)
    res["overall_items"] = 2  # 总体生存质量 / 总体健康状况：单独呈现，不计入领域分
    res["domain_scores"] = domain_scores
    res["overall"] = overall
    res["overall_0_100"] = overall_100
    return res


# ── DEBQ ──────────────────────────────────────────────────────────────────

def score_debq(items):
    """DEBQ：33 题，1–5 计分；三亚量表取均分（外部性饮食含 1 条反向条目）。"""
    groups = {"情绪性饮食": [], "外部性饮食": [], "限制性饮食": []}
    missing = {"情绪性饮食": 0, "外部性饮食": 0, "限制性饮食": 0}
    for it in items:
        title = it.get("question", "")
        scale, comp, rev = classify_item(title)
        if scale != "DEBQ" or comp not in groups:
            continue
        s = _option_score(it.get("answer_text"), OPT_1_5_DEBQ)
        if s is None:
            missing[comp] += 1
            continue
        if rev:
            s = _reverse(s, OPT_1_5_DEBQ)
        groups[comp].append(s)

    subscales = {}
    for comp, vals in groups.items():
        if vals:
            mean = sum(vals) / len(vals)
            subscales[comp] = {
                "sum": sum(vals), "mean": round(mean, 2), "n_items": len(vals),
                "n_expected": 13 if comp == "情绪性饮食" else 10,
                "level": _band(mean, DEBQ_LEVELS, "未分类"),
            }
    parts = []
    for comp, v in subscales.items():
        parts.append(f"{comp}: 均分 {v['mean']}（1–5，{v['level']}，{v['n_items']}/{v['n_expected']} 题）")
    interp = "；".join(parts) if parts else "DEBQ 无可计分条目"

    n_scored = sum(len(v) for v in groups.values())
    n_missing = sum(missing.values())
    res = _result("DEBQ", "荷兰饮食行为问卷（DEBQ）", None, n_scored, n_missing, 33, None, interp)
    res["subscales"] = subscales
    return res


# ── GSRS ──────────────────────────────────────────────────────────────────

def score_gsrs(items):
    """GSRS：15 题，1–5 计分；总分 15–75；同时给出 5 个症状维度均分。"""
    by_text = {}
    for it in items:
        scale, comp, _ = classify_item(it.get("question", ""))
        if scale == "GSRS":
            by_text[it.get("question", "")] = _option_score(it.get("answer_text"), OPT_1_5_GSRS)

    vals = [v for v in by_text.values() if v is not None]
    n_missing = len(by_text) - len(vals) + max(0, 15 - len(by_text))
    total = sum(vals) if vals else 0
    subscales = {}
    for sname, qs in _GSRS_SUB.items():
        sv = [by_text[q] for q in qs if by_text.get(q) is not None]
        if sv:
            subscales[sname] = {"mean": round(sum(sv) / len(sv), 2),
                                "n_items": len(sv), "n_expected": len(qs)}
    band = _band(total, GSRS_BANDS_CUSTOM) if vals else "—"
    interp = (f"GSRS 总分 {total}/75 — {band}（项目自定义操作性分档；"
              f"官方报告以 5 个症状维度均分为主）")
    res = _result("GSRS", "胃肠道症状评定量表（GSRS）", total if vals else None,
                  len(vals), max(0, 15 - len(vals)), 15, 75, interp)
    res["subscales"] = subscales
    return res


# ── VSI ───────────────────────────────────────────────────────────────────

def score_vsi(items):
    """
    VSI：15 题，六级计分。
    本问卷选项为 1–6，故原始总分域 15–90；
    官方为 0–6 计分（0–90），此处同时给出归一化分（原始 − 15）以便套用官方切点。
    "我相信偶尔的肠胃问题不会影响长期健康。" 为正向条目，已反向计分。
    """
    vals = []
    n_missing = 0
    for it in items:
        scale, comp, rev = classify_item(it.get("question", ""))
        if scale != "VSI":
            continue
        s = _option_score(it.get("answer_text"), OPT_1_6_VSI)
        if s is None:
            n_missing += 1
            continue
        if rev:
            s = _reverse(s, OPT_1_6_VSI)
        vals.append(s)
    total = sum(vals) if vals else 0
    normalized = total - 15 if vals else 0
    band = _band(total, VSI_BANDS_CUSTOM) if vals else "—"
    interp = (f"VSI 总分 {total}/90（原始六点计分，域 15–90；归一化 0–90 分 = {normalized}）— {band}"
              f"（项目自定义操作性分档；文献常用归一化切点 40）")
    res = _result("VSI", "内脏敏感指数（VSI）", total if vals else None,
                  len(vals), max(0, 15 - len(vals)), 15, 90, interp)
    res["normalized_0_90"] = normalized if vals else None
    return res


# ── BMI ───────────────────────────────────────────────────────────────────

def compute_bmi(height_cm, weight_kg):
    """中国成人体重判定（WS/T 428-2013）：<18.5 偏瘦；18.5–23.9 正常；24–27.9 超重；≥28 肥胖。"""
    h = _to_number(height_cm)
    w = _to_number(weight_kg)
    if not h or not w or h <= 0 or w <= 0 or h < 80 or h > 250 or w < 20 or w > 400:
        return None
    bmi = round(w / ((h / 100.0) ** 2), 1)
    if bmi < 18.5:
        cat, detail = "偏瘦", "<18.5"
    elif bmi < 24.0:
        cat, detail = "正常", "18.5–23.9"
    elif bmi < 28.0:
        cat, detail = "超重", "24.0–27.9"
    else:
        cat, detail = "肥胖", "≥28.0"
    return {
        "total": bmi, "category": cat, "n_items": 2,
        "interpretation": f"BMI {bmi} — 体重{cat}（{detail}，中国标准 WS/T 428-2013）",
    }


# ════════════════════════════════════════════════════════════════════════════
#  统一入口
# ════════════════════════════════════════════════════════════════════════════

SCORERS = {
    "PSS-14": score_pss14,
    "GAD-7": score_gad7,
    "PHQ-9": score_phq9,
    "PSQI": score_psqi,
    "IPAQ-S": score_ipaq,
    "WHOQOL-BREF": score_whoqol,
    "DEBQ": score_debq,
    "GSRS": score_gsrs,
    "VSI": score_vsi,
}

SCALE_NAME_ZH = {
    "PSS-14": "感知压力量表（PSS-14）",
    "GAD-7": "广泛性焦虑障碍量表（GAD-7）",
    "PHQ-9": "患者健康问卷抑郁量表（PHQ-9）",
    "PSQI": "匹兹堡睡眠质量指数（PSQI）",
    "IPAQ-S": "国际体力活动问卷（IPAQ-S）",
    "WHOQOL-BREF": "世界卫生组织生活质量简表（WHOQOL-BREF）",
    "DEBQ": "荷兰饮食行为问卷（DEBQ）",
    "GSRS": "胃肠道症状评定量表（GSRS）",
    "VSI": "内脏敏感指数（VSI）",
}


def group_items(answers):
    """
    把「题干 → 答案」映射归入各量表。
    answers: dict{question_title: answer_text} 或 list[{question, answer_text}]
    返回 (grouped, unknown)；grouped = {scale: [ {question, answer_text} ]}
    """
    if isinstance(answers, dict):
        pairs = [{"question": k, "answer_text": v} for k, v in answers.items()]
    else:
        pairs = list(answers)

    grouped = {}
    unknown = []
    for p in pairs:
        title = p.get("question", "")
        scale, _, _ = classify_item(title)
        if scale in ("_NON_SCORING",):
            continue
        if scale == "_UNKNOWN":
            unknown.append(title)
            continue
        grouped.setdefault(scale, []).append(p)
    return grouped, unknown


def score_all(answers, basic_info=None):
    """
    对一份答卷计全部分值。
    返回 {scale: result_dict, ..., "BMI": {...}, "_meta": {...}}
    """
    grouped, unknown = group_items(answers)
    out = {}
    for scale, scorer in SCORERS.items():
        items = grouped.get(scale, [])
        out[scale] = scorer(items)
    if basic_info:
        bmi = compute_bmi(basic_info.get("身高（cm）", basic_info.get("身高")),
                          basic_info.get("体重（kg）", basic_info.get("体重")))
        if bmi:
            out["BMI"] = bmi
    out["_meta"] = {
        "computed_at": datetime.now().isoformat(timespec="seconds"),
        "scoring_version": "2.0-official-rules",
        "unknown_items": unknown,
        "unknown_item_count": len(unknown),
    }
    return out


# ── 自检 ──────────────────────────────────────────────────────────────────

def coverage_report(answers):
    """覆盖率报告：每个量表识别到多少题、期望多少题。"""
    grouped, unknown = group_items(answers)
    expected = {"PSS-14": 14, "GAD-7": 7, "PHQ-9": 9, "PSQI": 18, "IPAQ-S": 7,
                "WHOQOL-BREF": 26, "DEBQ": 33, "GSRS": 15, "VSI": 15}
    rep = {}
    for s, n in expected.items():
        got = sum(1 for it in grouped.get(s, [])
                  if classify_item(it.get("question", ""))[1] not in ("extra", "overall100"))
        rep[s] = {"recognized": got, "expected": n, "coverage": round(got / n, 2) if n else None}
    rep["_unknown"] = len(unknown)
    return rep, unknown


if __name__ == "__main__":
    import json as _json
    demo = {q: "从不" for q in _PSS_NONREV + _PSS_REV}
    print(_json.dumps(score_pss14([{"question": k, "answer_text": v} for k, v in demo.items()]),
                      ensure_ascii=False, indent=2))
