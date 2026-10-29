#!/usr/bin/env bash
set -euo pipefail
# 用法：./recycle.sh <本项目内的文件或目录> [回收原因]
ROOT="$(cd "$(dirname "$0")" && pwd)"
python3 - "$ROOT" "$@" <<'PY'
import json
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1])
if len(sys.argv) < 3:
    raise SystemExit('用法：./recycle.sh <文件或目录> [原因]')
source = Path(sys.argv[2])
source = source if source.is_absolute() else root / source
# 不跟随待移动对象本身的符号链接；父目录必须仍在项目内。
source = source.parent.resolve() / source.name
try:
    relative = source.relative_to(root)
except ValueError:
    raise SystemExit('只能回收本项目内的文件或目录')
if not relative.parts or relative.parts[0] in {'recycle_bin', '.git'}:
    raise SystemExit('不能回收项目根目录、Git目录或读取回收站')
if not source.exists() and not source.is_symlink():
    raise SystemExit(f'找不到待回收对象：{relative}')
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
destination = root / 'recycle_bin' / stamp
destination.mkdir(parents=True)
shutil.move(str(source), str(destination / source.name))
(destination / 'record.json').write_text(json.dumps({
    'original_path': str(relative), 'reason': sys.argv[3] if len(sys.argv) > 3 else '用户指定回收',
    'recycled_at': stamp,
}, ensure_ascii=False, indent=2) + '\n')
print(f'已回收：{relative}')
PY
