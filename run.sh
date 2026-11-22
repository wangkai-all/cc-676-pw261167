#!/usr/bin/env bash
# 入仓修复工作台启动脚本
# 优先使用 .venv（含 scipy，HiGHS 整数规划后端）；
# 没有 .venv 或 scipy 不可用时，自动退回内置分支定界求解器（纯标准库）。
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "创建虚拟环境 .venv …"
  python3 -m venv .venv
  .venv/bin/pip install -q scipy || \
    echo "（scipy 安装失败也没关系，将使用内置分支定界求解器）"
fi

PORT="${PORT:-8074}"
if .venv/bin/python -c "import scipy" 2>/dev/null; then
  exec .venv/bin/python workbench/server.py
else
  echo "提示：未安装 scipy，使用内置分支定界求解器。"
  exec python3 workbench/server.py
fi
