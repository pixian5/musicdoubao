#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "1. 彻底清理旧产物..."
rm -rf build dist __pycache__

echo "2. 重新打包..."
/Users/x/Library/Python/3.14/bin/pyinstaller --noconfirm --windowed --name "吸气声弱化工具" --clean breath_reduce_mac.py

echo "3. 启动 app（如有弹窗请关注系统提示）..."
open dist/吸气声弱化工具.app

echo "全部完成。"
