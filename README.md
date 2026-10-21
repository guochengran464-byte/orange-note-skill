# 🍊 Orange Note Skill

**通过已登录浏览器，让 AI Agent 读取文件、执行代码、传输结果和管理后台任务。**

Orange Note 是面向 Claude Code、Codex 等 Agent 的通用操作技能。它把重复的文件处理、
有界执行和任务跟踪整理成一组 CLI 命令，通过 BrowserSkill 使用已有浏览器登录状态，
在受支持的 Jupyter 工作区中完成操作，无需向 Agent 复制登录凭据。

**版本：v4.0.1 · 许可证：MIT**

## 能做什么

| 场景 | 能力 |
| --- | --- |
| 查看文件 | 列目录、预览文本、检查 notebook 和常见数据对象结构 |
| 写入与传输 | 文本写入、二进制上传下载、断点续传、大小与校验和验证 |
| 执行代码 | 短 Python / shell 命令、有界输出、执行超时处理 |
| 运行长任务 | 脱离当前 Agent 会话的后台作业、持久状态、日志和结果清单 |
| 避免重复执行 | 提交前保存回执、按工作区隔离的幂等键、冲突检测 |
| 后续管理 | 列出任务、查询进度、按归属取消任务、记录结果验收 |
| 连接恢复 | 分层诊断、恢复已保存的工作区、隔离不同目标的控制会话 |

当前可执行后端是**内置连接器支持的 Jupyter 工作区**。连接地址需要通过该连接器的校验；
自建 Jupyter 或其他网页应用需要相应适配。通用的是 Agent 的操作流程和命令接口，
当前版本的兼容范围见 [COMPATIBILITY.md](COMPATIBILITY.md)。

## 安装

前置条件：Windows、Python 3、Edge 或 Chrome，以及已连接的 BrowserSkill CLI 和扩展。
Python 端只使用标准库。[BrowserSkill 安装与连接说明](docs/BROWSERSKILL_INSTALL.md)。

将仓库内容放入对应技能目录，保留 `scripts/`、`agents/` 和文档：

```text
Claude Code: ~/.claude/skills/orange-note-skill/
Codex:       ~/.agents/skills/orange-note-skill/
```

Codex 的默认提示可显式调用 `$orange-note-skill`；技能默认关闭隐式调用。

## 快速开始

在已登录的浏览器中打开目标工作区，等待启动，复制当前地址：

```bash
python scripts/sn.py doctor
python scripts/sn.py connect --url "<支持的工作区 URL>"
python scripts/sn.py probe
python scripts/sn.py ls work
python scripts/sn.py cat work/project/run.py
```

`sn.py` 保留现有 CLI 文件名，方便已有脚本继续调用。连接成功后，配置保存在用户目录；
运行 `doctor` 可查看具体路径。后续可用 `connect` 恢复已保存的目标。

### 文件与短代码

```bash
python scripts/sn.py write work/project/note.txt --content "hello"
python scripts/sn.py put ./report.pdf --remote work/project/report.pdf
python scripts/sn.py get work/project/figure.png --out ./figure.png
python scripts/sn.py run-python --code "print(1 + 1)"
python scripts/sn.py run-shell --cwd /data/work/project --cmd "python check.py"
python scripts/sn.py inspect /data/work/project/data.h5ad
```

默认禁止覆盖已有文件；明确需要替换时使用 `--overwrite`。新文件写入采用服务器端原子创建，
并做读回校验。下载先落到 `.part` 文件，完成验证后再发布；续传记录同时核对目标工作区和源文件身份。

文件路径 `work/...` 对应 `/data/work/...`；shell 默认工作目录是 `/data/work`，
执行项目脚本时建议明确传入 `--cwd`。

### 后台任务

```bash
python scripts/sn.py submit --cwd /data/work/project --cmd "python pipeline.py" --idempotency-key project-run-1
python scripts/sn.py jobs --remote
python scripts/sn.py poll <job_id>
python scripts/sn.py artifacts <job_id>
python scripts/sn.py cancel <job_id>
python scripts/sn.py accept <job_id> --note "已检查结果"
```

任务在返回前保存本地回执，并等待后台 runner 确认启动。同一工作区、同一幂等键和相同命令/cwd
返回原任务；改变命令或 cwd 会报告冲突，改变显示名称不会启动新任务。

任务可以把交付文件写入 `$SN_ARTIFACT_DIR`。进程是否结束、业务执行阶段和结果验收分别记录。
超时或通信中断后先用 `jobs` / `poll` 恢复状态，再决定下一步。

## 诊断与关闭

```bash
python scripts/sn.py doctor --deep
python scripts/sn.py close
```

普通小文件读取走文件 API；需要执行代码时复用当前控制标签页自建的内核。
不同工作区使用独立浏览器会话。`close` 会清理当前控制器记录的内核并结束其浏览器会话。

## 边界与安全

- 使用当前登录账号的真实权限；命令执行不是隔离沙箱。详见 [SECURITY.md](SECURITY.md)。
- 不导出浏览器 cookies、token 或 `_xsrf`；运行配置和任务回执不应提交到 Git。
- 单次同步执行最长 50 秒，更长任务使用 `submit`；上传上限 4 MiB。
- 下载按 256 KiB 分块。默认对不超过 64 MiB 的文件核对远端 SHA-256；更大文件报告 `size_only`。
- 独立 Agent 任务使用各自的 `SN_CONFIG_DIR`。删除、覆盖和取消任务仍以用户授权为准。

## 测试

```bash
python -m unittest discover -s tests -v
python -m compileall -q scripts tests
node --check scripts/jupyter_runtime.js
node --test tests/runtime.test.cjs
```

核心版本已经完成 Windows / Linux 回归与浏览器工作区实测。测试范围和未验证场景见
[验证记录](docs/VALIDATION_v4.0.1.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。
