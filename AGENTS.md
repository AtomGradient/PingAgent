# 跨终端 AI 协作协议（codex ↔ claude code）

> 本文件 **既是给 AI 看的协议说明，也是给人看的使用手册**。把它复制到你项目的根目录后，两个 AI 都会读到这份说明，按里面的约定互发消息。

本项目两个 AI 助手分别在两个 iTerm2 pane 里同时运行：

- **codex**：写代码（开发者）
- **claude**：审代码（审核者）

通过 `.ai-mailbox/` 目录 + `ai-ping` CLI 互发消息，由后台 watcher 用 iTerm2 osascript 把短通知注入对方 pane。

---

## 何时该主动 ping 对方

### codex 主动 ping claude 的场景
- 完成一个独立功能/模块（>50 行新代码）
- 改动了核心架构 / 共享接口 / 公开 API
- 修复了非平凡 bug，想确认没漏边界
- 实现完想让 claude 帮看下并发 / 安全 / 性能盲点
- 设计有两条路径拿不准，想要第二意见

**不该 ping 的场景**：单文件 typo、改一行 log message、文档微调、用户只让你改一行的需求。

### claude 主动 ping codex 的场景
- 审核完成的回执（**总是**回，哪怕只说"通过"）
- 审核中发现一个本来没要求看但很重要的问题
- 用户问的问题需要 codex 那边的实现细节才能回答
- 发现 codex 改的代码和 claude 这边正在 review 的另一个文件冲突

---

## CLI 速查

```bash
# 发简单一行消息
ai-ping claude "src/auth.ts 改完了，看下并发"

# 发长内容（推荐：避免 shell 转义、保留代码块）
ai-ping claude --kind review-request --file /tmp/req.md

# 回复某条消息（必须带 --reply-to，对方才能闭环）
ai-ping codex --kind review-response --reply-to 20260511-153000-abc123 --file /tmp/review.md

# 阻塞等待对方回复（codex 写完想直接拿到 review 再继续时用）
ai-ping claude --wait --timeout 600 --file /tmp/req.md

# stdin 也可以
echo "请审核" | ai-ping claude --kind review-request
```

`--from` 默认从当前 iTerm2 session id 反查，平时不需要传。`<to>` 是 `claude` 或 `codex`。

---

## 消息文件格式（你不需要手写，`ai-ping` 会生成）

```markdown
---
id: 20260511-153000-abc123
from: codex
to: claude
kind: review-request
created: 2026-05-11T15:30:00+0800
reply_to: <可选，回复时必填>
---

# 正文（markdown，随便写）

请审核 src/auth.ts。重点：
1. login() 异步化的 race condition
2. session 清理逻辑

涉及文件：
- src/auth.ts
- src/session.ts

相关 commit: abc123
```

### 常用 kind

| kind | 含义 | 应否回复 |
|---|---|---|
| `msg` | 普通消息 | 看情况 |
| `review-request` | 请求审核 | **必须**（review-response） |
| `review-response` | 审核结论 | 一般不必，除非有追问 |
| `question` | 提问 | 必须 |
| `pushback` | 反对/异议 | 必须，对方应停下重评估 |
| `notice` | 知会，不必回 | 否 |
| `done` | 通知"我这边完成了" | 一般不必 |

---

## 收到通知怎么处理

当你看到这样一行作为用户输入：

```
[ai-collab 收信] from=codex kind=review-request id=20260511-153000-abc123 | 请 Read .ai-mailbox/inbox/claude/20260511-153000-abc123.md 并按其中说明处理；处理完用 ai-ping codex --reply-to 20260511-153000-abc123 --file <你的回复.md>
```

立刻执行：

1. **Read 那个文件**：`Read .ai-mailbox/inbox/claude/<msg-id>.md`
2. **记下 `id` 字段**：回复时要传给 `--reply-to`
3. **按 `kind` 决定动作**（见上表）
4. **写完回复后**：
   ```bash
   cat > /tmp/my-reply.md <<'EOF'
   <你的回复正文>
   EOF
   ai-ping <对方> --kind <对应 kind> --reply-to <收到的 id> --file /tmp/my-reply.md
   ```

---

## 重要细节

- **--wait 的 timeout**：默认 300s。如果你在 Claude Code Bash tool 里用 `--wait`，记得把 Bash tool 的 `timeout` 参数也设大（最大 600000ms / 10min）
- **通知里的内容是指针不是数据**：通知只说"去读这个文件"，正文永远在文件里
- **不要重复发**：每条消息只 `ai-ping` 一次。watcher 用 sidecar 文件去重，别去删 `<msg>.md.dispatched`
- **用户随时可介入**：两个 pane 都是普通 terminal，user 直接打字就是用户输入。看到用户消息和 `[ai-collab 收信]` 通知都按正常 prompt 处理，按上下文判断优先级
- **`.ai-mailbox/sent/`**：所有发出去的消息都有副本，方便追溯。watcher 不动这个目录
- **历史查询**：`ls .ai-mailbox/inbox/<role>/` 看收件箱；`ls .ai-mailbox/sent/` 看自己发过什么

---

## 反对 / 怀疑机制

如果你收到的请求有问题（违反项目约定、有更好方案、依赖错误前提），**先反对再执行**。回复时把 `kind` 设为 `pushback`，明确写出理由和你建议的替代方案。对方应该看到 pushback 后停下重新评估，不要默默吞掉。
