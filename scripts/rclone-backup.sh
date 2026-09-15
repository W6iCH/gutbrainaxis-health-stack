#!/bin/bash
# ============================================================================
#  rclone 远程备份脚本（脱敏版）
#  所有凭据来自环境变量，见 config/env.example。
# ============================================================================
set -euo pipefail

APP_BASE="${APP_BASE:-/opt/gutbrainaxis}"
RCLONE_REMOTE="${RCLONE_REMOTE:-onedrive:opt-backup}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="${BACKUP_LOG:-/var/log/rclone-backup.log}"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
HOSTNAME=$(hostname)

echo "========== ${TIMESTAMP} 开始备份 ==========" >> "$LOG"

rclone copy "$APP_BASE" "$RCLONE_REMOTE" \
    --log-file="$LOG" --log-level INFO 2>&1

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "========== $(date "+%Y-%m-%d %H:%M:%S") 备份成功 ==========" >> "$LOG"
    STATS=$(rclone size "$RCLONE_REMOTE" 2>&1)
    python3 "$SCRIPT_DIR/send_alert.py" \
        "[✅ 备份成功] rclone $APP_BASE → $RCLONE_REMOTE (${TIMESTAMP})" \
        "服务器：${HOSTNAME}
时间：${TIMESTAMP}
状态：✅ 备份成功

备份统计：
${STATS}"
else
    echo "========== $(date "+%Y-%m-%d %H:%M:%S") 备份失败 (exit=$EXIT_CODE) ==========" >> "$LOG"
    ERROR_DETAIL=$(tail -30 "$LOG" | sed 's/^/  /')
    python3 "$SCRIPT_DIR/send_alert.py" \
        "[⚠️ 备份失败] rclone $APP_BASE → $RCLONE_REMOTE (${TIMESTAMP})" \
        "服务器：${HOSTNAME}
时间：${TIMESTAMP}
状态：⚠️ 失败（退出码 ${EXIT_CODE}）

错误日志（最后 30 行）：
${ERROR_DETAIL}

请登录服务器检查 ${LOG} 查看完整日志。"
fi

exit $EXIT_CODE
