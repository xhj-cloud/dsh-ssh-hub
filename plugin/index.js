"use strict";
/**
 * dsh-ssh-hub 主机插件：注册 ssh_hub 动态工具。
 *
 * 工具通过子进程调用本包的 Python CLI（纯标准库，无第三方依赖），密钥统一
 * 存放在 ~/.ssh/dsh-ssh-hub 并由 dsh 与系统 ssh 复用。
 *
 * 配置项（cordis.patch.yml 的 config）:
 *   projectDir  项目目录（默认本包所在目录，即安装后的 node_modules/dsh-ssh-hub）
 *   python      Python 可执行文件（默认 <projectDir>/.venv/bin/python，
 *               不存在时回退到 PATH 中的 python3；要求 >= 3.10）
 */

const { join } = require("node:path");
const { existsSync } = require("node:fs");

const name = "ssh-hub";
const inject = ["tools"];

const DEFAULT_PROJECT_DIR = join(__dirname, "..");
const MAX_OUTPUT = 30000;

function resolveProjectDir(config) {
  return (config && typeof config.projectDir === "string" && config.projectDir) || DEFAULT_PROJECT_DIR;
}

function commandExists(cmd) {
  try {
    const { execFileSync } = require("node:child_process");
    const probe = process.platform === "win32" ? "where" : "which";
    execFileSync(probe, [cmd], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function resolvePython(config) {
  if (config && typeof config.python === "string" && config.python) return config.python;
  const projectDir = resolveProjectDir(config);
  const venvCandidates =
    process.platform === "win32"
      ? [join(projectDir, ".venv", "Scripts", "python.exe")]
      : [join(projectDir, ".venv", "bin", "python")];
  for (const venv of venvCandidates) {
    if (existsSync(venv)) return venv;
  }
  // 回退到 PATH：Windows 常见命令名是 python，其余平台是 python3
  const pathNames = process.platform === "win32" ? ["python", "python3"] : ["python3", "python"];
  for (const name of pathNames) {
    if (commandExists(name)) return name;
  }
  return pathNames[0];
}

function usage(message) {
  return { error: message };
}

/** 把工具参数映射为 CLI 子命令 argv（不含 python -m ssh_hub 前缀）。 */
function buildArgv(command, args) {
  switch (command) {
    case "init":
    case "sync":
      return [command];
    case "list":
      return ["list", "--json"];
    case "key_ls":
      return ["key", "ls"];
    case "key_gen": {
      if (!args.alias) return usage("key_gen 需要 alias（服务器别名）");
      const argv = ["key", "gen", args.alias];
      if (args.force) argv.push("--force");
      return argv;
    }
    case "show":
    case "rm": {
      if (!args.alias) return usage(command + " 需要 alias（服务器别名）");
      return [command, args.alias];
    }
    case "add": {
      if (!args.alias) return usage("add 需要 alias（服务器别名）");
      if (typeof args.host !== "string" || !args.host) return usage("add 需要 host（服务器 IP 或域名）");
      const argv = ["add", args.alias, "--host", args.host];
      if (args.user) argv.push("--user", String(args.user));
      if (args.port) argv.push("--port", String(args.port));
      if (args.generate_key) argv.push("--generate");
      if (args.force) argv.push("--force");
      if (args.password) argv.push("--password", String(args.password));
      return argv;
    }
    case "password_set": {
      if (!args.alias) return usage("password_set 需要 alias（服务器别名）");
      if (typeof args.password !== "string" || !args.password) return usage("password_set 需要 password（明文密码）");
      return ["password", "set", args.alias, "--password", args.password];
    }
    case "password_get": {
      if (!args.alias) return usage("password_get 需要 alias（服务器别名）");
      return ["password", "get", args.alias];
    }
    case "password_rm": {
      if (!args.alias) return usage("password_rm 需要 alias（服务器别名）");
      return ["password", "rm", args.alias];
    }
    case "run": {
      if (!args.alias) return usage("run 需要 alias（服务器别名）");
      if (!Array.isArray(args.remote_command) || args.remote_command.length === 0) {
        return usage("run 需要 remote_command（远程命令字符串数组）");
      }
      const argv = ["run", args.alias];
      if (args.cwd) argv.push("--cwd", String(args.cwd));
      argv.push("--", ...args.remote_command.map(String));
      return argv;
    }
    case "get": {
      if (!args.alias) return usage("get 需要 alias（服务器别名）");
      if (typeof args.remote_path !== "string" || !args.remote_path) return usage("get 需要 remote_path（远程源路径）");
      const argv = ["get", args.alias, args.remote_path];
      if (args.local_path) argv.push(String(args.local_path));
      if (args.recursive) argv.push("--recursive");
      return argv;
    }
    case "put": {
      if (!args.alias) return usage("put 需要 alias（服务器别名）");
      if (typeof args.local_path !== "string" || !args.local_path) return usage("put 需要 local_path（本地源路径）");
      if (typeof args.remote_path !== "string" || !args.remote_path) return usage("put 需要 remote_path（远程目标路径）");
      const argv = ["put", args.alias, args.local_path, args.remote_path];
      if (args.recursive) argv.push("--recursive");
      return argv;
    }
    case "backup": {
      const argv = ["backup"];
      if (args.backup_dir) argv.push("--dir", String(args.backup_dir));
      return argv;
    }
    default:
      return usage("不支持的 command: " + String(command));
  }
}

/** 用子进程运行 CLI，捕获输出（成功与否都 resolve，不抛异常）。 */
function runCli(python, argv, options) {
  const { execFile } = require("node:child_process");
  return new Promise((resolve) => {
    execFile(python, argv, options, (err, stdout, stderr) => {
      const code = err && typeof err.code === "number" ? err.code : err ? 1 : 0;
      resolve({
        exit_code: code,
        stdout: String(stdout || ""),
        stderr: String(stderr || ""),
      });
    });
  });
}

function apply(ctx, config) {
  ctx.tools.register({
    name: "ssh_hub",
    description:
      "通过 dsh-ssh-hub 管理远程服务器 SSH 密钥/密码、执行远程命令并传输文件，用于 agent 在服务器上直接进行代码操作。密码以加密形式存于 macOS 钥匙串（不落明文），密钥与密码统一管理，其他 agent 可直接连接。常用代码工作流: run <alias> -- <cmd> 远程执行（--cwd 切目录，密钥失败自动改用密码）; get <alias> <remote_path> [local_path] 下载文件到本地编辑; put <alias> <local_path> <remote_path> 上传改好的文件; 配合本地文件工具完成 拉取->修改->上传->远程测试 闭环。注意: 复合命令（含 && | 重定向等）请用单个元素 bash -lc '...'，因为每个参数会被安全引用。命令: init 初始化; add 登记服务器(可 --generate 生成密钥, --password 存密码); list 列出(JSON); show 详情; rm 删除; sync 同步到 ~/.ssh; run 远程执行命令; get 下载(可 --recursive); put 上传(可 --recursive); password_set 保存密码(加密入钥匙串); password_get 读取密码; password_rm 删除密码; backup 备份; key_gen 生成密钥; key_ls 列出密钥。注意: 连接远程服务器与读写 ~/.ssh 属于宿主机/远程操作，涉及用户授权。",
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["command"],
      properties: {
        command: {
          type: "string",
          enum: ["init", "add", "list", "show", "rm", "sync", "run", "get", "put", "password_set", "password_get", "password_rm", "backup", "key_gen", "key_ls"],
          description: "要执行的子命令",
        },
        alias: { type: "string", description: "服务器别名；add/show/rm/run/get/put/key_gen 需要" },
        host: { type: "string", description: "服务器 IP 或域名；add 需要" },
        user: { type: "string", description: "登录用户名，默认 root；add 使用" },
        port: { type: "integer", description: "SSH 端口，默认 22；add 使用" },
        generate_key: { type: "boolean", description: "add 时同时生成 ed25519 密钥对" },
        force: { type: "boolean", description: "覆盖已存在的密钥（add/key_gen）" },
        cwd: { type: "string", description: "远程工作目录；run 使用（先 cd 再执行）" },
        remote_command: {
          type: "array",
          items: { type: "string" },
          description: '远程命令及参数数组，如 ["df", "-h"]；run 需要，参数会自动安全引用',
        },
        remote_path: { type: "string", description: "远程路径；get 为远程源路径，put 为远程目标路径" },
        local_path: { type: "string", description: "本地路径；get 为本地保存路径（默认当前目录），put 为本地源路径" },
        recursive: { type: "boolean", description: "目录递归传输；get/put 使用" },
        password: { type: "string", description: "服务器登录密码；add 或 password_set 使用，加密存入 macOS 钥匙串（不落明文磁盘）" },
        backup_dir: { type: "string", description: "备份输出目录；backup 使用（默认项目目录）" },
      },
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        required: ["exit_code"],
        properties: {
          exit_code: { type: "integer", description: "CLI 退出码；0 表示成功" },
          stdout: { type: "string", description: "标准输出" },
          stderr: { type: "string", description: "标准错误" },
        },
      },
      render: (_args, value) => [{ type: "text", text: JSON.stringify(value, null, 2) }],
    },
    async execute(args, exec) {
      const argv = buildArgv(args.command, args);
      if (argv.error) {
        return { exit_code: 2, stdout: "", stderr: argv.error };
      }
      const python = resolvePython(config);
      const options = {
        cwd: resolveProjectDir(config),
        env: process.env,
        maxBuffer: 4 * 1024 * 1024,
        timeout: args.command === "run" ? 120000 : 30000,
        ...(exec && exec.signal ? { signal: exec.signal } : {}),
      };
      const result = await runCli(python, ["-m", "ssh_hub", ...argv], options);
      if (result.stdout.length > MAX_OUTPUT) {
        result.stdout = result.stdout.slice(0, MAX_OUTPUT) + "\n...(输出已截断)";
      }
      if (result.stderr.length > MAX_OUTPUT) {
        result.stderr = result.stderr.slice(0, MAX_OUTPUT) + "\n...(输出已截断)";
      }
      return result;
    },
  });
}

module.exports = { name, apply, inject };
