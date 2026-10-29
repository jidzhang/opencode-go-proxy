# 待决策项 — 2026-10-13 代码审查遗留

2026-10-13 对工作区改动（空响应重试特性 + caller token 剥离）跑了 `/code-review max --fix`，
共 16 项发现，11 项已修复并经测试验证。本文记录需要人工决策的 1 项阻断项
和 2 项被跳过的较大问题，供回来后定夺。工作区改动尚未提交。

相关文件：
- `src/opencode_go_proxy/zen_upstream.py`（重试逻辑）
- `src/opencode_go_proxy/guards.py`、`src/opencode_go_proxy/passthrough.py`（token 校验/剥离）
- `plans/cleanup-progress.md`（历次审查修复批次）

---

## 决策 1（阻断）：reasoning delta 是否计入"有内容"

**当前状态**：`zen_upstream.py:1742` 的 `_RESPONSES_CONTENT_MARKERS` 现在只含
`response.output_text.delta`、`response.function_call_arguments.delta`、`function_call`、
`tool_call`。审查前的版本还包含 `reasoning_text.delta` 和 `reasoning_summary_text.delta`，
审查修复时把它们删掉了。

**背景**：`_relay_responses_verbatim` 会缓冲第一次 SSE 尝试，若流正常结束但
`_responses_buffer_has_content` 判定无内容，则丢弃缓冲并重试一次（最多重试一次，
上限 8MB）。判定为"有内容"的流直接 flush，不重试。

**矛盾点**：审查发现原版本的 reasoning 标记与 `_responses_buffer_has_content`
自身 docstring 矛盾——docstring 写明 reasoning-only 流按空处理。若把 reasoning
计入内容，则"只有 reasoning 输出"的完整流不会触发重试，而客户端 SDK 会丢弃带
签名的 reasoning part，回合仍然以 `empty_model_response` 失败——这恰恰是重试
特性要解决的场景。

**两种立场**：

| 选项 | 含义 | 连带工作 |
|------|------|----------|
| A. 保持现状（reasoning 不算内容） | reasoning-only 完整流触发一次重试，符合 docstring | 无 |
| B. 回退为计入内容 | 只要出现过 reasoning delta 就视为成功，绝不重试 | 恢复两个标记及 `zen_upstream.py:1748` 的解释注释；同步改测试 `test_verbatim_reasoning_only_terminal_retries_once` |

**判断依据**：如果当初把 reasoning 加入标记是笔误（例如照抄了中继过滤的
事件清单），选 A；如果这是刻意策略（"上游已经开始产出就不再花钱重试"），
选 B。注意选 B 时 reasoning-only 流在客户端依然会失败，重试等于放弃最后一次
自动挽救的机会——这是策略问题，不是对错问题。

---

## 决策 2（可选，设计取舍）：首 token 延迟

缓冲策略对所有 openai_responses 流生效（含 Go provider——`go_upstream.py:110`
共享 `handle_family_responses_request`）。健康流在生成结束或攒满 8MB 之前，
客户端只能收到 keepalive 注释行，首 token 延迟等于总生成时长。

- 影响面：长生成（如 120s）的交互体验；忽略注释行的客户端空闲看门狗可能误判超时。
- 备选方案：检测到首个内容标记后切换为实时直通（混合模式，放弃对该流的重试能力）；
  或仅对 zen provider 启用缓冲。
- 现状：未改。如果目前没有实际卡顿/超时反馈，可以接受现状。

## 决策 3（可选，低概率竞态）：keepalive 双锁交接

重试交接处 `ka_thread.join(timeout=interval)` 返回时，第一个 keepalive 线程
可能仍阻塞在 `handler.wfile.write`（客户端发送缓冲满）；随后 `_relay_stream`
用另一把锁写同一 socket。若客户端停滞恰好在这之后恢复，迟到的 `: keepalive`
注释可能插进 SSE 事件行之间（按 SSE 规范，`:` 注释行会被客户端忽略，不截断事件，最多是一行多余注释，不构成帧损坏）.

- 概率低（要求客户端停滞窗口与交接精确重叠）；修复需要结构性改动线程交接
  （共享锁或线程接管），风险大于收益，故跳过。
- 现状：未改。

## 仅记录、不建议处理

- 跨 `streaming.py` / `passthrough._relay_stream` / `_ZenStreamEngine` 抽取
  公共"SSE 中继 + 空重试"原语；`quota._headers_map` 归一化复用。超出本次 diff
  范围，属后续重构议题。
- 逐行 `_client_gone` 探测、list-of-bytes 缓冲：与既有代码先例一致，未改。

## 决议记录（2026-10-13）

- 决策 1：选 A——reasoning 不计入"有内容"，保持现状。依据：`reasoning_text.delta`
  标记只存在于当日未提交草稿，提交历史中无此设计（`git log -S` 零命中），无历史意图
  需要保护；实质上 reasoning-only 回合在客户端必失败，重试是最后的自动挽救，
  A 的最坏情况即 B 的结果。代码注释、docstring、测试三处口径已统一，无需改动。
- 决策 2：跳过。无卡顿/超时反馈则维持全量缓冲；若有反馈，演进路线为首个内容标记后
  转实时直通（放弃该流重试权）。
- 决策 3：跳过。竞态既低概率又低影响（见上文注释行说明）；此处措辞已按分析降级。
- 后续备忘（非阻断）：`_responses_buffer_has_content` 为字节子串匹配，reasoning 文本
  内容若字面含 `tool_call`/`function_call` 会误判为有内容；根治是按 JSON `type`
  字段判定，留待以后 hardening。
- 工作区改动未提交（按要求）。

## 验证状态（2026-10-13）

- 受影响 4 个测试文件 93/93 通过；`tests/test_zen_upstream.py` 收集 36 项
  （原 30 + 迁移 3 + 新增回归 3）。
- 全量套件失败列表与干净基线一致（均为既有 Windows/环境问题，非本次引入）。
