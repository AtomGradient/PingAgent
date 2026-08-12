# ai-ping 完全指南

把消息写成 `.ai-mailbox/inbox/<to>/<msg-id>.md` 的 CLI 工具。watcher 监听 inbox 并用 osascript 自动注入到对方 pane。

## 命令签名

```
ai-ping <to> [options] [<message-words...>]
```

## 速查（5 个最常用 pattern）

```bash
# 短消息（仅适合一句话；带特殊字符要 quote）
ai-ping claude "审一下 src/auth.ts"

# 长内容 / 带代码块 —— 推荐
ai-ping claude --kind review-request --file /tmp/req.md

# 回复某条消息（必须带 --reply-to，对方才能闭环）
ai-ping codex --reply-to 20260511-153000-abc123 --file /tmp/reply.md

# 阻塞等回复（codex 写完想直接拿到 review 再继续）
ai-ping claude --wait --timeout 600 --file /tmp/q.md

# stdin pipe
cat req.md | ai-ping claude --kind review-request
```

## 参数详解

| 参数 | 必需 | 说明 |
|---|---|---|
| `<to>` | ✓ | 目标 role，通常是 `claude` 或 `codex` |
| `--file <path>` | | 从文件读消息正文。**推荐**：避免 shell 转义，保留代码块/换行 |
| `--kind <kind>` | | 消息类型，默认 `msg`。完整表见下面 |
| `--reply-to <id>` | | 这是对某条消息的回复。`<id>` 来自被回复消息的 frontmatter 里 `id:` 字段 |
| `--from <role>` | | 显式指定发送者。默认从当前 pane 的 `$ITERM_SESSION_ID` 自动反查 |
| `--wait` | | 阻塞直到收到 `reply_to=本次msg_id` 的回复（每 2s 轮询） |
| `--timeout <sec>` | | `--wait` 的最大等待秒数。默认 300 |
| 位置参数 | | 短消息可直接作命令行参数；与 `--file` / stdin 三选一 |

## kind 表

| kind | 含义 | 应否回复 | 典型场景 |
|---|---|---|---|
| `msg` | 普通消息 | 看情况 | 闲聊、简短问题、知会 |
| `review-request` | 请求审核 | **必须** 回 `review-response` | 完成一个功能、修非平凡 bug、改架构 |
| `review-response` | 审核结论 | 一般不必（除非有追问） | review-request 的回执 |
| `question` | 提问 | 必须 | 设计选择、技术疑问 |
| `pushback` | 反对/异议 | 必须，对方应停下重评估 | 收到的请求依赖错误前提 / 有更好方案 |
| `notice` | 知会 | 否 | "我开始改 X 了，注意冲突" |
| `done` | 完成通知 | 一般不必 | "我这边搞完了" |

## 工作原理

```
[发送方 pane]                  [文件系统]                  [接收方 pane]

ai-ping claude "..."   ─►   inbox/claude/<id>.md
                                  │
                                  │  watcher (fswatch) 监听 inbox/claude/
                                  │
                                  ▼
                            osascript 用 .panes/claude.json 里的
                            session UUID 注入到对方 pane
                                  │
                                  ▼
                          [ai-collab 收信] from=... id=... 自动出现 + 提交
```

消息文件 = YAML frontmatter + markdown 正文。frontmatter 至少有 `id` `from` `to` `kind` `created`，回复时还有 `reply_to`。

## --wait 语义

`ai-ping claude --wait --timeout 600 --file /tmp/q.md`：

1. 写消息文件，记下本次 `msg_id`
2. watcher 立即注入对方 pane
3. **阻塞当前进程**，每 2s 扫描 `inbox/<本方-from>/` 找 `reply_to: <msg_id>` 的消息
4. 找到 → cat 回复内容、exit 0
5. 超时 → exit 2，消息留在对方 inbox（对方仍可异步回）

**重要**：
- 对方必须用 `--reply-to <id>` 回，否则解锁不了
- 如果在 Claude Code Bash tool 里用 `--wait`，记得把 Bash tool 的 `timeout` 也设大（最大 600000 ms）
- **不要双方同时 `--wait` —— 会互锁**

## 常见错误

| 错误信息 | 原因 | 解决 |
|---|---|---|
| `Cannot auto-detect --from` | 当前 pane 没注册 / `ITERM_SESSION_ID` 缺失 | 跑 `ai-pane-register <role>` 或显式 `--from <role>` |
| `No .ai-mailbox/ found upward from ...` | 当前目录不在已 register 过的项目下 | `cd <project>` 或确认 register 跑过 |
| `File not found: <path>` | `--file` 路径错 | 用绝对路径或确认文件存在 |
| `Cannot ping yourself (from=to=...)` | `--from` 和 `<to>` 同一个 role | 检查参数 |
| `target '<role>' not registered yet` | 对方还没 `ai-pane-register` | 让对方先 register |
| 消息已入邮箱但没有 `notification: dispatched` | watcher 停止、session 失效或 osascript 注入失败 | 先看 `.watch-<role>.log`；`ai-ping` 会自动拉起停止的 watcher，session 变化时重新 `ai-pane-register <role>` |
| `Timeout after Ns — no reply yet` | `--wait` 等过头了，对方还没回 | 检查对方 pane / 调大 `--timeout` |

## 常见误区

1. **正文别放命令行参数里**：除非真是一句话。带特殊字符、代码块、换行的内容 **永远用 `--file`**
2. **每条消息只发一次**：watcher 用 `.dispatched` sidecar 去重；重跑 ai-ping 会生成新 msg_id，不是覆盖。要真重发，删 sidecar
3. **`--reply-to` 决定能不能闭环**：发起方 `--wait` 严格匹配 `reply_to == 自己的 msg_id`；不带 reply_to 的消息只是新一条而已
4. **kind 决定对方行为**：`pushback` 应让对方停下，`msg` 可能被随手处理。**选对 kind 很重要**
5. **不要 ai-ping 自己**：脚本直接拒绝 from == to
6. **`--wait` 不会自动重传**：超时只是不再等，消息已经躺在对方 inbox

## 完整示例：一次 review 往返

**发起方（codex）：**

```bash
cat > /tmp/review-req.md <<'EOF'
请审核 commit abc123 的并发部分。

重点：
- src/auth.ts 的 login() 异步化是否有 race condition
- src/session.ts 的清理逻辑

测试：
- tests/auth_test.swift 已加新 case，跑通

设计权衡：
- 用了 actor 而不是 NSLock，因为 actor 在 swift 6 strict concurrency 下更安全
EOF

ai-ping claude --kind review-request --file /tmp/review-req.md
# 输出：Sent: 20260511-153000-abc123  (codex -> claude, kind=review-request)
```

**接收方（claude）** 看到通知 → Read inbox 文件 → 实际审核（可能 Read 相关源码 + Bash 跑测试）→ 

```bash
cat > /tmp/review.md <<'EOF'
## 结论：通过 with comments

### 严重问题
（无）

### 建议
1. `login()` 第 42 行：错误恢复路径漏了 `invalidate session()`，会泄漏 session
2. 测试没覆盖 race condition 场景，建议加 `testConcurrentLogin()`

### 思路
actor 选择合理。NSLock 版会有 reentrancy 风险，actor 自动序列化更安全。
EOF

ai-ping codex --kind review-response --reply-to 20260511-153000-abc123 --file /tmp/review.md
```

**codex 那边**自动收到通知 → Read 文件 → 看 review → 决定改还是讨论。

## 工作目录文件结构（参考）

```
<project>/.ai-mailbox/
├── .panes/
│   ├── codex.json              # 注册信息：session UUID + cwd + 时间戳
│   └── claude.json
├── inbox/
│   ├── codex/<msg-id>.md       # 给 codex 的消息
│   ├── codex/<msg-id>.md.dispatched   # watcher 去重标记
│   └── claude/<msg-id>.md
├── sent/<msg-id>.md            # 自己发出的副本（audit log）
├── .watch-codex.pid            # watcher PID
├── .watch-codex.log            # watcher 日志（osascript ok/error 都在这）
├── .watch-claude.pid
└── .watch-claude.log
```

调试时最有用的两个文件：`.watch-<role>.log`（看 watcher dispatch 是否 ok）+ `inbox/<role>/<id>.md`（看消息内容是否对）。只有成功注入目标 session 后才会生成 `<id>.md.dispatched`；`ai-ping` 会等待最多 4 秒并明确报告是否确认投递。
