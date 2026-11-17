#!/usr/bin/env bash
set -euo pipefail

ROOT="${DM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
ROOT="$(realpath "$ROOT")"
PORT="${PORT:-8790}"
API_PORT="${API_PORT:-8765}"
[ "$(id -u)" -eq 0 ] || { echo "请使用 sudo 运行" >&2; exit 1; }
for file in server/api.py server/run_web.py index.html host_config.json; do
  [ -f "$ROOT/$file" ] || { echo "缺少 $ROOT/$file" >&2; exit 1; }
done
if ! python3 -c 'import h5py, numpy, requests' 2>/dev/null; then
  apt-get update
  apt-get install -y python3 python3-h5py python3-numpy python3-requests
fi
python3 - "$PORT" "$API_PORT" <<'PY'
import sys
ports = [int(value) for value in sys.argv[1:]]
if len(set(ports)) != 2 or any(not 1 <= port <= 65535 for port in ports):
    raise SystemExit('网页端口和接口端口必须不同，且介于 1–65535')
PY
DATA_DIR="$(python3 - "$ROOT" <<'PY'
import json
from pathlib import Path
import sys
root = Path(sys.argv[1])
p = root / 'host_config.json'
cfg = json.loads(p.read_text())
if cfg.get('local_data_dir') == '/opt/dm-web/datasets':
    cfg['local_data_dir'] = str(root / 'datasets')
data = Path(cfg.get('local_data_dir') or root / 'datasets').expanduser()
if not data.is_absolute():
    data = root / data
cfg['local_data_dir'] = str(data.resolve())
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
print(cfg['local_data_dir'])
PY
 )"
mkdir -p "$DATA_DIR" "$ROOT/logs"
chmod 600 "$ROOT/host_config.json"
unit_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//%/%%}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  printf '"%s"' "$value"
}
for unit in dm-web-api dm-web; do
  if [ "$unit" = dm-web-api ]; then
    COMMAND="/usr/bin/python3 $(unit_quote "$ROOT/server/api.py") $(unit_quote "$DATA_DIR") $API_PORT"
  else
    COMMAND="/usr/bin/python3 $(unit_quote "$ROOT/server/run_web.py") $PORT"
  fi
  cat > "/etc/systemd/system/$unit.service" <<UNIT
[Unit]
Description=Deepmaster $unit
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${ROOT//%/%%}
ExecStart=$COMMAND
Environment=$(unit_quote "DM_HOST_CONFIG=$ROOT/host_config.json")
Environment=$(unit_quote "DM_DATA_DIR=$DATA_DIR")
Environment=$(unit_quote "DM_ADMIN_PASSWORD=${DM_ADMIN_PASSWORD:-admin123}")
Environment=$(unit_quote "DM_AGENT_URL=${DM_AGENT_URL:-}")
Environment="API_PORT=$API_PORT"
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
done
chmod 600 /etc/systemd/system/dm-web-api.service /etc/systemd/system/dm-web.service
systemctl daemon-reload
systemctl enable dm-web-api.service dm-web.service
systemctl restart dm-web-api.service dm-web.service
systemctl is-active --quiet dm-web-api.service dm-web.service
printf '部署完成：http://<服务器IP>:%s\n' "$PORT"
