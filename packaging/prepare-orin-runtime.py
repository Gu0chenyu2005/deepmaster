# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""为采集端暂存目录配置相对动态库路径。"""
import os
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(sys.argv[1])
patchelf = os.environ.get('PATCHELF', 'patchelf')
entries = [
    (root / 'prog/rl_deploy/build/rl_deploy', '$ORIGIN/../../../runtime/deploy'),
    (root / 'prog/service/RoboticsServiceProcess', '$ORIGIN/../../runtime/service:$ORIGIN/lib:$ORIGIN/SDK/arm64'),
]
for group in ('deploy', 'service'):
    entries.extend((p, '$ORIGIN') for p in (root / 'runtime' / group).iterdir() if p.is_file())
for binary, rpath in entries:
    # 暂存文件可能使用硬链接，修改前解除与源文件的共享。
    tmp = binary.with_name(binary.name + '.relocate')
    shutil.copy2(binary, tmp)
    os.replace(tmp, binary)
    subprocess.run([patchelf, '--set-rpath', rpath, str(binary)], check=True)

manifest_path = root / 'runtime/manifest.json'
manifest = json.loads(manifest_path.read_text())
for group, libraries in list(manifest.items()):
    if group not in ('deploy', 'service'):
        del manifest[group]
        continue
    for item in libraries:
        binary = root / 'runtime' / group / item['name']
        item['source_sha256'] = item['sha256']
        item['sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
        item['size'] = binary.stat().st_size
        item['source'] = 'Orin runtime library'
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
