#!/usr/bin/env bash
# ============================================================================
#  ci.sh — 发布前质量门禁（本地与 CI 通用）
# ============================================================================
#  依次执行：
#    1. 全量 shell 语法检查（bash -n）
#    2. 全量 Python 语法编译（py_compile）
#    3. 单元/集成测试（unittest discover tests/）
#    4. 配置一致性：schema ↔ app.yaml.example 无悬空键
#    5. 配置示例自校验（占位符只告警）
#    6. 脱敏扫描（真实数据/凭据/PII/不可移植路径）
#    7. 安装脚本 dry-run 幂等（可选，--with-install）
#    8. 上期已移除模块的残留关键词（词表由片段拼接，避免自匹配）
#
#  用法：
#      bash ci.sh                # 常规门禁（快，无需 root）
#      bash ci.sh --with-install # 追加 install.sh --dry-run（无需 root）
#      PY=/path/to/python3 bash ci.sh
#
#  退出码：0=全部通过；1=有失败项。
# ============================================================================
set -uo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PKG_DIR"

PY="${PY:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY="python3"
WITH_INSTALL="false"
[ "${1:-}" = "--with-install" ] && WITH_INSTALL="true"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
FAILED=0
declare -a RESULTS

step() { echo; echo "── $* ─────────────────────────────────────────"; }
pass() { RESULTS+=("PASS  $1"); echo "${GREEN}✅ $1${NC}"; }
fail() { RESULTS+=("FAIL  $1"); echo "${RED}❌ $1${NC}"; FAILED=$((FAILED + 1)); }

# ── 1. shell 语法 ──────────────────────────────────────────────────────────
step "1/8 shell 语法检查（bash -n）"
SH_N=0
while IFS= read -r f; do
  SH_N=$((SH_N + 1))
  if ! bash -n "$f" 2>/tmp/ci_sh_err; then
    fail "bash -n $f：$(cat /tmp/ci_sh_err | head -3)"
  fi
done < <(find . -name '*.sh' -not -path './_归档_*' -not -path './.git/*' | sort)
[ "$SH_N" -gt 0 ] && pass "bash -n 通过（$SH_N 个脚本）" || fail "未找到任何 .sh 脚本"

# ── 2. Python 语法 ────────────────────────────────────────────────────────
step "2/8 Python 语法检查（py_compile）"
PY_N=0
PY_ERR=0
while IFS= read -r f; do
  PY_N=$((PY_N + 1))
  if ! "$PY" -m py_compile "$f" 2>/tmp/ci_py_err; then
    PY_ERR=$((PY_ERR + 1))
    fail "py_compile $f：$(head -3 /tmp/ci_py_err)"
  fi
done < <(find . -name '*.py' -not -path './_归档_*' -not -path './.git/*' \
           -not -path '*/__pycache__/*' | sort)
[ "$PY_ERR" -eq 0 ] && pass "py_compile 通过（$PY_N 个文件）" || true

# ── 3. 单元/集成测试 ──────────────────────────────────────────────────────
step "3/8 测试（unittest discover）"
if "$PY" -m unittest discover -s tests -t . 2>/tmp/ci_test_err; then
  N=$(grep -oE 'Ran [0-9]+ tests' /tmp/ci_test_err | grep -oE '[0-9]+' || echo "?")
  pass "单元/集成测试通过（$N 项）"
else
  fail "测试失败：$(tail -20 /tmp/ci_test_err)"
fi

# ── 4. schema ↔ example 悬空键 ────────────────────────────────────────────
step "4/8 配置一致性（schema ↔ app.yaml.example）"
if "$PY" tools/appconfig.py --check-example-keys >/tmp/ci_keys 2>&1; then
  pass "无悬空键：$(grep -oE '[0-9]+ 个键' /tmp/ci_keys | head -1)"
else
  fail "悬空键检查失败：$(cat /tmp/ci_keys)"
fi

# ── 5. 示例配置自校验（占位符只告警）──────────────────────────────────────
step "5/8 示例配置自校验（--no-strict）"
if "$PY" tools/appconfig.py --check --config config/app.yaml.example --no-strict >/tmp/ci_cfg 2>&1; then
  pass "config/app.yaml.example 结构与类型合法"
else
  fail "示例配置校验失败：$(tail -20 /tmp/ci_cfg)"
fi

# ── 6. 脱敏扫描 ───────────────────────────────────────────────────────────
step "6/8 脱敏扫描（发布门禁）"
if [ -f tools/scan_sensitive.py ]; then
  if "$PY" tools/scan_sensitive.py --json >/tmp/ci_scan.json 2>/tmp/ci_scan_err; then
    pass "脱敏扫描：0 阻断项"
  else
    fail "脱敏扫描存在阻断项：$(head -20 /tmp/ci_scan_err)"
  fi
else
  fail "tools/scan_sensitive.py 缺失"
fi

# ── 7. install.sh --dry-run（可选）────────────────────────────────────────
step "7/8 install.sh --dry-run（幂等）"
if [ "$WITH_INSTALL" = "true" ]; then
  T1=$(mktemp); T2=$(mktemp)
  bash install.sh --dry-run --dry-run-prefix=/tmp/gba-ci-a >"$T1" 2>&1; RC1=$?
  bash install.sh --dry-run --dry-run-prefix=/tmp/gba-ci-b >"$T2" 2>&1; RC2=$?
  if [ "$RC1" -ne 0 ] || [ "$RC2" -ne 0 ]; then
    fail "install.sh --dry-run 退出码非 0（$RC1/$RC2）"
  elif ! diff -q <(sed 's|gba-ci-a|PREFIX|g' "$T1") <(sed 's|gba-ci-b|PREFIX|g' "$T2") >/dev/null; then
    fail "install.sh --dry-run 两次输出不一致（非幂等）"
  else
    pass "install.sh --dry-run 通过且幂等"
  fi
  rm -f "$T1" "$T2"
else
  echo "${YELLOW}⏭️  跳过（加 --with-install 启用）${NC}"
  RESULTS+=("SKIP  install.sh --dry-run")
fi

# ── 8. 上期已移除模块的残留关键词 ─────────────────────────────────────────
# 词表刻意用「片段拼接」构造：若直接写字面量，本脚本自身就会命中该模式。
step "8/8 上期已移除模块的残留关键词（排除 _归档_*/ 与 .git）"
STALE_PAT="$(printf '%s%s|%s%s' 'micro' 'biome' '菌' '群')"
STALE_HITS=$(grep -riE "$STALE_PAT" . \
  $(for d in ./_归档_*; do [ -d "$d" ] && printf ' --exclude-dir=%s' "$(basename "$d")"; done) \
  --exclude-dir=.git 2>/dev/null | wc -l | tr -d ' ')
if [ "$STALE_HITS" = "0" ]; then
  pass "残留关键词命中 0（排除归档与 .git）"
else
  fail "残留关键词命中 $STALE_HITS 处：$(grep -riE "$STALE_PAT" . --exclude-dir=.git 2>/dev/null | head -3)"
fi

# ── 汇总 ──────────────────────────────────────────────────────────────────
echo
echo "════════ CI 汇总 ════════"
for r in "${RESULTS[@]}"; do echo "  $r"; done
echo "═════════════════════════"
if [ "$FAILED" -eq 0 ]; then
  echo "${GREEN}✅ CI 全部通过${NC}"
  exit 0
fi
echo "${RED}❌ CI 失败项：$FAILED${NC}"
exit 1
