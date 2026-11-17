#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="$ROOT/dist"
SRC="${DM_PROGRAM_SOURCE:-$HOME/orin-progs}"
TS="$(date +%Y%m%d_%H%M%S)"
VERSION="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text().strip())' "$ROOT/VERSION")"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$DIST"

need() { [ -e "$1" ] || { echo "缺少 $1" >&2; exit 1; }; }

check_package() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
assert sorted(p.name for p in root.glob('*.sh')) == ['install.sh']
assert sorted(p.name for p in root.rglob('*.md')) == ['README.md']
for p in root.rglob('*'):
    assert p.name not in {'__pycache__', '.git', 'logs', 'backups', 'tests', '.secret'}, p
    assert not p.name.endswith(('.log', '.pyc', '.bak', '.db', '.db-wal', '.db-shm')), p
    assert p.name != 'camera_ports.conf', p
lines = (root / 'install.sh').read_text().splitlines()
assert all(not line.lstrip().startswith('#') for line in lines[1:])
PY
}

build_orin() {
  local P="$STAGE/dm-collect"
  local DEPLOY="$SRC/workspace/yc_M3/rl_deploy_3cam"
  local SERVICE="$SRC/opt/apps/roboticsservice"
  local PATCHELF_BIN="${PATCHELF:-$(command -v patchelf || true)}"
  [ -n "$PATCHELF_BIN" ] || PATCHELF_BIN="$HOME/.local/bin/patchelf"
  need "$PATCHELF_BIN"
  need "$DEPLOY/build/rl_deploy"
  need "$SERVICE/RoboticsServiceProcess"
  need "$SRC/runtime/manifest.json"
  python3 - "$DEPLOY/build/rl_deploy" <<'PY'
from pathlib import Path
import sys
binary = Path(sys.argv[1]).read_bytes()
for token in (b'ARMDOG_CAMERA_PORTS_FILE', b'ARMDOG_SHARED_FRAME_CAMERAS', b'ARMDOG_DATA_ROOT'):
    if token not in binary:
        raise SystemExit('请先同步最新采集程序二进制')
PY
  mkdir -p "$P/prog/rl_deploy/build" "$P/prog/rl_deploy/scripts" "$P/prog/service" "$P/runtime" "$P/agent" "$P/cam"
  cp -a "$DEPLOY/build/rl_deploy" "$P/prog/rl_deploy/build/"
  for name in conf model policy; do
    need "$DEPLOY/$name"
    cp -a "$DEPLOY/$name" "$P/prog/rl_deploy/"
  done
  for name in postprocess_vla_episode.py standardize_episode_timestamps.py; do
    cp -a "$DEPLOY/scripts/$name" "$P/prog/rl_deploy/scripts/"
  done
  for name in RoboticsServiceProcess runService.sh setting.ini epDBBackup.s3db lib plugins SDK translations; do
    cp -a "$SERVICE/$name" "$P/prog/service/"
  done
  for file in "$SERVICE/"*.so*; do cp -a "$file" "$P/prog/service/"; done
  cp -a "$SRC/runtime/deploy" "$SRC/runtime/service" "$SRC/runtime/manifest.json" "$P/runtime/"
  PATCHELF="$PATCHELF_BIN" python3 "$ROOT/packaging/prepare-orin-runtime.py" "$P"
  cp -a "$ROOT/orin/camera_shm_viewer.py" "$P/cam/"
  for name in agent runner cam system; do cp -a "$ROOT/server/$name.py" "$P/agent/"; done
  cp -a "$ROOT/packaging/host_config.orin.json" "$P/host_config.json"
  cp -a "$ROOT/packaging/install-orin.sh" "$P/install.sh"
  cp -a "$ROOT/packaging/README-orin.md" "$P/README.md"
  python3 - "$P" <<'PY'
from pathlib import Path
import shutil
import sys
root = Path(sys.argv[1])
for p in sorted(root.rglob('*'), reverse=True):
    if p.name == '__pycache__':
        shutil.rmtree(p)
    elif p.is_file() and (p.suffix.lower() in {'.md', '.log', '.pyc'} or '.bak' in p.name):
        if p != root / 'README.md':
            p.unlink()
PY
  chmod +x "$P/install.sh"
  check_package "$P"
  local OUT="$DIST/orin-collect-v$VERSION-$TS.tar.zst"
  tar -I 'zstd -3 -T0' -cf "$OUT" -C "$STAGE" dm-collect
  echo "$OUT"
}

build_server() {
  local P="$STAGE/dm-web"
  mkdir -p "$P/server"
  cp -a "$ROOT/index.html" "$ROOT/assets" "$P/"
  for name in api run_web auth db runner system cam geo auto_qc lerobot_export; do
    cp -a "$ROOT/server/$name.py" "$P/server/"
  done
  cp -a "$ROOT/packaging/host_config.server.json" "$P/host_config.json"
  cp -a "$ROOT/packaging/install-server.sh" "$P/install.sh"
  cp -a "$ROOT/packaging/README-server.md" "$P/README.md"
  find "$P" -type d -exec chmod 755 {} +
  find "$P" -type f -exec chmod 644 {} +
  chmod +x "$P/install.sh"
  check_package "$P"
  local OUT="$DIST/server-review-v$VERSION-$TS.tar.gz"
  tar -czf "$OUT" -C "$STAGE" dm-web
  echo "$OUT"
}

case "${1:-both}" in
  orin) build_orin ;;
  server) build_server ;;
  both) build_orin; build_server ;;
  *) echo "用法：$0 [orin|server|both]" >&2; exit 1 ;;
esac
