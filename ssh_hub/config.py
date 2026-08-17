"""路径与运行环境配置。

密钥库默认放在 ~/.ssh/dsh-ssh-hub/，同步目标默认是 ~/.ssh/。
两者都可用环境变量覆盖，便于测试与自定义布局：
  DSH_SSH_HUB_DIR  密钥库目录
  DSH_SSH_DIR      同步目标 SSH 目录
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_STORE_DIR = "DSH_SSH_HUB_DIR"
ENV_SSH_DIR = "DSH_SSH_DIR"

STORE_DIRNAME = "dsh-ssh-hub"
SSH_CONFIG_NAME = "dsh_ssh_hub_config"
SSH_MAIN_CONFIG = "config"


def home() -> Path:
    return Path(os.path.expanduser("~"))


def ssh_dir() -> Path:
    override = os.environ.get(ENV_SSH_DIR)
    return Path(override).expanduser() if override else home() / ".ssh"


def store_dir() -> Path:
    override = os.environ.get(ENV_STORE_DIR)
    return Path(override).expanduser() if override else ssh_dir() / STORE_DIRNAME
