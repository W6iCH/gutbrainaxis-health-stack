#!/bin/bash
# ============================================================
# 饮食记录反馈系统 — 一键启动 (v2 — 邮件流程已移除)
# ============================================================
# Usage:
#   bash run_all.sh              # 启动所有服务
#   bash run_all.sh webhook      # 仅启动 webhook 监听
#   bash run_all.sh server       # 仅启动反馈页面服务器
#   bash run_all.sh llm          # 仅启动 LLM 队列处理
#   bash run_all.sh status       # 查看服务状态
# ============================================================

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"

case "${1:-all}" in
  all)
    echo "╔═══════════════════════════════════════════════╗"
    echo "║   启动饮食记录反馈服务 (v2 — 无邮件)         ║"
    echo "╚═══════════════════════════════════════════════╝"
    echo ""

    # 1. Start webhook listener
    echo "[1/3] 📥 启动 Webhook 监听服务 (端口 ${PORT_DIET_WEBHOOK:-9876})..."
    python3 webhook_listener.py "${PORT_DIET_WEBHOOK:-9876}" &
    WH_PID=$!
    sleep 2
    if kill -0 "$WH_PID" 2>/dev/null; then
        echo "  ✅ Webhook listener running (PID: $WH_PID)"
    else
        echo "  ❌ Webhook listener failed to start"
    fi

    # 2. Start feedback server
    echo "[2/3] 🚀 启动反馈页面服务器 (端口 ${PORT_DIET_FEEDBACK:-8001})..."
    python3 diet_feedback_server.py "${PORT_DIET_FEEDBACK:-8001}" &
    FB_PID=$!
    sleep 2
    if curl -sf "http://localhost:${PORT_DIET_FEEDBACK:-8001}/ping" > /dev/null 2>&1; then
        echo "  ✅ Feedback server running on :${PORT_DIET_FEEDBACK:-8001} (PID: $FB_PID)"
    else
        echo "  ⚠ Feedback server may have failed"
    fi

    # 3. Start LLM queue worker
    echo "[3/3] 🧠 启动 LLM 队列处理..."
    python3 diet_llm_queue.py &
    LLM_PID=$!
    sleep 1
    if kill -0 "$LLM_PID" 2>/dev/null; then
        echo "  ✅ LLM queue worker running (PID: $LLM_PID)"
    fi

    echo ""
    echo "  📥 Webhook:   POST http://localhost:9876/"
    echo "  🌐 反馈页面:  http://localhost:8001/report?user=X&quest=Y&answer=Z"
    echo "  ⚡ 管理控制台: http://localhost:9000 (统一管理)"
    echo "  📝 日志:      tail -f logs/*.log"
    echo ""
    echo "  Press Ctrl+C to stop all services"

    trap "kill $WH_PID $FB_PID $LLM_PID 2>/dev/null; echo 'Services stopped.'" EXIT
    wait
    ;;

  webhook)
    echo "📥 启动 Webhook 监听..."
    python3 webhook_listener.py ${2:-9876}
    ;;

  server)
    echo "🚀 启动反馈页面服务器..."
    python3 diet_feedback_server.py ${2:-8001}
    ;;

  llm)
    echo "🧠 启动 LLM 队列处理..."
    python3 diet_llm_queue.py
    ;;

  sync)
    echo "🔄 手动触发数据同步..."
    python3 diet_sync_cron.py
    ;;

  status)
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  饮食记录反馈系统 — 状态"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    if curl -sf http://localhost:9876/health > /dev/null 2>&1; then
        echo "  📥 Webhook:    ✅ Running (:${PORT_DIET_WEBHOOK:-9876})"
    else
        echo "  📥 Webhook:    ❌ Not running"
    fi

    if curl -sf http://localhost:8001/ping > /dev/null 2>&1; then
        echo "  🌐 Feedback:   ✅ Running (:${PORT_DIET_FEEDBACK:-8001})"
    else
        echo "  🌐 Feedback:   ❌ Not running"
    fi

    # LLM queue status
    python3 diet_llm_queue.py status 2>/dev/null

    # Database stats
    echo ""
    echo "  📊 Database:"
    python3 -c "
import sys; sys.path.insert(0, '$BASE_DIR')
from diet_database import get_conn
conn = get_conn()
count = conn.execute('SELECT COUNT(*) as n FROM submissions').fetchone()['n']
students = conn.execute('SELECT COUNT(DISTINCT student_id) as n FROM submissions').fetchone()['n']
advice = conn.execute('SELECT COUNT(*) as n FROM submissions WHERE dietary_advice IS NOT NULL').fetchone()['n']
print(f'     总记录: {count} ｜ 学生数: {students} ｜ 已有建议: {advice}')
conn.close()
" 2>/dev/null

    echo ""
    ;;

  stop)
    echo "🛑 停止所有服务..."
    pkill -f "webhook_listener.py"
    pkill -f "diet_feedback_server.py"
    pkill -f "diet_llm_queue.py"
    echo "  ✅ 已停止"
    ;;

  *)
    echo "Usage: bash run_all.sh {all|webhook|server|llm|sync|status|stop}"
    ;;
esac
