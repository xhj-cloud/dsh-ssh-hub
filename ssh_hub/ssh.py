"""同步密钥与配置到 ~/.ssh，并执行 ssh 连接 / 远程命令。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ssh_hub.config import SSH_CONFIG_NAME, store_dir
from ssh_hub.keychain import get_password
from ssh_hub.store import Server

FRAGMENT_HEADER = "# 由 dsh-ssh-hub 自动生成，请勿手动编辑。运行 ssh-hub sync 重新生成。\n"


def _log(msg: str) -> None:
    """把诊断步骤追加到密钥库的 ssh-hub.log（便于排查连接问题）。"""
    try:
        store = store_dir()
        store.mkdir(parents=True, exist_ok=True)
        with open(store / "ssh-hub.log", "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def _identity_name(alias: str) -> str:
    return f"dsh_hub_{alias}"


def _server_block(server: Server, ssh_dir: Path) -> str:
    identity = ssh_dir / _identity_name(server.alias)
    lines = [
        f"Host {server.alias}",
        f"    HostName {server.host}",
        f"    User {server.user}",
        f"    Port {server.port}",
    ]
    if server.key:
        lines.append(f"    IdentityFile {identity}")
        lines.append("    IdentitiesOnly yes")
    return "\n".join(lines)


def sync(store: Path, ssh_dir: Path, servers: list[Server]) -> None:
    """把受管密钥复制到 ~/.ssh，并幂等写入 Include 配置片段。"""
    ssh_dir.mkdir(parents=True, exist_ok=True)
    try:
        ssh_dir.chmod(0o700)
    except OSError:
        pass

    blocks: list[str] = []
    for server in servers:
        if server.key:
            key_src = store / server.key
            pub_src = Path(str(key_src) + ".pub")
            dst = ssh_dir / _identity_name(server.alias)
            if key_src.exists():
                shutil.copy2(key_src, dst)
                try:
                    dst.chmod(0o600)
                except OSError:
                    pass
            if pub_src.exists():
                shutil.copy2(pub_src, Path(str(dst) + ".pub"))
        blocks.append(_server_block(server, ssh_dir))

    fragment = ssh_dir / SSH_CONFIG_NAME
    body = "\n\n".join(blocks)
    fragment.write_text(FRAGMENT_HEADER + body + ("\n" if body else ""), encoding="utf-8")
    try:
        fragment.chmod(0o600)
    except OSError:
        pass

    main_cfg = ssh_dir / "config"
    include_line = f"Include {SSH_CONFIG_NAME}"
    if main_cfg.exists():
        text = main_cfg.read_text(encoding="utf-8")
        if include_line not in text:
            main_cfg.write_text(include_line + "\n" + text, encoding="utf-8")
    else:
        main_cfg.write_text(include_line + "\n", encoding="utf-8")
    try:
        main_cfg.chmod(0o600)
    except OSError:
        pass


def run_ssh(args: list[str]) -> int:
    """执行 ssh（连接或远程命令），透传 stdin/stdout/stderr。"""
    if shutil.which("ssh") is None:
        print("错误: 未找到 ssh 命令", file=sys.stderr)
        return 1
    _log(f"ssh 密钥尝试: ssh -o ConnectTimeout=120 -o BatchMode=yes {' '.join(args)}")
    try:
        proc = subprocess.run(["ssh", "-o", "ConnectTimeout=120", "-o", "BatchMode=yes", *args], timeout=120)
    except subprocess.TimeoutExpired:
        _log("ssh 密钥尝试超时(120s)")
        print("错误: ssh 密钥认证超时(120s)", file=sys.stderr)
        return 124
    _log(f"ssh 密钥尝试退出码={proc.returncode}")
    return proc.returncode


def _askpass_script() -> Path:
    """生成 SSH_ASKPASS 脚本：从环境变量 DHSH_SSH_PASSWORD 输出密码（不落 argv）。"""
    store = store_dir()
    store.mkdir(parents=True, exist_ok=True)
    path = store / ".dsh_askpass.sh"
    path.write_text('#!/bin/sh\nprintf "%s\\n" "$DHSH_SSH_PASSWORD"\n', encoding="utf-8")
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _run_with_password(cmd: str, argv: list[str], password: str, timeout: int = 120) -> int:
    """用 SSH_ASKPASS 以密码驱动 ssh/scp（无需 pty；OpenSSH >= 8.4 支持）。

    密码通过环境变量传给 askpass 脚本，不落在命令行参数里。
    """
    askpass = _askpass_script()
    _log(f"密码回退: {cmd} {' '.join(argv)} (askpass={askpass})")
    env = dict(os.environ)
    env["SSH_ASKPASS"] = str(askpass)
    env["SSH_ASKPASS_REQUIRE"] = "force"  # 即使有 TTY 也强制走 askpass，避免卡在终端提示
    env["DHSH_SSH_PASSWORD"] = password
    try:
        proc = subprocess.run(
            [
                cmd,
                "-o", "ConnectTimeout=120",
                "-o", "NumberOfPasswordPrompts=1",
                "-o", "PubkeyAuthentication=no",
                "-o", "PreferredAuthentications=password",
                "-o", "StrictHostKeyChecking=accept-new",
                *argv,
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
        _log(f"密码回退退出码={proc.returncode}")
        return proc.returncode
    except subprocess.TimeoutExpired:
        _log(f"密码回退超时({timeout}s)")
        print("错误: 密码认证超时（超过 %ss）" % timeout, file=sys.stderr)
        return 124
    finally:
        try:
            askpass.unlink(missing_ok=True)
        except OSError:
            pass


def run_ssh_with_password(alias: str, command: list[str], password: str, timeout: int = 120) -> int:
    """用密码连接并执行远程命令（SSH_ASKPASS 驱动，透传 stdio）。"""
    return _run_with_password("ssh", [alias, *command], password, timeout)


def run_scp_with_password(
    kind: str,
    alias: str,
    source: str,
    dest: str,
    password: str,
    recursive: bool = False,
    timeout: int = 120,
) -> int:
    """用密码执行 scp 传输。kind: 'get' 远程->本地；'put' 本地->远程。"""
    if kind == "get":
        argv = [f"{alias}:{source}", dest]
    else:
        argv = [source, f"{alias}:{dest}"]
    if recursive:
        argv = ["-r", *argv]
    return _run_with_password("scp", argv, password, timeout)


def try_run_with_password(alias: str, command: list[str]) -> int | None:
    """密钥认证失败后尝试密码回退；未保存密码返回 None。"""
    pw = get_password(alias)
    if pw is None:
        return None
    return run_ssh_with_password(alias, command, pw)
