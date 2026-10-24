# 🍊 Orange Note Skill

**通过已登录浏览器，让 AI Agent 读取文件、执行代码、传输结果和管理后台任务。**

Orange Note 是面向 Claude Code、Codex 等 Agent 的通用操作技能。它把重复的文件处理、
有界执行和任务跟踪整理成一组 CLI 命令，通过 BrowserSkill 使用已有浏览器登录状态，
在受支持的 Jupyter 工作区中完成操作，无需向 Agent 复制登录凭据。

**版本：v4.1.4 · 许可证：MIT**

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

### 独立对话 / 多 Agent

每个对话创建一次独立 session；相同工作区也可以创建多个：

**每个用户都使用自己的工作区 URL。** 项目 ID 和工作区 ID 从 `--url` 动态解析，
只保存在该用户的本地 session 配置中；发布包不包含默认工作区或任何人的实际工作区信息。

```bash
python scripts/sn.py session create --url "<支持的工作区 URL>"
python scripts/sn.py --session <返回的logical_session_id> run-python --code "print(1 + 1)"
python scripts/sn.py --session <返回的logical_session_id> connect
python scripts/sn.py session list
```

后续每条命令都在子命令前带 `--session <id>`。不同 session 独享本地配置、回执、
BrowserSkill session、控制页和内核；同 session 的命令用进程锁协调，等待 10 秒仍被占用则返回
`session_busy`。没有全局“当前 session”，不同 agent 可以各自保存 ID。

一个 agent 一份 session/网页，两个 agent 各用一份。操作结束时保留页面，连续空闲约 5 分钟后
由 BrowserSkill 回收；这是空闲时限，不是打开 5 分钟就强制关闭。一个会话的操作不会续期另一个。
保留逻辑 ID，在时限内继续复用；过期后有新操作再用同一 ID 重连，Python 内存变量可能丢失。
用户明确说关闭时可立即运行该 ID 的 `close`；不发送仅用于阻止超时的保活请求。
页面短暂探测失败会报告错误并保留页面，避免自动刷新重新等待服务器。

创建失败也会返回已分配的 ID，可用该 ID 的 `connect` 恢复。`close` 保留配置和后台任务；
之后仍能用同一 ID 连接。浏览器重建后 Python 内存变量可能丢失。
`session list` 显示本地保存记录，实时状态用 `--session <id> probe` 检查。

默认状态根目录沿用旧版用户配置位置。要把状态放在 D:，在每次命令的环境中设置
`SN_CONFIG_DIR=D:\orange-note-state`；各 session 位于该目录的 `sessions/<id>/`。
同一 session 绑定一个工作区；换工作区创建新 session。服务器文件和算力仍然共享，
独立任务应使用不同输出目录和包含 session ID 的幂等键。`jobs --remote` 是工作区共享视图。

### 旧命令兼容

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

默认禁止覆盖已有文件；明确需要替换时使用 `--overwrite`。写入与上传先在同目录临时文件中
完成写入和读回校验，再原子发布；覆盖前发生写入或校验失败会保留旧文件。
下载先落到 `.part` 文件，完成验证后再发布；续传记录同时核对目标工作区和源文件身份。

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

### 清理已结束的任务

```bash
python scripts/sn.py --session <id> prune --days 30 --dry-run
python scripts/sn.py --session <id> prune --days 30
```

这是**工作区范围**的清理，会包含其他 session 的任务，并删除任务目录内的日志和 artifacts。
先预览，再在用户授权的清理范围内执行实际删除。仅清理任务 ID、manifest、已结束状态和结束时间
核对通过，且 PID 与进程组均不存在的任务；状态缺失、损坏、仍活动或无法确认的目录保留。
`eligible` 是候选数，`removed` 是实际删除数（dry-run 为 0）；最多列出 100 个候选 ID，超出时标记截断。
不估算释放空间，`freed_bytes` 为 `null`。失败或超时可能已经清理部分目录，需要检查结果后再操作。

RDS 检查每次建立新任务，读取当前文件，避免复用旧摘要；它仍会通过 `readRDS` 在服务器内存中
加载完整对象。大对象检查前需核对文件大小和可用内存，任务目录由用户按需要清理。

## 诊断与按需关闭

```bash
python scripts/sn.py doctor --deep
```

普通小文件读取走文件 API；需要执行代码时复用当前控制标签页自建的内核。
不同工作区使用独立浏览器会话。`close` 会清理当前控制器记录的内核并结束其浏览器会话。
它不是每次操作后的收尾步骤；用户明确要求释放某个会话时才执行：

```bash
python scripts/sn.py --session <用户要求关闭的ID> close
```

启动服务时使用 `bsk daemon start --foreground --session-idle 5m`，具体说明见
[BrowserSkill 配置](docs/BROWSERSKILL_INSTALL.md)。空闲回收的是浏览器控制会话，
不会停止整个 DCS 工作区或主动取消已脱离的后台任务；远端内核是否自动回收由 Jupyter 策略决定。

## 边界与安全

- 使用当前登录账号的真实权限；命令执行不是隔离沙箱。详见 [SECURITY.md](SECURITY.md)。
- 不导出浏览器 cookies、token 或 `_xsrf`；运行配置和任务回执不应提交到 Git。
- 单次同步执行最长 50 秒，更长任务使用 `submit`；上传上限 4 MiB。
- 下载按 256 KiB 分块。默认对不超过 64 MiB 的文件核对远端 SHA-256；更大文件报告 `size_only`。
- 服务端忽略 Range 并返回明显超出当前分块的整文件响应时，下载会在读取正文前失败。
- 多对话使用独立逻辑 session；不带 `--session` 的旧命令仍使用共享状态。删除、覆盖和取消任务仍以用户授权为准。

## 测试

```bash
python -m unittest discover -s tests -v
python -m compileall -q scripts tests
node --check scripts/jupyter_runtime.js
node --test tests/runtime.test.cjs
```

v4.1.0 已完成少量针对性检查，以及 3 个 Luna Agent 进入同一工作区的真实浏览器实测：
独立内核、变量保留、重叠执行和关闭一个 session 不影响其他 session 均通过。
当前版本的检查范围见 [v4.1.4 验证记录](docs/VALIDATION_v4.1.4.md)，
本次变化见 [发布说明](docs/RELEASE_v4.1.4.md)；
会话功能的历史实测见 [v4.1.0 记录](docs/VALIDATION_v4.1.0.md)，旧引擎的历史验证见
[v4.0.1 记录](docs/VALIDATION_v4.0.1.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。
