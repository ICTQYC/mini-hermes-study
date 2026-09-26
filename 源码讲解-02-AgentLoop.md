# 源码讲解 · 第 2 章 Agent Loop

> 每节的结构都一样：
> **mini 版怎么做的 → 真实 Hermes 怎么做的 → 差在哪 / 为什么。**
>
> 所有结论都标了 `文件:行号` 来源。行号会漂，**按方法名 grep 更可靠**：
> `grep -rn "def 方法名" ~/.hermes/hermes-agent/agent/*.py`

[TOC]

---

## 两份代码的路径

```bash
# mini 版（我们自己写的，3174 行，零依赖）
$HOME/Desktop/study/hermes学习/mini-hermes/

# Hermes 源码（真实实现）
~/.hermes/hermes-agent/
```

**一键打印本文提到的所有 mini 侧坐标**（省得翻文件）：

```bash
cd "$HOME/Desktop/study/hermes学习/mini-hermes"
echo "① 中断"      && grep -n "_interrupt\b\|def interrupt" run_agent.py
echo "②③ 并发决策" && grep -n "force_sequential\|pool.map\|ThreadPoolExecutor" run_agent.py
echo "④ 预算"      && grep -n "budget.consume" run_agent.py && grep -n "def consume\|refund" agent/iteration_budget.py
echo "⑤ lifecycle" && grep -n "def run_conversation\|def _run_conversation_turn" run_agent.py
```

---

# 0. 先说结论：五处差异一览

| # | 主题 | mini 版 | 真实版 | 性质 |
|---|---|---|---|---|
| 1 | 中断 | 1 个 `threading.Event` 旗子 | **软/硬两档 + 3 个通道**（interrupt/steer/redirect）+ CAS | 简化 |
| 2 | 并发结果回填 | `pool.map` | **预分配数组按下标写** + 开始顺序闸门 + 富记录 | 简化 |
| 3 | 工具批次调度 | 整批并发 **或** 整批串行 | **分段**（屏障 / 路径冲突 / 降级合并三层判据） | **我们的写法是它的特例** |
| 4 | 迭代预算 | `while` 顶部 `consume()` | 消费在**每轮准备阶段**；while 只读；`refund` 有两类真实用途 | 读写分离 |
| 5 | agent loop 的拆分 | 2 个方法 | **30 个 `turn_*.py`** | 规模差异 |

**唯一"我们的写法被真实版完整包含"的是第 3 项**——其余四项都是"需求深度不同"，不是"写错了"。

---

# 1. 中断控制

**真实版位置**：`agent/interrupt_control.py`（`class InterruptControlMixin`）

**mini 版位置**：`run_agent.py:185`（建 Event）、`:230`（每轮清）、`:437`（主线程轮询）、
`:446`（收到 Ctrl-C 置位）、`:528`（`interrupt()`）

## 1.1 mini 版是怎么做的

```python
self._interrupt = threading.Event()      # 185：建旗子

def run_conversation(self, ...):
    self._interrupt.clear()              # 230：每轮开始清零
    ...

# API 调用在别的线程跑，主线程轮询等：
if self._interrupt.is_set():             # 437：检查旗子
    raise AgentInterrupted()
result = result_q.get(timeout=0.1)       # 所以必须轮询，不能阻塞等

def interrupt(self) -> None:
    self._interrupt.set()                # 528：置旗子
```

## 1.2 机制的正名：协同式取消

**前提：Python 没法安全杀死一个线程**（没有 `Thread.kill()`）。所以只能：

```text
设一个旗子 → 各处在"安全点"自己检查旗子 → 主动退出
```

这就叫 **协同式取消（cooperative cancellation）**。

**关键理解**：`threading.Event` **没有时间语义**——它是纯粹的**布尔旗子**。
它只表达"有人要求停"，**不表达"什么时候停"**；什么时候停由**检查旗子的人**决定。

> 所以上面那段必须用 `get(timeout=0.1)` **轮询**，不能阻塞等——
> 阻塞在 `get()` 上的那次调用不会自己醒来去看旗子。

## 1.3 真实版的四点不同

### ① 软 / 硬两档，而硬取消**不是**"终止进程"

`interrupt()` 的 docstring 原文：

> *``hard_cancel``: explicit stop; **compression may honor it even while ordinary interrupts are masked**.*

**硬取消 = 优先级更高的取消请求**：普通中断在某些情况下**会被屏蔽/忽略**，
硬取消**穿透那些屏蔽**。

不是进程级，不是"杀死"。是**强度**。

**它解决的问题是"用户按了没反应"**——不是死锁。比如 agent 卡在等用户回答的地方
（approval prompt / clarify）时，普通取消可能够不到它。

### ② 三个通道（mini 版只有一个旗子）

模块 docstring 第一行：

> *Soft/hard interrupt requests, tool-thread interrupt propagation, pending **steer/redirect queues**.*

| 通道 | 干什么 | mini 有吗 |
|---|---|---|
| `interrupt` | 打断当前这一轮 | ✓ |
| `steer` | **不打断**，往正在跑的轮次里**插入引导** | ✗ |
| `redirect` | 打断，**并把你新说的话当成这一轮的替代目标** | ✗ |

这解释了一个看起来奇怪的参数：

```python
def clear_interrupt(self, *, preserve_redirect: bool = False) -> bool:
```

**"清除中断"和"清除重定向"是两件事**——"我清掉中断状态，但用户刚才那句重定向的话别丢"。

还有一个细节，注释里明说了这种"隔离"：

> *Tool cancellation attribution stays separate from ``_interrupt_message``,
> which may carry the user's full next message.*

**"取消的归因"和"用户的下一条消息"分开两个字段存**——后者可能装着用户整段新输入。

### ③ `require_generation`：带代号的取消（CAS）

docstring 原文：

> *``require_generation``: activity-generation claim — published only if the turn's generation
> still matches at the final mutation edge; **returns False if the turn resumed meanwhile**.*

翻译：**"只在这一轮的活动代号仍然匹配时才生效；如果这一轮已经自己继续跑下去了，就放弃中止。"**

它解决的竞态：

```text
① 你点了"停止"
② 但请求送达之前，模型刚好返回了结果、这一轮继续往下跑了
③ 这时再执行"中止" → 会杀掉一个已经前进的状态
```

> 🎓 这是 **CAS（compare-and-swap）/ 乐观并发控制**，和我们 todo 的 `revision` 是**同一类解法**：
> 一个单调递增的版本号 + "我手里这个决定还是不是最新的"。
>
> 面试里这是个高频考点（并发控制、幂等、版本号），值得记住模式名。

### ④ 和上下文压缩的交互

`interrupt_control.py` 里有一段极复杂的 `_fence_cancel_before_commit`，
处理"压缩正在提交时收到硬取消"，注释里带着 issue 号（#99758 P1）。
mini 版没有压缩，所以没有这个问题——**这是第 5 章（context-compression）会碰到的真实难题**。

## 1.4 小结

| | mini 版 | 真实版 |
|---|---|---|
| 机制 | `threading.Event` + 轮询 | 同机制，但软/硬两档 |
| 通道数 | **1** | **3** |
| 竞态处理 | 无 | `require_generation`（CAS） |
| 与压缩的交互 | 无 | 有 |

---

# 2. 并发结果怎么保证顺序

**真实版位置**：`agent/tool_executor.py`（concurrent 那条路径）

**mini 版位置**：`run_agent.py:587-593`

## 2.1 mini 版是怎么做的

```python
if force_sequential:
    results = [self._run_one_tool(p, task_id) for p in parsed]
else:
    with ThreadPoolExecutor(max_workers=min(8, len(parsed))) as pool:
        results = list(pool.map(self._run_one_tool_with_task, parsed))
```

**`pool.map` 的作用是"保持入参顺序"**：即使第 2 个工具先跑完，返回的列表里它仍然在第 2 位。

## 2.2 真实版：预分配数组 + 按下标写

```python
self.results[i] = _ToolOutcome(pc.ref(...), pc.parse_error, 0.0, True, True)  # 参数解析失败
self.results[index] = outcome                                                 # 正常完成
if self.results[skipped_i] is None:                                           # 空位检查
    self.results[skipped_i] = _ToolOutcome(ref, result, 0.0, True, False)     # 补"跳过"
r = batch.results[i]                                                          # 按原序读出
```

**同一个保证（按原序读出），两种实现。**

## 2.3 为什么不用 `pool.map`

| 能力 | `pool.map` | 预分配数组 |
|---|---|---|
| 乱序完成、按序读出 | ✓ | ✓ |
| **"不经过执行"的结果也能有位置** | ✗ | ✓ |
| **"空位"这个概念**（跳过 / 放弃） | ✗ | ✓ |
| 分段收集（按批读 `batch.results`） | ✗（一次性返回） | ✓ |

**核心**：`pool.map` 要求"每个元素都经过同一个函数、产出恰好一个结果"。
但真实系统里**有的结果不是"执行出来的"**——参数解析失败、被跳过、被中断放弃，
**都占一个位置**。

## 2.4 额外的：它连"开始顺序"都要管

同一个文件里有个 `begin_in_order`：

```python
def begin_in_order(self, order: int, callback=None, *, tool_name: str = "") -> bool:
    """Wait for ``order``, run ``callback``, advance. Returns False if abandoned."""
    # Bounded wait so one wedged dispatch can't starve later-ordered workers; on
    # expiry proceed out of order (interleaved prompts beat starvation). ``>=`` (not
    # ``==``) releases every skipped worker at once; abandoned short-circuits.
```

**它不只保证"结果顺序"，还保证"开始顺序"**——而且是**带超时的闸门**：
超时就乱序放行，注释给的理由是 *interleaved prompts beat starvation*（交错的提示好过饿死）。

`pool.map` 里完全没有这个概念。

## 2.5 结果是富记录，不是字符串

```python
_ToolOutcome(ref, result, duration, is_error, blocked)
```

带 `duration` / `is_error` / `blocked`。mini 版只回传字符串。

**为什么重要**：有了 `is_error` 和 `blocked`，后续才能分别处理
（比如"被阻塞"不该算失败、"失败"要不要进"没反应"汇报）。

---

# 3. ★ 工具批次是「分段」，不是「整批并发 / 整批串行」

**这条最重要——它推翻了我们第 2 章的一个简化。**

**真实版位置**：`agent/tool_dispatch_helpers.py:164` — `_plan_tool_batch_segments(tool_calls, *, execution_cwd)`

**mini 版位置**：`run_agent.py:580-593`（**就这 14 行**）

## 3.1 mini 版是怎么做的

```python
force_sequential = len(parsed) <= 1 or any(
    model_tools.registry.is_interactive(p["name"]) for p in parsed
)
if force_sequential:
    results = [... 整批串行 ...]
else:
    with ThreadPoolExecutor(...) as pool:
        results = list(pool.map(...))       # 整批并发
```

**两个档位：整批并发，或者整批串行。** 只要有一个 interactive 工具，**整批退化成串行**。

## 3.2 docstring 原文（答案整段就在这里面）

```text
Split a tool-call batch into ordered ("parallel"|"sequential", calls) segments.

Call order is preserved exactly (a later call never crosses an earlier barrier), so
result order and side-effect boundaries match fully-sequential execution. Barriers:
_NEVER_PARALLEL_TOOLS, unparseable/non-dict args, anything not parallel-safe.
Path-scoped tools join a run only if they don't conflict with its reservations:
reader↔reader overlap stays parallel; any overlap involving a writer closes the run so
the call starts a NEW run after the conflicting one lands. Runs shorter than two calls
demote to sequential (it owns the richer inline dispatch); adjacent sequential merge.
```

> 📌 **习惯**：Hermes 的 docstring 写"为什么"写得很足。
> **看到 docstring 就把整段抄下来**——这里的答案就整段在里面。

## 3.3 三层判据

### 第 1 层 · 屏障（barrier）

```text
_NEVER_PARALLEL_TOOLS      ← 永远不能并行的工具（interactive 类在这）
参数解析失败 / 非 dict      ← 连执行都执行不了
任何"非并行安全"的          ← 白名单式判定
```

命中屏障 → **关掉当前并行段，这个调用单独走串行**。

**这一层就是我们 mini 版实现的全部。**

### 第 2 层 · 路径冲突 ★（最想不到的一层）

```python
if any(
    (is_writer or existing_is_writer) and _paths_overlap(scoped_path, existing)
    for scoped_path in scoped_paths
    for existing, existing_is_writer in reserved_paths
):
    _close_parallel()      # 关掉并行段，让这个调用排在冲突者落地之后
```

判据精确成一句话：**路径重叠 **且** 至少一个是写者 → 冲突。**

| 组合 | 能并行吗 | mini 版 |
|---|---|---|
| `read_file a.py` + `read_file b.py` | ✓ 读↔读，不重叠 | ✓ 并行 |
| `read_file a.py` + `read_file a.py` | ✓ **读↔读重叠，仍然并行** | ✓ 并行 |
| **`read_file a.py` + `write_file a.py`** | **✗ 重叠且有写者** | **⚠️ 会并发跑** |
| `write_file a.py` + `write_file b.py` | ✓ 不重叠 | ✓ 并行 |
| `write_file a.py` + `write_file a.py` | ✗ 重叠 | ⚠️ 会并发跑 |

> ⚠️ **这是 mini 版的一个真实隐患**：我们只看"是不是 interactive"，
> **完全不看路径冲突**。所以"读 a.py + 写 a.py"在我们这儿会**并发跑**——
> 可能读到写了一半的内容。
>
> 不算 bug（模型很少这么干），但**是一个真实存在的保证差异**。
> 想验证的话：让它"读 x.txt，同时往 x.txt 追加一行"，看会不会出问题。

### 第 3 层 · 降级与合并

```text
一段里 < 2 个调用  → 降级为串行（docstring: "it owns the richer inline dispatch"）
相邻的串行段       → 合并
```

**注意 `it owns the richer inline dispatch` 这句**：串行路径**拥有更丰富的内联分发能力**。
所以"不足 2 个调用就降级"**不只是"并行没意义"，还有功能理由**——
串行那条路才能完整处理 agent 级工具。

> 这也解释了 mini 版 `len(parsed) <= 1` 为什么不是随手写的：
> **只有 1 个调用时走串行是必要的**（那个调用可能是 agent 级工具）。

## 3.4 收口：我们的写法是它的特例

Hermes 里还有这个函数：

```python
def _should_parallelize_tool_batch(tool_calls) -> bool:
    """True iff the planner yields a single all-parallel segment for the WHOLE batch."""
    if len(tool_calls) <= 1:
        return False
    segments = _plan_tool_batch_segments(tool_calls)
    return len(segments) == 1 and segments[0][0] == "parallel"
```

**它用 planner 的结果来回答"这一批要不要走并发路径"。**

对照我们 mini 版的：

```python
force_sequential = len(parsed) <= 1 or any(... is_interactive ...)
```

**这就是它的简化实现——我们跳过了 planner。**

> ✅ **结论**：我们 mini 版**不是写错了**，是写了 `_should_parallelize_tool_batch` 的简化版。
> 真实版先**规划**（分段），再决定每段怎么跑；我们只有两个档位。
>
> **你在第 2 章学到的那条规则（interactive 必须打断并发）完全正确**——
> 粗糙的是**实现它的方式**：真实版只有"该串行的那一段"串行，**其余段照样并发**。
>
> 这是读源码最该给你的东西：不是"哦原来一样"，而是"**哦原来还有更好的做法**"。

---

# 4. 迭代预算的消费点

**真实版位置**：`agent/turn_iteration_prep.py:361`、`agent/conversation_loop.py:1520`、
`agent/turn_finalizer.py:128`、`agent/iteration_budget.py`

**mini 版位置**：`run_agent.py:271`、`agent/iteration_budget.py:22`

## 4.1 mini 版是怎么做的

```python
while True:
    if not self.budget.consume():          # 消费和判断合并成一句
        return {"final_response": "...预算耗尽..."}
```

## 4.2 真实版：消费点不在 `while` 顶部

```python
# ① 循环条件只"读"（无副作用），而且被多处读：
agent/conversation_loop.py:1520
    while (s.api_call_count < agent.max_iterations
           and agent.iteration_budget.remaining > 0) or agent._budget_grace_call:

# ② 真正的"扣减"在每轮的准备阶段：
agent/turn_iteration_prep.py:361
    elif not agent.iteration_budget.consume():
```

**为什么分开？** 因为"剩余多少"这个值，**别处也要读**：

```python
agent/turn_finalizer.py:128
    api_call_count >= agent.max_iterations or agent.iteration_budget.remaining <= 0
```

所以需要**一个只读接口**（`remaining`）和**一个写入接口**（`consume`）。

> **mini 版把两者合一**：导致别处想读就得产生副作用（读一次少一次）。
> 这条设计原则很通用：**读取状态和修改状态要分开**，否则"看一眼"和"用一次"就分不清了。

## 4.3 `refund()` 的真实理由（两处，都很具体）

**第一处** `agent/turn_tool_round.py:186`：

```python
# Refund the iteration when the ONLY tool was execute_code (programmatic tool
# calling) — cheap RPC-style calls shouldn't eat the budget.
if {tc.function.name for tc in assistant_message.tool_calls} == {"execute_code"}:
    agent.iteration_budget.refund()
```

→ **唯一工具是 `execute_code` 时退款**：程序化工具调用是**便宜的 RPC 式调用**，
不该吃预算。

**第二处** `agent/turn_preflight_gate.py:56`：

```python
v.api_call_count -= 1
agent._api_call_count = v.api_call_count
with suppress(Exception):
    agent.iteration_budget.refund()
```

→ Ollama runtime context 太小、**提前失败退出**时退款：**"这一轮不算数"**
（不是模型的问题，是环境配置问题，不该罚它）。

> **我们 mini 版的 `refund()` 从来没人调**——抄了接口没抄用法。
> 真实版调它的理由是这两类：**"便宜调用"和"环境导致的失败"。**

## 4.4 默认值

| | 默认 |
|---|---|
| mini 版 | 500 |
| Hermes **主** agent | **无限**（`sys.maxsize`，`agent_init.py:2232`） |
| Hermes **子** agent | **250**（`delegation.max_iterations`，曾有迁移 50 → 250） |

> 预期外的答案：**主 agent 默认不限次数，子 agent 才限。**
> 理由：主 agent 有用户在盯（能随时中断），子 agent 无人看管（必须设上限）。
>
> 同类思路在别处也有：主 agent 可以问用户（clarify / approval），
> 子 agent **不能问**（`delegate_task` 的约束之一）。

---

# 5. 9 步 lifecycle 的真实映射

**真实版位置**：`agent/conversation_loop.py:1579`（`def run_conversation`）+ `agent/turn_*.py`

**mini 版位置**：`run_agent.py:202`（`run_conversation`，步骤 1~3.5）、
`:268`（`_run_conversation_turn`，步骤 4~9 的循环）

## 5.1 答案：9 步拆成了 30 个文件

```console
$ ls ~/.hermes/hermes-agent/agent/turn_*.py | wc -l
      30
```

完整清单：`turn_api_call` / `turn_api_error` / `turn_api_request` / `turn_author` /
`turn_context` / `turn_context_compaction` / `turn_empty_response` / `turn_explainers` /
`turn_facade` / `turn_facade_lease` / `turn_failure_copy` / `turn_final_response` /
`turn_finalizer` / `turn_iteration_prep` / `turn_liveness` / `turn_loop_errors` /
`turn_overflow` / `turn_preflight` / `turn_preflight_gate` / `turn_recovery` /
`turn_request_assembly` / `turn_response_check` / `turn_response_intake` / `turn_retry_state` /
`turn_stop_gates` / `turn_summary` / `turn_tool_round` / `turn_tool_validation` /
`turn_truncation` / `turn_usage`

## 5.2 映射表

| 文档的步 | 真实归属 | mini 版 |
|---|---|---|
| 1. 生成 task_id | `agent_init.py` / `turn_context.py` | `run_conversation` 前几行 |
| 2. 用户消息进历史 | `conversation_loop.py:170`（`append_message`） | 同 |
| 3. 构建/复用系统提示词 | `conversation_loop.py:767`（`stage_surface_switch_note`）、`:788`（`_persist_system_prompt`） | 同 |
| 4. 预检压缩 | **`turn_preflight.py` + `turn_preflight_gate.py` + `turn_context_compaction.py`** | 一个 `_preflight_compression` |
| 5. 构建 API 消息 | **`turn_request_assembly.py`** | `_build_api_messages` |
| 6. 注入临时提示层 | **`turn_context.py`** | `_inject_ephemeral_layers` |
| 7. 打缓存标记 | `conversation_loop.py:1124`（`strip_anthropic_cache_control`） | `_apply_prompt_caching` |
| 8. 可中断 API 调用 | **`turn_api_call.py` + `turn_api_request.py` + `turn_api_error.py` + `turn_retry_state.py`** | `_interruptible_api_call` |
| 9. 解析响应 → 回环 | **`turn_response_intake.py` + `turn_response_check.py` + `turn_tool_validation.py` + `turn_tool_round.py`** | `_parse_response` + `_execute_tool_calls` |

**另外这一批没有对应的文档步骤，但真实版有独立文件**：

| 文件 | 管什么 |
|---|---|
| `turn_finalizer.py` / `turn_final_response.py` | 收尾 |
| `turn_usage.py` / `turn_summary.py` | token 统计 / 摘要 |
| `turn_loop_errors.py` / `turn_recovery.py` | 出错与恢复 |
| `turn_overflow.py` / `turn_truncation.py` / `turn_empty_response.py` | 上下文溢出 / 截断 / 空响应 |
| `turn_stop_gates.py` | 停止条件 |
| `turn_liveness.py` | 活性（配合第 1 节的 generation） |
| `turn_facade.py` / `turn_facade_lease.py` | 门面 + 租约 |
| `turn_explainers.py` / `turn_failure_copy.py` | 给用户看的解释文案 |

## 5.3 "为什么拆"——答案写在他们自己的开发规范里

`~/.hermes/hermes-agent/AGENTS.md` 原文：

> **Don't recreate god files.** A file passing ~2,000 lines or a function passing ~300 lines /
> cyclomatic complexity 30 is the signal to split along `<stem>_<topic>` FIRST, in its own commit.

而且是**硬规矩**（写进规范的纪律，不是风格偏好）：

> **Find code by topic, not by facade:** `grep -rn "def name" <dir>/<stem>_*.py`.
> Reading the facade first is the expensive way.

**这条纪律反过来解释了为什么 `run_agent.py` 只剩下 1560 行的门面。**

## 5.4 一个容易搞混的点：`run_conversation` 有两个

| 位置 | 干什么 |
|---|---|
| `agent/turn_facade.py` | **门面**：约 178 行 / 25 条语句（turn 准入、会话租约、记账作用域） |
| `agent/conversation_loop.py:1579` | **真身**：真正跑循环的地方 |

**你在官方文档里看到的"9 步清单"，就是这个门面函数逐条翻译出来的**——
所以文档读者看到的是"一个函数里 9 步"，实际代码里散在 30 个文件。

---

# 6. 第 2 章差距清单（可选的改进项）

不急着改，列在这里，等第 4 章（`tools-runtime.md`）之后统一处理：

| # | 差距 | 值不值得改 |
|---|---|---|
| 1 | 并发不看**路径冲突**（读 a.py + 写 a.py 会并发） | 中等——想体验"分段 planner"的话值得 |
| 2 | `force_sequential` 只有两档，没有分段 | 低——1~2 个工具时收益为负 |
| 3 | `pool.map` 而非预分配数组（无法表达"跳过/放弃"的结果） | 低——需要"某些结果不经过执行"时才有意义 |
| 4 | 结果只回传字符串，没有 `is_error` / `blocked` / `duration` | 中等——`is_error` 会让"没反应"类排查更容易 |
| 5 | `refund()` 抄了没人调 | 低——要等 `execute_code` 那类工具出现 |
| 6 | 没有 steer / redirect 通道 | 低——那是多轮对话体验，第 7 章（gateway）才相关 |
| 7 | 没有 `require_generation`（CAS） | 低——除非出现"多入口改同一状态" |

**判断标准**：这条差距**在新的需求出现之前**值不值得补？
如果补了只是"更像 Hermes"但没人用得上，**就先不补**（第 4 节那笔账：抽表在 1 个工具时是负债）。
