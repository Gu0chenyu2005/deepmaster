#!/usr/bin/env bash
# 运行前端验收；--e2e 增加双机测试并写入测试数据。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${DM_TEST_PYTHON:-/usr/bin/python3}"
export PYTHONPATH="${DM_TEST_PYTHONPATH:-/home/edisonchen/.local/lib/python3.10/site-packages}"
export DM_TEST_URL="${DM_TEST_URL:-http://127.0.0.1:8790}"

SUITES=(t_welcome3 t_noflip t_deck t_navflip2 t_settings t_edge t_acct t_middot)
[ "${1:-}" = "--e2e" ] && SUITES+=(t_e2e)

echo "目标: $DM_TEST_URL    解释器: $PY"
curl -s -m 3 -o /dev/null "$DM_TEST_URL/api/health" \
  || { echo "[✗] $DM_TEST_URL 不通，请先部署并启动服务端" >&2; exit 1; }

total_pass=0; total_fail=0; failed=()
for s in "${SUITES[@]}"; do
  src="$ROOT/tests/acceptance/$s.py"
  [ -f "$src" ] || { echo "[✗] 缺套件 $src" >&2; exit 1; }
  tmp="$(mktemp)"
  sed "s#^B = \"http://127.0.0.1:8790\"#B = \"$DM_TEST_URL\"#" "$src" > "$tmp"
  echo
  echo "===== $s ====="
  out="$(cd "$ROOT/tests/acceptance" && timeout 600 "$PY" "$tmp" 2>&1)"
  echo "$out" | tail -4
  p=$(echo "$out" | grep -c "^PASS")
  f=$(echo "$out" | grep -c "^FAIL")
  total_pass=$((total_pass + p)); total_fail=$((total_fail + f))
  [ "$f" -gt 0 ] && failed+=("$s")
  rm -f "$tmp"
done

echo
echo "================================"
echo "合计 通过 $total_pass 项 / 失败 $total_fail 项"
if [ "$total_fail" -gt 0 ]; then
  echo "失败套件: ${failed[*]}"
  exit 1
fi
echo "全绿。注意：t_acct 会建/删测试账号，t_e2e 会写库并留下 agent_devices/upload_job，跑完自行清。"
