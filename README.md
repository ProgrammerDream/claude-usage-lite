# claude-usage-lite

轻量的 Claude Code 本地用量统计工具：多会话、子代理去重、token 与美元估算、持续追加状态行。Python 3.9+，运行时仅使用标准库。

默认只读本地 JSONL 日志，不启动 Claude、不发送模型请求、不读取登录凭据。可显式启用实验性的账号额度查询。

## 安装与使用

```bash
git clone https://github.com/ProgrammerDream/claude-usage-lite.git
cd claude-usage-lite
python3 claude_usage.py
```

或者安装为命令（尚未发布到 PyPI）：

```bash
python3 -m pip install .
claude-usage
```

Windows 可使用 `python` 代替 `python3`。

| 需求 | 命令 |
|---|---|
| 最近 24 小时有调用的会话与合计 | `claude-usage` |
| 最近 72 小时 | `claude-usage --hours 72` |
| 每 10 分钟追加一行汇总 | `claude-usage --status` |
| 每分钟刷新本地统计 | `claude-usage --status --interval 60` |
| 指定刷新次数 | `claude-usage --status --count 3` |
| 查看单条会话及其子代理 | `claude-usage SESSION_UUID` |
| 显式查询账号额度 | `claude-usage --api` |
| 监测本地用量并低频查询额度 | `claude-usage --status --api` |
| 显示版本 | `claude-usage --version` |

`--interval` 单位为秒，最小 60，默认 600。Ctrl+C 停止监测。状态模式追加汇总行，不清屏；完整会话明细使用单次模式。

额度栏同时显示北京时间的重置日期、时间和剩余时长，例如 `5小时 37%（重置 09-09 06:30:00，剩 4小时9分）`。总周和可选的 Fable 周额度分别使用接口返回的重置时间；无重置时间显示未知，过期缓存不会被当作未来的重置计划。

## 多目录

默认读取 `~/.claude/projects`，支持 `CLAUDE_CONFIG_DIR`。`--data-dir` 接受 Claude 配置目录或其 `projects` 子目录，可重复传入：

```bash
claude-usage --data-dir /path/to/first/.claude --data-dir /path/to/second/.claude
```

只需日志的读取权限。不会自动扫描其他操作系统用户的主目录。多个目录的成本可以汇总，但若它们来自不同账号，不能与某一个账号的额度百分比直接对应。

## 输出与统计口径

每个会话显示最后调用时间（北京时间）、目录、完整会话 ID、模型、请求数、子代理请求数和三种成本：

- **全历史**：最近有调用的所选会话，其日志中保留的全部记录；显式指定 ID 时不要求近期活跃。
- **最近时段**：仅统计 `--hours` 指定时间范围内的请求。
- **当前窗口**：有有效账号窗口重置时间时，按“重置时间减 5 小时”与请求日志时间估算；否则显示未知。

美元是 **API 标价等价值**，不是订阅扣款、官方美元余额或固定套餐配额。缓存读取包含多次读取历史，不等于上下文长度；思考 token 已包含在输出中。

日志按请求 ID、消息 ID、模型去重；同一请求的多个消息片段不会重复收费。子代理归入父会话。对于跨会话复制的相同请求，归到扫描范围内最早出现的会话。正在写入的不完整末行会在后续刷新时重读。

不包含没有提供日志的设备、网页、已删除记录或尚未写入的调用。工具没有推断进程是否仍在运行，最后调用时间表示日志中的时间。窗口边界附近的跨界请求可能存在归属误差。

## 模型价格

内置标准速度下的 Fable 5.1、Opus 5 估算单价，源自开发时检查的 Claude Code 本机计价表，**不保证与未来 API 价格一致**。未知模型、未知缓存 TTL 分项或非标准速度的请求会标为未计价；金额后的 `*` 表示合计不完整。

通过 JSON 覆盖或新增模型价格，每项单位为美元/百万 token：

```bash
claude-usage --pricing pricing.example.json
```

示例文件使用虚构模型名，替换为日志里的真实模型 ID 与已核对单价即可。Web search 暂按每次 $0.01 估算；其他额外工具费用不在本工具的计价范围内。

## 可选账号额度接口

`--api` 使用当前配置目录 `.credentials.json` 中的 OAuth access token，向 `https://api.anthropic.com/api/oauth/usage` 发送只读请求。可通过 `--credentials /path/to/.credentials.json` 指定文件。不会打印、复制或刷新令牌；401 时请通过 Claude 官方登录流程恢复认证。

此接口不是稳定的公共 API：字段可能变化，也可能返回 429。5h、总周和可选模型专项栏来自接口，不通过本地 token 推算。没有 Fable 栏就不显示。

- 跨进程锁和持久缓存使重复启动遵守查询间隔；账号接口至少间隔 10 分钟。
- 429 遵守 `Retry-After`，无有效值时等待一小时；等待期间本地日志仍按 `--interval` 刷新。
- 缓存值标明采集时间，越过重置点显示过期，不冒充实时额度。
- 状态文件仅保存额度响应与等待时间，不保存令牌。默认放在 XDG/Windows 用户缓存目录下的 `claude-usage-lite`，按凭据路径隔离。

只想看本地成本时，保持默认或使用 `--local`，不会产生额度接口请求。Fable、总周和 5h 不是可相加的独立余额。

## 开发与验证

```bash
python3 -m unittest discover -s tests -v
```

测试使用虚构数据和模拟网络，不需要账号或真实凭据。CI 覆盖 Python 3.9 / 3.12 及 Linux / Windows。当前输出为中文、时间固定为北京时间；后续可扩展语言与时区。

## License

MIT。项目与 Anthropic 无隶属关系。
