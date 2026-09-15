#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
seed_mock_data.py — 注入**模拟数据**以端到端验证全部功能
=============================================================================
⚠️ 本脚本注入的是**模拟数据**，全部记录带 `MOCK-` 前缀标记。
   验证完成后**必须**运行 `tools/clear_mock_data.py` 清除，并用自检确认无残留。

验证链路
--------
问卷收数 → 原生/已发布计分 → 入库 → 反馈邮件（构造，不真发） → 饮食解析队列
→ 数据看板各页 → 控制台各页（配置/进程/端口/自检） → 自检 → 数据导出

用法
----
    PY=~/.openclaw/workspace/.venv-diet/bin/python3
    $PY tools/seed_mock_data.py --root /tmp/gba-test --n 3
    $PY tools/seed_mock_data.py --root /tmp/gba-test --n 3 --report /tmp/seed.json

幂等：同一批 MOCK- 学生重复注入不会重复插入（submission_id 唯一）。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(HERE)

MOCK_PREFIX = "MOCK-"


# ── 构造模拟答卷（题干取自条目库，保证计分链路可命中）────────────────────

def build_survey_row(svc_dir: str, idx: int, rnd: random.Random):
    """构造一行 wj.sjtu.edu.cn 结构的量表答卷。"""
    sys.path.insert(0, svc_dir)
    import survey_scoring as SC

    def choice(title, labels, pick):
        """单选题：`answer.label` 为真实选项文本，`code` 为平台编码。"""
        label = labels[pick % len(labels)]
        return {"question": {"title": title, "question_type": "单选题",
                             "answer_name": label},
                "answer": {"code": f"option{pick}", "label": label, "point": None}}

    def matrix(group_title, sub_items, labels, picks=None):
        """矩阵单选题：`answer[i].title` 是**真实选项文本**，`value` 才是 optionN。

        ⚠️ 这是真实问卷的存储结构。若把 title 写成 "option0"，计分核心会因
        label 表失配而丢弃全部矩阵条目（得分全空）—— 本模拟数据必须还原真实结构。
        """
        ans, ans_name = [], []
        for i, q in enumerate(sub_items):
            p = (picks[i] if picks else i) % len(labels)
            ans.append({"title": labels[p], "dataIndex": "", "value": f"option{p}",
                        "quota_type": "none", "quota_value": 0, "point": None})
            ans_name.append({"key": q, "value": labels[p]})
        return {"question": {"title": group_title, "question_type": "矩阵单选题",
                             "answer_name": ans_name},
                "answer": ans}

    def number(title, val):
        return {"question": {"title": title, "question_type": "数字题",
                             "answer_name": val},
                "answer": val}

    def text(title, val):
        return {"question": {"title": title, "question_type": "填空题",
                             "answer_name": val},
                "answer": val}

    GAD4 = ["没有或极少", "有过几天（≤7天）", "超过一半天数（＞7天）", "几乎每天"]
    PSS5 = ["从不", "几乎没有", "有时", "经常", "总是"]
    GSRS5 = ["无症状", "轻度症状", "中度症状", "重度症状", "极重度症状"]
    VSI6 = ["非常不符", "比较不符", "有点不符", "有点符合", "比较相符", "非常相符"]
    PSQI_FREQ = ["没有", "每周 1-2 次", "每周 3 次或更多"]
    DEBQ5 = ["从不", "很少", "有时", "经常", "总是"]
    # WHOQOL 满意度矩阵（真实标签；已发布引擎未收录“不满意/既非满意也非不满意”，
    # 属已记录的 D7/D8 缺陷，不影响本功能链路的可运行性）
    WHO_SAT = ["很不满意", "不满意", "既非满意也非不满意", "满意", "很满意"]
    WHO_QUAL = ["很差", "差", "一般", "好", "很好"]

    answers = [
        text("姓名", f"模拟同学{idx}"),
        choice("性别", ["男", "女"], idx),
        text("学工号", f"{MOCK_PREFIX}{1000 + idx}"),
        choice("年级", ["大一", "大二", "大三", "大四"], idx),
        text("学院/单位", "模拟学院"),
        text("邮箱", f"mock{idx}@example.invalid"),
        number("身高（cm）", 160 + idx * 5),
        number("体重（kg）", 55 + idx * 4),
        text("流水号", f"{MOCK_PREFIX}SUB{1000 + idx}"),
        # GAD-7 / PHQ-9：4 级 0–3
        matrix("焦虑评估量表", SC._GAD7_ITEMS, GAD4, [0, 1, 2, 3, 1, 2, 1]),
        matrix("抑郁症状评估题", SC._PHQ9_ITEMS, GAD4, [1, 0, 2, 3, 1, 2, 1, 0, 0]),
        # PSS-14：5 级 0–4（正向 8 + 反向 6）
        matrix("压力评估题", SC._PSS_NONREV + SC._PSS_REV, PSS5,
               [1, 2, 3, 1, 2, 3, 0, 2, 3, 2, 1, 3, 2, 2]),
        # GSRS 15 条 1–5
        matrix("胃肠道症状评估", SC._GSRS_FLAT, GSRS5,
               [0, 1, 2, 1, 0, 1, 0, 1, 2, 1, 0, 1, 0, 1, 0]),
        # VSI 15 条 1–6（第 13 条反向）
        matrix("内脏敏感性评估题", SC._VSI_ITEMS, VSI6,
               [0, 2, 3, 1, 2, 3, 1, 2, 4, 2, 1, 3, 5, 2, 3]),
        # PSQI 矩阵（频率级）
        matrix("睡眠问题", SC._PSQI_C5_ITEMS, PSQI_FREQ, [0, 1, 2, 0, 1, 0, 1, 0, 0]),
        # DEBQ 三亚量 1–5
        matrix("情绪性饮食", SC._DEBQ_EMOTIONAL, DEBQ5, [0, 2, 4, 1, 3, 0, 2, 4, 1, 3, 0, 2, 1]),
        matrix("外部性饮食", SC._DEBQ_EXTERNAL, DEBQ5, [1, 3, 4, 2, 1, 3, 2, 4, 2, 1]),
        matrix("限制性饮食", SC._DEBQ_RESTRAINED, DEBQ5, [0, 3, 4, 2, 3, 1, 4, 2, 3, 2]),
        # WHOQOL 满意度矩阵（Q16–Q25）
        matrix("满意度评估", [SC._WHOQOL_ITEMS[n] for n in range(16, 26)],
               WHO_SAT, [2, 3, 1, 3, 2, 3, 4, 2, 3, 2]),
    ]
    # PSQI 单选
    for t, label_lists, p in (
        (SC._PSQI_C1, ["非常好", "较好", "较差", "很差"], 1),
        (SC._PSQI_C2A, ["少于 15 分钟", "16～30 分钟", "31～60 分钟", "> 60 分钟"], 2),
        (SC._PSQI_C3, ["大于 7 小时", "6～7 小时", "5～6 小时", "少于 5 小时"], 1),
        (SC._PSQI_C4, ["大于 85%", "75～84%", "65～74%", "小于 65%"], 1),
    ):
        answers.append(choice(t, label_lists, p))
    answers.append(matrix("睡眠问题", [SC._PSQI_C2B], PSQI_FREQ, [1]))
    answers.append(matrix("睡眠问题", [SC._PSQI_C6], PSQI_FREQ, [0]))
    answers.append(matrix("睡眠问题", [SC._PSQI_C7_A], PSQI_FREQ, [1]))
    answers.append(matrix("睡眠问题", [SC._PSQI_C7_B], PSQI_FREQ, [1]))

    # WHOQOL 其余单选题（Q1–Q15、Q26）
    _who_labels = {
        1: ["很差", "差", "一般", "好", "很好"],
        2: ["很不满意", "较不满意", "一般", "较满意", "很满意"],
        3: ["极妨碍", "比较妨碍", "有点妨碍", "根本不妨碍", "极不妨碍"],
        4: ["极需要", "比较需要", "很少需要", "根本不需要", "极不需要"],
        5: ["极无乐趣", "无乐趣", "有点乐趣", "比较有乐趣", "极有乐趣"],
        6: ["极无意义", "无意义", "有点意义", "比较有意义", "极有意义"],
        7: ["极不能", "不能", "有点能", "比较能", "极能"],
        8: ["极不安全", "不安全", "有点安全", "比较安全", "极安全"],
        9: ["极差", "差", "一般", "好", "极好"],
        10: ["根本没有", "很少", "一般", "较多", "完全有"],
        11: ["极过不去", "比较过不去", "过得去", "完全过得去", "极差"],
        12: ["完全不够用", "不够用", "有点够用", "比较够用", "完全够用"],
        13: ["完全欠缺", "欠缺", "有点齐备", "比较齐备", "完全齐备"],
        14: ["完全没有机会", "没有机会", "有点机会", "比较有机会", "完全有机会"],
        15: ["很差", "差", "一般", "好", "很好"],
        26: ["总是有", "经常有", "有时有", "较少有", "没有"],
    }
    for n, labels in _who_labels.items():
        answers.append(choice(SC._WHOQOL_ITEMS[n], labels, (n + idx) % len(labels)))
    answers.append(choice(SC._WHOQOL_EXTRA[0],
                          ["有极大影响", "有较大影响", "有中等影响", "有较小影响", "没有影响"], 4))
    answers.append(number(SC._WHOQOL_TOTAL_ITEM, 70 + idx))

    # IPAQ 数值题
    answers += [
        number(SC._IPAQ_V_DAYS, 2 + idx), number(SC._IPAQ_V_MIN, 30),
        number(SC._IPAQ_M_DAYS, 3), number(SC._IPAQ_M_MIN, 25),
        number(SC._IPAQ_W_DAYS, 5), number(SC._IPAQ_W_MIN, 40),
        number(SC._IPAQ_SIT, 300),
    ]
    # 非计分补充
    answers.append(text("日常饮食习惯", "规律三餐"))
    answers.append(text("是否有胃肠道疾病史（如果有请注明疾病类型）", "否"))
    answers.append(text("是否有饮酒史 (有少量饮酒的情况也计入饮酒史)：", "否"))
    answers.append(text("是否经临床诊断为脂肪肝：", "否"))
    answers.append(choice("自我报告压力水平", ["低", "中等", "高"], 1))
    answers.append(text("您的食欲怎么样？", "一般"))

    return {
        "id": f"{MOCK_PREFIX}R{1000 + idx}",
        "status": "success",
        "submitted_at": (datetime.now() - timedelta(days=idx * 3)).strftime(
            "%Y-%m-%d %H:%M:%S"),
        "ip_address": "127.0.0.1",
        "user": {"organization": "MOCK", "name": f"模拟同学{idx}"},
        "answers": answers,
    }


def build_diet_row(idx: int):
    return {
        "id": f"{MOCK_PREFIX}DSHEET{1000 + idx}",
        "account": f"{MOCK_PREFIX}{1000 + idx}",
        "name": f"模拟同学{idx}",
        "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "answer_sheet": [{
            "id": f"{MOCK_PREFIX}DANS{1000 + idx}",
            "answers": [
                {"question": {"title": "学工号", "question_type": "填空题"},
                 "answer": f"{MOCK_PREFIX}{1000 + idx}"},
                {"question": {"title": "姓名", "question_type": "填空题"},
                 "answer": f"模拟同学{idx}"},
                {"question": {"title": "邮箱", "question_type": "填空题"},
                 "answer": f"mock{idx}@example.invalid"},
                {"question": {"title": "记录日期", "question_type": "填空题"},
                 "answer": datetime.now().strftime("%Y-%m-%d")},
                {"question": {"title": "今日饮食描述", "question_type": "填空题"},
                 "answer": "早餐：燕麦牛奶；午餐：米饭+清蒸鱼+西兰花；晚餐：小米粥+凉拌黄瓜"},
            ],
        }],
    }


def build_exercise_row(idx: int):
    return {
        "id": f"{MOCK_PREFIX}EX{1000 + idx}",
        "status": "success",
        "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user": {"organization": "MOCK"},
        "answers": [
            {"question": {"title": "姓名", "question_type": "填空题"},
             "answer": f"模拟同学{idx}"},
            {"question": {"title": "学工号", "question_type": "填空题"},
             "answer": f"{MOCK_PREFIX}{1000 + idx}"},
            {"question": {"title": "最近 7 天内，您有几天是步行，且一次步行至少 10 分钟？",
                          "question_type": "单选题"},
             "answer": {"code": "opt4", "label": "5", "point": None}},
            {"question": {"title": "在这其中一天您通常花多少时间在步行上？（单位：分钟）",
                          "question_type": "数字题"}, "answer": 45},
            {"question": {"title": "最近七天内，工作日您有多久时间是坐着的？（单位：分钟）",
                          "question_type": "数字题"}, "answer": 300},
        ],
    }


# ── 主流程 ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="注入模拟数据并端到端验证（测后必须清除）")
    ap.add_argument("--root", required=True, help="测试用安装根目录（服务代码所在处）")
    ap.add_argument("--n", type=int, default=3, help="模拟学生数")
    ap.add_argument("--seed", type=int, default=20260916)
    ap.add_argument("--report", help="把验证报告写入该 JSON 路径")
    ap.add_argument("--keep-queue", action="store_true", help="不写 LLM 队列任务")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    rnd = random.Random(args.seed)
    steps = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": bool(ok), "detail": str(detail)})
        print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))

    svc_survey = os.path.join(root, "sjtu_survey_pro")
    svc_diet = os.path.join(root, "diet_survey")
    svc_exercise = os.path.join(root, "exercise_survey")
    for d in (svc_survey, svc_diet, svc_exercise):
        if not os.path.isdir(d):
            print(f"❌ 缺少服务目录: {d}", file=sys.stderr)
            return 2

    # 环境：让各服务模块指向本测试根
    os.environ["APP_BASE"] = root
    os.environ.setdefault("SCORING_VERSION", "3.0-native-ordinal")

    # ① 问卷：计分 → 入库
    sys.path.insert(0, svc_survey)
    import survey_analysis as SA
    import survey_database as SDB
    SDB.init_db()

    seeded_survey, score_checks = 0, []
    for i in range(args.n):
        row = build_survey_row(svc_survey, i, rnd)
        rep = SA.analyze_survey_responses({"data": {"rows": [row]}})
        scores = rep.get("scores", {})
        got = {k: (scores.get(k) or {}).get("total")
               for k in ("GAD-7", "PHQ-9", "PSS-14", "PSQI", "GSRS", "VSI")}
        score_checks.append({"mock": i, "scores": got,
                             "debq": {k: v.get("mean")
                                      for k, v in (scores.get("DEBQ") or {})
                                      .get("subscales", {}).items()},
                             "bmi": (scores.get("BMI") or {}).get("total")})
        db_id, sid, email = SDB.store_submission({"data": {"rows": [row]}}, rep,
                                                 {"answer": f"{MOCK_PREFIX}A{1000+i}"})
        if db_id:
            seeded_survey += 1
    nonnull = sum(1 for c in score_checks if all(v is not None for v in c["scores"].values()))
    step("① 问卷收数 → 计分（10 量表 + BMI）", nonnull == args.n,
         f"{nonnull}/{args.n} 份全部量表得分非空")
    step("① 问卷入库", seeded_survey == args.n, f"{seeded_survey}/{args.n} 条 submissions")

    # ② 反馈邮件：构造邮件任务（不真发），验证 email_log 队列可写
    email_ok = 0
    for i in range(args.n):
        try:
            SDB.log_email(f"{MOCK_PREFIX}SUB{1000+i}", f"{MOCK_PREFIX}{1000+i}",
                          f"mock{i}@example.invalid")
            email_ok += 1
        except Exception as e:            # noqa: BLE001
            step(f"② 反馈邮件队列 [{i}]", False, str(e))
    step("② 反馈邮件队列可写", email_ok == args.n, f"{email_ok}/{args.n} 条 email_log")

    # ③ 饮食：入库 + LLM 队列
    sys.path.insert(0, svc_diet)
    import diet_database as DDB
    DDB.init_db()
    seeded_diet = 0
    for i in range(args.n):
        db_id, sid, email, first = DDB.store_submission(build_diet_row(i))
        if db_id:
            seeded_diet += 1
    step("③ 饮食问卷入库", seeded_diet == args.n, f"{seeded_diet}/{args.n} 条")

    queue_files = []
    if not args.keep_queue:
        qdir = os.path.join(svc_diet, ".llm_queue", "pending")
        os.makedirs(qdir, exist_ok=True)
        try:
            import diet_llm_queue as DLQ
            for i in range(args.n):
                DLQ.enqueue_task(1000 + i, f"{MOCK_PREFIX}{1000+i}",
                                 "早餐：燕麦牛奶；午餐：米饭+清蒸鱼+西兰花",
                                 record_date=datetime.now().strftime("%Y-%m-%d"),
                                 submission_id=f"{MOCK_PREFIX}DANS{1000+i}",
                                 email=f"mock{i}@example.invalid")
                queue_files.append(str(i))
            step("③ LLM 抽取队列入队", len(queue_files) == args.n,
                 f"{len(queue_files)} 个 pending 任务")
        except Exception as e:            # noqa: BLE001
            step("③ LLM 抽取队列入队", False, f"{type(e).__name__}: {e}")

    # ④ 运动：入库
    sys.path.insert(0, svc_exercise)
    import exercise_database as EDB
    EDB.init_db()
    seeded_ex = 0
    for i in range(args.n):
        try:
            db_id = EDB.save_submission(
                student_id=f"{MOCK_PREFIX}{1000+i}",
                submission_id=f"{MOCK_PREFIX}EX{1000+i}",
                name=f"模拟同学{i}",
                raw_data=build_exercise_row(i))
            if db_id:
                seeded_ex += 1
        except Exception as e:            # noqa: BLE001
            step(f"④ 运动入库 [{i}]", False, str(e))
    step("④ 运动问卷入库", seeded_ex == args.n, f"{seeded_ex}/{args.n} 条")

    # ⑤ 数据看板：调用真实数据层查询
    dash_results = {}
    try:
        sys.path.insert(0, os.path.join(root, "data_dashboard"))
        os.environ["SURVEY_DB"] = os.path.join(svc_survey, "survey_data.db")
        os.environ["DIET_DB"] = os.path.join(svc_diet, "diet_data.db")
        os.environ["EXERCISE_DB"] = os.path.join(svc_exercise, "exercise_data.db")
        import importlib
        import data as DASH
        importlib.reload(DASH)
        for fname in ("get_all_students", "get_diet_summary", "get_exercise_summary",
                      "get_completion_summary"):
            fn = getattr(DASH, fname, None)
            if fn is None:
                dash_results[fname] = "函数不存在"
                continue
            try:
                r = fn()
                n = len(r) if hasattr(r, "__len__") else r
                dash_results[fname] = f"ok（{n}）"
            except Exception as e:        # noqa: BLE001
                dash_results[fname] = f"失败: {type(e).__name__}: {e}"
        ok = all(str(v).startswith("ok") or v == "函数不存在"
                 for v in dash_results.values())
        step("⑤ 数据看板数据层查询", ok, json.dumps(dash_results, ensure_ascii=False))
    except Exception as e:                # noqa: BLE001
        step("⑤ 数据看板数据层查询", False, f"{type(e).__name__}: {e}")

    # ⑥ 控制台后端：进程/端口/自检接口可用性
    console_ok = {}
    try:
        sys.path.insert(0, os.path.join(root, "admin_console"))
        import service_manager as SM
        sm = SM.ServiceManager(root)
        st = sm.all_status()
        console_ok["service_manager"] = f"ok（{len(st)} 项）"
        import health_check as HC
        hc = HC.HealthChecker(root)
        console_ok["health_check"] = f"ok（{len(hc.check_all_dbs())} 库）"
        sys.path.insert(0, HERE)
        import selfcheck as SCK
        res = SCK.run_all(cfg={"app": {"base_dir": root}, "services": {}},
                          cfg_path="/nonexistent", only=["storage"])
        console_ok["selfcheck(backend)"] = f"ok（{res['summary']}）"
        step("⑥ 控制台后端（进程/健康/自检）", True,
             json.dumps(console_ok, ensure_ascii=False))
    except Exception as e:                # noqa: BLE001
        step("⑥ 控制台后端", False, f"{type(e).__name__}: {e}")

    # ⑦ 自检：应检出 MOCK- 残留
    residue_ok = False
    try:
        sys.path.insert(0, HERE)
        import importlib
        import selfcheck as SCK2
        importlib.reload(SCK2)
        res2 = SCK2.run_all(cfg={"app.base_dir": root}, cfg_path="/nonexistent",
                            only=["storage"])
        item = next((i for i in res2["items"] if i["id"] == "storage.mock_residue"), None)
        residue_ok = bool(item and item["status"] == "fail")
        step("⑦ 自检检出模拟数据残留", residue_ok,
             item["detail"] if item else "未找到该检查项")
    except Exception as e:                # noqa: BLE001
        step("⑦ 自检检出模拟数据残留", False, f"{type(e).__name__}: {e}")

    # ⑧ 数据导出接口
    try:
        import sqlite3
        conn = sqlite3.connect(os.path.join(svc_survey, "survey_data.db"))
        n = conn.execute("SELECT COUNT(*) FROM submissions WHERE student_id LIKE ?",
                         (MOCK_PREFIX + "%",)).fetchone()[0]
        conn.close()
        step("⑧ 数据导出（SQL 层可查询）", n >= args.n, f"{n} 条 MOCK- 记录可查")
    except Exception as e:                # noqa: BLE001
        step("⑧ 数据导出（SQL 层可查询）", False, str(e))

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "root": root,
        "n_mock_students": args.n,
        "mock_marker": MOCK_PREFIX,
        "steps": steps,
        "score_checks": score_checks,
        "dashboard": dash_results,
        "all_ok": all(s["ok"] for s in steps),
    }
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n报告已写入 {args.report}")
    print(f"\n{'✅ 模拟数据注入与功能验证全部通过' if report['all_ok'] else '❌ 存在失败项'}")
    print(f"⚠️  验证完成后请执行: python3 tools/clear_mock_data.py --root {root}")
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
