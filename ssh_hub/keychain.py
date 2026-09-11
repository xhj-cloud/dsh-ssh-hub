"""macOS 钥匙串中的服务器密码存取（不落明文磁盘）。

密码以 generic-password 形式存入当前用户的登录钥匙串，仅存元信息到
servers.json（标记），凭据本体由系统钥匙串加密保管。

非 macOS 平台（无 `security` 命令）自动降级：读取返回 None，
写入抛出明确错误，其余功能不受影响。
"""
from __future__ import annotations

import shutil
import subprocess

from ssh_hub.store import HubError

SERVICE = "dsh-ssh-hub"


def _available() -> bool:
    """当前平台是否有 macOS 钥匙串（security 命令）。"""
    return shutil.which("security") is not None


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def get_password(alias: str) -> str | None:
    """读取别名对应的密码；未保存返回 None（非 macOS 平台同样返回 None）。"""
    if not _available():
        return None
    proc = _run(["security", "find-generic-password", "-s", SERVICE, "-a", alias, "-w"])
    if proc.returncode != 0:
        return None
    return proc.stdout.rstrip("\n")


def set_password(alias: str, password: str) -> None:
    """保存或更新（-U）别名对应的密码。"""
    if not password:
        raise HubError("密码不能为空")
    if not _available():
        raise HubError("当前平台不支持钥匙串密码存储（仅 macOS 可用）")
    proc = _run(["security", "add-generic-password", "-s", SERVICE, "-a", alias, "-w", password, "-U"])
    if proc.returncode != 0:
        raise HubError(f"保存到钥匙串失败: {proc.stderr.strip()}")


def delete_password(alias: str) -> bool:
    """删除别名对应的密码；返回是否真的删除了。"""
    if not _available():
        return False
    proc = _run(["security", "delete-generic-password", "-s", SERVICE, "-a", alias])
    return proc.returncode == 0


def has_password(alias: str) -> bool:
    return get_password(alias) is not None
