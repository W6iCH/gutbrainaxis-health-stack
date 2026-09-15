#!/bin/bash
# ============================================================
# 问卷反馈系统 — 一键启动 (v2 — 邮件流程已移除)
# ============================================================
# Usage:
#   bash run_all.sh              # 启动所有服务
#   bash run_all.sh server       # 仅启动网页反馈服务
#   bash run_all.sh status       # 查看服务状态
# ============================================================

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"

case "${1:-all}" in
  all)
    echo "╔═══════════════════════════════════════════════╗"
    echo "║   启动问卷反馈服务 (v2 — 无邮件)             ║"
    echo "╚═══════════════════════════════════════════════╝"
    echo ""

    # Start web server
    echo "[1/2] 🚀 启动网页反馈服务器 (端口 8000 + 8080)..."
    python3 feedback_server.py 8000 8080 &
    WEB_PID=$!
    sleep 2

    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        echo "  ✅ Web feedback server running on :8000 (PID: $WEB_PID)"
    elif curl -sf http://localhost:8080/health > /dev/null 2>&1; then
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
        echo "  📊 Feedback:   ✅ Running (:8000)"
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
