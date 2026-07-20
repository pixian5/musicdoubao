#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

echo "0. 关闭正在运行的程序..."
pkill -TERM -f "吸气声弱化工具" 2>/dev/null || true
sleep 1
pkill -KILL -f "吸气声弱化工具" 2>/dev/null || true

echo "1. 彻底清理旧产物..."
rm -rf build dist __pycache__

echo "2. 重新打包..."
if command -v pyinstaller >/dev/null 2>&1; then
  PYI=pyinstaller
elif [ -x "$HOME/Library/Python/3.14/bin/pyinstaller" ]; then
  PYI="$HOME/Library/Python/3.14/bin/pyinstaller"
elif [ -x ".venv/bin/pyinstaller" ]; then
  PYI=".venv/bin/pyinstaller"
else
  echo "错误：找不到 pyinstaller。请先 pip install pyinstaller" >&2
  exit 1
fi

"$PYI" --noconfirm --windowed --name "吸气声弱化工具" --clean \
  --collect-all breath \
  --hidden-import breath \
  --hidden-import breath.detect \
  --hidden-import breath.render \
  --hidden-import breath.limit \
  --hidden-import breath.io \
  --hidden-import breath.config \
  --hidden-import breath.constants \
  --hidden-import breath.segments \
  breath_reduce_mac.py

echo "3. 启动 app（如有弹窗请关注系统提示）..."
open dist/吸气声弱化工具.app

echo "全部完成。"
