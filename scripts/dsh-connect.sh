#!/usr/bin/env bash
# dsh 环境辅助脚本：统一通过 dsh-ssh-hub 连接远程服务器。
# 前置条件：已运行 ssh-hub sync（之后系统 ssh 也能直接使用同一别名）。
#
# 用法:
#   scripts/dsh-connect.sh <alias>                # 交互式连接
#   scripts/dsh-connect.sh <alias> <cmd...>       # 远程执行命令
set -euo pipefail

ALIAS="${1:-}"
if [[ -z "$ALIAS" ]]; then
  echo "用法: $0 <alias> [command...]" >&2
  exit 2
fi
shift
exec ssh "$ALIAS" "$@"
