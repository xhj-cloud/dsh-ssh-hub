"""密钥生成与权限管理。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ssh_hub.store import HubError


def keys_dir(store: Path) -> Path:
    return store / "keys"


def ensure_secure_dirs(store: Path, ssh_dir: Path) -> None:
    """保证目录存在且权限收紧（密钥库 700、~/.ssh 700）。"""
    for path in (store, keys_dir(store), ssh_dir):
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass


def generate_keypair(store: Path, alias: str, force: bool = False) -> Path:
    """调用系统 ssh-keygen 生成 ed25519 密钥对，返回私钥路径。"""
    out = keys_dir(store) / alias
    if out.exists() and not force:
        raise HubError(f"密钥已存在: {out}（如需覆盖请加 --force）")
    if shutil.which("ssh-keygen") is None:
        raise HubError("未找到 ssh-keygen，请先安装 OpenSSH")
    # Windows 版 ssh-keygen 新建 .pub 时会附加 Everyone 只读 ACL（显式 DACL），
    # 部分沙箱/安全策略会因此锁定该文件，导致写入失败（Permission denied，
    # 留下 0 字节 .pub）。预创建空 .pub 让 ssh-keygen 打开已有文件写入即可
    # 规避；其他平台上该文件只是被截断重写，无副作用。
    pub = Path(str(out) + ".pub")
    try:
        if pub.exists() and pub.stat().st_size == 0:
            pub.unlink()  # 清理上次失败运行遗留的空 .pub
    except OSError:
        pass
    try:
        pub.touch()
    except OSError:
        pass
    proc = subprocess.run(
        [
            "ssh-keygen", "-t", "ed25519",
            "-f", str(out),
            "-N", "",
            "-C", f"{alias}@dsh-ssh-hub",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise HubError(f"ssh-keygen 失败: {proc.stderr.strip()}")
    try:
        out.chmod(0o600)
    except OSError:
        pass  # Windows/沙箱下 chmod 为尽力而为，不影响密钥使用
    return out


def import_key(store: Path, alias: str, source: Path, force: bool = False) -> Path:
    """把已有私钥复制进受管密钥库（同时复制 .pub 公钥）。"""
    src = source.expanduser()
    if not src.is_file():
        raise HubError(f"源密钥不存在: {src}")
    ensure_secure_dirs(store, store.parent)
    dst = keys_dir(store) / alias
    if dst.exists() and not force:
        raise HubError(f"密钥已存在: {dst}（如需覆盖请加 --force）")
    shutil.copy2(src, dst)
    try:
        dst.chmod(0o600)
    except OSError:
        pass
    pub_src = Path(str(src) + ".pub")
    if pub_src.exists():
        shutil.copy2(pub_src, Path(str(dst) + ".pub"))
    return dst
