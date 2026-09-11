# 2026-09-12 Windows 兼容性修复与 dsh 插件适配

本项目原为 macOS 设计（钥匙串密码、`.venv/bin/python`、POSIX 路径假设）。在
**Windows 11 + OpenSSH 8.1p1 + dsh（DeepSeek Harness）沙箱** 环境下首次部署时暴露了
4 个问题，其中 2 个导致核心流程（`add --generate`）直接失败。本文档记录问题现象、
根因与修复方式。修复后测试 4/4 通过，全流程冒烟测试正常，插件已在 dsh web profile
以 0.1.1 版本安装验证。

## 问题 1：ssh-keygen 生成的 .pub 为 0 字节（核心阻塞）

**现象**

```text
$ ssh-hub add web01 --host 1.2.3.4 --generate
错误: ssh-keygen 失败: fdopen C:\...\store\keys\web01.pub failed: Permission denied
```

- 私钥文件完整（411 字节，`ssh-keygen -y` 可正常推导公钥）
- `.pub` 文件被创建但只有 **0 字节**
- 退出码 255；该 0 字节 `.pub` 随后被锁定——任何进程（包括文件属主）都无法再写入、
  删除或修改它的 ACL

**根因**

Windows 版 ssh-keygen 新建 `.pub` 时会附加一条显式 DACL：`Everyone: ReadAndExecute`
（公钥设计上对所有人可读）。dsh 的文件沙箱（workspace-write 模式）会拦截对
"Everyone 可访问"文件的后续写操作，于是：

1. ssh-keygen 创建 `.pub`（0 字节，带 Everyone DACL）
2. 往 `.pub` 写内容 → 被沙箱拦截 → `fdopen ... Permission denied`
3. 文件此后对所有修改操作永久拒绝（连删除都不行）

其他平台（macOS/Linux）的 ssh-keygen 不会附加这种 DACL，因此问题只在
"Windows + 沙箱/安全策略" 组合下出现。

**修复**（`ssh_hub/keys.py::generate_keypair`）

调用 ssh-keygen **之前**预创建一个空的 `.pub` 文件（继承目录正常 ACL）：
ssh-keygen 打开已存在的文件写入时不会附加新 DACL，问题绕开。同时在预创建前清理
上次失败运行遗留的 0 字节 `.pub`。对 macOS/Linux 无副作用（文件只是被截断重写）。

```python
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
```

## 问题 2：chmod 在 Windows/沙箱下抛 PermissionError

**现象**

密钥生成成功后，`generate_keypair` 末尾的 `out.chmod(0o600)` 抛
`PermissionError: [WinError 5] 拒绝访问`（沙箱禁止文件属性变更；Windows 上
chmod 实际只控制只读位，非关键操作）。

**修复**

按项目既有惯例（`ensure_secure_dirs` 等已用 try/except 包裹 chmod），把全部 5 处
裸 chmod 调用改为尽力而为（Windows/沙箱下静默跳过，不影响密钥使用）：

- `ssh_hub/keys.py`：`generate_keypair` 的私钥 chmod、`import_key` 的副本 chmod
- `ssh_hub/ssh.py`：`sync` 的密钥副本 chmod、配置片段 chmod；`_askpass_script` 的脚本 chmod

## 问题 3：keychain.py 在非 macOS 平台直接崩溃

**现象**

```text
$ ssh-hub list
FileNotFoundError: [WinError 2] 系统找不到指定的文件
```

`list` 会对每台服务器调用 `get_password()`，后者直接调用 macOS 专属的
`security` 命令；Windows 上没有该命令，subprocess 抛未捕获的 FileNotFoundError，
整个 `list` 命令崩溃（`list --json` 同样受影响）。

**修复**（`ssh_hub/keychain.py`）

以 `shutil.which("security")` 探测平台能力，不可用时优雅降级：

- `get_password` / `has_password` → 返回 `None` / `False`
- `delete_password` → 返回 `False`
- `set_password` → 抛出明确错误「当前平台不支持钥匙串密码存储（仅 macOS 可用）」
  （由 CLI 统一错误处理捕获，退出码 1）

其余功能（密钥管理、sync、run、get/put、backup）完全不受影响。

## 问题 4：dsh 插件在 Windows 上无法定位 Python

**现象**

插件 `plugin/index.js` 的 `resolvePython()` 只按 macOS 布局找
`<projectDir>/.venv/bin/python`，找不到就回退到 PATH 里的 `python3`。
Windows 上 venv 的 Python 位于 `.venv\Scripts\python.exe`，且常见命令名是
`python` 而非 `python3` —— 回退目标不存在，插件所有工具调用必然失败
（`execFile("python3", ...)` → ENOENT）。

**修复**（`plugin/index.js::resolvePython`）

- venv 探测按平台区分：Windows 查 `.venv\Scripts\python.exe`，其余平台查 `.venv/bin/python`
- PATH 回退按平台排序并逐个探测（`where` / `which`）：Windows 依次试
  `python`、`python3`；其余平台依次试 `python3`、`python`
- `config.python` 显式配置仍具有最高优先级（行为不变）

插件以 `cwd = 已安装包目录` 执行 `python -m ssh_hub`，包目录自带 `ssh_hub/`
源码，因此 PATH 中的系统 Python（≥3.10）即可运行，无需全局安装。

## 其他

- 版本号 `0.1.0` → `0.1.1`（package.json 与 pyproject.toml 同步）
- 安装注意：`file:` 目录依赖在 pnpm 侧有内容缓存，修改本地源码后若版本号不变，
  重装可能报 "Already up to date" 而不刷新已安装副本；需 bump 版本或先卸载再安装
- 部署验证环境：Windows 11、Python 3.11.6、OpenSSH_for_Windows_8.1p1、
  dsh web profile（插件以 bundle 层安装，guard 事务已提交）

## 验证

```text
$ python -m unittest discover -s tests -v
Ran 4 tests
OK

# 隔离环境全流程冒烟（DSH_SSH_HUB_DIR / DSH_SSH_DIR 指向临时目录）
ssh-hub init / add web01 --generate / list / list --json /
sync（config + dsh_ssh_hub_config + 密钥副本）/ backup / key ls / show  全部通过
```
