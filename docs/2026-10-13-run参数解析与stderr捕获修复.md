# 2026-10-13 run 命令参数解析与 stderr 捕获修复

在 dsh 插件接入后的 macOS 实测中，`ssh-hub run` 连续暴露 3 个问题：`--cwd/--timeout`
参数完全失效、`capture_stderr` 路径直接崩溃、以及远端非零退出码触发密码回退导致的
**双重执行**。三处均已修复并逐项实证验证（真实主机 + 端到端去重标记），测试
14/14 通过。

## 问题 1：`capture_stderr=True` 时 run_ssh 崩溃（fileno）

**现象**

```text
$ ssh-hub run dell-c4130 hostname
io.UnsupportedOperation: fileno
```

**根因**

`subprocess.run(stderr=StringIO())` 会崩溃：`subprocess` 的 `stderr=` 参数需要真实
文件描述符（fd），而 `io.StringIO` 没有 `fileno()`。该路径在 `capture_stderr=True`
（run 第一跳）时必然触发。

**修复**（`ssh_hub/ssh.py::run_ssh`，commit 34dbd68）

- 捕获分支改用 `stderr=subprocess.PIPE, text=True`，返回 `proc.stderr or ""`
- `capture_stderr=False` 时不传 `stderr` 参数（透传到父进程，行为不变）

## 问题 2：`--cwd/--timeout` 被吞，ssh 报 `illegal option -- -`

**现象**

```text
$ ssh-hub run dell-c4130 --cwd /tmp pwd
# exit 255
# ssh: illegal option -- -
# （命令根本没有执行；ssh-hub.log 里可见 --cwd /tmp 被原样传给 ssh）
```

任何带 `--cwd` 或 `--timeout` 的调用都这样；不带这两个参数时正常。

**根因**

`command` 参数使用 `nargs=argparse.REMAINDER`：它会把 `<alias>` 之后的**所有**
token（包括 `--cwd`、`--timeout` 及其值）全收进 `command`。于是 `args.cwd` /
`args.timeout` 永远是 None，这两个选项被 `shlex.quote` 后原样拼进远程命令串，
最终变成 `ssh ... --cwd /tmp pwd`——macOS 版 ssh 的 getopt 把 `--cwd` 解析成
自己的选项，直接 `illegal option -- -` 退出 255，远程命令从未执行。

**修复**（`ssh_hub/cli.py::build_parser`，commit 5a926b1）

`nargs=argparse.REMAINDER` → `nargs="*"`。`"*"` 模式下 argparse 正确识别已注册的
选项（`--cwd/--timeout`），只把剩余 token 收进 `command`，并自动消费 `--`
分隔符。

## 问题 3：双重执行 / stderr 混流 / 超时混用 / password_get（①②③⑥）

**现象**

- ① 远端命令以非零退出（如 `curl -f` 下载失败）时，`rc=1` 被误判为"认证失败"，
  密码回退通道把**同一条命令再执行一遍**。对非幂等操作（POST/提交类）就是重复提交，
  且最终返回回退那次的退出码，掩盖了真实结果。
- ② 第一跳的 stderr 直接打到终端，与回退那次的输出混在一起，无法区分。
- ③ 连接握手与命令执行共用同一个超时（120s）：主机掉线时要干等满 120 秒。
- ⑥ agent 侧 enum 里存在 `password_get`，可读出钥匙串明文密码。

**修复**（`ssh_hub/ssh.py` / `ssh_hub/cli.py` / `plugin/index.js`，commit 98ff466）

- ① 新增 `is_transport_failure(rc)`：仅 `255`（OpenSSH 连接/认证层）与
  `124`（超时）触发密码回退；远端命令自身退出码（1–254）不再误触发。
  `cmd_get/cmd_put`（scp）同步对齐：仅 255 触发回退。
- ② 第一跳 stderr 缓冲；回退成功时只输出机读标记
  `[key-auth-failed → password-ok]`；回退也失败时才透传第一跳 stderr 供诊断。
- ③ `ConnectTimeout` 120s → 15s（仅管 TCP/SSH 握手）；命令超时独立，
  `run` 新增 `--timeout` 可调（插件层默认 600s，上限 3600s）。
- ⑥ agent 工具 enum 移除 `password_get`；人类 CLI（`ssh-hub password get`）不受影响。

## 验证

```text
$ python -m unittest discover -s tests -v
Ran 14 tests
OK
```

- 98ff466 新增 `TransportFailureTest`（6 条）：锁定非零退出码不触发回退、
  255/124 触发回退、回退失败传播
- 5a926b1 新增 `RunArgParsingTest`（4 条）：`--cwd + --timeout`、仅 `--cwd`、
  无额外参数、命令内含 `--` 开头参数（`ls --all`）均不被误吞

真实主机（dell-c4130）端到端矩阵：

| 用例 | 结果 |
|---|---|
| `run` + `hostname` | exit 0 |
| `run` + `cwd=/tmp` + `pwd` | exit 0，`/tmp` |
| `run` + `timeout=30` + `hostname` | exit 0 |
| `run` + `cwd` + `timeout` 同时使用 | exit 0，`/tmp` |
| 远程 flag：`ls -la /tmp` | exit 0 |
| 复合命令：`bash -lc 'echo A=$((1+1)); …'` | exit 0 |
| 去重实证：`rm -f /tmp/m; echo once >> /tmp/m; exit 7` | exit_code 7，标记文件仅 1 行（命令只执行一次） |

## 其他

- 残留 nit（未改，测试已锁住当前行为）：`plugin/index.js` 仍把 `--cwd/--timeout`
  排在 `<alias>` 之后。当前能正常工作是因为 argparse 允许选项出现在位置参数之后；
  按 CLI 惯例选项应放在位置参数之前。
- 测试命令以 README 为准：`python -m unittest discover -s tests -v`
  （`-t .` 形式会因 tests/ 无 `__init__.py` 而报 "Start directory is not importable"）。
