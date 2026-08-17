"""服务器清单（servers.json）的读写。"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
INVENTORY_NAME = "servers.json"


class HubError(Exception):
    """dsh-ssh-hub 业务错误。"""


@dataclass
class Server:
    alias: str
    host: str
    user: str = "root"
    port: int = 22
    key: str | None = None  # 相对密钥库的私钥路径（不含 .pub）
    created_at: str = ""


def validate_alias(alias: str) -> str:
    if not ALIAS_RE.match(alias):
        raise HubError(
            f"非法的服务器别名 {alias!r}（仅允许字母、数字、下划线、短横线，最长 64 字符）"
        )
    return alias


class Inventory:
    """服务器清单，数据保存在密钥库的 servers.json。"""

    def __init__(self, store: Path) -> None:
        self.store = store
        self.path = store / INVENTORY_NAME

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "servers": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HubError(f"清单文件损坏: {self.path} ({exc})") from exc

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def list_servers(self) -> list[Server]:
        return [Server(**s) for s in self._load()["servers"].values()]

    def get(self, alias: str) -> Server:
        raw = self._load()["servers"].get(alias)
        if raw is None:
            raise HubError(f"别名 {alias!r} 不存在")
        return Server(**raw)

    def add(self, server: Server) -> Server:
        data = self._load()
        if server.alias in data["servers"]:
            raise HubError(f"别名 {server.alias!r} 已存在，请先删除或换用其它别名")
        validate_alias(server.alias)
        server.created_at = datetime.now(timezone.utc).isoformat()
        data["servers"][server.alias] = asdict(server)
        self._save(data)
        return server

    def remove(self, alias: str) -> Server:
        data = self._load()
        raw = data["servers"].pop(alias, None)
        if raw is None:
            raise HubError(f"别名 {alias!r} 不存在")
        self._save(data)
        return Server(**raw)

    def update(self, server: Server) -> Server:
        data = self._load()
        if server.alias not in data["servers"]:
            raise HubError(f"别名 {server.alias!r} 不存在")
        data["servers"][server.alias] = asdict(server)
        self._save(data)
        return server
