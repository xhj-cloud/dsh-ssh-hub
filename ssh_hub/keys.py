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
    out.chmod(0o600)
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
    dst.chmod(0o600)
    pub_src = Path(str(src) + ".pub")
    if pub_src.exists():
        shutil.copy2(pub_src, Path(str(dst) + ".pub"))
    return dst
