#!/bin/bash
# ============================================================
# 问卷反馈系统 — 本地手动启动（仅调试用）
# ============================================================
# ⚠️ 说明修正（设计复查）：
#   旧注释写「v2 — 邮件流程已移除」，与仓库实际情况**不符**：
#   本包保留了完整邮件链路（email_feedback.py + email_log 表 + 每日报告单元）。
#   生产部署请使用 systemd（install.sh 安装的 research-* 单元）；
#   本脚本仅供本地手动调试单进程。
#
# Usage:
#   bash run_all.sh              # 启动网页反馈服务
#   bash run_all.sh server       # 仅启动网页反馈服务
#   bash run_all.sh status       # 查看服务状态
#   bash run_all.sh fetch        # 手动拉取问卷数据
# ============================================================

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"

case "${1:-all}" in
  all)
    echo "╔═══════════════════════════════════════════════╗"
    echo "║   启动问卷反馈服务（本地调试；生产用 systemd）║"
    echo "╚═══════════════════════════════════════════════╝"
    echo ""

    # Start web server
    echo "[1/2] 🚀 启动网页反馈服务器 (端口 ${PORT_SURVEY_FEEDBACK:-8000} + ${PORT_SURVEY_FEEDBACK_ALT:-8080})..."
    python3 feedback_server.py "${PORT_SURVEY_FEEDBACK:-8000}" "${PORT_SURVEY_FEEDBACK_ALT:-8080}" &
    WEB_PID=$!
    sleep 2

    if curl -sf "http://localhost:${PORT_SURVEY_FEEDBACK:-8000}/health" > /dev/null 2>&1; then
        echo "  ✅ Web feedback server running on :${PORT_SURVEY_FEEDBACK:-8000} (PID: $WEB_PID)"
    elif curl -sf "http://localhost:${PORT_SURVEY_FEEDBACK_ALT:-8080}/health" > /dev/null 2>&1; then
        echo "  ✅ Web feedback server running on :8080 (PID: $WEB_PID)"
    else
        echo "  ❌ Web server failed to start"
    fi

    echo "[2/2] ⚡ 管理控制台由主 run_all.sh 统一启动"
    echo ""
    echo "  📊 报告页面: http://localhost:8000/report?user=X&quest=Y&answer=Z"
    echo "  ⚡ 管理控制台: http://localhost:9000 (统一管理)"
    echo ""
    echo "  Press Ctrl+C to stop all services"

    trap "kill $WEB_PID 2>/dev/null; echo 'Services stopped.'" EXIT
    wait
    ;;

  server)
    echo "🚀 启动网页反馈服务器..."
    python3 feedback_server.py 8080
    ;;

  status)
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        echo "  📊 Feedback:   ✅ Running (:${PORT_SURVEY_FEEDBACK:-8000})"
    else
        echo "  📊 Feedback:   ❌ Not running"
    fi
    ;;

  fetch)
    echo "📥 获取最新问卷数据..."
    python3 fetch_survey_results.py
    ;;

  *)
    echo "Usage: bash run_all.sh {all|server|status|fetch}"
    ;;
esac
