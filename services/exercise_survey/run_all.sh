#!/bin/bash
# Exercise Survey — 管理脚本
# Usage: bash run_all.sh {start|stop|restart|status}
#
# 可移植约定（不写死绝对路径）：
#   APP_DIR      服务代码目录，默认为本脚本所在目录
#   VENV_PYTHON  Python 解释器，默认为 $APP_DIR/../../venv/bin/python3（若存在），否则 python3
#   SYNC_INTERVAL 同步间隔秒数，默认 900（15 分钟）
set -uo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
if [ -z "${VENV_PYTHON:-}" ]; then
  if [ -x "$APP_DIR/../../venv/bin/python3" ]; then
    VENV_PYTHON="$APP_DIR/../../venv/bin/python3"
  else
    VENV_PYTHON="$(command -v python3)"
  fi
fi
SYNC_INTERVAL="${SYNC_INTERVAL:-900}"
LOG_DIR="${LOG_DIR:-$APP_DIR/logs}"
mkdir -p "$LOG_DIR"

case "${1:-start}" in
  start|all)
    echo "━━━ 启动 Exercise Survey Sync Cron ━━━"
    # Start sync cron loop (every SYNC_INTERVAL seconds)
    cd "$APP_DIR" || exit 1
    nohup bash -c "while true; do '$VENV_PYTHON' '$APP_DIR/exercise_sync_cron.py'; sleep $SYNC_INTERVAL; done" \
      > "$LOG_DIR/sync_loop.log" 2>&1 &
    echo "  ✅ exercise_sync_cron (every ${SYNC_INTERVAL}s)"
    ;;

  stop)
    echo "🛑 停止 Exercise Survey..."
    pkill -f "exercise_sync_cron.py"
    echo "  ✅ 已停止"
    ;;

  restart)
    bash "$0" stop
    sleep 2
    bash "$0" start
    ;;

  status)
    pgrep -f exercise_sync_cron.py > /dev/null 2>&1 \
      && echo "  ✅ Exercise Sync (PID: $(pgrep -f exercise_sync_cron.py))" \
      || echo "  ❌ Exercise Sync"
    "$VENV_PYTHON" -c "
import os, sqlite3
db = os.environ.get('EXERCISE_DB', os.path.join('$APP_DIR', 'exercise_data.db'))
if not os.path.exists(db):
    print('  Exercise: 数据库尚未初始化')
else:
    c = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    n = c.execute('SELECT COUNT(*) FROM submissions').fetchone()[0]
    s = c.execute('SELECT COUNT(DISTINCT student_id) FROM submissions').fetchone()[0]
    print(f'  Exercise: {n} records, {s} students')
    c.close()
" 2>/dev/null
    ;;

  *)
    echo "Usage: bash run_all.sh {start|stop|restart|status}"
    ;;
esac
