"""dsh-ssh-hub 命令行入口。

用法示例:
  ssh-hub init
  ssh-hub add web01 --host 192.168.1.10 --user ubuntu --generate
  ssh-hub sync
  ssh-hub connect web01
  ssh-hub run web01 -- df -h
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path

from ssh_hub import __version__
from ssh_hub.config import SSH_CONFIG_NAME, ssh_dir, store_dir
from ssh_hub.keychain import delete_password, get_password, set_password
from ssh_hub.keys import ensure_secure_dirs, generate_keypair, import_key
from ssh_hub.ssh import CMD_TIMEOUT, CONNECT_TIMEOUT, is_transport_failure, run_ssh, sync, try_run_with_password
from ssh_hub.store import HubError, Inventory, Server
from ssh_hub.transfer import build_scp_args, run_scp, run_scp_transfer


def _store_and_inventory() -> tuple[Path, Inventory]:
    store = store_dir()
    return store, Inventory(store)


def cmd_init(args: argparse.Namespace) -> int:
    store = store_dir()
    ensure_secure_dirs(store, ssh_dir())
    Inventory(store).list_servers()
    print(f"已初始化密钥库: {store}")
    print(f"SSH 目录: {ssh_dir()}（可用 DSH_SSH_DIR / DSH_SSH_HUB_DIR 覆盖）")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    store, inv = _store_and_inventory()
    ensure_secure_dirs(store, ssh_dir())
    key: str | None = None
    if args.generate:
        path = generate_keypair(store, args.alias, force=args.force)
        key = str(path.relative_to(store))
    elif args.key:
        path = import_key(store, args.alias, Path(args.key), force=args.force)
        key = str(path.relative_to(store))
    server = inv.add(
        Server(alias=args.alias, host=args.host, user=args.user, port=args.port, key=key)
    )
    print(f"已添加服务器 {server.alias} -> {server.user}@{server.host}:{server.port}")
    if getattr(args, "password", None) or os.environ.get("DSH_SSH_PASSWORD"):
        pw = _read_password_arg(args)
        if pw is not None:
            set_password(server.alias, pw)
            print(f"已保存 {server.alias} 的密码到 macOS 钥匙串（加密存储）")
    else:
        # 未提供任何密码来源时，静默跳过（不阻塞 add 主流程）
        pass
    print(f"提示: 运行 ssh-hub sync 同步到 {ssh_dir()}，之后即可直接 ssh {server.alias}")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    store, inv = _store_and_inventory()
    server = inv.remove(args.alias)
    if server.key:
        for p in (store / server.key, Path(str(store / server.key) + ".pub")):
            if p.exists():
                p.unlink()
    print(f"已删除服务器 {server.alias}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    _, inv = _store_and_inventory()
    servers = inv.list_servers()
    if not servers:
        print("(暂无服务器，使用 ssh-hub add 添加)")
        return 0
    if args.json:
        entries = []
        for s in servers:
            entry = {"alias": s.alias, "host": s.host, "user": s.user, "port": s.port, "key": s.key}
            if getattr(args, "with_passwords", False):
                entry["has_password"] = get_password(s.alias) is not None
            entries.append(entry)
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return 0
    for s in servers:
        key_mark = f" (key: {s.key})" if s.key else " (默认密钥)"
        if getattr(args, "with_passwords", False):
            pw_mark = " (密码:钥匙串)" if get_password(s.alias) else " (无密码)"
        else:
            pw_mark = ""
        print(f"{s.alias:<24} {s.user}@{s.host}:{s.port}{key_mark}{pw_mark}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    _, inv = _store_and_inventory()
    s = inv.get(args.alias)
    print(
        json.dumps(
            {
                "alias": s.alias,
                "host": s.host,
                "user": s.user,
                "port": s.port,
                "key": s.key,
                "created_at": s.created_at,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_key_gen(args: argparse.Namespace) -> int:
    store, inv = _store_and_inventory()
    ensure_secure_dirs(store, ssh_dir())
    path = generate_keypair(store, args.alias, force=args.force)
    rel = str(path.relative_to(store))
    try:
        server = inv.get(args.alias)
        server.key = rel
        inv.update(server)
        print(f"已生成密钥对并绑定到服务器 {args.alias}: {rel}")
    except HubError:
        print(f"已生成密钥对: {path} (+ .pub)")
        print(f"提示: 该别名尚未登记，可用 ssh-hub add {args.alias} --host <host> --key {rel} 完成添加")
    return 0


def cmd_key_ls(args: argparse.Namespace) -> int:
    store = store_dir()
    kd = store / "keys"
    if not kd.exists():
        print("(密钥库为空)")
        return 0
    for p in sorted(kd.iterdir()):
        if p.suffix != ".pub":
            pub = Path(str(p) + ".pub")
            print(f"{p.name}  {'[有公钥]' if pub.exists() else '[无公钥]'}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    store, inv = _store_and_inventory()
    servers = inv.list_servers()
    sync(store, ssh_dir(), servers)
    print(f"已同步 {len(servers)} 个服务器配置到 {ssh_dir()}")
    print(f"生成配置文件: {ssh_dir() / SSH_CONFIG_NAME}")
    print("现在任何工具（包括 dsh 的 bash 命令）都可以直接 ssh <alias> 使用。")
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    _, inv = _store_and_inventory()
    server = inv.get(args.alias)
    print(f"连接 {server.alias} ({server.user}@{server.host}:{server.port}) ...", file=sys.stderr)
    rc, key_err = run_ssh([server.alias], capture_stderr=True)
    if is_transport_failure(rc):
        # 第一跳 stderr 已缓冲，不透传；回退失败时再输出
        fallback_rc = try_run_with_password(server.alias, [])
        if fallback_rc == 0:
            return 0
        # 回退也失败：输出第一跳的 stderr 供诊断
        if key_err:
            print(key_err, file=sys.stderr, end="")
        return fallback_rc
    # 非传输层失败：第一跳 stderr 已透传（capture_stderr=True 时不在此处输出）
    if key_err:
        print(key_err, file=sys.stderr, end="")
    return rc


def cmd_run(args: argparse.Namespace) -> int:
    _, inv = _store_and_inventory()
    server = inv.get(args.alias)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("用法: ssh-hub run <alias> [--cwd DIR] -- <command...>", file=sys.stderr)
        return 2
    quoted = [shlex.quote(c) for c in command]
    if args.cwd:
        quoted = ["cd", shlex.quote(args.cwd), "&&", *quoted]
    timeout = getattr(args, "timeout", None) or CMD_TIMEOUT
    rc, key_err = run_ssh([server.alias, *quoted], timeout=timeout, capture_stderr=True)
    if is_transport_failure(rc):
        # 传输层失败（连接/认证）：缓冲第一跳 stderr，尝试密码回退
        fallback_rc = try_run_with_password(server.alias, quoted, timeout=timeout)
        if fallback_rc == 0:
            return 0
        # 回退也失败：输出第一跳的 stderr 供诊断
        if key_err:
            print(key_err, file=sys.stderr, end="")
        return fallback_rc
    # 远端命令自身返回非零（正常情况）：输出第一跳 stderr（如有），直接返回
    if key_err:
        print(key_err, file=sys.stderr, end="")
    return rc


def cmd_get(args: argparse.Namespace) -> int:
    store, inv = _store_and_inventory()
    server = inv.get(args.alias)
    local = args.local_path or "."
    argv = build_scp_args(server, store, "get", args.remote_path, local, recursive=args.recursive)
    print(f"下载 {args.alias}:{args.remote_path} -> {local}", file=sys.stderr)
    rc = run_scp(argv)
    if is_transport_failure(rc):
        pw = get_password(args.alias)
        if pw is not None:
            print("密钥认证失败，尝试密码认证 ...", file=sys.stderr)
            return run_scp_transfer("get", server, args.remote_path, local, pw, recursive=args.recursive)
    return rc


def cmd_put(args: argparse.Namespace) -> int:
    store, inv = _store_and_inventory()
    server = inv.get(args.alias)
    argv = build_scp_args(server, store, "put", args.local_path, args.remote_path, recursive=args.recursive)
    print(f"上传 {args.local_path} -> {args.alias}:{args.remote_path}", file=sys.stderr)
    rc = run_scp(argv)
    if is_transport_failure(rc):
        pw = get_password(args.alias)
        if pw is not None:
            print("密钥认证失败，尝试密码认证 ...", file=sys.stderr)
            return run_scp_transfer("put", server, args.local_path, args.remote_path, pw, recursive=args.recursive)
    return rc


def _read_password_arg(args: argparse.Namespace) -> str | None:
    """按 DSH_SSH_PASSWORD 环境变量 > --password > 交互输入 的顺序取密码。

    环境变量优先于命令行参数：环境变量不出现在 ps aux / shell history 中，
    而 --password 会落在 argv 里，同机其他用户可通过 ps 看到。
    """
    env_pw = os.environ.get("DSH_SSH_PASSWORD")
    if env_pw:
        return env_pw
    if getattr(args, "password", None):
        return args.password
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass(f"为 {args.alias} 输入密码: ")
    print("错误: 未提供密码（环境变量 DSH_SSH_PASSWORD 或 --password <pwd>）", file=sys.stderr)
    return None


def cmd_password(args: argparse.Namespace) -> int:
    alias = args.alias
    if args.action == "set":
        pw = _read_password_arg(args)
        if pw is None:
            return 1
        set_password(alias, pw)
        print(f"已保存 {alias} 的密码到 macOS 钥匙串（加密存储）")
        return 0
    if args.action == "get":
        pw = get_password(alias)
        if pw is None:
            print(f"未找到 {alias} 的密码", file=sys.stderr)
            return 1
        print(pw)
        return 0
    if args.action == "rm":
        if delete_password(alias):
            print(f"已删除 {alias} 的密码")
            return 0
        print(f"未找到 {alias} 的密码", file=sys.stderr)
        return 1
    return 2


def cmd_backup(args: argparse.Namespace) -> int:
    store = store_dir()
    if not store.exists():
        print("密钥库尚未初始化，请先运行 ssh-hub init", file=sys.stderr)
        return 1
    out_dir = Path(args.dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    import tempfile

    # 用临时目录打包：密钥库 + SSH 配置片段（恢复时 Include 片段也一起还原）
    fragment = ssh_dir() / SSH_CONFIG_NAME
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "dsh-ssh-hub"
        shutil.copytree(store, stage, symlinks=True)
        if fragment.exists():
            shutil.copy2(fragment, stage / SSH_CONFIG_NAME)
        base = out_dir / f"dsh-ssh-hub-backup-{stamp}"
        shutil.make_archive(str(base), "gztar", root_dir=tmp, base_dir="dsh-ssh-hub")

    backup_path = Path(str(base) + ".tar.gz")
    print(f"备份完成: {backup_path}")
    if fragment.exists():
        print(f"（含 SSH 配置片段 {SSH_CONFIG_NAME}，恢复后仍需运行 ssh-hub sync 重新注入 Include）")

    # 可选加密：openssl aes-256-cbc -pbkdf2（需要系统安装 openssl）
    if getattr(args, "encrypt", False):
        if shutil.which("openssl") is None:
            print("错误: 未找到 openssl 命令，无法加密备份", file=sys.stderr)
            return 1
        import getpass
        import subprocess as _sp

        pw = os.environ.get("DSH_SSH_BACKUP_PASSWORD")
        if not pw:
            pw = getpass.getpass("备份加密密码: ")
        enc_path = Path(str(backup_path) + ".enc")
        # 密码通过环境变量传给 openssl 的 -pass env: 方式，不落 argv
        env = dict(os.environ, DSH_SSH_BACKUP_PASSWORD=pw)
        try:
            _sp.run(
                [
                    "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
                    "-in", str(backup_path), "-out", str(enc_path),
                    "-pass", "env:DSH_SSH_BACKUP_PASSWORD",
                ],
                check=True,
                capture_output=True,
                env=env,
            )
        except _sp.CalledProcessError as exc:
            print(f"加密失败: {exc.stderr.decode() if exc.stderr else exc}", file=sys.stderr)
            return 1
        backup_path.unlink()
        print(f"加密备份: {enc_path}")
        print("恢复命令: openssl enc -d -aes-256-cbc -pbkdf2 -in <file>.enc -out backup.tar.gz -pass env:DSH_SSH_BACKUP_PASSWORD")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ssh-hub",
        description="集中管理远程服务器 SSH 密钥的 CLI 工具（密钥统一存放于 ~/.ssh，供 dsh 与系统 ssh 复用）。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化密钥库目录")
    p_init.set_defaults(func=cmd_init)

    p_add = sub.add_parser("add", help="登记一台远程服务器")
    p_add.add_argument("alias")
    p_add.add_argument("--host", required=True, help="服务器 IP 或域名")
    p_add.add_argument("--user", default="root", help="登录用户名（默认 root）")
    p_add.add_argument("--port", type=int, default=22, help="SSH 端口（默认 22）")
    key_group = p_add.add_mutually_exclusive_group()
    key_group.add_argument("--generate", action="store_true", help="同时生成新的 ed25519 密钥对")
    key_group.add_argument("--key", metavar="PATH", help="导入已有私钥（复制进密钥库）")
    p_add.add_argument("--force", action="store_true", help="覆盖已存在的密钥")
    p_add.add_argument(
        "--password",
        help="同时把登录密码存入 macOS 钥匙串（加密）；"
        "推荐用 DSH_SSH_PASSWORD 环境变量代替，避免密码出现在 ps aux / shell history 中",
    )
    p_add.set_defaults(func=cmd_add)

    p_rm = sub.add_parser("rm", help="删除服务器登记（连同其受管密钥）")
    p_rm.add_argument("alias")
    p_rm.set_defaults(func=cmd_rm)

    p_ls = sub.add_parser("list", help="列出所有服务器")
    p_ls.add_argument("--json", action="store_true", help="JSON 输出（便于 dsh / 脚本解析）")
    p_ls.add_argument(
        "--with-passwords",
        action="store_true",
        help="查询钥匙串显示密码状态（每台服务器一次 subprocess 调用，服务器多时较慢）",
    )
    p_ls.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="查看单个服务器详情")
    p_show.add_argument("alias")
    p_show.set_defaults(func=cmd_show)

    p_key = sub.add_parser("key", help="密钥管理子命令")
    key_sub = p_key.add_subparsers(dest="key_cmd", required=True)
    p_kg = key_sub.add_parser("gen", help="生成 ed25519 密钥对并绑定到别名")
    p_kg.add_argument("alias")
    p_kg.add_argument("--force", action="store_true")
    p_kg.set_defaults(func=cmd_key_gen)
    p_kl = key_sub.add_parser("ls", help="列出密钥库中的密钥")
    p_kl.set_defaults(func=cmd_key_ls)

    p_sync = sub.add_parser("sync", help="同步密钥与配置到 ~/.ssh")
    p_sync.set_defaults(func=cmd_sync)

    p_con = sub.add_parser("connect", help="SSH 连接服务器")
    p_con.add_argument("alias")
    p_con.set_defaults(func=cmd_connect)

    p_run = sub.add_parser("run", help="在远程服务器上执行命令")
    p_run.add_argument("alias")
    p_run.add_argument("--cwd", help="远程工作目录（先 cd 再执行）")
    p_run.add_argument("--timeout", type=int, default=None, help=f"命令超时秒数（默认 {CMD_TIMEOUT}；连接超时固定 {CONNECT_TIMEOUT}s）")
    p_run.add_argument("command", nargs=argparse.REMAINDER)
    p_run.set_defaults(func=cmd_run)

    p_get = sub.add_parser("get", help="从远程服务器下载文件/目录（scp）")
    p_get.add_argument("alias")
    p_get.add_argument("remote_path")
    p_get.add_argument("local_path", nargs="?", default=".", help="本地保存路径（默认当前目录）")
    p_get.add_argument("--recursive", action="store_true", help="递归下载目录")
    p_get.set_defaults(func=cmd_get)

    p_put = sub.add_parser("put", help="上传本地文件/目录到远程服务器（scp）")
    p_put.add_argument("alias")
    p_put.add_argument("local_path")
    p_put.add_argument("remote_path")
    p_put.add_argument("--recursive", action="store_true", help="递归上传目录")
    p_put.set_defaults(func=cmd_put)

    p_pw = sub.add_parser("password", help="管理服务器密码（macOS 钥匙串加密存储）")
    pw_sub = p_pw.add_subparsers(dest="action", required=True)
    p_pws = pw_sub.add_parser("set", help="保存/更新密码")
    p_pws.add_argument("alias")
    p_pws.add_argument(
        "--password",
        help="明文密码（可选；推荐用 DSH_SSH_PASSWORD 环境变量代替，"
             "避免密码出现在 ps aux / shell history 中）",
    )
    p_pws.set_defaults(func=cmd_password)
    p_pwg = pw_sub.add_parser("get", help="读取密码（明文输出）")
    p_pwg.add_argument("alias")
    p_pwg.set_defaults(func=cmd_password)
    p_pwr = pw_sub.add_parser("rm", help="删除密码")
    p_pwr.add_argument("alias")
    p_pwr.set_defaults(func=cmd_password)

    p_bak = sub.add_parser("backup", help="备份密钥库为 tar.gz（含 SSH 配置片段）")
    p_bak.add_argument("--dir", default=".", help="备份输出目录（默认当前目录）")
    p_bak.add_argument(
        "--encrypt",
        action="store_true",
        help="用 openssl aes-256-cbc -pbkdf2 加密备份（密码从 DSH_SSH_BACKUP_PASSWORD 读取或交互输入）",
    )
    p_bak.set_defaults(func=cmd_backup)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except HubError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
