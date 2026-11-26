#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUITE="${DM_UBUNTU_SUITE:-jammy}"
BASE="$ROOT/third_party/offline/$SUITE"
mkdir -p "$BASE"
STAGE="$(mktemp -d "$BASE/.build.XXXXXX")"
APT_ROOT="$(mktemp -d)"
cleanup() {
  python3 -c 'import shutil,sys; [shutil.rmtree(p, ignore_errors=True) for p in sys.argv[1:]]' "$APT_ROOT" "$STAGE"
}
trap cleanup EXIT

download_debs() {
  local arch="$1"
  local codename="$2"
  local mirror="$3"
  local output="$4"
  shift 4
  local lists="$APT_ROOT/lists-$arch"
  local cache="$APT_ROOT/cache-$arch"
  local status="$APT_ROOT/status-$arch"
  local sources="$APT_ROOT/sources-$arch.list"
  mkdir -p "$lists/partial" "$cache/archives/partial" "$output"
  : > "$status"
  printf 'deb [arch=%s] %s %s main universe\n' "$arch" "$mirror" "$codename" > "$sources"
  printf 'deb [arch=%s] %s %s-updates main universe\n' "$arch" "$mirror" "$codename" >> "$sources"
  printf 'deb [arch=%s] %s %s-security main universe\n' "$arch" "$mirror" "$codename" >> "$sources"
  local -a apt_opts=(
    -o "APT::Architecture=$arch"
    -o "APT::Architectures=$arch"
    -o "Dir::Etc::sourcelist=$sources"
    -o "Dir::Etc::sourceparts=-"
    -o "Dir::State::lists=$lists"
    -o "Dir::State::status=$status"
    -o "Dir::Cache::archives=$cache/archives"
    -o "Dir::Cache::pkgcache=$APT_ROOT/pkgcache-$arch.bin"
    -o "Dir::Cache::srcpkgcache=$APT_ROOT/srcpkgcache-$arch.bin"
  )
  apt-get "${apt_opts[@]}" update
  apt-get "${apt_opts[@]}" install --download-only --yes --no-install-recommends "$@"
  find "$cache/archives" -maxdepth 1 -type f -name '*.deb' -exec cp -t "$output" {} +
  find "$output" -maxdepth 1 -type f -name '*.deb' -print -quit | grep -q . || {
    printf '没有下载到 %s 的 deb 依赖包\n' "$arch" >&2
    exit 1
  }
}

mkdir -p "$STAGE/amd64/apt" "$STAGE/amd64/python-wheels" "$STAGE/arm64/apt"
download_debs amd64 "$SUITE" http://archive.ubuntu.com/ubuntu "$STAGE/amd64/apt" \
  python3-venv ca-certificates ffmpeg python3-h5py python3-numpy python3-requests python3-pil
download_debs arm64 "$SUITE" http://ports.ubuntu.com/ubuntu-ports "$STAGE/arm64/apt" \
  python3-h5py python3-numpy python3-opencv openssh-client sshpass lsof \
  gstreamer1.0-tools gir1.2-gst-plugins-bad-1.0 gstreamer1.0-plugins-bad \
  gstreamer1.0-nice gstreamer1.0-plugins-good python3-gi python3-gst-1.0
python3 -m pip download --only-binary=:all: --dest "$STAGE/amd64/python-wheels" \
  'numpy==1.26.4' 'h5py==3.16.0' 'pillow==12.3.0' \
  'pyarrow==25.0.1' 'pandas==2.3.3' \
  'python-dateutil==3.0.0' 'pytz==2026.6' 'six==1.17.0' 'tzdata==2026.6'
python3 "$ROOT/packaging/index-offline-repo.py" "$STAGE"
rm -rf "$BASE/server-debs" "$BASE/orin-debs" "$BASE/server-wheels" \
  "$BASE/amd64" "$BASE/arm64" "$BASE/manifest.json"
mv "$STAGE/amd64" "$STAGE/arm64" "$STAGE/manifest.json" "$BASE/"
