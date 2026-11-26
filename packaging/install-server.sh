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
SUITE="${DM_UBUNTU_SUITE:-$(. /etc/os-release; printf '%s' "$VERSION_CODENAME")}"
[ "$SUITE" = jammy ] || { printf '离线依赖包针对 Ubuntu 22.04 (jammy)，当前系统为 %s。\n' "$SUITE" >&2; exit 1; }
OFFLINE="$ROOT/third_party/offline/$SUITE/amd64"
for dir in "$OFFLINE/apt" "$OFFLINE/python-wheels"; do
  [ -d "$dir" ] || { printf '缺少离线依赖目录：%s\n' "$dir" >&2; exit 1; }
done
for file in Packages Packages.gz; do
  [ -s "$OFFLINE/apt/$file" ] || { printf '离线 APT 索引缺失：%s\n' "$OFFLINE/apt/$file" >&2; exit 1; }
done
APT_SOURCE="$(mktemp)"
trap 'python3 -c '\''import os,sys; os.unlink(sys.argv[1]) if os.path.exists(sys.argv[1]) else None'\'' "$APT_SOURCE"' EXIT
printf 'deb [trusted=yes] file:%s/apt ./\n' "$OFFLINE" > "$APT_SOURCE"
APT_OFFLINE=(-o "Dir::Etc::sourcelist=$APT_SOURCE" -o 'Dir::Etc::sourceparts=-')
apt-get "${APT_OFFLINE[@]}" update
apt-get "${APT_OFFLINE[@]}" install --yes \
  python3-venv ca-certificates ffmpeg python3-h5py python3-numpy python3-requests python3-pil
/usr/bin/python3 -m venv "$ROOT/.export-venv"
"$ROOT/.export-venv/bin/python" -m pip install --no-index --find-links "$OFFLINE/python-wheels" \
  'numpy==1.26.4' 'h5py==3.16.0' 'pillow==12.3.0' \
  'pyarrow==25.0.1' 'pandas==2.3.3' \
  'python-dateutil==3.0.0' 'pytz==2026.6' 'six==1.17.0' 'tzdata==2026.6'
python3 -c 'import h5py, numpy, requests, PIL'
command -v ffmpeg >/dev/null || { printf '离线安装后仍缺少 ffmpeg。\n' >&2; exit 1; }
EXPORT_PYTHON="$ROOT/.export-venv/bin/python"
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
Environment=$(unit_quote "DM_EXPORT_PYTHON=$EXPORT_PYTHON")
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
