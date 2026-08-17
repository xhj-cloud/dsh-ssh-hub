"""远程文件传输（基于系统 scp，密钥走 hub 受管库）。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ssh_hub.keychain import get_password
from ssh_hub.ssh import run_scp_with_password
from ssh_hub.store import HubError, Server


def build_scp_args(
    server: Server,
    store: Path,
    kind: str,
    source: str,
    dest: str,
    recursive: bool = False,
) -> list[str]:
    """构造 scp 参数。kind: 'get' 远程->本地；'put' 本地->远程。"""
    if shutil.which("scp") is None:
        raise HubError("未找到 scp 命令，请先安装 OpenSSH")
    args = ["scp", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
    if recursive:
        args.append("-r")
    if server.key:
        key = store / server.key
        if not key.exists():
            raise HubError(f"受管密钥不存在: {key}")
        args += ["-i", str(key)]
    # 与 run 命令一致：走别名（~/.ssh/config），保证与 ssh 相同的认证路径（含 keychain 解锁）
    if kind == "get":
        args.append(f"{server.alias}:{source}")
        args.append(dest)
    else:
        args.append(source)
        args.append(f"{server.alias}:{dest}")
    return args


def run_scp(args: list[str]) -> int:
    """执行 scp，透传 stdio。"""
    proc = subprocess.run(args)
    return proc.returncode


def run_scp_transfer(
    kind: str,
    server: Server,
    source: str,
    dest: str,
    password: str,
    recursive: bool = False,
) -> int:
    """用密码执行 scp 传输。kind: 'get' 远程->本地；'put' 本地->远程。"""
    return run_scp_with_password(
        kind, server.alias, source, dest, password, recursive=recursive, timeout=120
    )
