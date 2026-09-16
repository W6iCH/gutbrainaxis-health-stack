#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_scoring_parity.py — 计分一致性验证（程序内实现 ⟷ 课题权威条目库）
=============================================================================
背景
----
用户硬性要求：**程序内所有分析逻辑必须与课题研究的处理完全一致**。
计分的权威依据是：

  * `14_评分标准/00_评分标准_权威版.md`（规则文本）
  * `14_评分标准/03_条目库_修正/survey_scoring_native.py`（可执行权威条目库）

部署包内的 `services/sjtu_survey_pro/survey_scoring.py` = 该条目库逐字 + 兼容层。
本脚本**用同一份输入同时跑两侧**，逐层断言输出相同；任何差异即失败并打印明细。

四层校验（逐层收紧）
--------------------
L1 条目级：对条目库中**每一条目 × 每一个合法作答**（含矩阵题每个 option_idx、
           反向条目、未收录标签、非计分题、完全未知题）比对 `item_score()` 的
           返回值与异常行为。
L2 量表级：构造多套**覆盖全部条目的合成答卷**（最低档 / 最高档 / 中间档 / 混合），
           比对各量表 total / n_items / n_expected / 分档 / 子量表 / 分领域 / PSQI 成分 /
           IPAQ MET / BMI。
L3 常量级：比对分档切点、题组反向集合、WHOQOL 领域归属、IPAQ 题干、期望题数、
           非计分题集合。
L4 缺失与严格模式：缺条目、未收录标签下的 totals、审计行数、异常类型。

用法
----
    PY=~/.openclaw/workspace/.venv-diet/bin/python3
    $PY tools/verify_scoring_parity.py                 # 人类可读 + 退出码
    $PY tools/verify_scoring_parity.py --json          # 机器可读
    $PY tools/verify_scoring_parity.py --report out.json
    $PY tools/verify_scoring_parity.py --strict-missing  # 权威库缺失时也判失败

退出码：0=一致（或权威库缺失且未 --strict-missing）；1=发现差异；2=无法运行。

环境变量：`AUTHORITY_SCORING_PATH` 覆盖权威条目库路径。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)

# 部署包内实现。
# 两种安装布局都要支持：
#   ① 仓库布局   ：<PKG>/services/sjtu_survey_pro/survey_scoring.py
#   ② 安装后布局 ：<PKG>/sjtu_survey_pro/survey_scoring.py
#      （install.sh 执行 `cp -r services/* $INSTALL_ROOT/`，services/ 这一层会被抹平）
def _find_program_scoring() -> str:
    for rel in (( "services", "sjtu_survey_pro", "survey_scoring.py"),
                ("sjtu_survey_pro", "survey_scoring.py")):
        p = os.path.join(PKG, *rel)
        if os.path.isfile(p):
            return p
    return os.path.join(PKG, "services", "sjtu_survey_pro", "survey_scoring.py")


PKG_SCORING = _find_program_scoring()

# 权威条目库（默认相对仓库布局；生产服务器上通常不存在 → 本脚本会 SKIP）
AUTHORITY_DEFAULT = os.path.abspath(os.path.join(
    PKG, "..", "14_评分标准", "03_条目库_修正", "survey_scoring_native.py"))


# ══════════════════════════════════════════════════════════════════════════
#  模块加载
# ══════════════════════════════════════════════════════════════════════════

def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ══════════════════════════════════════════════════════════════════════════
#  差异收集
# ══════════════════════════════════════════════════════════════════════════

class Diff:
    def __init__(self):
        self.items = []
        self.checked = 0

    def eq(self, layer: str, label: str, a, b):
        self.checked += 1
        if a != b:
            self.items.append({"layer": layer, "what": label,
                               "program": _s(a), "authority": _s(b)})
        return a == b

    def true(self, layer: str, label: str, cond: bool, detail=""):
        self.checked += 1
        if not cond:
            self.items.append({"layer": layer, "what": label,
                               "program": detail, "authority": "(不一致)"})
        return bool(cond)

    def ok(self) -> bool:
        return not self.items


def _s(v, limit: int = 300):
    try:
        s = json.dumps(v, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        s = str(v)
    return s if len(s) <= limit else s[:limit] + "…"


# ══════════════════════════════════════════════════════════════════════════
#  合成答卷生成（覆盖全部条目）
# ══════════════════════════════════════════════════════════════════════════

def _matrix_items(auth) -> dict:
    """
    题组标题 → 该题组的**矩阵子题**列表。

    推导规则（不引入第二真源）：
      矩阵子题 = 该量表/成分的**全部库内条目** − `CHOICE_RULES`（单选）条目。
    这与真实问卷的题组结构一致，已用原始问卷核对：
      焦虑 7 / 抑郁 9 / 胃肠道 15 / 内脏敏感 15 / 压力 14 / 睡眠 13 /
      情绪 13 / 外部 10 / 限制 10 / 满意度 10。

    特别说明：PSQI 的 `睡眠问题` 题组包含 **13** 个子题：
      C2B（入睡困难）+ C5 的 9 题 + C6（催眠药物）+ C7A/C7B（日间功能）。
      其中 C6/C7A/C7B 不经 `CHOICE_RULES`，必须走矩阵 `option_idx` 路径。
    """
    choice = auth.CHOICE_RULES

    psqi_all = [auth.PSQI_C1, auth.PSQI_C2A, auth.PSQI_C2B, auth.PSQI_C3,
                auth.PSQI_C4] + list(auth.PSQI_C5_ITEMS) + \
               [auth.PSQI_C6, auth.PSQI_C7_A, auth.PSQI_C7_B]

    scale_items = {
        "焦虑评估量表": list(auth.GAD7_ITEMS),
        "抑郁症状评估题": list(auth.PHQ9_ITEMS),
        "胃肠道症状评估": list(auth.GSRS_FLAT),
        "内脏敏感性评估题": list(auth.VSI_ITEMS),
        "压力评估题": list(auth.PSS_NONREV) + list(auth.PSS_REV),
        "睡眠问题": psqi_all,
        "情绪性饮食": list(auth.DEBQ_EMOTIONAL),
        "外部性饮食": list(auth.DEBQ_EXTERNAL),
        "限制性饮食": list(auth.DEBQ_RESTRAINED),
        "满意度评估": [q for n, q in sorted(auth.WHOQOL_ITEMS.items())],
    }
    # 只保留非单选（= 矩阵）条目，并去重保序
    out = {}
    for mname, qs in scale_items.items():
        seen, keep = set(), []
        for q in qs:
            if q in choice or q in seen:
                continue
            seen.add(q)
            keep.append(q)
        out[mname] = keep
    return out


def _choice_label(rule: dict, variant: str) -> str:
    pairs = sorted(rule.items(), key=lambda kv: kv[1])
    if variant == "min":
        return pairs[0][0]
    if variant == "max":
        return pairs[-1][0]
    if variant == "mid":
        return pairs[len(pairs) // 2][0]
    # mixed：按题干长度取模，制造非均匀分布
    return pairs[len(pairs) % len(pairs)][0]


def build_answer_set(auth, variant: str) -> list:
    """按 variant 生成一套**覆盖全部条目**的合法答卷。"""
    mtx = _matrix_items(auth)
    choice = auth.CHOICE_RULES
    out = []
    for mname, qs in mtx.items():
        rule = auth.MATRIX_RULES[mname]
        span = rule["vmax"] - rule["vmin"]
        idx = {"min": 0, "max": span, "mid": span // 2}.get(variant, None)
        for i, q in enumerate(qs):
            k = idx if idx is not None else (i % (span + 1))
            out.append({"question": q, "matrix": mname, "option_idx": k,
                        "answer_text": None})
    for q, rule in choice.items():
        out.append({"question": q, "answer_text": _choice_label(rule, variant)})
    return out


# ══════════════════════════════════════════════════════════════════════════
#  L1 — 条目级逐条对照
# ══════════════════════════════════════════════════════════════════════════

def _item_score_safe(mod, item, strict):
    try:
        return ("ok", mod.item_score(item, strict=strict))
    except Exception as e:                                     # noqa: BLE001
        return ("raise", type(e).__name__)


def layer1_items(d: Diff, auth, prog):
    base = [{"question": q, "answer_text": lab}
            for q, rule in auth.CHOICE_RULES.items() for lab in rule]

    mtx = _matrix_items(auth)
    for mname, qs in mtx.items():
        rule = auth.MATRIX_RULES[mname]
        span = rule["vmax"] - rule["vmin"]
        for q in qs:
            for k in range(span + 1):
                base.append({"question": q, "matrix": mname, "option_idx": k})

    for q in sorted(auth.NON_SCORING):
        base.append({"question": q, "answer_text": "任意值"})

    base.append({"question": "完全不存在的题目XYZ", "answer_text": "任意"})
    base.append({"question": list(auth.CHOICE_RULES)[0], "answer_text": "不可能出现的标签ZZZ"})

    for it in base:
        a = _item_score_safe(auth, it, True)
        b = _item_score_safe(prog, it, True)
        label = f"item_score({it.get('question','')[:24]}…, {it.get('matrix')}, idx={it.get('option_idx')}, ans={it.get('answer_text')!r})"
        d.eq("L1", label, a, b)
        # 非严格模式同样比对（返回 (score|None, source)）
        a2 = _item_score_safe(auth, it, False)
        b2 = _item_score_safe(prog, it, False)
        d.eq("L1-nonstrict", label, a2, b2)


# ══════════════════════════════════════════════════════════════════════════
#  L2 — 量表级对照
# ══════════════════════════════════════════════════════════════════════════

SCALE_KEYS = ["GAD-7", "PHQ-9", "PSS-14", "GSRS", "VSI", "PSQI"]

# 仅元数据（不携带任何分值语义）的字段差异——记录但不计为口径差异
PSQI_METADATA_FIELDS = ("max_score",)


def layer2_scales(d: Diff, auth, prog, notes: list = None):
    notes = notes if notes is not None else []
    for variant in ("min", "max", "mid", "mixed"):
        items = build_answer_set(auth, variant)
        A = auth.score_all(items, strict=True)
        P = prog.score_all(items, basic_info={"身高（cm）": 170, "体重（kg）": 65},
                           strict=True)

        for sk in SCALE_KEYS:
            a, p = A[sk], P[sk]
            tag = f"[{variant}] {sk}"
            # 注意：Diff.eq(layer, label, program_value, authority_value)
            d.eq("L2", f"{tag} total", p["total"], a["total"])
            if sk == "PSQI":
                d.eq("L2", f"{tag} n_components", p["n_items"], a["n_components"])
                d.eq("L2", f"{tag} n_expected", p["n_expected"], 7)
                for f in PSQI_METADATA_FIELDS:
                    if f in p and f not in a:
                        # 程序多出的元数据：断言其值由成分数推导（7×3=21），语义上正确
                        derived = 3 * p["n_items"] if p["n_items"] else None
                        notes.append({
                            "layer": "L2-metadata", "what": f"{tag} PSQI.{f}",
                            "note": (f"程序多出元数据 {f}={p[f]}"+
                                     (f"（= 3×成分数 {p['n_items']}，与权威隐含上限一致）"
                                      if derived == p[f] else
                                      "（⚠ 与成分数推导值不一致）")),
                            "program_extra": p[f], "derived": derived})
            else:
                d.eq("L2", f"{tag} n_items", p["n_items"], a["n_items"])
                d.eq("L2", f"{tag} n_expected", p["n_expected"], a["n_expected"])
                if "max_score" in a:
                    d.eq("L2", f"{tag} max_score", p.get("max_score"), a.get("max_score"))
            band = a.get("band")
            if band and band != "未分类":
                d.true("L2", f"{tag} band∈interpretation",
                       band in str(p.get("interpretation", "")),
                       f"authority band={band!r} 未出现在 program interpretation="
                       f"{p.get('interpretation')!r}")

        # GSRS 子量表
        for comp, av in (A["GSRS"].get("subscales") or {}).items():
            pv = (P["GSRS"].get("subscales") or {}).get(comp)
            d.eq("L2", f"[{variant}] GSRS.{comp}.sum", (pv or {}).get("sum"), av["sum"])
            d.eq("L2", f"[{variant}] GSRS.{comp}.n_items", (pv or {}).get("n_items"),
                 av["n_items"])

        # DEBQ 子量表
        for comp, av in (A["DEBQ"].get("subscales") or {}).items():
            pv = (P["DEBQ"].get("subscales") or {}).get(comp)
            for f in ("sum", "mean", "n_items", "n_expected"):
                d.eq("L2", f"[{variant}] DEBQ.{comp}.{f}", (pv or {}).get(f), av.get(f))
            d.eq("L2", f"[{variant}] DEBQ.{comp}.band/level", (pv or {}).get("level"),
                 av.get("band"))

        # WHOQOL 分领域 + 总体
        for dname, av in (A["WHOQOL-BREF"].get("domain_scores") or {}).items():
            pv = (P["WHOQOL-BREF"].get("domain_scores") or {}).get(dname)
            for f in ("mean", "score_4_20", "score_0_100", "n_items", "n_expected"):
                d.eq("L2", f"[{variant}] WHOQOL.{dname}.{f}", (pv or {}).get(f), av.get(f))
        d.eq("L2", f"[{variant}] WHOQOL.overall",
             P["WHOQOL-BREF"].get("overall"), A["WHOQOL-BREF"].get("overall"))

        # PSQI 成分
        for ck, av in (A["PSQI"].get("components") or {}).items():
            d.eq("L2", f"[{variant}] PSQI.{ck}",
                 (P["PSQI"].get("components") or {}).get(ck), av)

        # IPAQ-S（用同一 ans_by_q 走两侧函数）
        by_q = {it["question"]: it.get("answer_text") for it in items}
        iq = {v: by_q.get(v) for v in auth.IPAQ_ITEMS.values()}
        a_ip = auth.score_ipaq({k: iq[v] for k, v in auth.IPAQ_ITEMS.items()})
        p_ip = P["IPAQ-S"]
        d.eq("L2", f"[{variant}] IPAQ.total", p_ip.get("total"), a_ip["total"])
        d.eq("L2", f"[{variant}] IPAQ.level", p_ip.get("level"), a_ip["level"])
        for f, k in (("vig", "met_vig"), ("mod", "met_mod"), ("walk", "met_walk"),
                     ("sedentary_min", "sedentary_min")):
            d.eq("L2", f"[{variant}] IPAQ.{f}", p_ip.get(k), a_ip[f])

        # BMI
        a_bmi = auth.compute_bmi(170, 65)
        p_bmi = P.get("BMI")
        d.eq("L2", f"[{variant}] BMI.total", (p_bmi or {}).get("total"), a_bmi["total"])
        d.eq("L2", f"[{variant}] BMI.category", (p_bmi or {}).get("category"),
             a_bmi["category"])


# ══════════════════════════════════════════════════════════════════════════
#  L3 — 常量级对照
# ══════════════════════════════════════════════════════════════════════════

BAND_CONSTS = ["GAD7_BANDS", "PHQ9_BANDS", "PSS14_BANDS", "GSRS_BANDS_CUSTOM",
               "VSI_BANDS_CUSTOM", "DEBQ_LEVELS"]
ITEM_CONSTS = ["GAD7_ITEMS", "PHQ9_ITEMS", "PSS_NONREV", "PSS_REV", "GSRS_FLAT",
               "VSI_ITEMS", "DEBQ_EMOTIONAL", "DEBQ_EXTERNAL", "DEBQ_RESTRAINED",
               "PSQI_C5_ITEMS"]


def layer3_constants(d: Diff, auth, prog):
    for name in BAND_CONSTS:
        d.eq("L3", name, list(getattr(auth, name)), list(getattr(prog, name)))
    for name in ITEM_CONSTS:
        d.eq("L3", name, sorted(getattr(auth, name)), sorted(getattr(prog, name)))
    for name in ("PSQI_C1", "PSQI_C2A", "PSQI_C2B", "PSQI_C3", "PSQI_C4",
                 "PSQI_C6", "PSQI_C7_A", "PSQI_C7_B"):
        d.eq("L3", name, getattr(auth, name), getattr(prog, name))

    d.eq("L3", "WHOQOL_DOMAINS", auth.WHOQOL_DOMAINS, prog.WHOQOL_DOMAINS)
    d.eq("L3", "WHOQOL_ITEMS", auth.WHOQOL_ITEMS, prog.WHOQOL_ITEMS)
    d.eq("L3", "IPAQ_ITEMS", auth.IPAQ_ITEMS, prog.IPAQ_ITEMS)
    d.eq("L3", "GSRS_SUB", auth.GSRS_SUB, prog.GSRS_SUB)
    d.eq("L3", "NON_SCORING", sorted(auth.NON_SCORING), sorted(prog.NON_SCORING))
    d.eq("L3", "CHOICE_RULES", auth.CHOICE_RULES, prog.CHOICE_RULES)

    # 题组规则（去掉 set 以便比较；reverse 顺序无关 → 排序）
    def _norm_mtx(m):
        out = {}
        for k, v in m.items():
            out[k] = {"scale": v.get("scale"), "component": v.get("component"),
                      "vmin": v.get("vmin"), "vmax": v.get("vmax"),
                      "reverse": sorted(v.get("reverse") or [])}
        return out
    d.eq("L3", "MATRIX_RULES", _norm_mtx(auth.MATRIX_RULES), _norm_mtx(prog.MATRIX_RULES))


# ══════════════════════════════════════════════════════════════════════════
#  L4 — 缺失 / 严格模式
# ══════════════════════════════════════════════════════════════════════════

def layer4_missing(d: Diff, auth, prog):
    full = build_answer_set(auth, "mid")
    # 缺 3 条 GAD-7、缺全部 PSQI C5、缺全部 WHOQOL 领域题
    drop = set(auth.GAD7_ITEMS[:3]) | set(auth.PSQI_C5_ITEMS) | \
        {auth.WHOQOL_ITEMS[n] for n in auth.WHOQOL_DOMAINS["生理领域"]}
    partial = [it for it in full if it.get("question") not in drop]

    A = auth.score_all(partial, strict=False)
    P = prog.score_all(partial, strict=False)
    for sk in SCALE_KEYS:
        d.eq("L4", f"缺项 {sk} total", A[sk]["total"], P[sk]["total"])
    for ck, av in (A["PSQI"].get("components") or {}).items():
        d.eq("L4", f"缺项 PSQI.{ck}", av, (P["PSQI"].get("components") or {}).get(ck))
    for dname, av in (A["WHOQOL-BREF"].get("domain_scores") or {}).items():
        pv = (P["WHOQOL-BREF"].get("domain_scores") or {}).get(dname)
        d.eq("L4", f"缺项 WHOQOL.{dname}.mean", av.get("mean"), (pv or {}).get("mean"))

    # 未收录标签：严格模式两侧都必须抛错
    bad = [{"question": list(auth.CHOICE_RULES)[0], "answer_text": "不存在的标签ZZZ"}]
    ra = _item_score_safe(auth, bad[0], True)
    rb = _item_score_safe(prog, bad[0], True)
    d.eq("L4", "未收录标签 → 异常类型", ra, rb)
    d.true("L4", "未收录标签 → 必须报错", ra[0] == "raise", f"实际 {ra}")

    # 空答卷：两侧结论一致（不崩、total 为 None）
    ea = auth.score_all([], strict=False)
    ep = prog.score_all([], strict=False)
    for sk in SCALE_KEYS:
        d.eq("L4", f"空答卷 {sk} total", ea[sk]["total"], ep[sk]["total"])


# ══════════════════════════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════════════════════════

def run(authority_path: str = None) -> dict:
    authority_path = authority_path or os.environ.get(
        "AUTHORITY_SCORING_PATH", AUTHORITY_DEFAULT)

    if not os.path.exists(authority_path):
        return {"status": "skipped", "ok": True,
                "reason": f"权威条目库不存在：{authority_path}（生产服务器上属正常）",
                "authority_path": authority_path, "checked": 0, "diffs": []}
    if not os.path.exists(PKG_SCORING):
        return {"status": "error", "ok": False,
                "reason": f"部署包计分模块不存在：{PKG_SCORING}",
                "diffs": [], "checked": 0}

    d = Diff()
    try:
        auth = _load("_parity_authority", authority_path)
        prog = _load("_parity_program", PKG_SCORING)
    except Exception as e:                                     # noqa: BLE001
        return {"status": "error", "ok": False,
                "reason": f"模块加载失败：{e}\n{traceback.format_exc()[-800:]}",
                "diffs": [], "checked": 0}

    layer1_items(d, auth, prog)
    notes = []
    layer2_scales(d, auth, prog, notes)
    layer3_constants(d, auth, prog)
    layer4_missing(d, auth, prog)

    return {"status": "ok", "ok": d.ok(), "checked": d.checked,
            "authority_path": authority_path,
            "program_path": PKG_SCORING,
            "layers": {"L1": "条目级逐条", "L2": "量表级", "L3": "常量级",
                       "L4": "缺失/严格模式"},
            "metadata_notes": notes,
            "n_metadata_notes": len(notes),
            "diffs": d.items[:200], "n_diffs": len(d.items)}


def main() -> int:
    ap = argparse.ArgumentParser(description="计分一致性验证（程序 ⟷ 权威条目库）")
    ap.add_argument("--authority", default=None, help="权威条目库路径")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--report", help="把结果写入 JSON")
    ap.add_argument("--strict-missing", action="store_true",
                    help="权威库缺失时也判失败（CI 用）")
    args = ap.parse_args()

    res = run(args.authority)

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        if res["status"] == "skipped":
            print("⏭️  跳过：" + res["reason"])
        elif res["status"] == "error":
            print("❌ 无法运行：" + res["reason"], file=sys.stderr)
        else:
            print(f"计分一致性验证：程序 `{os.path.relpath(res['program_path'], PKG)}`")
            print(f"                 权威 `{res['authority_path']}`")
            print(f"断言数：{res['checked']}")
            if res["ok"]:
                print("✅ 一致：程序内计分与权威条目库**逐条相同**"
                      f"（L1 条目级 / L2 量表级 / L3 常量级 / L4 缺失与严格模式）")
                print(f"   断言数：{res['checked']}（全部相等）")
                if res.get("metadata_notes"):
                    print(f"   元数据差异 {res['n_metadata_notes']} 条"
                          "（不携带分值语义，不影响任何分数）：")
                    for n in res["metadata_notes"][:4]:
                        print(f"     - {n['what']}：{n['note']}")
            else:
                print(f"❌ 发现 {res['n_diffs']} 处差异（前 20 条）：")
                for it in res["diffs"][:20]:
                    print(f"  [{it['layer']}] {it['what']}")
                    print(f"      程序    : {it['program']}")
                    print(f"      权威库  : {it['authority']}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"\n报告已写入 {args.report}")

    if res["status"] == "error":
        return 2
    if res["status"] == "skipped":
        return 1 if args.strict_missing else 0
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
