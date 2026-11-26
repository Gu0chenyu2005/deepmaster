#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
OUT="${1:-$ROOT/dist/deepmaster-v${VERSION}-source.tar.gz}"
mkdir -p "$(dirname "$OUT")"
git -C "$ROOT" archive --format=tar.gz --prefix="deepmaster/" --output="$OUT" HEAD
python3 - "$OUT" <<'PY'
import sys
import tarfile

blocked = {".git", ".secret", "deepmaster.db", "logs", "__pycache__"}
with tarfile.open(sys.argv[1], "r:gz") as archive:
    for member in archive.getmembers():
        parts = member.name.split("/")
        if (any(part in blocked for part in parts)
                or any(part == ".env" or part.startswith(".env.") for part in parts)):
            raise SystemExit("发布包包含受限文件: " + member.name)
        if member.name.endswith((".log", ".pyc", ".db", ".db-wal", ".db-shm")):
            raise SystemExit("发布包包含运行时文件: " + member.name)
PY
printf '%s\n' "$OUT"
