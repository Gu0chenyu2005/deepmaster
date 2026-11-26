#!/usr/bin/env bash
set -euo pipefail

ROOT="${DM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
ROOT="$(realpath "$ROOT")"
AGENT_PORT="${DM_AGENT_PORT:-5111}"
[ "$(id -u)" -eq 0 ] || { echo "请使用 sudo 运行" >&2; exit 1; }
[ "$(uname -m)" = aarch64 ] || { echo "采集端需要 aarch64 系统" >&2; exit 1; }
for file in agent/agent.py agent/runner.py agent/cam.py agent/system.py host_config.json \
  prog/rl_deploy/build/rl_deploy prog/service/runService.sh prog/service/RoboticsServiceProcess \
  cam/camera_shm_viewer.py cam/webrtc_preview.py; do
  [ -f "$ROOT/$file" ] || { echo "缺少 $ROOT/$file" >&2; exit 1; }
done
SUITE="${DM_UBUNTU_SUITE:-$(. /etc/os-release; printf '%s' "$VERSION_CODENAME")}"
[ "$SUITE" = jammy ] || { printf '离线依赖包针对 Ubuntu 22.04 (jammy)，当前系统为 %s。\n' "$SUITE" >&2; exit 1; }
OFFLINE="$ROOT/third_party/offline/$SUITE/arm64/apt"
[ -d "$OFFLINE" ] || { printf '缺少采集端离线依赖目录：%s\n' "$OFFLINE" >&2; exit 1; }
for file in Packages Packages.gz; do
  [ -s "$OFFLINE/$file" ] || { printf '离线 APT 索引缺失：%s\n' "$OFFLINE/$file" >&2; exit 1; }
done
APT_SOURCE="$(mktemp)"
trap 'python3 -c '\''import os,sys; os.unlink(sys.argv[1]) if os.path.exists(sys.argv[1]) else None'\'' "$APT_SOURCE"' EXIT
printf 'deb [trusted=yes] file:%s ./\n' "$OFFLINE" > "$APT_SOURCE"
APT_OFFLINE=(-o "Dir::Etc::sourcelist=$APT_SOURCE" -o 'Dir::Etc::sourceparts=-')
apt-get "${APT_OFFLINE[@]}" update
apt-get "${APT_OFFLINE[@]}" install --yes \
  python3 python3-h5py python3-numpy python3-opencv openssh-client sshpass lsof \
  gstreamer1.0-tools gir1.2-gst-plugins-bad-1.0 gstreamer1.0-plugins-bad \
  gstreamer1.0-nice gstreamer1.0-plugins-good python3-gi python3-gst-1.0
if ! command -v gst-inspect-1.0 >/dev/null || \
   ! dpkg-query -W -f='${Status}' gstreamer1.0-plugins-bad 2>/dev/null | grep -q 'install ok installed' || \
   ! gst-inspect-1.0 webrtcbin >/dev/null 2>&1 || \
   ! gst-inspect-1.0 nicesrc >/dev/null 2>&1 || \
   ! gst-inspect-1.0 nicesink >/dev/null 2>&1 || \
   ! gst-inspect-1.0 nvv4l2h264enc >/dev/null 2>&1 || \
   ! gst-inspect-1.0 h264parse >/dev/null 2>&1; then
  printf '采集端离线安装后仍缺少 GStreamer 插件或 NVIDIA JetPack 编码器，请核对 JetPack 版本和本地依赖包。\n' >&2
  exit 1
fi
for plugin in webrtcbin nicesrc nicesink nvv4l2h264enc h264parse rtph264pay; do
  gst-inspect-1.0 "$plugin" >/dev/null 2>&1 || {
    printf '采集端缺少 GStreamer 插件：%s\n' "$plugin" >&2
    exit 1
  }
done
if [ ! -f /opt/ros/humble/setup.bash ]; then
  printf '缺少 ROS 2 Humble：请先安装并配置 ROS 2 Humble，再重新运行安装脚本。\n' >&2
  exit 1
fi
if ! command -v rs-enumerate-devices >/dev/null 2>&1; then
  printf '缺少 librealsense 工具：请先安装与 Orin/JetPack 匹配的 RealSense 驱动，再重新运行安装脚本。\n' >&2
  exit 1
fi
camera_python_ready=0
if /usr/bin/python3 -c 'import pyrealsense2 as rs; rs.context()' 2>/dev/null; then
  camera_python_ready=1
else
  for module in /home/*/.local/lib/python*/site-packages/pyrealsense2*; do
    [ -e "$module" ] || continue
    camera_user="$(stat -c '%U' "$module")"
    if runuser -u "$camera_user" -- /usr/bin/python3 -c 'import pyrealsense2 as rs; rs.context()' 2>/dev/null; then
      camera_python_ready=1
      break
    fi
  done
fi
if [ "$camera_python_ready" -eq 0 ]; then
  printf '警告：未找到与系统驱动匹配的 pyrealsense2 Python 绑定；远控仅控制不可用，近控与常规采集仍可使用。\n' >&2
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
