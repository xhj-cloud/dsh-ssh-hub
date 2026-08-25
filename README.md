# dsh-ssh-hub

集中管理远程服务器 SSH 密钥的 **Python CLI 工具**。密钥统一存放在 `~/.ssh` 下由 dsh 统一管理，
登记后的服务器会写入 SSH 配置别名，**dsh（DeepSeek Harness）与系统 ssh 复用同一套密钥**。

## 特性

- **集中存储**：密钥与服务器清单统一存放在 `~/.ssh/dsh-ssh-hub/`（目录 700 / 私钥 600）
- **一键登记**：`add --generate` 一步完成「登记服务器 + 生成 ed25519 密钥对」
- **自动同步**：`sync` 把密钥复制到 `~/.ssh/`，并在 `~/.ssh/config` 顶部幂等注入
  `Include dsh_ssh_hub_config`，不覆盖你已有的配置
- **开箱即用**：同步后 `ssh <alias>` 即可连接，scp / rsync / dsh 的 bash 命令全部可用
- **远程执行**：`run <alias> -- <cmd>` 免交互执行远程命令
- **加密备份**：`backup` 一键打包密钥库
- **密码管理**：每台服务器密码加密存入 **macOS 钥匙串**（不落明文磁盘），
  `run`/`get`/`put` 密钥认证失败时自动改用密码（SSH_ASKPASS 驱动，无需 pty）
- **可隔离测试**：密钥库与 SSH 目录均可通过环境变量覆盖，测试不触碰真实 `~/.ssh`

## 快速开始

```bash
cd dsh-ssh-hub
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # 联网环境安装 ssh-hub 命令；离线环境可直接用 ./ssh-hub
# 注: 本项目核心功能仅依赖标准库，pip 不可用时
#     ./ssh-hub 与 python -m ssh_hub 等效

ssh-hub init
ssh-hub add web01 --host 192.168.1.10 --user ubuntu --generate
ssh-hub sync

ssh web01                   # 系统 ssh 立即生效
ssh-hub connect web01       # 或经 hub 连接
ssh-hub run web01 -- df -h  # 免交互执行远程命令
```

## 与 dsh 的集成

1. **统一入口**：执行一次 `ssh-hub sync` 后，任何 dsh 的 bash 工具调用都可以直接
   `ssh <alias>`、`scp`、`rsync` 连接远程服务器，密钥由 hub 统一管理，无需重复配置。
2. **辅助脚本**：`scripts/dsh-connect.sh <alias> [cmd...]` 提供带参数校验的连接封装，
   dsh agent 可直接调用。
3. **机器可读清单**：`ssh-hub list --json` 输出 JSON，供 dsh agent 检索已登记的服务器。
4. **环境变量**（可选覆盖，默认即可用）：

   | 变量 | 默认值 | 说明 |
   | --- | --- | --- |
   | `DSH_SSH_HUB_DIR` | `~/.ssh/dsh-ssh-hub` | 密钥库目录（唯一真源） |
   | `DSH_SSH_DIR` | `~/.ssh` | 同步目标 SSH 目录 |

## 命令一览

| 命令 | 说明 |
| --- | --- |
| `ssh-hub init` | 初始化密钥库目录 |
| `ssh-hub add <alias> --host H [--user U] [--port P] [--generate|--key PATH]` | 登记服务器（可顺带生成/导入密钥） |
| `ssh-hub rm <alias>` | 删除服务器登记及受管密钥 |
| `ssh-hub list [--json]` | 列出所有服务器 |
| `ssh-hub show <alias>` | 查看单个服务器详情 |
| `ssh-hub key gen <alias> [--force]` | 生成 ed25519 密钥对（若已登记则自动绑定） |
| `ssh-hub key ls` | 列出密钥库中的密钥 |
| `ssh-hub sync` | 同步密钥与配置到 `~/.ssh` |
| `ssh-hub connect <alias>` | SSH 连接服务器 |
| `ssh-hub run <alias> [--cwd DIR] -- <cmd>` | 远程执行命令（`--cwd` 先切目录，参数自动安全引用） |
| `ssh-hub get <alias> <remote_path> [local_path] [--recursive]` | 从服务器下载文件/目录（scp） |
| `ssh-hub put <alias> <local_path> <remote_path> [--recursive]` | 上传文件/目录到服务器（scp） |
| `ssh-hub password set <alias> [--password PWD]` | 保存服务器密码到 macOS 钥匙串（加密） |
| `ssh-hub password get <alias>` | 读取密码（明文输出，供 agent/脚本） |
| `ssh-hub password rm <alias>` | 删除已保存的密码 |
| `ssh-hub backup [--dir D]` | 备份密钥库为 tar.gz |

## 目录布局

```text
~/.ssh/
├── config                     # 你的原有配置，顶部自动注入 Include（幂等）
├── dsh_ssh_hub_config         # hub 自动生成，请勿手动编辑
├── dsh_hub_<alias>            # 同步出的私钥副本（与 .pub 一起）
└── dsh-ssh-hub/               # 密钥库（唯一真源）
    ├── servers.json           # 服务器清单
    └── keys/<alias>           # 受管私钥
```

## 安全说明

- 密钥库目录 700、私钥 600、配置片段 600，符合 OpenSSH 权限要求
- `servers.json` 仅记录元信息（host/user/port），不含任何密钥材料
- 建议定期 `ssh-hub backup` 并妥善保管备份文件

## dsh 插件安装

`dsh-ssh-hub` 同时是一个 dsh（DeepSeek Harness）主机插件：向运行中的 harness 注册
`ssh_hub` 动态工具，使 agent 可直接通过工具管理密钥、连接远程服务器并做代码操作
（`run` 远程执行 + `get`/`put` scp 文件传输，完成 拉取→修改→上传→远程测试 闭环）。

### 一键安装（推荐）

```bash
dsh plugin --profile web add github:xhj-cloud/dsh-ssh-hub
```

安装后 dsh 自动把本包注册为 profile 的 bundle 层，重启 `dsh web` 生效；之后任何
agent 会话都可直接使用 `ssh_hub` 工具。卸载：`dsh plugin --profile web remove dsh-ssh-hub`。

要求：Node >= 18（随 dsh 自带）；PATH 中有 Python >= 3.10（CLI 为纯标准库，无第三方
依赖；若包目录内存在 `.venv/bin/python` 则优先使用）。

### 本地开发安装

- 插件源码：`plugin/index.js`（零依赖 CJS，子进程调用本项目的 Python CLI）
- 入口：在 `~/.dsh/profiles/web/plugins/dsh-ssh-hub/` 放一个入口文件，并在
  `cordis.patch.yml` 的 `ssh-hub` 条目中指向它（profile 本地路径，非 node_modules），
  config 指定 `projectDir` 为本项目目录
- **重载**：修改 `plugin/index.js` 后，在 plugins 目录新建 `index-rN.js`（复制入口内容）
  并把 patch 的 `name` 换成新文件即可热重载（入口会先清除源码 require 缓存）。
  Python CLI 部分的改动（`ssh_hub/*.py`）无需重载，每次调用即取最新代码
- **密码**：`ssh_hub` 工具的 `add`/`password_set` 会把密码加密存入 macOS 钥匙串，
  任何 agent 无需知道明文即可连接（密钥失败自动回退密码，SSH_ASKPASS 驱动）

## 开发与测试

```bash
python -m unittest discover -s tests -v
```

测试通过 `DSH_SSH_HUB_DIR` / `DSH_SSH_DIR` 环境变量把密钥库与同步目标指向临时目录，
全程不触碰真实 `~/.ssh`。

## License / 许可证

本项目采用 [CC BY-NC 4.0](LICENSE)（Creative Commons 署名-非商业性使用 4.0 国际）协议开源：可以自由查看、使用、修改和分发，但**禁止任何商业用途**。
