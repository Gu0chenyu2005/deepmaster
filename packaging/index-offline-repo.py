#!/usr/bin/env python3
import gzip
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

base = Path(sys.argv[1])
for directory in (base / 'amd64' / 'apt', base / 'arm64' / 'apt'):
    entries = []
    seen = defaultdict(list)
    for package in sorted(directory.glob('*.deb')):
        fields = subprocess.check_output(
            ['dpkg-deb', '-f', str(package), 'Package', 'Version', 'Architecture'], text=True
        ).splitlines()
        if len(fields) != 3:
            raise SystemExit('无法读取 deb 元数据：' + package.name)
        name, version, architecture = fields
        key = (name, architecture)
        seen[key].append((version, package.name))
        control = subprocess.check_output(['dpkg-deb', '-f', str(package)], text=True).rstrip()
        extra = '\nFilename: %s\nSize: %d\nSHA256: %s' % (
            package.name,
            package.stat().st_size,
            hashlib.sha256(package.read_bytes()).hexdigest(),
        )
        entries.append(control + extra)
    duplicates = {key: values for key, values in seen.items() if len(values) > 1}
    if duplicates:
        details = '; '.join('%s:%s' % (key, values) for key, values in duplicates.items())
        raise SystemExit('离线仓库含有同一依赖的多个版本：' + details)
    index = ('\n\n'.join(entries) + '\n').encode()
    (directory / 'Packages').write_bytes(index)
    (directory / 'Packages.gz').write_bytes(gzip.compress(index))

wheel_versions = defaultdict(list)
for wheel in (base / 'amd64' / 'python-wheels').glob('*.whl'):
    parts = wheel.name.split('-')
    if len(parts) < 5:
        raise SystemExit('无效 wheel 文件名：' + wheel.name)
    wheel_versions[parts[0].lower().replace('_', '-')].append((parts[1], wheel.name))
duplicates = {name: versions for name, versions in wheel_versions.items() if len(versions) > 1}
if duplicates:
    raise SystemExit('wheelhouse 含有同一依赖的多个版本：' + str(duplicates))

files = []
for path in sorted(p for p in base.rglob('*') if p.is_file() and p.name != 'manifest.json'):
    files.append({
        'path': str(path.relative_to(base)),
        'size': path.stat().st_size,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
    })
(base / 'manifest.json').write_text(json.dumps({
    'ubuntu_suite': base.name if not base.name.startswith('.') else base.parent.name,
    'files': files,
}, ensure_ascii=False, indent=2) + '\n')
print('离线依赖索引和哈希清单已生成：%d 个文件' % len(files))
