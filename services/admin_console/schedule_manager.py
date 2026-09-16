#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
schedule_manager.py — 控制台「调度」页后端
=============================================================================
把 app.yaml 里**全部时间相关配置**暴露给控制台可视化查看与修改，并保证
「改配置即改行为」：

  * `describe()`  —— 列出全部时间项（键/当前值/类型/单位/作用/生效方式）
                     + 当前分析窗口状态 + 静默时段状态 + 各 timer 的 OnCalendar
  * `update()`    —— 写入 app.yaml（先校验、再备份、原子替换），
                     可选立即重渲染 systemd env / timer 片段
  * `preview()`   —— 只计算不落盘（用于「变更预览」）

写入安全
--------
1. 先用 `tools/appconfig.py` 的 schema 校验（占位符只告警，不阻断）；
2. **写前备份** app.yaml → `<config>.bak.<ts>`；
3. 原子替换（tmp + os.replace）；
4. 写审计由 app.py 负责。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
APP_BASE = os.environ.get("APP_BASE", "/opt")

# ── 定位 tools/appconfig.py（仓库布局 / 安装布局都支持）──────────────────
_CANDIDATES = [
    HERE.parent.parent / "tools",                 # 仓库: 12_部署包_可发布/tools
    Path(APP_BASE) / "tools",                     # 安装: $APP_BASE/tools
    HERE.parent / "tools",
]
for c in _CANDIDATES:
    if (c / "appconfig.py").is_file():
        if str(c) not in sys.path:
            sys.path.insert(0, str(c))
        break

try:
    import appconfig as AC
except Exception as e:                                        # pragma: no cover
    AC = None
    _IMPORT_ERR = str(e)
else:
    _IMPORT_ERR = ""

# 时间项分组：键 → (中文名, 单位, 作用, 生效方式)
GROUPS = [
    ("分析调度（LLM 峰谷）", [
        ("analysis.mode", "调度模式", "realtime|offpeak|hybrid", "窗口外是否消费队列", "重启 diet-llm-queue 服务（或等待 worker 自动重读，≤30s）"),
        ("analysis.window_start", "窗口开始", "HH:MM", "低谷窗口起点（可跨天）", "同上（worker 每 window_check_seconds 重算）"),
        ("analysis.window_end", "窗口结束", "HH:MM", "低谷窗口终点", "同上"),
        ("analysis.timezone", "窗口时区", "IANA", "窗口判定时区", "同上"),
        ("analysis.workdays", "生效星期", "1-7 逗号分隔", "只在指定星期消费", "同上"),
        ("analysis.throttle_seconds", "任务间隔", "秒", "每任务之间限流间隔", "同上"),
        ("analysis.concurrency", "并发数", "个", "worker 并发（建议 1）", "需重启 diet-llm-queue 服务"),
        ("analysis.max_per_window", "单窗口配额", "个（0=不限）", "一个窗口最多处理数", "同上"),
        ("analysis.hybrid_daytime_max", "hybrid 白天额度", "个/日", "hybrid 模式窗口外每日上限", "同上"),
        ("analysis.idle_sleep_seconds", "空闲轮询", "秒", "队列空时轮询间隔", "同上"),
        ("analysis.window_check_seconds", "窗口检查间隔", "秒", "挂起/恢复灵敏度", "同上"),
    ]),
    ("定时拉取与巡检", [
        ("schedule.survey_sync_minutes", "量表拉取周期", "分钟", "量表兜底拉取（webhook 为主）", "重渲染 timer 片段并 daemon-reload"),
        ("schedule.diet_sync_minutes", "饮食拉取周期", "分钟", "饮食兜底拉取", "同上"),
        ("schedule.exercise_sync_minutes", "运动拉取周期", "分钟", "运动拉取", "同上"),
        ("schedule.health_monitor_minutes", "健康巡检周期", "分钟", "进程/队列/端口巡检", "同上"),
        ("survey_platforms.scale.fetch_interval_minutes", "量表拉取（平台侧）", "分钟", "文档用；与 timer 保持一致", "重渲染 timer"),
        ("survey_platforms.diet.fetch_interval_minutes", "饮食拉取（平台侧）", "分钟", "文档用", "重渲染 timer"),
        ("survey_platforms.exercise.fetch_interval_minutes", "运动拉取（平台侧）", "分钟", "文档用", "重渲染 timer"),
    ]),
    ("每日时点", [
        ("schedule.daily_report_at", "每日报告时刻", "HH:MM", "日报发送时刻", "重渲染 timer 片段并 daemon-reload"),
        ("schedule.daily_report_retry_minutes", "日报重试间隔", "分钟", "日报失败重试", "重启 daily-report 相关调用方"),
        ("schedule.backup_at", "每日备份时刻", "HH:MM", "备份触发时刻", "重渲染 timer 片段并 daemon-reload"),
        ("backup.timeout_minutes", "备份超时", "分钟", "单次备份上限", "下次备份生效"),
        ("backup.retention", "备份保留份数", "份", "保留最近 N 份", "下次备份生效"),
        ("backup.verify_after", "备份后校验", "bool", "校验 tar 完整性", "下次备份生效"),
    ]),
    ("静默时段", [
        ("schedule.quiet_hours_start", "静默开始", "HH:MM（空=关闭）", "夜间免打扰起点", "运行期各脚本即时生效"),
        ("schedule.quiet_hours_end", "静默结束", "HH:MM", "夜间免打扰终点", "同上"),
        ("schedule.quiet_hours_suppress_alerts", "静默压制告警", "bool", "静默期不发 warning 级告警", "同上"),
        ("schedule.quiet_hours_skip_sync", "静默跳过拉取", "bool", "静默期跳过定时拉取（webhook 不受影响）", "同上"),
        ("schedule.quiet_hours_skip_backup", "静默跳过备份", "bool", "静默期是否跳过备份", "同上"),
        ("alert.quiet_hours_start", "告警静默开始（覆盖）", "HH:MM（空=继承）", "仅告警的独立静默窗口", "同上"),
        ("alert.quiet_hours_end", "告警静默结束（覆盖）", "HH:MM", "仅告警的独立静默窗口", "同上"),
    ]),
    ("告警与冷却", [
        ("alert.cooldown_minutes", "告警冷却", "分钟", "同一告警去重间隔", "下次巡检生效"),
        ("alert.severity_threshold", "告警级别下限", "info|warning|critical", "低于该级别不发信", "下次巡检生效"),
        ("alert.stale_data_minutes", "数据滞后阈值", "分钟", "超时未处理即告警", "下次巡检生效"),
    ]),
    ("LLM 调用", [
        ("llm.primary.timeout_seconds", "主 API 超时", "秒", "单次调用上限", "重启 diet-llm-queue"),
        ("llm.primary.max_retries", "主 API 重试次数", "次", "失败重试", "同上"),
        ("llm.primary.retry_backoff_seconds", "重试退避", "秒", "重试等待基数", "同上"),
        ("llm.backup.timeout_seconds", "备用 API 超时", "秒", "备用调用上限", "同上"),
    ]),
    ("Webhook 重试与限流", [
        ("webhook.enabled", "Webhook 开关", "bool", "是否启用回调接收", "重启 survey-webhook 服务"),
        ("webhook.max_body_bytes", "请求体上限", "字节", "防超大请求", "同上"),
        ("webhook.rate_limit_per_minute", "限流阈值", "次/分钟/IP", "防重投风暴", "同上"),
        ("webhook.retry_max_attempts", "重试上限", "次", "本地重试最大次数", "同上（含 LLM 队列重试）"),
        ("webhook.retry_backoff_seconds", "重试退避", "秒", "重试等待基数", "同上"),
        ("webhook.retry_poll_seconds", "重试轮询间隔", "秒", "重试队列扫描间隔", "同上"),
        ("webhook.timer_only_after_minutes", "无回调判定", "分钟", "超时判定回落定时拉取", "同上"),
    ]),
    ("自检探活", [
        ("selfcheck.enabled", "自检开关", "bool", "是否启用自检", "下次自检生效"),
        ("selfcheck.http_timeout_seconds", "探活超时", "秒", "HTTP 探活上限", "同上"),
        ("selfcheck.external_probe", "外部探测", "bool", "是否探测外部依赖", "同上"),
        ("selfcheck.external_probe_interval_seconds", "外部探测间隔", "秒", "限流保护", "同上"),
        ("selfcheck.external_probe_timeout_seconds", "外部探测超时", "秒", "单次外部探测上限", "同上"),
        ("selfcheck.cache_ttl_seconds", "结果缓存", "秒", "自检结果缓存时长", "同上"),
    ]),
    ("SMTP 发送", [
        ("smtp.timeout_seconds", "SMTP 超时", "秒", "连接/发送上限", "重启相关服务"),
        ("smtp.send_retry_max_attempts", "发送重试", "次", "邮件发送重试", "同上"),
        ("smtp.send_retry_backoff_seconds", "发送退避", "秒", "重试等待基数", "同上"),
    ]),
    ("数据库", [
        ("database.busy_timeout_seconds", "SQLite 锁等待", "秒", "避免瞬时锁冲突报错", "重启相关服务"),
    ]),
]

ALL_TIME_KEYS = [k for _, items in GROUPS for (k, *_rest) in items]


def config_path() -> str:
    p = os.environ.get("RESEARCH_APP_CONFIG")
    if p and os.path.exists(p):
        return p
    for cand in ("/etc/research-app/app.yaml",
                 str(HERE.parent.parent / "config" / "app.yaml"),
                 str(Path(APP_BASE) / "config" / "app.yaml")):
        if os.path.exists(cand):
            return cand
    return os.environ.get("RESEARCH_APP_CONFIG", "/etc/research-app/app.yaml")


def _flat(d: dict, pre: str = "") -> dict:
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            out.update(_flat(v, f"{pre}{k}."))
        else:
            out[f"{pre}{k}"] = v
    return out


def _nested_set(data: dict, key: str, value):
    parts = key.split(".")
    cur = data
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def describe() -> dict:
    """列出全部时间项 + 当前窗口/静默状态 + timer 渲染预览。"""
    path = config_path()
    raw, schema, err = {}, {}, ""
    if AC:
        try:
            raw = AC._load_yaml(path)
            with open(AC.DEFAULT_SCHEMA, encoding="utf-8") as f:
                schema = json.load(f).get("fields", {})
        except Exception as e:                                # noqa: BLE001
            err = str(e)
            raw = {}
    else:
        err = f"无法加载 tools/appconfig.py：{_IMPORT_ERR}"
    flat = _flat(raw)

    groups = []
    for title, items in GROUPS:
        rows = []
        for key, label, unit, effect, apply_how in items:
            rule = schema.get(key, {}) if isinstance(schema, dict) else {}
            rows.append({
                "key": key, "label": label, "unit": unit,
                "effect": effect, "apply": apply_how,
                "type": rule.get("type", "str"),
                "value": flat.get(key, ""),
                "enum": rule.get("enum"),
                "min": rule.get("min"), "max": rule.get("max"),
            })
        groups.append({"title": title, "items": rows})

    window, quiet, timers = {}, None, []
    if AC:
        try:
            full = AC.flatten(raw)
            window = AC.window_state(full)
            quiet = AC.is_quiet_hours(full)
            for unit in AC.TIMER_SPEC:
                timers.append({"unit": unit, "on_calendar":
                               AC.timer_oncalendar(full, unit)})
        except Exception as e:                                # noqa: BLE001
            err = err or str(e)

    return {
        "config_path": path,
        "writable": os.access(path, os.W_OK),
        "n_time_keys": len(ALL_TIME_KEYS),
        "groups": groups,
        "window": window,
        "quiet_hours_now": quiet,
        "timers": timers,
        "error": err,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def preview(updates: dict) -> dict:
    """只计算不落盘：返回每条变更的前后值 + 校验结果 + timer 预览。"""
    path = config_path()
    if not AC:
        return {"ok": False, "errors": [f"appconfig 不可用：{_IMPORT_ERR}"],
                "changes": []}
    raw = AC._load_yaml(path)
    flat_before = _flat(raw)
    changes = []
    for k, v in (updates or {}).items():
        if k not in ALL_TIME_KEYS:
            continue
        changes.append({"key": k, "before": flat_before.get(k, ""), "after": v})
    return {"ok": True, "changes": changes, "errors": [],
            "note": "预览模式：未写入磁盘"}


def update(updates: dict, apply_timers: bool = True) -> dict:
    """
    写入 app.yaml。返回 {success, changes, errors, backup, timers_rendered}。
    流程：过滤已知键 → 逐条类型转换 → 校验 → 备份 → 原子写 → 可选重渲染 timer。
    """
    path = config_path()
    if not AC:
        return {"success": False, "error": f"appconfig 不可用：{_IMPORT_ERR}"}
    if not os.path.exists(path):
        return {"success": False, "error": f"配置文件不存在：{path}"}

    valid = {k: v for k, v in (updates or {}).items() if k in ALL_TIME_KEYS}
    ignored = sorted(set(updates or {}) - set(valid))
    if not valid:
        return {"success": False, "error": "没有可更新项",
                "ignored": ignored}

    raw = AC._load_yaml(path)
    with open(AC.DEFAULT_SCHEMA, encoding="utf-8") as f:
        schema = json.load(f)
    fields = schema.get("fields", {})
    flat_before = _flat(raw)

    errors, changes = [], []
    for k, v in valid.items():
        t = (fields.get(k, {}) or {}).get("type", "str")
        try:
            if t == "bool":
                val = v if isinstance(v, bool) else str(v).strip().lower() in (
                    "1", "true", "yes", "on", "y")
            elif t == "int":
                val = int(str(v).strip())
            elif t == "float":
                val = float(str(v).strip())
            else:
                val = v if isinstance(v, str) else str(v)
                if val == "" and k not in AC.OPTIONAL_EMPTY_OK:
                    pass                                     # 允许清空（校验会提示）
        except (TypeError, ValueError):
            errors.append(f"{k}: 值 {v!r} 无法转换为 {t}")
            continue
        _nested_set(raw, k, val)
        changes.append({"key": k, "before": flat_before.get(k, ""), "after": val})

    if errors:
        return {"success": False, "errors": errors, "changes": changes}

    # 校验（占位符只告警）
    _, verrs, _vwarns = AC.validate(raw, schema, {}, strict_placeholders=False)
    if verrs:
        return {"success": False, "errors": verrs, "changes": changes}

    # 备份 + 原子写
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.bak.{stamp}"
    try:
        shutil.copy2(path, backup)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False,
                           default_flow_style=False)
        os.replace(tmp, path)
    except OSError as e:
        return {"success": False, "error": f"写入失败：{e}", "changes": changes}

    timers_rendered = []
    if apply_timers:
        try:
            unit_dir = os.environ.get("SYSTEMD_DIR", "/etc/systemd/system")
            timers_rendered = [os.path.basename(p) for p in
                               AC.render_timer_fragments(AC.flatten(raw), unit_dir)]
        except OSError as e:
            timers_rendered = [f"⚠ timer 渲染失败: {e}"]

    return {"success": True, "changes": changes, "ignored": ignored,
            "backup": backup, "timers_rendered": timers_rendered,
            "note": "已写入 app.yaml；timer 片段已重渲染，需 systemctl daemon-reload 生效"}
