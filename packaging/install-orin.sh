#!/usr/bin/env bash
set -euo pipefail

ROOT="${DM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
ROOT="$(realpath "$ROOT")"
AGENT_PORT="${DM_AGENT_PORT:-5111}"
[ "$(id -u)" -eq 0 ] || { echo "请使用 sudo 运行" >&2; exit 1; }
[ "$(uname -m)" = aarch64 ] || { echo "采集端需要 aarch64 系统" >&2; exit 1; }
for file in agent/agent.py agent/runner.py agent/cam.py agent/system.py host_config.json \
  prog/rl_deploy/build/rl_deploy prog/service/runService.sh prog/service/RoboticsServiceProcess \
  cam/camera_shm_viewer.py; do
  [ -f "$ROOT/$file" ] || { echo "缺少 $ROOT/$file" >&2; exit 1; }
done
if ! python3 -c 'import h5py, numpy, cv2' 2>/dev/null || ! command -v ssh >/dev/null || ! command -v sshpass >/dev/null; then
  apt-get update
  apt-get install -y python3 python3-h5py python3-numpy python3-opencv openssh-client sshpass
fi
python3 - "$ROOT" "$AGENT_PORT" <<'PY'
import json
from pathlib import Path
import sys
root = Path(sys.argv[1])
if not 1 <= int(sys.argv[2]) <= 65535:
    raise SystemExit('采集端端口必须介于 1–65535')
p = root / 'host_config.json'
cfg = json.loads(p.read_text())
for key, value in cfg.items():
    if isinstance(value, str) and (value == '/opt/dm-collect' or value.startswith('/opt/dm-collect/')):
        cfg[key] = str(root) + value[len('/opt/dm-collect'):]
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
PY
chmod 600 "$ROOT/host_config.json"
chmod +x "$ROOT/prog/rl_deploy/build/rl_deploy" "$ROOT/prog/service/runService.sh" "$ROOT/prog/service/RoboticsServiceProcess"
mkdir -p "$ROOT/data" "$ROOT/logs"
for binary in "$ROOT/prog/rl_deploy/build/rl_deploy" "$ROOT/prog/service/RoboticsServiceProcess"; do
  dependencies="$(ldd "$binary")"
  if printf '%s\n' "$dependencies" | grep -q 'not found'; then
    printf '动态库缺失：%s\n%s\n' "$binary" "$dependencies" >&2
    exit 1
  fi
done
unit_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//%/%%}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  printf '"%s"' "$value"
}
cat > /etc/systemd/system/dm-agent.service <<UNIT
[Unit]
Description=Deepmaster collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${ROOT//%/%%}/agent
ExecStart=/usr/bin/python3 $(unit_quote "$ROOT/agent/agent.py") --config $(unit_quote "$ROOT/host_config.json") --discovery-port $AGENT_PORT --port 0
Environment=DM_CAM_UPSTREAM=http://127.0.0.1:5000
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=3
KillMode=process
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable dm-agent.service
systemctl restart dm-agent.service
systemctl is-active --quiet dm-agent.service
printf '部署完成，请在网页设备列表中连接采集端。\n'
