#!/usr/bin/env python3
"""
逐量表单元测试 — 修正版计分核心（survey_scoring.py）
============================================================
运行:
    python3 test_scoring.py            # 需与 survey_scoring.py 同级的导入路径
    python3 -m pytest test_scoring.py  # 亦兼容 pytest

覆盖范围
--------
1. PSS-14  反向条目极值（最低分/最高分构造）、分档边界
2. GAD-7 / PHQ-9  阈值边界（4/5, 9/10, 14/15, 19/20）
3. PSQI    7 成分算法、潜伏期与睡眠时长键不冲突、全好/全差极值
4. WHOQOL-BREF  反向条目（第 3/4/26 题）、四领域分、0–100 总分不混入领域分
5. DEBQ    三亚量表归属、外部性饮食反向条目、1–5 量纲
6. IPAQ-S  MET 计算与截断（天数≤7、单日≤180 分钟）、非数字答案不静默归 0
7. GSRS    量程 15–75（原代码错误上界 105）
8. VSI     量程 15–90（原代码错误上界 105/75）、正向条目反向
9. BMI     中国标准（WS/T 428-2013）四档边界
10. 缺项处理：单条缺失、超 20% 缺失标记 invalid、全缺失
11. 量表识别：食物频率项（"咖啡"）不得误归 DEBQ；PHQ-9 条目不得误归 PSQI/WHOQOL
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "services", "sjtu_survey_pro"))

import survey_scoring as S  # noqa: E402

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} :: {detail}")
        print(f"  ❌ {name}  {detail}")


def mk(pairs):
    """构造条目列表：[(题干, 答案), ...]。"""
    return [{"question": q, "answer_text": a} for q, a in pairs]


def fill(texts, answer):
    return mk([(t, answer) for t in texts])


# ════════════════════════════════════════════════════════════════════════════
print("\n=== 1. PSS-14 反向计分 ===")
# 最高压力：负面条目全选"总是"，正面（反向）条目全选"从不"
high = mk([(q, "总是") for q in S._PSS_NONREV] + [(q, "从不") for q in S._PSS_REV])
r = S.score_pss14(high)
check("全高压力 = 56", r["total"] == 56, f"got {r['total']}")
check("全高压力分档=高压力", "高压力" in r["interpretation"], r["interpretation"])
check("n_items=14", r["n_items"] == 14, str(r["n_items"]))

low = mk([(q, "从不") for q in S._PSS_NONREV] + [(q, "总是") for q in S._PSS_REV])
r = S.score_pss14(low)
check("全低压力 = 0", r["total"] == 0, f"got {r['total']}")
check("全低压力分档=低压力", "低压力" in r["interpretation"], r["interpretation"])

# 若不做反向计分，high 场景会得到 8*4 + 6*0 = 32（错误值），验证反向确实生效
check("反向计分确实生效（非朴素加总 32）", S.score_pss14(high)["total"] != 32)

# 分档边界 18/19 与 37/38
check("PSS 分档边界函数", S._band(18, S.PSS14_BANDS) == "低压力"
      and S._band(19, S.PSS14_BANDS) == "中等压力"
      and S._band(37, S.PSS14_BANDS) == "中等压力"
      and S._band(38, S.PSS14_BANDS) == "高压力")

# ════════════════════════════════════════════════════════════════════════════
print("\n=== 2. GAD-7 / PHQ-9 阈值 ===")
for total, expect in [(0, "无焦虑症状"), (4, "无焦虑症状"), (5, "轻度焦虑"),
                      (9, "轻度焦虑"), (10, "中度焦虑"), (14, "中度焦虑"),
                      (15, "重度焦虑"), (21, "重度焦虑")]:
    check(f"GAD-7 {total} → {expect}", S._band(total, S.GAD7_BANDS) == expect)

for total, expect in [(4, "无抑郁症状"), (5, "轻度抑郁"), (9, "轻度抑郁"),
                      (10, "中度抑郁"), (14, "中度抑郁"), (15, "中重度抑郁"),
                      (19, "中重度抑郁"), (20, "重度抑郁"), (27, "重度抑郁")]:
    check(f"PHQ-9 {total} → {expect}", S._band(total, S.PHQ9_BANDS) == expect)

r = S.score_gad7(fill(S._GAD7_ITEMS, "几乎每天"))
check("GAD-7 全满 = 21", r["total"] == 21, str(r["total"]))
r = S.score_phq9(fill(S._PHQ9_ITEMS, "几乎每天"))
check("PHQ-9 全满 = 27", r["total"] == 27, str(r["total"]))
check("PHQ-9 条目数 = 9", r["n_items"] == 9, str(r["n_items"]))

# ════════════════════════════════════════════════════════════════════════════
print("\n=== 3. PSQI 官方 7 成分 ===")
good = mk([
    (S._PSQI_C1, "非常好"),
    (S._PSQI_C2A, "少于 15 分钟"),
    (S._PSQI_C2B, "没有"),
    (S._PSQI_C3, "大于 7 小时"),
    (S._PSQI_C4, "大于 85%"),
    (S._PSQI_C6, "没有"),
    (S._PSQI_C7_A, "没有"),
    (S._PSQI_C7_B, "没有"),
] + [(q, "没有") for q in S._PSQI_C5_ITEMS])
r = S.score_psqi(good)
check("PSQI 全好 = 0", r["total"] == 0, str(r["total"]))
check("PSQI 成分数 = 7", len([k for k, v in r["components"].items()
                              if not k.endswith("明细") and not k.endswith("数据来源") and v is not None]) == 7,
      str(r["components"]))
check("PSQI 全好 → 睡眠质量尚可", "尚可" in r["interpretation"], r["interpretation"])

bad = mk([
    (S._PSQI_C1, "很差"),
    (S._PSQI_C2A, "> 60 分钟"),
    (S._PSQI_C2B, "每周 3 次或更多"),
    (S._PSQI_C3, "少于 5 小时"),
    (S._PSQI_C4, "小于 65%"),
    (S._PSQI_C6, "每周 3 次或更多"),
    (S._PSQI_C7_A, "每周 3 次或更多"),
    (S._PSQI_C7_B, "每周 3 次或更多"),
] + [(q, "每周 3 次或更多") for q in S._PSQI_C5_ITEMS])
r = S.score_psqi(bad)
check("PSQI 全差 = 21", r["total"] == 21, str(r["total"]))
check("PSQI 全差 → 睡眠质量差", "睡眠质量差" in r["interpretation"], r["interpretation"])

# 键冲突回归：睡眠时长答案不得命中"入睡耗时"映射
mixed = mk([(S._PSQI_C1, "非常好"), (S._PSQI_C3, "少于 5 小时"),
            (S._PSQI_C2A, "少于 15 分钟"),
            (S._PSQI_C4, "大于 85%"), (S._PSQI_C6, "没有"),
            (S._PSQI_C7_A, "没有"), (S._PSQI_C7_B, "没有")]
           + [(q, "没有") for q in S._PSQI_C5_ITEMS])
r = S.score_psqi(mixed)
check("睡眠时长<5h 计 3 分（未与入睡耗时键冲突）", r["components"]["C3_睡眠时长"] == 3,
      str(r["components"]))
check("入睡耗时 ≤15min 计 0 分", r["components"]["C2_明细"]["入睡耗时档分"] == 0,
      str(r["components"]))

# C2 归并：入睡耗时 2 档 + 频率 1 档 = 3 → 归并 2
r = S.score_psqi(mk([(S._PSQI_C2A, "31～60 分钟"), (S._PSQI_C2B, "每周 1-2 次")]))
check("C2 归并 (2+1=3) → 2", r["components"]["C2_睡眠潜伏期"] == 2, str(r["components"]))
r = S.score_psqi(mk([(S._PSQI_C2A, "> 60 分钟"), (S._PSQI_C2B, "每周 3 次或更多")]))
check("C2 归并 (3+2=5) → 3", r["components"]["C2_睡眠潜伏期"] == 3, str(r["components"]))

# C3 数值小时题
r = S.score_psqi(mk([(S._PSQI_C3_NUM, "8")]))
check("C3 数值 8h → 0", r["components"]["C3_睡眠时长"] == 0, str(r["components"]))
r = S.score_psqi(mk([(S._PSQI_C3_NUM, "4.5")]))
check("C3 数值 4.5h → 3", r["components"]["C3_睡眠时长"] == 3, str(r["components"]))

# ════════════════════════════════════════════════════════════════════════════
print("\n=== 4. WHOQOL-BREF 反向条目与四领域分 ===")
best = mk([(S._WHOQOL_ITEMS[n], "很满意") for n in range(1, 27)])
r = S.score_whoqol(best)
check("WHOQOL 全满意 → 4 个领域分", len(r["domain_scores"]) == 4, str(list(r["domain_scores"])))
check("WHOQOL 领域分上界 ≤100",
      all(v["score_0_100"] <= 100 for v in r["domain_scores"].values()), str(r["domain_scores"]))

# 反向：第 3/4/26 题答"极差"应折算为高（正向）分
rev_test = mk([(S._WHOQOL_ITEMS[3], "根本不妨碍"),     # 反向：5 → 1
               (S._WHOQOL_ITEMS[4], "根本不需要"),     # 反向：5 → 1
               (S._WHOQOL_ITEMS[26], "没有消极感受")])  # 反向：5 → 1
r = S.score_whoqol(rev_test)
phys = r["domain_scores"].get("生理领域", {})
check("反向条目生效（生理领域均分=1 → 0–100 = 0）",
      phys.get("mean") == 1.0, str(phys))

# 0–100 自评总分不得混入领域分
mixed = mk([(S._WHOQOL_ITEMS[n], "很满意") for n in range(1, 27)] + [(S._WHOQOL_TOTAL_ITEM, "100")])
r = S.score_whoqol(mixed)
check("0–100 自评单独呈现 = 100", r["overall_0_100"] == 100, str(r.get("overall_0_100")))
check("0–100 自评未进入领域分",
      all(v["score_0_100"] <= 100 for v in r["domain_scores"].values()), str(r["domain_scores"]))
check("总体题单独呈现（总体生存质量）", "总体生存质量" in r["overall"], str(r["overall"]))

# 领域归属：社会关系领域应为 3 题
check("社会关系领域 n_expected=3",
      r["domain_scores"]["社会关系领域"]["n_expected"] == 3,
      str(r["domain_scores"]["社会关系领域"]))

# ════════════════════════════════════════════════════════════════════════════
print("\n=== 5. DEBQ 三亚量表与反向条目 ===")
items = mk([(q, "从不") for q in S._DEBQ_EMOTIONAL + S._DEBQ_EXTERNAL + S._DEBQ_RESTRAINED])
r = S.score_debq(items)
check("DEBQ 三亚量表齐全", set(r["subscales"]) == {"情绪性饮食", "外部性饮食", "限制性饮食"},
      str(list(r["subscales"])))
check("情绪性饮食 13 题", r["subscales"]["情绪性饮食"]["n_items"] == 13,
      str(r["subscales"]["情绪性饮食"]))
check("外部性饮食 10 题", r["subscales"]["外部性饮食"]["n_items"] == 10,
      str(r["subscales"]["外部性饮食"]))
check("限制性饮食 10 题", r["subscales"]["限制性饮食"]["n_items"] == 10,
      str(r["subscales"]["限制性饮食"]))
check("1–5 量纲：全'从不' → 均分 1.0",
      r["subscales"]["情绪性饮食"]["mean"] == 1.0, str(r["subscales"]["情绪性饮食"]))
check("无 total 相加（仅子量表均分）", r["total"] is None, str(r["total"]))

# 外部性饮食反向条目："你能否抵制美味食物的诱惑" 答"从不" 应折算为 5
ext_rev = mk([("你能否抵制美味食物的诱惑？", "从不")])
r = S.score_debq(ext_rev)
check("外部性饮食反向条目生效（从不 → 5）", r["subscales"]["外部性饮食"]["sum"] == 5,
      str(r["subscales"]["外部性饮食"]))

# ════════════════════════════════════════════════════════════════════════════
print("\n=== 6. IPAQ-S MET 计算与截断 ===")
r = S.score_ipaq(mk([
    (S._IPAQ_V_DAYS, "7"), (S._IPAQ_V_MIN, "60"),
    (S._IPAQ_M_DAYS, "7"), (S._IPAQ_M_MIN, "120"),
    (S._IPAQ_W_DAYS, "7"), (S._IPAQ_W_MIN, "30"),
]))
expect = round(7 * 60 * 8.0 + 7 * 120 * 4.0 + 7 * 30 * 3.3)
check(f"MET 计算正确 = {expect}", r["total"] == expect, f"got {r['total']}")

# 截断：单日 300 分钟应封顶 180；天数 10 应封顶 7
r = S.score_ipaq(mk([(S._IPAQ_V_DAYS, "10"), (S._IPAQ_V_MIN, "300")]))
check("单日时长封顶 180 分钟且天数封顶 7",
      r["total"] == round(7 * 180 * 8.0), f"got {r['total']}")

# 非数字答案不得静默计 0 而不留痕
r = S.score_ipaq(mk([(S._IPAQ_V_DAYS, "不确定")]))
check("非数字答案记为缺失（n_missing≥1）", r["n_missing"] >= 1, str(r["n_missing"]))

check("IPAQ 三档切点：3000", S.score_ipaq(mk([(S._IPAQ_V_DAYS, "7"), (S._IPAQ_V_MIN, "60")]))
      ["activity_level"] == "高体力活动水平")
check("IPAQ 三档切点：低",
      S.score_ipaq(mk([(S._IPAQ_W_DAYS, "1"), (S._IPAQ_W_MIN, "20")]))["activity_level"] == "低体力活动水平")

# ════════════════════════════════════════════════════════════════════════════
print("\n=== 7. GSRS 量程 ===")
r = S.score_gsrs(fill(S._GSRS_FLAT, "无症状"))
check("GSRS 全无症状 = 15（下界）", r["total"] == 15, str(r["total"]))
check("GSRS max_score = 75（原代码 105 错误）", r["max_score"] == 75, str(r["max_score"]))
r = S.score_gsrs(fill(S._GSRS_FLAT, "极重度"))
check("GSRS 全极重度 = 75（上界可达）", r["total"] == 75, str(r["total"]))
check("GSRS 5 个症状维度", len(r["subscales"]) == 5, str(list(r["subscales"])))
check("GSRS 维度题数合计 = 15",
      sum(v["n_expected"] for v in r["subscales"].values()) == 15, str(r["subscales"]))
check("GSRS 75 落在重度档", "重度" in S._band(75, S.GSRS_BANDS_CUSTOM))

# ════════════════════════════════════════════════════════════════════════════
print("\n=== 8. VSI 量程与正向条目反向 ===")
r = S.score_vsi(fill(S._VSI_ITEMS, "非常不符"))
# 14 条得 1 分 + 反向条目"非常不符"(1) → 6 分 = 20；
# 全表理论下界 15 需反向条目答"非常相符"（其余答"非常不符"）。
check("VSI 全'非常不符' = 20（含反向条目折算）", r["total"] == 20, str(r["total"]))
check("VSI max_score = 90（原代码 105/75 错误）", r["max_score"] == 90, str(r["max_score"]))
check("VSI 归一化 = 5", r["normalized_0_90"] == 5, str(r["normalized_0_90"]))
theoretical_min = mk([(q, "非常相符" if q in S._VSI_REVERSE else "非常不符")
                      for q in S._VSI_ITEMS])
check("VSI 一致最低 = 15（下界可达）", S.score_vsi(theoretical_min)["total"] == 15,
      str(S.score_vsi(theoretical_min)["total"]))
# 15 题中 14 题"非常相符"(6) + 反向题"非常不符"(1→6) = 90
consistent_max = [(q, "非常不符" if q in S._VSI_REVERSE else "非常相符") for q in S._VSI_ITEMS]
r = S.score_vsi(mk(consistent_max))
check("VSI 一致最高 = 90（上界可达）", r["total"] == 90, str(r["total"]))
check("VSI 归一化上界 = 75", r["normalized_0_90"] == 75, str(r["normalized_0_90"]))
r = S.score_vsi(mk([("我相信偶尔的肠胃问题不会影响长期健康。", "非常不符")]))
check("VSI 正向条目反向生效（非常不符 → 6）", r["total"] == 6, str(r["total"]))

# ════════════════════════════════════════════════════════════════════════════
print("\n=== 9. BMI 中国标准 ===")
cases = [(165, 45.0, "偏瘦"), (170, 60.0, "正常"), (170, 72.0, "超重"), (170, 85.0, "肥胖")]
for h, w, cat in cases:
    b = S.compute_bmi(h, w)
    check(f"BMI {h}cm/{w}kg → {cat}", b and b["category"] == cat, str(b))

b = S.compute_bmi(170, 53.5)   # 18.51 → 正常（下边界）
check("BMI 18.5 边界 → 正常", b["category"] == "正常", str(b))
b = S.compute_bmi(170, 53.0)   # 18.34 → 18.3 偏瘦
check("BMI 18.3 → 偏瘦", b["category"] == "偏瘦", str(b))
b = S.compute_bmi(170, 69.0)   # 23.88 → 23.9 正常
check("BMI 23.9 → 正常", b["category"] == "正常", str(b))
b = S.compute_bmi(170, 69.4)   # 24.01 → 24.0 超重
check("BMI 24.0 边界 → 超重", b["category"] == "超重", str(b))
b = S.compute_bmi(170, 80.6)   # 27.89 → 27.9 超重
check("BMI 27.9 → 超重", b["category"] == "超重", str(b))
b = S.compute_bmi(170, 81.0)   # 28.03 → 28.0 肥胖
check("BMI 28.0 边界 → 肥胖", b["category"] == "肥胖", str(b))
check("身高异常（0）返回 None", S.compute_bmi(0, 60) is None)
check("体重非数字返回 None", S.compute_bmi(170, "未知") is None)

# ════════════════════════════════════════════════════════════════════════════
print("\n=== 10. 缺项处理 ===")
partial = mk([(q, "总是") for q in S._PSS_NONREV[:4]])   # 仅 4/14 条
r = S.score_pss14(partial)
check("部分作答：n_items 反映实际条目数", r["n_items"] == 4, str(r["n_items"]))
check("部分作答：缺失比 >20% → invalid", r["valid"] is False, str(r))
check("部分作答：给出 warning", bool(r["warning"]), str(r.get("warning")))

r = S.score_pss14([])
check("全缺失：total = None 且 invalid", r["total"] is None and r["valid"] is False, str(r))

one_missing = mk([(q, "总是") for q in S._PSS_NONREV] +
                 [(q, "从不") for q in S._PSS_REV[:5]])   # 13/14 条
r = S.score_pss14(one_missing)
check("缺 1 条：仍 valid", r["valid"] is True, str(r))
check("缺 1 条：n_missing = 1", r["n_missing"] == 1, str(r["n_missing"]))
check("缺 1 条：未按 0 计入分母（分档未虚低）", r["total"] == 8 * 4 + 5 * 4, str(r["total"]))

blank = mk([(q, "") for q in S._PSS_NONREV])
r = S.score_pss14(blank)
check("空字符串答案视为缺失", r["total"] is None, str(r))

r = S.score_gad7(mk([(S._GAD7_ITEMS[0], "几乎每天"),
                     (S._GAD7_ITEMS[1], "没有或极少 ")]))  # 带尾随空格
check("答案尾随空格仍可计分", r["n_items"] == 2, str(r))

# ════════════════════════════════════════════════════════════════════════════
print("\n=== 11. 量表识别（原 find_scale_by_title 误判回归） ===")
check("食物频率项'咖啡'不得归入 DEBQ", S.classify_item("咖啡")[0] == "_UNKNOWN",
      str(S.classify_item("咖啡")))
check("'大米及制品（精米饭 / 米粉等）'不得归入任何量表",
      S.classify_item("大米及制品（精米饭 / 米粉等）")[0] == "_UNKNOWN")
check("PHQ-9 睡眠条目不得归入 PSQI",
      S.classify_item("过去两周，您是否入睡困难、睡不安稳或睡眠过多？")[0] == "PHQ-9")
check("PHQ-9 食欲条目不得归入 WHOQOL",
      S.classify_item("过去两周，您是否有食欲不振或过度进食（吃太多）？")[0] == "PHQ-9")
check("PHQ-9 注意力条目不得归入 WHOQOL",
      S.classify_item("过去两周内，您是否对事物专注有困难，例如看书或看电视不能集中注意力？")[0] == "PHQ-9")
check("WHOQOL 睡眠满意度条目归 WHOQOL 而非 PSQI",
      S.classify_item("您对自己的睡眠情况满意吗？")[0] == "WHOQOL-BREF")
check("GSRS 症状条目（烧心）可识别", S.classify_item("烧心（胃灼热，胸骨后灼热）")[0] == "GSRS")
check("IPAQ 步行条目可识别", S.classify_item(S._IPAQ_W_DAYS)[0] == "IPAQ-S")
check("IPAQ 静坐条目可识别", S.classify_item(S._IPAQ_SIT)[0] == "IPAQ-S")
check("姓名等人口学项被显式排除", S.classify_item("姓名")[0] == "_NON_SCORING")
check("未知题干返回 _UNKNOWN", S.classify_item("完全无关的新题目")[0] == "_UNKNOWN")

# 全部量表 100% 覆盖（用条目库自检）
full_answers = {}
for q in (S._PSS_NONREV + S._PSS_REV + S._GAD7_ITEMS + S._PHQ9_ITEMS + S._GSRS_FLAT
          + S._VSI_ITEMS + S._DEBQ_EMOTIONAL + S._DEBQ_EXTERNAL + S._DEBQ_RESTRAINED
          + [_PSQI_C1 := S._PSQI_C1, S._PSQI_C2A, S._PSQI_C2B, S._PSQI_C3,
             S._PSQI_C3_NUM, S._PSQI_C4, S._PSQI_C6]
          + S._PSQI_C5_ITEMS + [S._PSQI_C7_A, S._PSQI_C7_B]
          + [_IPAQ_V_DAYS := S._IPAQ_V_DAYS, S._IPAQ_V_MIN, S._IPAQ_M_DAYS, S._IPAQ_M_MIN,
             S._IPAQ_W_DAYS, S._IPAQ_W_MIN, S._IPAQ_SIT]
          + [S._WHOQOL_ITEMS[n] for n in range(1, 27)]):
    full_answers[q] = "从不"
rep, unknown = S.coverage_report(full_answers)
for scale, v in rep.items():
    if scale == "_unknown":
        continue
    check(f"覆盖率 {scale} = 100%", v["recognized"] == v["expected"], str(v))

# ════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print(f"  测试通过 {PASS}  |  失败 {FAIL}")
if FAILURES:
    print("\n失败明细:")
    for f in FAILURES:
        print("  -", f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
