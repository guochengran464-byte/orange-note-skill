Orange Note v4.1.0 支持独立对话和多个 Agent 同时进入同一工作区。

### 本版更新

- 新增 `session create --url`、`session list` 和每条命令的 `--session <id>`。
- 每个对话独享配置、任务回执、BrowserSkill session、控制标签页和 Jupyter 内核。
- 同一 session 用进程锁协调命令，关闭和恢复只针对该 session。
- 保留不带 `--session` 的旧命令兼容行为。

### 每个用户使用自己的工作区

首次使用时提供自己的 URL：

```bash
python scripts/sn.py session create --url "<你自己的工作区URL>"
python scripts/sn.py --session <返回的logical_session_id> run-python --code "print(1 + 1)"
python scripts/sn.py --session <返回的logical_session_id> close
```

项目 ID 和工作区 ID 从 URL 动态解析，只保存在各用户的本地 session 配置中。
发布包不含预设的个人工作区、登录凭据或测试 session 状态。当前连接器支持的仍是 DCS Jupyter 工作区。

### 验证与安装

3 个 Luna Agent 已完成真实工作区实测：独立内核、变量跨调用保留、重叠执行，
以及关闭一个 session 时另外两个仍能正常完成。另有少量本地 session 和文件锁检查。
这次未做高负载或长任务压力测试；服务器文件、账号权限和算力仍然共享。

下载本页的 `orange-note-skill-v4.1.0.zip` 或 GitHub 自动生成的 Source code (zip)，
将内容放入对应 Agent 的 `orange-note-skill/` 技能目录，详见 README。
