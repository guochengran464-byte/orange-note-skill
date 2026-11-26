# Orange Note v5.0.0 更新说明

本版改善 Orange Note 在繁忙工作区的连接和状态读取，并新增批量作业与目录传输。

- 控制页默认使用轻量入口，减少 JupyterLab 后台标签页卡顿。连接时区分登录失效、容器停止和启动超时。只有确认操作尚未发出时，才重试页面观察冲突。
- `cat --tail`、`multi-cat`、`poll --lite` 和 `batch status` 可通过 HTTP 读取内容或状态，无需启动 Jupyter 内核。
- 新增服务器端批量作业队列、目录双向同步、大文件分块上传选项、主机负载诊断和常见测序文件检查。
- 提交作业时可指定线程数和本地临时目录。状态文件支持通过 `sn_stage` 更新。
- macOS 可使用 `scripts/sn` 启动器。升级可继续使用原有逻辑会话 ID 和后台作业回执。

本地检查覆盖 Python 和 JavaScript 回归测试、BrowserSkill 0.5.0 连通性和现有会话记录的读取。实际工作区行为取决于部署的 BrowserSkill 和 Jupyter 服务版本。命令和边界见 [SKILL.md](../SKILL.md)。
