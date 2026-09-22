# 第 3 章 · Prompt Assembly（系统提示词拼装）

> 官方原文：`官方文档-Developer-Guide/prompt-assembly.md`（336 行）
> 对照源码：`~/.hermes/hermes-agent/agent/system_prompt.py`（817 行）、
> `agent/prompt_builder.py`（1745 行）、`agent/subdirectory_hints.py`（236 行）、
> `agent/surface_switch.py`（124 行）
>
> 读法同第 2 章：**段落式翻译 + `【源码】` 块接真实实现 + `【动手】` 块接 mini 版 +
> 每节末尾「为什么这么设计 / 砍掉会怎样」。**

---

## 0. 这一章的一句话

**系统提示词不是"每轮重新拼一遍的字符串"，而是一份"冻结的、按缓存友好度分层的产物"。**

Hermes 把提示词拼装拆成两个东西：

| | 是什么 | 什么时候变 |
|---|---|---|
| **缓存态（cached）** | 系统提示词的三大层 | 只在**会话开始**和**压缩后重建**时 |
| **调用时临时层（ephemeral）** | 只贴在**这一次** API 请求上的附加文本 | 每次调用都可以不同，**不写回历史** |

原文开篇就把这件事列为项目最重要的设计选择之一，因为它同时影响四件事：

- token 用量
- prompt 缓存的有效性
- 会话连续性
- 记忆的正确性

**主要文件**（原文列的，实际更多）：

- `run_agent.py`
- `agent/prompt_builder.py`
- `tools/memory_tool.py`

> 【源码】原文这三个文件是**门面/主题分工**的典型：
> `run_agent.py` 只是 facade，真正装配的是 `agent/system_prompt.py` 的
> `build_system_prompt_parts()`。原文漏了 `agent/system_prompt.py` 这个最关键的文件。
> **这是原文第一个不精确的地方**——它自己在下一段就说"见 `agent/system_prompt.py`"。

---

## 1. 缓存态 vs 调用时临时层

原文标题就是 **Cached system prompt layers**（缓存住的系统提示词层），
后面另有一节 **API-call-time-only layers**（只在 API 调用时存在的层）。

这两个概念是理解全章的钥匙：

```text
┌──────────────────────────────────────────────────────────┐
│ 缓存态（写进 history 的第一条 system 消息）                │
│   ├── stable  层   ← 整个会话不变                          │
│   ├── context 层   ← 跨会话可能变（换目录、换 worktree）    │
│   └── volatile 层  ← 重建时最可能变（技能、记忆、时间）      │
├──────────────────────────────────────────────────────────┤
│ 调用时临时层（只贴在本次请求的副本上）                      │
│   ├── ephemeral_system_prompt                            │
│   ├── prefill messages                                   │
│   ├── 网关派生的会话上下文覆盖                              │
│   └── 后轮由 Honcho/外部记忆召回、塞进本轮 user 消息的内容   │
└──────────────────────────────────────────────────────────┘
        ↑ 上框进缓存，下框每次现拼          （原文图意）
```

### ⚠️ 先澄清最容易误解的一点：每次请求都发**完整的那份**

"缓存态"这个名字很容易让人以为"发过一次，之后服务器就存着了"。**不是的。**

实测（真跑一遍 loop，抓下每一轮**实际发出的**请求）：

| 请求 | system 消息 | 历史部分 | 请求总长 |
|---|---|---|---|
| #1 | **875** 字符 | 36 | 988 |
| #2 | **875** 字符 ← 逐字节相同 | 239 | 1195 |
| #3 | **875** 字符 ← 逐字节相同 | 473 | 1433 |
| #4 | **875** 字符 ← 逐字节相同 | 645 | 1609 |

**每一轮都把完整的 system prompt 重新发了一遍**，而且字节一模一样。

为什么必须这样：**API 是无状态的**。服务器不记得你上一轮发过什么——你不发，它就不知道。
所谓"上下文"是我们**每一轮重新拼一份完整的**塞进去的，不是服务器替你存着。

那"缓存"省了什么？**省的是服务器端对这段前缀做预填充（prefill）的计算**——
也就是把这段文字转成 KV 的计算量。**字节还是要传的。**

> 📌 所以"缓存态"这句话要说得更准：
> **不是"发过一次以后就不发了"，而是"每次都要发、而且每次发的是同一份"。**
> "同一份"才是它全部的价值。

这样一来三层的分工就更清楚了：

```text
stable + context ──── 字节不变 → 服务器能复用这段前缀的 KV 计算
volatile         ──── 放最后   → 它变了也切不断前面的复用
临时层            ──── 根本不进历史 → 连"缓存态"都不碰
```

### 为什么这么设计

**因为省钱。** 提供商的 prompt 缓存是**前缀缓存**：
如果一段前缀和上次完全一样，就能按"缓存命中"的价格读，通常便宜一个数量级。

只要系统提示词**字节级稳定**，整个会话的 system 部分就一直在缓存里。
一旦某轮往里塞一个"当前时间"，**从那一字节开始的后缀全部作废**，
下一轮要把整个上下文重新按全价算一遍。

### 砍掉会怎样

不做这个分离，最直接的后果是**账单翻倍**，而且是"用户看不见的翻倍"——
功能上一切正常，只是钱多花了。这也是为什么 Hermes 把它写成"最重要设计之一"：
**它没有症状，只有代价。**

### 它什么时候才会变（重建时机）

源码里只有**一个入口**能改变缓存态：`invalidate_system_prompt()`。
所以"什么情况下会变"就等于问"谁调用了它"——一共这几个时机：

| 时机 | Hermes 里的位置 |
|---|---|
| **上下文压缩之后** ★ 最主要的那个 | `agent/conversation_compression.py:2834` |
| **新会话（`/new`）** | `cli_commands_mixin.py:332`（顺带重置 todo、通知记忆 provider） |
| **会话切换 / 会话边界** | `cli_session_mixin.py:553` |
| **历史被倒回**（rewind / truncate） | `_publish_truncated_history(..., invalidate_prompt=True)` |
| **中途故障转移到"需要缓存"的 provider** | `agent/system_prompt.py:758` |

Hermes 自己的模块说明把总规则一句话说完了：

> Built once per session and reused across turns
> (**only context compression triggers a rebuild**) so the upstream prefix cache stays warm.
>
> （每个会话构建一次、跨轮复用；**只有上下文压缩会触发重建**，
> 这样上游的前缀缓存才能保持热的。）

#### ⚠️ "内容住在哪一层" ≠ "什么触发重建"

这两件事很容易被文档混成一件。你想改的东西**住在哪层**，和**什么时机重建**，
是两个独立的问题：

| 你想改的东西 | 住在哪一层 | 怎么生效 |
|---|---|---|
| 项目规则 `AGENTS.md`（跟目录走） | **context 层** | 换目录 → 会话边界清掉工作区快照的 pin → 在那个时机重建 |
| 技能索引（装了新 skill） | **volatile 层** | 重建时被刷新（**它自己不会触发重建**） |
| `SOUL.md`（人格） | **stable 层** | 同上，等下一个重建时机 |

结论：**在会话中途编辑 `SOUL.md`、装新 skill、换目录 —— 都不会自动重建**，
要等下一个重建时机（压缩，或 `/new`）。

`invalidate_system_prompt` 的 docstring 正是在说这件事：

> 强制下一轮重建（压缩之后）：**从磁盘重新加载记忆**，好让重建的提示词能捕获
> 本会话里发生的写入……

反过来读就明白了：**压缩之前的那些写入，本来是不会被捕获的。**

#### mini 版差在哪

mini 只有 **2 个**时机（新会话 / 显式 `invalidate`），Hermes 有 **4 个**——
多出来的是「压缩之后」和「**存盘提示词校验**」。

后者是 `_stored_prompt_matches_runtime()`：**校验存盘的那份还对不对**
（拿运行时环境块的边界当锚点），不对就重建。**mini 还没做** → 差距清单第 2 条。

实测 mini 的行为（用一个保证不会撞车的唯一标记验证）：

| 场景 | 缓存态字节数 | 含我写的标记 |
|---|---|---|
| ① 会话开始（磁盘上有标记） | 915 | **是** |
| ② 磁盘上把标记删了 | **915（不变）** | **是** ← 本会话还在用旧那份 |
| ③ 显式 `invalidate` 之后 | 903 | **否** ← 这才看到磁盘上的新内容 |
| ④ 换 `session_id` | 874 | 否 ← **只差一行 `Session ID:`**（diff 过） |

> ⚠️ **演示时踩的坑（第四次同根因）**：我第一版脚本拿 `表格` 当标记，
> 结果打出"（变了）"是**假的**——`表格` 这个词**恰好也出现在平台提示的原文里**
> （"不要用 Markdown 语法（终端不渲染 **粗体**、# 标题、表格）"），子串检查命中了
> 不相关的地方。
>
> 教训：**"这个词出现了" ≠ "我写的那段被加载了"**。
> 要验证"某段内容有没有进提示词"，标记必须**保证不会撞车**
> （上面用的是 `标记_记忆写入_只有这里会出现_QZ9`）。
> 这和前面"拿长度当内容有没有变的代理指标"是同一个错误：**用间接信号代替直接观察**。

---

## 2. 三层装配

原文：

> 缓存的系统提示词按**三个有序层**装配（见 `agent/system_prompt.py`）：
>
> 1. **stable** —— 身份（`SOUL.md` 或回退身份）、工具/模型指导、编码作业简报
> 2. **context** —— 调用方给的 `system_message`、项目上下文文件
>    （`.hermes.md` / `AGENTS.md` / `CLAUDE.md` / `.cursorrules`）、
>    然后是与 worktree 相关的 git 工作区快照、运维指令和平台提示
> 3. **volatile** —— 技能索引、内建记忆快照（`MEMORY.md`）、用户画像快照（`USER.md`）、
>    外部记忆 provider 块、时间戳/会话/模型/provider 行，然后是运行时环境提示
>    （主机 / home / **当前工作目录**）
>
> 最终系统提示词按 `stable` → `context` → `volatile` 拼接。

### 【源码】真身

```python
# agent/system_prompt.py:659  build_system_prompt_parts()
def build_system_prompt_parts(agent, system_message=None) -> Dict[str, str]:
    """Assemble the system prompt as three ordered cache tiers: ``stable`` (identity,
    guidance and the coding brief), ``context`` (caller ``system_message``, project
    context files, workspace snapshot and remaining workspace guidance) and
    ``volatile`` (skills index, memory, user profile, external memory block,
    timestamp line, runtime environment hints).  Worktree-dependent blocks follow project
    context so a shared context file can remain in the longest common prefix across worktrees.
    Never re-rendered mid-session."""

    # ── Stable tier ────────────────────────────────────────────────
    stable_parts, _soul_loaded = _identity_parts(agent, _ctx_len)
    _help_guidance_slot = len(stable_parts)
    stable_parts.append(HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS)
    stable_parts.extend(_guidance_parts(agent))
    skills_prompt = _skills_prompt(agent)
    # Skill-pointer variant requires BOTH skill_view AND the hermes-agent skill
    # in the rendered index (pure string check — inherits the index's stability).
    if "skill_view" in (agent.valid_tool_names or set()) and "- hermes-agent:" in skills_prompt:
        stable_parts[_help_guidance_slot] = HERMES_AGENT_HELP_GUIDANCE
    stable_parts.extend(_alibaba_identity_part(agent))
    stable_parts.extend(_auto_load_parts(agent))          # 钉住的技能 = 每会话常量
    environment_hints = _pb.build_environment_hints()
    coding_prefix_parts, coding_workspace_parts, coding_trailing_parts = _coding_parts(agent)
    stable_parts.extend(coding_prefix_parts)
    post_workspace_parts = _post_workspace_parts(agent)

    # ── Context tier ──────────────────────────────────────────────
    context_parts: List[str] = []
    if system_message is not None:
        context_parts.append(system_message)
    context_parts.extend(_context_files_part(agent, _ctx_len, _soul_loaded))
    if coding_workspace_parts:
        context_parts.extend([*coding_workspace_parts, *coding_trailing_parts, *post_workspace_parts])
    else:
        stable_parts.extend([*coding_trailing_parts, *post_workspace_parts])

    # ── Volatile tier ─────────────────────────────────────────────
    volatile_parts: List[str] = [skills_prompt, *_memory_parts(agent)]
    volatile_parts.extend(_plugin_section_blocks(_frozen_plugin_prompt_sections(agent), "after_memory"))
    volatile_parts.append(_timestamp_line(agent))
    if environment_hints:
        environment_hints = environment_hints.replace(
            _pb.RUNTIME_ENVIRONMENT_HEADING, "> " + _pb.RUNTIME_ENVIRONMENT_HEADING)
        volatile_parts.append(f"{_pb.RUNTIME_ENVIRONMENT_HEADING}\n\n{environment_hints}\n\n{_pb.RUNTIME_ENVIRONMENT_END}")

    return {"stable": _join_tier(stable_parts),
            "context": _join_tier(context_parts),
            "volatile": _join_tier(volatile_parts)}


# agent/system_prompt.py:722
def build_system_prompt(agent, system_message=None) -> str:
    """Assemble the full prompt; cached on ``agent._cached_system_prompt`` and
    only rebuilt after compression. Tiers are ordered stable -> context ->
    volatile so implicit longest-prefix caches keep the unchanged scaffold."""
    parts = build_system_prompt_parts(agent, system_message=system_message)
    agent._cached_system_prompt_static = parts["stable"]
    return "\n\n".join(p for p in (parts["stable"], parts["context"], parts["volatile"]) if p)
```

### 三个值得单独拿出来看的细节

**① 返回值是"分层的 dict"，不是一个大字符串**

`build_system_prompt_parts()` 返回 `{"stable": ..., "context": ..., "volatile": ...}`，
另有一个 `build_system_prompt()` 负责把它们拼成一个字符串。

**为什么要分开？** 看那句：`agent._cached_system_prompt_static = parts["stable"]`——
**stable 那一层被单独存了一份**。后面做"存储的提示词还是不是当前这份"的校验时，
只需要比这一层（它本来就该不变）。

**② `_join_tier` 会丢掉空项**

```python
def _join_tier(parts):
    """Join non-empty parts; None/blank entries are dropped."""
    return "\n\n".join(p.strip() for p in parts if p and p.strip())
```

`_identity_parts` 返回的 `[_soul_content]` 可能是 `None`（没有 SOUL.md）。
**所以"空"是靠 join 时丢弃表达的，不是靠调用方判断。** 这比到处写 `if content:` 干净。

**③ 一个"先占位、后替换"的技巧**

```python
_help_guidance_slot = len(stable_parts)
stable_parts.append(HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS)
...
skills_prompt = _skills_prompt(agent)
if "skill_view" in (agent.valid_tool_names or set()) and "- hermes-agent:" in skills_prompt:
    stable_parts[_help_guidance_slot] = HERMES_AGENT_HELP_GUIDANCE
```

这段在解决一个**顺序依赖**：给 Agent 看的"如何用 Hermes 文档"那段话有两个版本，
用哪个取决于**技能索引里有没有 `hermes-agent` 这一项**——
而技能索引是后面才构建的。

但技能索引属于 **volatile** 层，这段文案属于 **stable** 层。
所以不能"先建索引再决定"，只能**先放占位符，等索引建好再回填那个下标**。

> 📌 注释里那句 *pure string check — inherits the index's stability*
> （纯字符串检查，所以继承索引的稳定性）是在说：这个判断只看字符串，
> **不引入新的不确定性**——同样的索引永远得出同样的结论，所以 stable 层还是稳定的。

### 顺序为什么重要（worktree 共享前缀）

原文：

> 在 context 层内部，**共享的项目文件排在任何"指明当前 worktree"的内容之前**。
> 同一个项目、在不同 git worktree 里跑的会话，于是能共享**覆盖整个 context 块**的提示词前缀，
> 而不是在"第一处依赖当前目录的行"就断掉——那段共享前缀正是**最长前缀缓存**能复用的部分。
> 没有工作区快照的会话，其尾部指导留在 stable 层；运行时环境块**永远结束 volatile 层**。

翻译成人话：**这是一条排序规则，而排序在这里直接等于钱。**

### 先拆两个词

| | 装什么 | 跨 worktree 一样吗 |
|---|---|---|
| **共享文件** | 项目上下文文件（`AGENTS.md` / `.hermes.md` / `CLAUDE.md` / `.cursorrules`） | **一样**（它是提交在仓库里的） |
| **worktree 快照** | git 工作区状态 | **不一样**（每个 worktree 各在一个分支上） |

> 【源码】worktree 快照的实际内容（`agent/coding_context.py`）：
> `_git(cwd, "status", "--porcelain=2", "--branch")` → `_parse_status()` 解析出
> **分支名（`branch.head`）、上游分支、ahead/behind**，以及
> **staged / modified / untracked / conflicts 四个计数**。
> 另外它支持 `workspace_block` 参数*replays the caller's pinned session-start snapshot
> instead of probing git again*（重放会话开始时钉住的快照，不再探一次 git）——
> **因为多探一次 git 就可能得到一个不同的值，那就破坏稳定性了。**
>
> git worktree 的作用：**同时**把同一个仓库的多个分支检出到不同目录：
> `~/atlas`（main）、`~/atlas-feat-x`（feature-x）、`~/atlas-bugfix`（bugfix）。

### 核心原理：缓存是「最长前缀」匹配

不是"内容相似就命中"，而是**从第一个字节开始逐字节比**。
比到第一个不同的字节——**从那里往后全部作废**。

所以：

> **谁排前面，谁就免费搭上前缀；谁排后面，谁一变就废掉它后面所有东西。**

### 两种顺序的对比

**❌ 假设顺序是：worktree 快照 → AGENTS.md**

```text
         会话A(main)        会话B(feat-x)      会话C(bugfix)
         ────────────       ────────────       ────────────
         [stable 层 …]      [stable 层 …]      [stable 层 …]     ← ✅ 共享
         分支: main         分支: feature-x     分支: bugfix       ← ✗ 从这行起分叉
         AGENTS.md 全文     AGENTS.md 全文     AGENTS.md 全文     ← 内容明明一样，
                                                                    但已在分叉点之后
                                                                  → ❌ 不共享
```

共享前缀只有 stable 层——**AGENTS.md 白白浪费了。**

**✅ 实际顺序：AGENTS.md → worktree 快照**

```text
         会话A(main)        会话B(feat-x)      会话C(bugfix)
         ────────────       ────────────       ────────────
         [stable 层 …]      [stable 层 …]      [stable 层 …]     ← ✅ 共享
         AGENTS.md 全文     AGENTS.md 全文     AGENTS.md 全文     ← ✅ 仍然一样 → 也共享！
         分支: main         分支: feature-x     分支: bugfix       ← 到这里才分叉
```

共享前缀 = **整个 context 块**（把 `AGENTS.md` 包进去了）。

### 算一下差多少

假设 `AGENTS.md` 5000 token、worktree 快照 50 token：

| 顺序 | 全价计算的量 |
|---|---|
| 快照在前 | **5000+ token**（AGENTS.md 全在后缀里） |
| 文件在前 | **约 50 token**（只剩快照那点） |

**只是把两块内容对调了位置，差 100 倍。**

### 抽象成一句原则

> **按「变化频率」从低到高排。** 越不容易变的排越前，越容易变的排越后。

**这条原则在这一章出现三次，全是同一个道理**：

| 位置 | 排在前面（不易变） | 排在后面（易变） |
|---|---|---|
| **context 层内** | `AGENTS.md`（跨 worktree 不变） | git 工作区快照 |
| **层之间** | stable 层（整个会话不变） | context 层 → volatile 层 |
| **volatile 层内** | 技能索引 / 记忆 | …时间戳 → **运行时环境块（含 cwd）** |

第三条最好懂：**cwd 随时会变，所以它是整个提示词的最后一个块。**
mini 版现在其实也是这么排的（`BASE_IDENTITY` → cwd 在最后）——**碰巧排对了**。

### 更本质的一句：什么叫「字节级稳定」

**不是语义稳定，是逐字节稳定。**

```text
✅ 同一份内容、同样的空格、同样的换行  → 缓存复用
❌ 多了一个空行、少了一个空格          → 缓存从那一字节断掉
```

这就是为什么 Hermes 的 `AGENTS.md` 把这条写成硬约束：

> *system prompt **byte-stable** for the life of a conversation*
> （系统提示词在一次对话的生命周期里保持**字节级**稳定）

也是为什么 `_join_tier()` 会在拼接时丢掉空项——**空字符串进去也会产生多余的 `\n\n`，
那也是字节差异。**

### 存储提示词的校验

原文：

> 对**已存储的提示词**的后果：`_stored_prompt_matches_runtime()`
> （`agent/conversation_loop.py`）读取渲染后的 `# Hermes runtime environment` 边界之后的
> 第一段主机信息，并在**绝对末尾**用一个闭合标记来把这个布局和"引用了该标题的旧式散文"区分开。
> 运行时边界跟在所有项目、运维、记忆和插件文本之后，所以那些块里的示例**不会冒充**成运行时 cwd。
> 模型/provider 在运行时边界之前读，排除掉嵌述描述。`Platform:` **故意不算身份字段**：
> 一次界面切换（桌面 ↔ TUI）保留存储的字节，把当前界面的指导作为**一次性说明**
> 送到**每轮的 user 消息通道**（`agent/surface_switch.py`），于是缓存前缀活下来了（#104414）。
> 旧式提示词保留它们原来的"主机在 context 之前"锚点，所以重排之前持久化的提示词仍然能通过校验。

**这段在解决一个问题**：会话恢复时，磁盘上存着一份系统提示词。
现在要判断：**"存的那份"还是不是"现在这份"？**

判断方法是拿"运行时环境块"当指纹。但这里有两个坑：

| 坑 | 解法 |
|---|---|
| 用户在 AGENTS.md 里**引用了** `# Hermes runtime environment` 这个标题，会被误认成真边界 | 要求边界出现在"第一段主机信息"之前，且**用绝对末尾的闭合标记**双重确认 |
| 界面从桌面切到 TUI，如果算作身份变化，就要重建提示词 → 缓存全废 | **`Platform:` 故意不算身份字段**；界面指导改走"每轮 user 消息的一次性说明" |

> 🎓 第二条是这一章最值得记的一条设计原则：
> **"什么算身份"这个问题，答案是按"改了之后值不值得让缓存失效"来定的，不是按"语义上属不属于"。**
> 界面上写着你是终端还是桌面，语义上明显是身份的一部分——
> **但因为它会变，就被踢出了身份字段。**

### 什么时候不加载 SOUL.md

原文：

> 当 `skip_context_files` 被设置时（例如子 agent 委派），SOUL.md 不加载，
> 改用硬编码的 `DEFAULT_AGENT_IDENTITY`。

> 【源码】
> ```python
> def _identity_parts(agent, ctx_len):
>     wants_soul = agent.load_soul_identity or not agent.skip_context_files
>     _soul_content = _pb.load_soul_md(ctx_len, home_override=_agent_home(agent)) if wants_soul else None
>     return ([_soul_content], True) if _soul_content else ([DEFAULT_AGENT_IDENTITY], False)
> ```
> 注意第二个返回值 `soul_loaded` 会往下传，用来**防止 SOUL.md 出现两次**（后面 §6 讲）。

### 为什么这么设计 / 砍掉会怎样

| 决策 | 砍掉的后果 |
|---|---|
| 分三层 | 所有内容混在一起，看不出"哪些会变"，重建时只能整份重来 |
| stable 排最前 | 最该稳定的内容夹在中间，缓存前缀活不长 |
| context 层内"共享文件在前、worktree 在后" | 每个 worktree 各自废掉一整层缓存 |
| `Platform:` 不算身份 | 桌面切 TUI 就全量重建提示词 |
| `_join_tier` 丢空项 | 到处都是 `if x:` 判断，且容易漏出多余空行（空行也是字节差异！） |

---

## 3. ⚠️ 文档的「组装示例」和源码不一致的三处

原文在 §2 之后给了一个 **Concrete example: assembled system prompt**，
用"Layer 1 ~ Layer 10"的编号列出最终提示词长什么样，并声明
*comments show the source of each section*（注释标明每段的来源）。

**它自己标了 "a simplified view"（简化视图）——但简化不该改变顺序，而它改变了顺序，而且和它自己的三层描述矛盾。**

### 原文给的 Layer 1~10

```text
Layer 1:  Agent Identity (from ~/.hermes/SOUL.md)
Layer 2:  Tool-aware behavior guidance
Layer 3:  Honcho static block (when active)
Layer 4:  Optional system message (from config or API)
Layer 5:  Frozen MEMORY snapshot
Layer 6:  Frozen USER profile snapshot
Layer 7:  Skills index
Layer 8:  Context files (from project directory)
Layer 9:  Timestamp + session
Layer 10: Platform hint
```

### 三处不一致（实测）

| # | 原文示例的顺序 | 源码里的实际归属 | 性质 |
|---|---|---|---|
| 1 | Layer 5/6 记忆、画像 **→ 然后** Layer 7 技能索引 | volatile 层是 `[skills_prompt, *_memory_parts()]`——**技能索引在前** | ⚠️ **和原文自己 §2 的三层描述矛盾**（那里写的是 "skills index, built-in memory snapshot…"） |
| 2 | Layer 8 项目上下文文件，排在记忆/技能**之后** | context 文件属于 **context 层**，即排在**整个 volatile 层之前** | 示例顺序与源码相反 |
| 3 | Layer 10 平台提示放在**最后** | 平台提示在 `_post_workspace_parts()` 里，进 **context 层**（有 workspace 快照时）或 **stable 层**（无快照时）；**最后的是"运行时环境块"**，示例里**根本没有** | 示例顺序错 + 漏了一整块 |

### 真实的顺序（逐块详解）

**先看骨架，再看每一块装什么、为什么在这一层。**

```text
┌─ stable 层（整个会话不变）───────────────────────────────────┐
│ ① 身份          SOUL.md 或 DEFAULT_AGENT_IDENTITY            │
│ ② 帮助指引      HERMES_AGENT_HELP_GUIDANCE[_NO_SKILLS]       │
│ ③ 指导块        任务完成 / 并行调用 / 工具感知 / steer /       │
│                 工具使用强制* / Google 专属* / 执行指导*       │
│ ④ 模型身份*     仅 alibaba provider                          │
│ ⑤ 钉住的技能    skills.auto_load                             │
│ ⑥ 编码前缀      coding posture 的 prefix 部分                 │
│ ⑦ 【仅无 workspace 时】编码尾部 + post_workspace              │
└──────────────────────────────────────────────────────────────┘
┌─ context 层（跨会话可能变）──────────────────────────────────┐
│ ⑧ 调用方 system_message（ephemeral 的那份绝不在这里）          │
│ ⑨ 项目上下文文件  AGENTS.md / .hermes.md / …（4 级优先级）    │
│ ⑩ 【仅当有 workspace 快照时】                                │
│     git 工作区快照 → 编码尾部 → post_workspace：              │
│       环境探测行 → bot-mode 协议 → profile 行 → 平台提示      │
└──────────────────────────────────────────────────────────────┘
┌─ volatile 层（重建时最可能变，刻意放最后）───────────────────┐
│ ⑪ 技能索引      ← 打头，不是打尾                             │
│ ⑫ MEMORY.md 快照 → USER.md 快照 → 外部记忆 provider 块        │
│ ⑬ 插件段（钉在一个粗粒度锚点上）                             │
│ ⑭ 时间戳行 + Session ID / Model / Provider / Platform        │
│ ⑮ 运行时环境块  ← 永远在最后（含 cwd）                       │
└──────────────────────────────────────────────────────────────┘
```

**标 `*` 的是条件性出现的块**（按模型 / 按 provider / 按配置开关）。

---

#### stable 层 · ① 身份

| | |
|---|---|
| 源码 | `_identity_parts()` — 见 §5 |
| 内容 | `SOUL.md` 全文，或回退的 `DEFAULT_AGENT_IDENTITY` |
| 为什么在最前 | 它是"这个 agent 是谁"——**整个会话最不该变的东西** |

#### stable 层 · ② 帮助指引（两版之一）

```python
HERMES_AGENT_HELP_GUIDANCE            # 有 skill_view + 索引里有 hermes-agent 时用这版
HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS  # 否则用这版
```

两版的差别在**怎么指路**：有技能时是 *load it with `skill_view(name='hermes-agent')`*；
没有技能时是 *Point the user there (or read it yourself if you have a way to fetch web content)*
（指给用户看，或者你自己想办法去抓）。

**这就是 §2 那段"先占位、后回填"的主角**——因为用哪版取决于**还没建好的技能索引**。

#### stable 层 · ③ 指导块（一个 `*` 集中区）

```python
def _guidance_parts(agent):
    """Universal + tool-aware + model-gated guidance blocks, each gated by its config.yaml key."""
    parts = []
    if agent.valid_tool_names:
        parts += [text for flag, text in (
            ("_task_completion_guidance",   TASK_COMPLETION_GUIDANCE),
            ("_parallel_tool_call_guidance", PARALLEL_TOOL_CALL_GUIDANCE),
        ) if getattr(agent, flag, True)]
    parts.append(_tool_guidance_block(agent))
    if not agent.valid_tool_names:
        return parts
    # Steering only lands inside tool results, so only reachable with tools.
    parts.append(STEER_CHANNEL_NOTE)
    if _model_gate(agent._tool_use_enforcement, agent.model, TOOL_USE_ENFORCEMENT_MODELS):
        parts.append(TOOL_USE_ENFORCEMENT_GUIDANCE)
        if any(g in (agent.model or "").lower() for g in ("gemini", "gemma")):
            parts.append(GOOGLE_MODEL_OPERATIONAL_GUIDANCE)
    if _model_gate(getattr(agent, "_execution_guidance", "auto"), agent.model, EXECUTION_GUIDANCE_MODELS):
        parts.append(execution_guidance_text())
    return parts
```

**三个值得注意的点：**

**a. 模型门控的 `auto` 语义**（源码注释原文）：

> *agent.tool_use_enforcement / agent.execution_guidance: "auto" (default)*
> *matches the hardcoded model lists; true/false force; a list gives custom model-name substrings.*
> *Execution guidance is an independent gate so DeepSeek/Kimi/Qwen-class models get it
> even with enforcement off.*

也就是说门控有三种写法：`"auto"`（用硬编码列表）/ `true|false`（强制开或关）/ **一个字符串列表**（自己指定匹配哪些模型名子串）。
**而且"执行指导"是独立开关**——即使工具使用强制关掉了，DeepSeek/Kimi/Qwen 这类模型仍然能拿到执行指导。

> 📌 这解释了**我们自己的系统提示词里为什么有一段 "Tool-use enforcement (for GPT/Codex models only)"**。
> 实测 `TOOL_USE_ENFORCEMENT_MODELS = ("gpt", "codex", "gemini", "gemma", "grok", "glm", "qwen", "deepseek", "muse")`
> ——**名单比那段标题说的长得多**（标题只说 GPT/Codex）。
> ⚠️ **这是文档示例的第四处不精确**：Layer 2 下面那段标题写的是
> *Tool-use enforcement (for GPT/Codex models only)*，实际适用范围包含 deepseek 等。
> （不过这段标题文字本身可能来自几个月前的版本，**模型名单是活数据，标题是硬编码文字**——
> 这类"文字跟不上数据"的地方，以列表常量为准。）

**b. `STEER_CHANNEL_NOTE` 的准入条件**是"有工具"**，理由是注释里那句**：

> *Steering only lands inside tool results, so only reachable with tools.*
> （引导只会落在工具结果里，所以只有带工具时才可达。）

**这是 §7 那个"子目录提示拼在工具结果上"的同一个机制**——
`steer`（用户中途插话引导）也是挂在工具结果上的。
**所以没有工具时，这个通道根本不存在，那段说明也就不该发。**

**c. 每个块都有自己的 config 开关**，靠 `getattr(agent, flag, True)` 的默认值 `True` 表达"默认开"。

#### stable 层 · ④ 模型身份块（只有 alibaba 有）

```python
def _alibaba_identity_part(agent):
    """Alibaba Coding Plan always reports "glm-4.7" as the model name; inject
    the real identity so the agent can answer correctly."""
    if agent.provider != "alibaba":
        return []
    _model_short = agent.model.rsplit("/", 1)[-1]
    return [
        f"You are powered by the model named {_model_short}. "
        f"The exact model ID is {agent.model}. "
        f"When asked what model you are, always answer based on this information, "
        f"not on any model name returned by the API."
    ]
```

**这个块是个"供应商 bug 的补丁"，而且补在提示词层。**

背景：阿里云 Coding Plan **总是把模型名报成 `glm-4.7`**，不管实际是什么模型。
于是在提示词里**显式告诉模型"你叫什么"**，并明确禁止它相信 API 返回的名字。

> 🎓 **这节课很值**：模型的身份信息可能来自两处——**API 返回**和**提示词**。
> 当 API 不可信时，**提示词的优先级被显式抬到 API 之上**。
>
> 注意它的措辞是 *always answer based on this information, **not on any model name
> returned by the API***——**光说"你是什么模型"不够，还必须显式否定另一个来源。**
> 否则模型可能两边都看到，然后随机挑一个。

#### stable 层 · ⑤ 钉住的技能

```python
def _auto_load_parts(agent):
    """``skills.auto_load`` blocks, resolved once per agent lifecycle (config, skill files and
    HERMES_IGNORE_RULES are read on the first build only) so the prompt stays byte-stable
    across model switches, compression and static-prefix restoration."""
```

**"读一次，之后不读"** —— `_auto_load_skills_resolved` 是个缓存标志。
理由是那句 *read on the first build only*：如果每次重建都重新读配置文件，
**用户中途改一下 `skills.auto_load`，提示词就变了** = 缓存作废。

**门控条件（源码注释原文）**：

> 与 `_skills_prompt` 同一个门控：没有技能工具就什么都不给，
> 而且 **`skip_context_files` 的 agent 也不给**（委派出去的子 agent、curator/review fork、
> 网关的卫生检查 agent）——**钉住的技能是"给用户这个会话的运维指导"，不是给每个内部 fork 的载荷。**

> 📌 这句话和第 2 章讲的"**子 agent 不加载 SOUL.md**"是同一套判断：
> **内部 fork 不该继承用户的个人化配置**——那些东西是给"用户面前的那个 agent"的。

#### stable 层 · ⑥⑦ 编码作业简报

`_coding_parts()` 返回**三段**：`(prefix, workspace, trailing)`。

- **`prefix`** → 进 stable（作业简报本身，和 cwd 无关）
- **`workspace` + `trailing`** → 进 context（依赖当前 worktree）

**而"没有 workspace 时全留在 stable"的分支**（源码注释原文）：

> *Preserve the stable placement for non-workspace sessions; there is no
> worktree snapshot whose later position would improve their prefix.*
> （没有 worktree 快照，就没有"往后放能改善前缀"这回事。）

**同一段的 docstring 还解释了那个 pin 机制**：

> *The workspace block is a live git probe after project context, ahead of the whole
> volatile band; re-probing at the compaction rebuild re-emits different bytes for any
> repo that moved and defeats the keep-prompt fast path. So the bytes are pinned per
> session on the agent, keyed by the resolved cwd (a gateway serves many cwds), and
> replayed on rebuilds; `reset_session_state` drops the pin at a session boundary.*

```python
pinned = getattr(agent, "_frozen_workspace_snapshot", None)
# "" is a real pinned value (no workspace here) — only a cwd mismatch re-probes.
replay = pinned[1] if pinned is not None and pinned[0] == cwd_key else None
```

**三个细节：**

| 细节 | 为什么 |
|---|---|
| 按 **cwd 作 key** 钉住 | *a gateway serves many cwds*——一个网关进程同时服务多个会话，不能只存一份 |
| `""` 算**真实值** | "这里没有 workspace"也是一个结论，不该被当成"没钉住"而重探 |
| `reset_session_state` 清 pin | **只有会话边界才该重新探** git |

#### stable 层 · ⑧~⑩ 都在 context 层，见上面骨架

---

#### context 层 · ⑨ 项目上下文文件

见 §6（4 级优先级 / 安全扫描 / 截断 / frontmatter 剥离），不重复。

**它为什么在 context 层**：换目录、换仓库就变，**但整个会话内不变**——
比 stable 易变，比记忆/时间戳稳得多。

#### context 层 · ⑩ workspace 快照与它后面的块

这里是个**"打包在一起"的结构**：

```python
if coding_workspace_parts:
    context_parts.extend([*coding_workspace_parts, *coding_trailing_parts, *post_workspace_parts])
```

`_post_workspace_parts()` 里有**四样东西**（源码 docstring 原文）：

| 顺序 | 块 | 说明 |
|---|---|---|
| 1 | **环境探测行** | `config.yaml` 的 `agent.environment_probe`；**干净时一行都不给**；远端后端跳过。探测失败必须不影响提示词构建（`except: pass`） |
| 2 | **bot-mode 协议** | 只在 bot 模式下 |
| 3 | **profile 行** | `_active_profile_line()` |
| 4 | **平台提示** | `platform_hint()` — 见 §4 |

**③ profile 行为什么存在**（源码 docstring 原文）：

> *Name the running profile so the agent doesn't conflate `~/.hermes/skills` (default)
> with `~/.hermes/profiles/<active>/skills`.*
> （指名当前 profile，免得 agent 把默认 profile 的技能目录和当前 profile 的搞混。）

**它实际上是给模型的一段"别动别人的东西"的护栏**（实测文本）：

> *Active Hermes profile: default. Other profiles (if any) live under …/profiles/<name>/.
> Each profile has its own skills/, plugins/, cron/, and memories/ that affect a different
> session than this one. **Do not modify another profile's skills/plugins/cron/memories
> unless the user explicitly directs you to.***

> 💡 **这段文字你在自己的系统提示词里就能看到**——多 profile 环境下它出现的意义是：
> **agent 有文件系统和 shell 权限，不告诉它边界，它就可能去"修好"另一个 profile 的配置。**
> 这不是权限系统能拦的（同一个用户、同一个文件系统），**只能靠告知**。

---

#### volatile 层 · ⑪⑫⑬ 技能索引 / 记忆 / 插件段

- **⑪ 技能索引**：见 §10。**打头的理由见 §3 末尾**（变化点放本层最前面 = 爆炸半径最小）
- **⑫ 记忆**：`_memory_parts()` = `MEMORY.md` 块 → `USER.md` 块 → 外部 provider 块。
  见 §9。外部 provider 块的门控**和工具注入用同一个检查**，
  理由：*so we never advertise provider tools that the agent's toolset configuration has
  already gated off (#81014)*（**绝不宣传一个已经被工具集配置关掉的工具**）
- **⑬ 插件段**：注释说明它被 *confined to one coarse anchor in the volatile tail*
  （限制在 volatile 尾部的**一个粗粒度锚点**上），
  这样 *a resumed process can reconstruct the stable prefix without re-running plugins*
  （**被恢复的进程不必重跑插件就能重建稳定前缀**）

#### volatile 层 · ⑭ 时间戳行——这一块最讲究

```python
def _timestamp_line(agent):
    """Date-only so the prompt is byte-stable for the day; zone + offset so
    tools needn't guess EST vs EDT. Long-lived sessions get an "as of" line on
    rebuild days (the cache prefix is already invalidated at that boundary)."""
```

**只有日期，没有时刻**——理由是 *byte-stable for the day*：
**当天之内，这一行的字节完全不变。** 如果写上时分秒，每一轮都变，缓存每轮都断。

**分两行的设计**（源码注释很长，很值得读）：

```python
timestamp_line = f"Conversation started: {_start.strftime('%A, %B %d, %Y')}{_zone_suffix}"
if now.strftime("%Y%m%d") != _start.strftime("%Y%m%d"):
    timestamp_line += (f"\nToday's date (as of the last context rebuild): {now...} "
                       "— trust this over the start date for what day it is now; query tools for exact time.")
```

| 行 | 何时出现 | 稳定性 |
|---|---|---|
| `Conversation started: …` | 总是 | **永不变**（会话开始日） |
| `Today's date (as of the last context rebuild): …` | **只在跨天时** | 只在重建时刷 |

**源码注释里给的理由（这段话本身就是一份设计文档）**：

> 长期会话（Bot Mode 的永久聊天、用户从不关掉的即时通讯频道）会跨越很多天和很多次压缩。
> **孤零零一个"出生日期"会让模型以为自己还活在老日子里。**
> 提示词在**每次压缩边界**都会重建，所以顺便把重建当天也盖个章：
> `started` 保持锚定且字节稳定，`as of` 恰好**在缓存前缀本来就要失效的时候**刷新，
> 所以这一行**不产生额外的缓存抖动**。同一会话只在同一天时**整个第二行都不出现**——
> 没什么可纠正的，而且**单行形状对当天保持字节完全一致（前缀缓存安全）**。

> 🎓 **这是全章最漂亮的一处设计，值得单独记：它同时满足了三个互相拉扯的要求。**
>
> | 要求 | 不要第二行会怎样 | 无脑加第二行会怎样 |
> |---|---|---|
> | 字节稳定 | ✓ | ✗ 每轮都变 |
> | 长期会话知道"今天几号" | ✗ 模型以为还在会话开始那天 | ✓ |
> | 不增加缓存抖动 | —— | ✗ |
>
> **解法：把"会变的东西"绑在"本来就要失效的那个时刻"上。**
> 压缩重建 = 缓存前缀一定要失效的事件 → **那就趁那个时机刷新**，
> 于是这次刷新是**免费的**。

##### 为什么"已经失效"就是"免费"（把成本算出来）

这句 **"任何必须更新的时间戳/版本号，都应该绑到一个已经发生失效的事件上，
而不是引入新的失效点"** 值得展开——它是一条**成本判断**，不是审美偏好。

**第一步：代价模型。** 缓存失效的代价 = 把**当时的整个上下文**从头重新 prefill 一遍。
而这个代价**随上下文增长，越到会话后期越贵**。实测（每轮一次约 600 字符的工具结果）：

```text
system 固定部分：876 字符
每轮请求大小：985 → 1791 → 2597 → ... → 10663
实测每轮增长：806 字符/轮
```

也就是说：**第 1 天失效和第 7 天失效，同样是"一次失效"，价格差 7 倍。**

**第二步：区分"谁引起的失效"。**

> 压缩发生时，历史被重写、系统提示词本来就必须重建 → **缓存前缀本来就要全废**。
> 在这个时刻顺便把时间戳刷了 → 这次刷新的**边际成本 = 0**。

源码原话：

> `as of` refreshes exactly when the cache prefix is **already being invalidated** (compaction),
> so the added line **costs no extra cache churn**.
>
> （`as of` 恰好在前缀**本来就要失效**时刷新，所以这一行**不产生额外的缓存抖动**。）

注意措辞：不是"失效很少"，不是"失效代价小"，而是 **"already"——失效已经发生了，我只是搭便车**。

**第三步：算一遍。** 场景 = Bot Mode 那种"永不关掉的频道"，7 天不关、每天 50 轮
（每轮增长用上面实测的 806 字符）：

| 时刻 | 上下文大小 | 设计②（每天刷新） | 设计③（绑压缩） |
|---|---|---|---|
| 第 1 天末 | 41,201 字符 | **失效** | 无压缩 → 不刷新 |
| 第 2 天末 | 81,526 | **失效** | 压缩发生 → 刷新 |
| 第 3 天末 | 121,851 | **失效** | 无压缩 → 不刷新 |
| 第 4 天末 | 162,176 | **失效** | 压缩发生 → 刷新 |
| 第 5 天末 | 202,501 | **失效** | 无压缩 → 不刷新 |
| 第 6 天末 | 242,826 | **失效** | 压缩发生 → 刷新 |
| 第 7 天末 | 283,151 | **失效** | 无压缩 → 不刷新 |

- **设计②**：额外制造 **7 次**全量重算，累计要重算 **1,135,232 字符**
- **设计③**：额外制造 **0 次**——那 3 次失效是**压缩本来就有的**

📌 **要算的不是"更新了几次"，而是"有没有额外制造失效"**：

| 设计 | 时间戳更新次数 | **额外制造的失效点** |
|---|---|---|
| ① 不更新（只写会话开始那天） | 0 | 0，但模型以为还活在老日子 |
| ② 按需求粒度更新（每天刷） | 7 | **7** ← 每次都要付整个上下文 |
| ③ **绑到已失效的时刻** | 3 | **0** ✓ |

设计③ 更新的次数并不比②少多少（3 vs 7），但**额外代价是 0**。这就是它的全部价值。

**第四步：反向的一半——不需要更新时，一个字都别动。**

同一段注释里还有一句：

> *Same-day sessions skip the second line entirely — nothing to correct, and the single-line
> shape stays byte-identical for the day (**prefix-cache safe**).*
>
> （同一天的会话完全跳过第二行——**没什么可纠正的**，而且单行形态对当天保持字节一致。）

"nothing to correct" = **没有需要纠正的东西，就别动**。
绝大多数会话（当天开、当天关）因此**完全不承受这个功能的代价**。

所以这条原则是**两半**：要更新时绑到已失效的时刻；**不需要更新时一个字都别动**。

##### 抽象成一条判据（可以搬到你自己的项目里）

```text
有个东西"必须定期刷新"（时间戳 / 版本号 / 能力列表 / 配置快照）
    ↓
第一步：先列出这个系统里"所有会失效的时刻"
        （本系统：压缩、新会话、会话边界、存盘校验失败）
    ↓
第二步：把刷新绑到其中一个时刻上
        选择标准 = 这个时刻"本来就要失效" → 我的刷新就免费
    ↓
第三步（反向检查）：这个刷新会不会自己创造新时刻？
        会 → 方案错了，回去改绑
```

一句话判据：

> **"如果这个刷新不发生，缓存会失效吗？"**
> - **不会** → 你**创造了**一个新失效点 ✗
> - **会** → 你**搭了便车** ✓

##### 这套代码里的同类应用，以及反面写法

| 东西 | 绑在哪个"已有事件"上 |
|---|---|
| 时间戳第二行 | 压缩边界 |
| **记忆的新鲜度** | 压缩边界（`invalidate` 里顺带"从磁盘重新加载记忆"） |
| **插件段重渲染** | 同一个压缩边界（清掉冻结的插件快照） |
| **能力 epoch（版本号）** | **写一个 epoch 号，恢复时比一次**——变了才重建。源码原文：<br>`Capability epoch lets the restore path rebuild **ONCE per user-initiated capability change** in an eternal session.`（`agent/system_prompt.py:358`、`agent/conversation_loop.py:610`） |
| **工作区 git 快照的 pin** | 会话开始那一刻（不重探） |

反过来，"引入新的失效点"长什么样：

| 反面写法 | 为什么它是新失效点 |
|---|---|
| 每轮把"当前时间（含时分秒）"写进提示词 | 每轮字节都不同 |
| 每轮重新探测 git 状态 | 每轮都可能不同（**而且文件没变也可能拿到不同值**） |
| 每次请求生成新 `session_id` 写进 volatile 层 | ★ 实测过，见下 |
| 每轮问插件"你的段变了吗" | 每轮都在问、每轮都可能变 |

★ 那条 `session_id` 是"引入新失效点"的**最小可复现形态**（实测 diff）：

```text
字节数：875 vs 874   逐字节相同？False
差异行：
    -Session ID: 171e8abc8e5c
    +Session ID: 20260921_ab
```

**只有一个字段变了，但缓存前缀从那一行往后全部作废。**

> **一句话**：**"已经失效"是最便宜的更新时机，因为它是免费的；
> 自己创造失效时刻，代价是当时整个上下文重新算一遍——而且越到会话后期越贵。**

还有两个小分支：

- **Bot Mode 无时间感时**（`_bot_chat_timeless_prompt`）：整个时间戳行换成**只有时区**。
- **trailer**：`Session ID` / `Model` / `Provider` / `Platform` 四行，各自只在有值时输出
  （`agent.pass_session_id` 为假时连 Session ID 都不给）。

#### volatile 层 · ⑮ 运行时环境块（永远最后）

```python
RUNTIME_ENVIRONMENT_HEADING = "# Hermes runtime environment"
RUNTIME_ENVIRONMENT_END = "<!-- End Hermes runtime environment -->"

def build_environment_hints() -> str:
    """Execution-environment block: local backends get host OS$HOME; remote/sandbox
    backends get ONLY the backend's own state (the agent's tools cannot touch the host).
    WSL and embedder hints are appended."""
```

**这里有一条我之前没想到的原则：远端后端不给主机信息。**

| 后端 | 给什么 | 为什么 |
|---|---|---|
| `local` | **主机的** OS / home / **cwd** | agent 的工具直接作用在这台机器上 |
| `docker` / `ssh` / `modal` / `daytona` / `singularity` / `vercel_sandbox` / `managed_modal` | **只有后端自己的状态** | *the agent's tools cannot touch the host*——**告诉它主机的 cwd 是有害的误导** |

> 📌 **理由值得想一遍**：如果 agent 跑在 Docker 里，而提示词写着
> `Working directory: $HOME/project`（宿主机的路径），
> 那么 agent 会以为 `read_file $HOME/project/x.py` 是可达的——**在实际是容器里的情况下**。
> **"正确的信息 + 错误的执行环境"比"没有信息"更糟。**
>
> 这是"提示词必须描述**真实**的执行环境，不能描述你以为的环境"的一个实例。

**还有一处细节**（`build_system_prompt_parts` 里）：

```python
if environment_hints:
    environment_hints = environment_hints.replace(
        _pb.RUNTIME_ENVIRONMENT_HEADING, "> " + _pb.RUNTIME_ENVIRONMENT_HEADING)
    volatile_parts.append(f"{_pb.RUNTIME_ENVIRONMENT_HEADING}\n\n{environment_hints}\n\n{_pb.RUNTIME_ENVIRONMENT_END}")
```

**把 `environment_hints` 内部出现的那句标题加上引用前缀 `"> "`**，
这样它就不会和"真正的边界标题"混淆——因为**存盘提示词的校验要靠这个标题当锚点**（见 §2）。

> 🎓 **这是"防御性转义"的一个教科书例子**：
> 一个块的内容里**恰好包含**了外层协议用的分隔符，
> 如果不转义，**外层的解析就会错**。加个 `"> "` 是最小代价的消歧。

---

### 一句话总结每一层的"为什么"

| 层 | 装的东西的共同点 | 什么时候变 |
|---|---|---|
| **stable** | 与任务、目录、时间**全都无关** | 会话开始 |
| **context** | 与**当前项目/目录/界面**有关 | 换目录、换 worktree、换界面平台 |
| **volatile** | 运行时**动态**产生的（技能、记忆、时间、环境） | 每次重建都最可能不同 |

### 【源码】平台提示的归属是**条件式**的

这条原文完全没提，但它是个真实的分支：

```python
if coding_workspace_parts:
    # 有 workspace 快照 → 快照之后的块都进 context 层
    context_parts.extend([*coding_workspace_parts, *coding_trailing_parts, *post_workspace_parts])
else:
    # 没有快照 → 没有"晚放能改善前缀"的理由，就留在 stable 层
    # Preserve the stable placement for non-workspace sessions; there is no
    # worktree snapshot whose later position would improve their prefix.
    stable_parts.extend([*coding_trailing_parts, *post_workspace_parts])
```

注释把理由写得非常清楚：**"没有 worktree 快照，就没有'把它往后放能改善前缀'这回事"**——
放后面对缓存没有任何好处，那就别动它，留在更稳定的层里。

> 📌 **这是第 3 章第二个值得记的设计原则**：
> **一个块放哪一层，取决于"把它放前面/后面，对共享前缀有没有影响"。**
> 不是"它语义上算配置还是算环境"。
> 没有快照时，放前面反而更稳（少一层会变的东西）。

### 【源码】为什么 volatile 层以技能索引打头

```python
# Skills are runtime-mutable, so the index leads the volatile band: on a longest-prefix
# backend an unchanged index stays inside the reused prefix; a changed one re-prefills from here.
volatile_parts: List[str] = [skills_prompt, *_memory_parts(agent)]
```

**理由**：技能索引是"可能被运行时改掉"的东西。把它放在 volatile 层的**最开头**，
那么当它**没变**时，它连同前面所有内容一起躺在复用前缀里；
只有它**变了**才从这里重新预填充。

**反过来如果把它放 volatile 层末尾**：它一变，它自己作废——但这时它后面已经没有东西了，
**看起来一样**。区别在于：放末尾时，**它前面的记忆块**如果在最坏情况下要重算……
不，更准确的说法是：**放在层的开头是"爆炸半径最小"的位置**——
它一变，只有它自己和它后面的重新预填充，而它自己恰好是这一层里最可能变的东西。

> （这一条原文没讲，源码注释讲了。**注意它和第 ② 点"共享文件在前"不是同一套逻辑**：
> 那里是"不变的东西往前放"，这里是"最可能变的东西放在它那一层的最前面"——
> 两者的共同目标是**让变化点尽可能靠后 / 让不变前缀尽可能长**。）

---

## 4. 平台提示的定制（`platform_hints`）

原文：

> 平台提示（上面示例里的 Layer 10）是 Hermes 为 Telegram、WhatsApp、Slack、CLI
> 和其他平台注入的**按界面定制**的指导——比如"你在终端上，别用 Markdown"。
> 内建默认值住在 `PLATFORM_HINTS`（`agent/system_prompt.py`）；
> 插件提供的平台通过平台注册表提供自己的提示。

> 【源码】`PLATFORM_HINTS` 实际在 **`agent/prompt_builder.py:634`**（不是原文说的 `system_prompt.py`）。
> 实测包含：`whatsapp` / `whatsapp_cloud` / `telegram` / `discord` / …（每个平台一大段，
> 内容细到"图片会以照片形式发送"、"加 `[[audio_as_voice]]` 才会发成语音条"）。

### 管理员怎么改

原文：

> 管理员可以从 `config.yaml` 的顶层 `platform_hints` 键**追加或替换单个平台**的提示，
> 不影响其他平台：

```yaml
platform_hints:
  whatsapp:
    append: >
      When tabular output would be useful, invoke the table_formatting
      skill instead of emitting a Markdown table.
  slack:
    replace: "You are on Slack. Keep responses tight and avoid wide tables."
  telegram: "Prefer short messages; split long answers."   # shorthand = append
```

> - `append` —— 保留内建提示，在后面加上额外文本
> - `replace` —— 完全替换内建提示
> - **裸字符串** —— `append` 的简写
> - 两者同时存在时 **`replace` 胜出**
> - **格式错误的条目会被防御性地忽略并回退到未修改的默认值**，
>   所以坏的配置值永远不会破坏提示词装配，也不会跨平台泄露

### 【源码】解析逻辑

```python
# agent/system_prompt.py:55
def _resolve_platform_hint(agent, platform_key, default_hint) -> str:
    """Apply the ``platform_hints.<platform>`` config override: ``replace``
    substitutes the default, ``append`` adds text (a bare string is shorthand
    for append). Malformed entries fall back to the unmodified default so bad
    config can never break prompt assembly or leak across platforms."""
    overrides = getattr(agent, "_platform_hint_overrides", None)
    spec = overrides.get(platform_key) if platform_key and isinstance(overrides, dict) else None
    if isinstance(spec, str):
        spec = {"append": spec}                    # 裸字符串 → append
    if not isinstance(spec, dict):
        return default_hint                        # 坏配置 → 原样返回，绝不抛
    replace_text, append_text = (v.strip() if isinstance(v, str) else ""
                                 for v in (spec.get("replace"), spec.get("append")))
    base = replace_text or default_hint            # replace 优先（or 短路）
    return f"{base}\n\n{append_text}".strip() if append_text else base
```

**两处值得看**：

1. **`if not isinstance(spec, dict): return default_hint`** ——
   坏配置的默认行为是"当作没配"，**不是报错、也不是拒绝启动**。
   理由写在 docstring 里：*bad config can never break prompt assembly or leak across platforms*。
   **"永远不要因为一个用户的错误配置让整个 agent 起不来"**——这是配置面的通用原则。
2. **`base = replace_text or default_hint`** —— "replace 优先"是靠 `or` 短路实现的，
   不需要额外判断。

### cron 任务的平台提示（**本次文档新增的内容**）

原文（本地副本刚同步进来的那段）：

> cron 任务以平台 `cron` 运行，但它们的最终回复落在任务的 `deliver` 目标频道上，
> 所以 cron agent 的提示词**也会带上那个频道**的提示
> （内建文本 + 它的 `platform_hints.<channel>` 覆盖），放在一行
> `Delivery destination (<channel>):` 之下。
> 因此 `platform_hints.slack.append` **既作用于真实 Slack 聊天，也作用于投递到 Slack 的定时任务**；
> 而 `platform_hints.cron` 仍然只管 cron 那一段本身。

> 【源码】`_cron_delivery_hint()`。
> **设计含义**：一个定时任务在 Slack 里投递，它就得遵守 Slack 的格式约定（不能发宽表格）。
> 否则你会看到"CLI 格式的内容"出现在 Slack 里。
> **这是"提示词要跟着输出目的地走，而不是跟着进程身份走"**的又一例——
> 和前面 `Platform:` 不算身份字段是同一种思路。

> 【源码】`_cron_delivery_hint` 的 docstring 还提到一个反面理由：
> *a user's `platform_hints.slack.append` reaches Slack-delivered scheduled jobs as well as live Slack chats*
> ——**让用户配一次就到处生效**，不是让他在两个地方配两遍。

### 为什么这个覆盖不破坏缓存

原文：

> 覆盖在**系统提示词构建时**解析（会话开始，以及压缩时，因为压缩会重建提示词）。
> 对一份固定配置，它产出的是**字节稳定**的提示，所以它和内置提示一起住在 **stable 层**，
> 不破坏 prompt 缓存——**它不是对冻结提示词的会话中期实时改动**。

**这句是整章最该背下来的一句。**它把"可配置"和"缓存稳定"两个看似矛盾的要求调和了：

```text
✅ 配置在"构建时"读一次 → 同一份配置永远产出同样的字节 → 缓存安全
❌ 每轮去读一次配置文件 → 用户中途改了配置，下一轮提示词就变了 → 缓存作废
```

> 📌 这条在 Hermes 里是**反复出现的主旋律**，我在自己的系统提示词里就能看到它的用例：
> skill 相关的斜杠命令默认"**延迟生效**"（下次会话才变），要立刻生效得显式加 `--now`。
> 同一个理由。

---

## 5. `SOUL.md` 怎么进提示词

原文：

> `SOUL.md` 住在 `~/.hermes/SOUL.md`，充当 agent 的身份——**系统提示词的最开头一段**。
> `prompt_builder.py` 里的加载逻辑如下：

```python
# From agent/prompt_builder.py (simplified)
def load_soul_md() -> Optional[str]:
    soul_path = get_hermes_home() / "SOUL.md"
    if not soul_path.exists():
        return None
    content = soul_path.read_text(encoding="utf-8").strip()
    content = _scan_context_content(content, "SOUL.md", user_authored=True)  # Security scan: warn + load, never block
    content = _truncate_content(content, "SOUL.md")       # Cap scales with model context window (20k floor); config override wins
    return content
```

> 当 `load_soul_md()` 返回内容时，它**替换**硬编码的 `DEFAULT_AGENT_IDENTITY`。
> 然后 `build_context_files_prompt()` 会带着 `skip_soul=True` 被调用，
> 防止 SOUL.md 出现两次（一次作为身份，一次作为上下文文件）。
>
> 如果 `SOUL.md` 不存在，系统回退到：（那段 "You are Hermes Agent, built by Nous Research.
> Be direct: match the length of your reply…" 的文本）

### ⚠️ 关键细节：`user_authored=True` 的含义（**本次文档新增**）

原文对这一行的注释是：

> *Security scan: warn + load, never block*（安全扫描：警告 + 照常加载，**永不阻断**）

而正文里那段说明是（**本次同步新增的一整句**）：

> - **Security scanned** —— 检查提示注入模式（不可见 unicode、"忽略之前的指令"、凭据外泄尝试）。
>   命中会把**项目文件**替换成一个 `[BLOCKED: …]` 标记；
>   而用户自己在 `HERMES_HOME` 里的 `SOUL.md` 是**被警告但仍然加载**的
>   （它在写入时就经过人工批准，所以和 `config.yaml` 属于**同一信任等级**）

> 【源码】docstring 把完整理由写得很清楚，值得整段抄下来：
>
> > `*user_authored*`（用户自己 `HERMES_HOME` 里的 SOUL.md）：命中时**警告但仍然加载**。
> > SOUL.md 和 `config.yaml` 属于同一信任等级——文件工具对它的写入要过
> > **受保护指令的审批门**（`tools/file_tools_write_guards.py`），而且**项目检出永远无法提供它**——
> > 所以一个用户如果在自己的安全指引里**记录**了"忽略之前的指令"这句话，
> > **不该因此丢掉整个身份文件**（#112570）。
> > 项目目录里的文件（仓库的 AGENTS.md / .cursorrules / .hermes.md）是**跟着检出进来的，继续阻断**；
> > 由 profile 分发（`hermes profile install <git-url>`）拥有的 SOUL.md 也继续阻断——
> > 那种情况下 `load_soul_md` 传的是 `user_authored=False`。
>
> 还有一个只在这个细节里出现的东西：
>
> > 开头的 UTF-8 BOM 是 **Windows 编辑器的产物，不是注入**。
>
> ```python
> if content.startswith("\ufeff"):
>     content = content[1:]
> ```
>
> **"误报也要考虑"**——一个 BOM 字符就会让用户的身份文件被替换成 `[BLOCKED]`，
> 而这种误报的代价（agent 失去人格）远高于收益。

### 这一节的设计原则

> 📌 **第 3 章第三个值得记的原则：按"信任来源"分级，而不是按"内容"分级。**
>
> | 来源 | 信任等级 | 命中注入模式时 |
> |---|---|---|
> | 用户自己的 `SOUL.md` | 同 `config.yaml`（写入有人工审批） | **警告 + 照常加载** |
> | 仓库里跟检出进来的 `AGENTS.md` | 陌生人给的 | **阻断，替换成 `[BLOCKED]`** |
> | profile 分发带来的 `SOUL.md` | 陌生人给的 | **阻断** |
>
> **同一个文件内容、同一个扫描命中，处理方式取决于"谁写的"，而不是"写了什么"。**

### 为什么 mini 版现在没有这个

mini 版的 `agent/prompt_builder.py`（41 行）里，`BASE_IDENTITY` 是**硬编码在 Python 里**的。
所以：

| | Hermes | mini 版现状 |
|---|---|---|
| 身份来源 | `~/.hermes/SOUL.md`（用户可改），没有才回退硬编码 | 只有硬编码 |
| 好处 | 用户不用改代码就能换人格 | —— |
| 代价 | —— | **想换人格就得改 Python** |

这一章我们要给 mini 版补上这个：**`~/.mini-hermes/SOUL.md` 优先，缺省回退到硬编码身份。**

---

## 6. 项目上下文文件怎么注入

原文：

> `build_context_files_prompt()` 使用一套**优先级系统**——**只加载一种**项目上下文（首个匹配者胜出）：

```python
# From agent/prompt_builder.py (simplified)
def build_context_files_prompt(cwd=None, skip_soul=False):
    cwd_path = Path(cwd).resolve()

    # Priority: first match wins — only ONE project context loaded
    project_context = (
        _load_hermes_md(cwd_path)       # 1. .hermes.md / HERMES.md (walks to git root)
        or _load_agents_md(cwd_path)    # 2. AGENTS.md (cwd only)
        or _load_claude_md(cwd_path)    # 3. CLAUDE.md (cwd only)
        or _load_cursorrules(cwd_path)  # 4. .cursorrules / .cursor/rules/*.mdc
    )

    sections = []
    if project_context:
        sections.append(project_context)

    # SOUL.md from HERMES_HOME (independent of project context)
    if not skip_soul:
        soul_content = load_soul_md()
        if soul_content:
            sections.append(soul_content)

    if not sections:
        return ""

    return (
        "# Project Context\n\n"
        "The following project context files have been loaded "
        "and should be followed:\n\n"
        + "\n".join(sections)
    )
```

**"只加载一种"是靠 `or` 链实现的**——前一个返回空串/None 就试下一个。
这个写法比 `if/elif` 短，但**语义上有个坑**：一个**空文件**会被当成"没有"，
于是加载下一个。这通常是你想要的（空文件没有信息量），但要知道它有这个行为。

### 上下文文件发现的细节

原文表：

| 优先级 | 文件 | 搜索范围 | 备注 |
|---|---|---|---|
| 1 | `.hermes.md`, `HERMES.md` | CWD **向上到 git root** | Hermes 原生的项目配置 |
| 2 | `AGENTS.md` | **⚠️ 从 git root 到 CWD 链式合并**（原文写"只在 CWD"，是错的，见下） | 常见的 agent 指令文件 |
| 3 | `CLAUDE.md` | **只在 CWD** | 兼容 Claude Code |
| 4 | `.cursorrules`, `.cursor/rules/*.mdc` | **只在 CWD** | 兼容 Cursor |

> <details>
> <summary>⚠️ 更正记录：原文第 2 行写的是「`AGENTS.md`（CWD only）」，实测是错的</summary>
>
> **原文说法**（这份表 + 正文的 "`AGENTS.md` (cwd only)"）：
>
> > `AGENTS.md` | CWD only | Common agent instruction file
>
> **源码实际说法**（`agent/prompt_builder.py:1652`，`_load_agents_md` 的 docstring）：
>
> > *AGENTS.md — **merged directory chain from git root down to cwd**.
> > Each directory on the chain contributes its own ``AGENTS.override.md`` /
> > ``AGENTS.md`` / ``agents.md`` (first name wins per directory) as its own
> > provenance-labelled section.*
>
> `_agents_md_candidates()`（同文件 1575 行）的实现也确认了：它遍历
> `_agents_md_directory_chain(cwd_resolved)`，也就是**整条目录链**。
>
> **准确的说法是两个层次**（原文把它们混成了一句）：
>
> | 层次 | 规则 |
> |---|---|
> | **跨类型**（`.hermes.md` / `AGENTS.md` / `CLAUDE.md` / `.cursorrules`） | 首个匹配者胜出，**只加载一种** ← 原文"only ONE project context loaded"对的是这一层 |
> | **`AGENTS.md` 类型内部** | **从 git root 到 cwd 链式合并**，每层目录各贡献一个文件，各自带来源标签 ← 原文漏了这一层 |
>
> **为什么链式才是对的**：看 Hermes 自己的仓库——
>
> ```text
> ~/.hermes/hermes-agent/AGENTS.md          ← 全局规则 + 一张"路由表"
> ~/.hermes/hermes-agent/agent/AGENTS.md    ← 在 agent/ 里干活时，这一份**同时**加载
> ~/.hermes/hermes-agent/tools/AGENTS.md    ← 在 tools/ 里时加载这一份
> ```
>
> 根 `AGENTS.md` 里那张路由表写着 *working in `agent/` → read `agent/AGENTS.md`*，
> **链式合并让"走到哪加载到哪"变成自动的**。
>
> 如果只加载 cwd 那一份，**根目录的全局规则（"Never give up on the right solution"、
> 依赖版本策略、提交纪律…）在你进入任何子目录时会静默丢失**。
>
> **验证方式**（我实测过）：造一个仓库，根 `AGENTS.md` 和 `pkg/AGENTS.md` 各写一句独立规则，
> 从 `pkg/sub` 跑，打印 context 层，两层都在、顺序从根到 cwd、各带来源标签
> （`## ../../AGENTS.md` / `## ../AGENTS.md`）。
>
> ⚠️ **mini 版第一版就是照这份文档实现的，所以也错了**——
> 修法见《源码讲解-03-PromptAssembly.md》§2。
> </details>

所有上下文文件都会：

> - **Security scanned** —— 检查提示注入模式（不可见 unicode、"忽略之前的指令"、凭据外泄尝试）。
>   命中会把**项目文件**替换成一个 `[BLOCKED: …]` 标记；
>   而用户自己在 `HERMES_HOME` 里的 `SOUL.md` 是被警告但仍然加载的
>   （见 §5）
> - **Truncated** —— 用 **70/20 的头/尾切分**加一个截断标记，上限是
>   `context_file_max_chars` 个字符。上限**随模型的上下文窗口缩放**
>   （**20000 字符下限，500K 上限**）；`config.yaml` 里显式写的
>   `context_file_max_chars` **永远优先**
> - **YAML frontmatter stripped** —— `.hermes.md` 的 frontmatter 会被去掉
>   （留给未来的配置覆盖用）

> 【源码】上限公式（实测，原文没给）：
>
> ```python
> # agent/prompt_builder.py:1063
> CONTEXT_TRUNCATE_HEAD_RATIO = 0.7        # ← 头 70%
> CONTEXT_TRUNCATE_TAIL_RATIO = 0.2        # ← 尾 20%
>
> # agent/prompt_builder.py:1072
> def _dynamic_context_file_max_chars(context_length):
>     """Char cap from the model's window, clamped to [20K floor, 500K ceiling]; flat default when unknown."""
>     if not isinstance(context_length, int) or context_length <= 0:
>         return CONTEXT_FILE_MAX_CHARS                      # 窗口未知 → 用固定下限
>     budget = int(context_length * CHARS_PER_TOKEN * _CONTEXT_FILE_WINDOW_FRACTION)
>     return max(CONTEXT_FILE_MAX_CHARS, min(budget, _CONTEXT_FILE_DYNAMIC_CEILING))
> ```
>
> 三件事：
> 1. **是"窗口的一个比例"，不是绝对值**——`窗口 token 数 × 每 token 字符数 × 一个分数`。
>    模型窗口越大，允许的 `AGENTS.md` 越长。
> 2. 上下限是 `max(下限, min(预算, 上限))`——**换句话说永远是"夹在中间"的那个值**。
> 3. **窗口未知时回退到固定下限**，不是无限（防御性默认：宁可少给，不要爆上下文）。
>
> 另外 `_get_context_file_max_chars()` 的优先级是：
> **`config.yaml` 显式配置 > 动态计算值**，靠 `int(val) if ... else ...` 一行表达。

### 【源码】截断的真实实现

```python
# agent/prompt_builder.py:1460
def _truncate_content(content, filename, max_chars=None, context_length=None,
                      read_path=None, queue_warning=True) -> str:
    """Head/tail truncation with a marker in the middle; ``read_path`` (default ``filename``) is what the
    agent is told to ``read_file`` to recover the full content."""
    if max_chars is None:
        max_chars = _get_context_file_max_chars(context_length)
    if len(content) <= max_chars:
        return content
    ...
    msg = f"⚠️  Context file {filename} TRUNCATED: {len(content)} chars exceeds limit of {max_chars} — {remedy}"
    logger.warning(msg)
    head_chars = int(max_chars * CONTEXT_TRUNCATE_HEAD_RATIO)   # 0.70
    tail_chars = int(max_chars * CONTEXT_TRUNCATE_TAIL_RATIO)   # 0.20
    marker = (
        f"\n\n[...truncated {filename}: kept {head_chars}+{tail_chars} of {len(content)} chars. The middle is "
        f"omitted — if you need the full instructions, read the complete file with the read_file tool: "
        f"{read_path or filename}]\n\n"
    )
    return content[:head_chars] + marker + content[-tail_chars:]
```

**四件事值得注意：**

**① 70 + 20 = 90，不是 100。** 剩下的 10% 额度留给了那个标记本身——
这样"截断后的结果"仍然不超过 `max_chars`。**这是个常被忽略的细节：
截断标记本身要占额度，否则你的"上限"会被自己破掉。**

**② 头 70 尾 20 的不对称是有理由的。** 文件开头通常是"是什么/怎么用"，
结尾通常是"注意事项/已知问题"。中间最可能是大段细节——**切中间信息损失最小**。

**③ 标记里带了"怎么拿回全文"的指令**：

> *if you need the full instructions, read the complete file with the read_file tool: {read_path}*

**Agent 被截断之后不是无助的——它被明确告知了补救动作。**
这比干巴巴地写"内容被截断"有用得多。

**④ 警告同时进日志和聊天**

```python
# build_system_prompt() 里：
for warning in drain_truncation_warnings():
    agent._emit_status(warning)
```

注释：*Surface context-file truncation warnings in chat, not only in logs.*
（把截断警告显示在聊天里，别只写日志。）

**因为"我的 AGENTS.md 没生效"是个非常难猜的问题**——用户会以为是模型不听话，
而不是"文件被截断了"。**让代价可见。**

### 【源码】读文件有个 5 秒超时，且跑在守护线程上

```python
_CONTEXT_FILE_READ_TIMEOUT_SECS = 5.0

def _read_text_with_timeout(path, timeout=None) -> Optional[str]:
    """``path.read_text()`` on a daemon thread so a slow file can't stall startup."""
```

**为什么**：`AGENTS.md` 可能在一个**卡住的网络挂载盘**上（NFS / sshfs / 断线的共享盘）。
一次 `read_text()` 就能让 agent **启动不了**。

**用守护线程 + 超时**：读不完就放弃（返回 None，当作没有这个文件），
而不是"最多等 5 秒"——守护线程不会阻止进程退出。

> 📌 **这条设计原则值得记住：**"读取用户提供的路径"是一个**可能永远不返回**的操作。
> 任何"启动时读取用户环境"的代码都应该有这个保护。
> mini 版现在直接 `Path.read_text()`，第 3 章实现时值得照抄这个模式。

### 为什么是"只加载一种"，而不是全部合并

这条原文没解释，但能推出来：

```text
全部合并的问题：
  一个仓库里同时有 AGENTS.md 和 CLAUDE.md 和 .cursorrules
  → 三份内容都塞进提示词
  → ① 上下文被吃掉 ② 三份规则互相矛盾时没有优先级语义
  → 模型不知道该听谁的

只取第一个的问题：
  优先级低的文件被完全忽略
  → 但"优先级"语义清楚：谁的约定更具体听谁的
```

**这是个"显式优先级"胜过"全都给"的例子**——和 Hermes 的
"一个 Agent 级工具、一张执行表"是同一种审美：**让冲突在结构上不可能存在 **。

---

## 7. 会话中逐步发现子目录的 `AGENTS.md`（原文没讲，但源码里有）

原文只在表里提了一句：

> `AGENTS.md` (CWD at startup; **subdirectories discovered progressively during the session
> via `agent/subdirectory_hints.py`**)

**这一句话背后是 236 行代码**，值得单独讲：

```python
# agent/subdirectory_hints.py
_HINT_FILENAMES = ["AGENTS.override.md", "AGENTS.md", "agents.md", "CLAUDE.md", "claude.md", ".cursorrules"]
_MAX_HINT_CHARS = 32_000
_PATH_ARG_KEYS = {"path", "file_path", "workdir"}
_COMMAND_TOOLS = {"terminal"}
_MAX_ANCESTOR_WALK = 5  # ancestor levels walked per path — bounds deep-path scans
_NAV_COMMANDS = frozenset({"cd", "pushd"})
```

| 常量 | 作用 |
|---|---|
| `_HINT_FILENAMES` | 同一目录里**多个候选文件的优先级**（注意 `AGENTS.override.md` 最高——给"覆盖而不改动原文件"留了口子） |
| `_MAX_HINT_CHARS = 32_000` | 上限；原文提过 *delivers up to 32k and truncates head/tail with a warning past that* |
| `_PATH_ARG_KEYS` / `_COMMAND_TOOLS` | **追踪工具的路径参数**（`read_file` 的 `path`、`terminal` 的 `workdir`…） |
| `_NAV_COMMANDS` | **追踪 `cd` / `pushd`** —— shell 命令里的目录切换也算"agent 去了哪" |
| `_MAX_ANCESTOR_WALK = 5` | 每个路径最多向上走 5 层，**防止深度路径扫爆** |

**机制**：agent 一旦访问某个新目录（读文件、列目录、`cd` 进去），
就去看那个目录（和它的最多 5 层祖先）有没有 `AGENTS.md` 之类，
有就**把它的内容补进提示词**。

**为什么需要它**：单体 `AGENTS.md` 会变得巨大。
Hermes 仓库自己就是这么干的——它的根 `AGENTS.md` 只放"到处都适用的规则"，
然后有一张**路由表**指向 `agent/AGENTS.md`、`tools/AGENTS.md`、`gateway/AGENTS.md`…

**这直接回答了一个我经常看到的疑问**："为什么 AGENTS.md 要拆成很多个？"

```text
❌ 一个大 AGENTS.md：每个会话都加载全部 → 浪费上下文，且大部分内容与当前任务无关
✅ 根 + 按目录拆：走到哪加载到哪 → 只有相关规则进上下文
```

> ⚠️ **它注入到哪？（我最初推断了"每轮临时路径"，实测是错的——比那个有意思。）**
>
> 【源码】`agent/tool_executor.py:1063`：
>
> ```python
> subdir_hints = agent._subdirectory_hints.check_tool_call(function_name, function_args)
> if subdir_hints:
>     if _is_multimodal_tool_result(persisted_result):
>         # Hint goes on the text summary part so the model still sees it; image blocks untouched.
>         _append_subdir_hint_to_multimodal(persisted_result, subdir_hints)
>     else:
>         persisted_result += subdir_hints
> ```
>
> **它被拼在「工具结果」上——`persisted_result += subdir_hints`。**
> 既不是系统提示词，也不是调用时临时层，而是**跟着工具结果那条消息一起进历史**。
>
> **为什么挂在这里很聪明**：
>
> | 理由 | 说明 |
> |---|---|
> | **时机与相关性天然吻合** | agent 刚"走进"那个目录的那一次工具调用，正好收到那个目录的规则。不用猜它会不会用到 |
> | **不碰缓存前缀** | 系统提示词一个字节都不动；工具结果本来就是这一轮的新内容 |
> | **不影响旧会话** | 规则跟着"那次访问"留存，而不是变成全局状态 |
>
> 还有一处细节值得一提：**多模态结果要单独处理**——
> 提示必须拼在**文本部分**，图片块不能动（注释：*Hint goes on the text summary part
> so the model still sees it; image blocks untouched*）。
> **否则一次字符串拼接就会把图片块弄坏。**

---

## 8. 只在 API 调用时存在的层

原文：

> 这些东西被**故意不**持久化进缓存的系统提示词：
>
> - `ephemeral_system_prompt`
> - prefill messages（预填充消息）
> - 网关派生的会话上下文覆盖
> - 后轮由 Honcho/外部记忆召回、注入当前轮 user 消息的内容
>
> `pre_llm_call` 插件上下文也落在这条"调用时"路径上：它被追加到**当前轮的 user 消息**，
> 而不是写进缓存的系统提示词。当多个插件返回上下文时，
> Hermes 会把这些上下文块**拼接**起来。
>
> **这个分离让稳定前缀保持稳定，以利缓存。**

### ⚠️ 更正一下：它们**不是**全都挂在 user 消息上

我第一版这里写的是"它们全都挂在当轮 user 消息上"——**这个说法不准**。
逐条去代码里核对之后，这四样的落点**各不相同**：

| 调用时的层 | 落在哪 | 源码位置 |
|---|---|---|
| `ephemeral_system_prompt` | **拼在"有效 system 消息"末尾** | `agent/turn_context.py:1152` |
| 后轮的 Honcho / 外部记忆召回 | 当轮 **user 消息** | 原文明确说了 |
| `pre_llm_call` 插件的上下文 | 当轮 **user 消息**（多个插件返回时**拼接**） | 原文明确说了 |
| 网关派生的会话上下文覆盖 | ⚠️ **原文没写明落点**——我只确认了它属于"调用时"这一类 | — |

**`ephemeral_system_prompt` 是拼在 system 段上的**，原文用的词是 *effective system*：

```python
# agent/turn_context.py:1152
if agent.ephemeral_system_prompt:
    effective_system = (effective_system + "\n\n" + agent.ephemeral_system_prompt).strip()
```

而它所在函数的 docstring 里那句是关键：

> *``ephemeral_system_prompt``) is added at API time only — ``messages`` stays untouched*
> （只在 API 调用时加上——`messages` 一个字节都不动）

**所以准确的共同点不是"都挂在 user 消息上"，而是：**

> 🎯 **它们都只作用于"这一次请求的副本"，都不写回 `messages`。**
> 至于是加在 system 段还是 user 段，是各自的实现细节。

**为什么必须只作用于副本**（第 2 章我们只是"照做"，现在知道为什么了）：

```text
messages（历史）        ← 存盘、下一轮复用、缓存前缀就是它
    ↓ 拷一份
api_messages（请求）    ← 临时层加在这里
    ↓
发给提供商

改历史 = 改缓存前缀 = 从那一字节起全部作废
改副本 = 只影响这一请求，历史干净、下一轮照旧
```

这和 §9 的"记忆延迟生效"、§7 的"工作区快照要钉住"是**同一个约束的三种表现**。

### 那用户想加"一些要求"时，到底该放哪？

这是用起来最容易糊涂的一件事。**四个地方，行为完全不同**：

| 你想加的 | 放哪 | 归属哪层 | 生效时机 | 打断缓存吗 |
|---|---|---|---|---|
| **这次就这么干** | `HERMES_EPHEMERAL_SYSTEM_PROMPT` / prefill messages<br>（mini 版：`--system`） | **调用时临时层** | **立刻** | 看值是否每轮变 |
| agent 的人格 / 常驻行为 | `~/.hermes/SOUL.md` | **stable 层（身份）** | 下个会话（mini：`/new`） | 只在重建时 |
| 关于"我"的长期事实 / 偏好 | `~/.hermes/MEMORY.md` / `USER.md` | **volatile 层** | 下个会话 | 只在重建时 |
| 这个项目的规则 | 项目里的 `AGENTS.md` 等 | **context 层** | 下个会话 | 只在重建时 |
| 某个界面的格式要求 | `config.yaml` 的 `platform_hints` | stable 或 context（**条件式**） | 构建时读一次 | 只在重建时 |

**分界线只有一条：**

```text
「这次就这么干」   → 走调用时临时层 → 立刻生效、不进历史、不动缓存态
「以后一直这么干」 → 走缓存态的某一层 → 只能等「重建时机」
```

**为什么不能两者兼得**：因为"缓存态"的定义就是"整份字节在会话内不变"。
一旦允许"改了立刻生效"，它就不再是缓存态——缓存前缀从那一字节起全废。

Hermes 把这条写成了硬规矩（`AGENTS.md` 里原文）：

> 斜杠命令如果需要改系统提示词的状态（技能 / 工具 / 记忆），必须是 **cache-aware** 的：
> **默认延迟失效**（下次会话才生效），要立刻生效得显式加 `--now`
> （官方范例是 `/skills install --now`）。

### 实测：同一个要求，两种放法

**放 `SOUL.md`（缓存态）**

```console
① 会话开始（没有 SOUL.md）     → 891 字符，含要求吗 False
② 写了 SOUL.md 后本会话仍是    → 891 字符，含要求吗 False     ← 故意的
③ 这时敲 /prompt               → ⚠️ 与当前生效的不一致：
                                  生效的那份 891 字符，这份重建的是 656 字符
                                  想让新内容生效：/new 开新会话
④ 新会话（读到 SOUL.md）       → 656 字符，含要求吗 True
```

⚠️ **注意 ④ 比 ① 短了 235 字符，方向是反直觉的——但这不是 bug。**

原因是两层叠加：

```text
① 没有 SOUL.md → 用的是 DEFAULT_AGENT_IDENTITY（一段较长的默认身份）
④ 有 SOUL.md   → SOUL.md **替换**掉那份默认身份
                 而我这个测试用的 SOUL.md 只有十几个字
⇒ 替换后的总长度**变短**了
```

**所以"字符数变了"不代表"加了东西"，也可能是"换掉了更长的东西"。**
要判断内容有没有变，看**内容**（`含要求吗 True/False`），不要看长度。

（这也解释了 §5 那句话的分量：`load_soul_md()` 返回内容时是**替换** `DEFAULT_AGENT_IDENTITY`，
不是追加。）

**走 `--system`（调用时临时层）**

```console
缓存态提示词完全一样吗      → True（905 == 905 字节），且**不含**临时要求
请求里多出来的            → 只有 2 行：一个空行 + 那行临时要求，在 system 段末尾
user 消息                 → 没动
```

**注意那个数字**：有临时层和没临时层的两份**缓存态提示词逐字节相同（905 == 905）**。
临时要求只存在于"这一次请求"里。

（那次比较我把两边的 `session_id` 固定成同一个值才得到 `True`——原因见下面的踩坑记录。）

**"立刻生效"的代价要讲清楚**：

```text
请求里的 system 段 = 缓存态那份 + 临时要求

· 临时要求每轮都一样 → 请求字节稳定 → 缓存照常命中
· 临时要求每轮都在变 → 系统段每轮不同 → 这一段就命中不了
```

所以它是个**显式开关**：用"这几轮可能没命中缓存"换"能临时改变行为"。

> ⚠️ **测这个结论时我错了两次**，值得记：
>
> 1. 第一次测"会不会打断缓存"，打出"会"——因为**前一个实验已经往 `SOUL.md` 里写过那个要求**，
>    被测 agent 的提示词里本来就有它（来自 SOUL.md，不是临时层）
> 2. 第二次测"两份提示词是否相同"，打出 `False`——因为构造 agent 的辅助函数
>    **每次生成新的 `session_id`**，而它写在 volatile 层的 trailer 里。**固定它之后才是 True**
>
> 两次都是**干扰变量没控住**。第二次我干脆把两份的 diff 打出来，才看清唯一差异就是那一行 session ID。
>
> 3. 还有一次是**写文档时自己把自己绕进去**：我看到"加了 SOUL.md 之后总长反而变短"，
>    第一反应是"实现有 bug"。实际是 `SOUL.md` **替换**了那份更长的 `DEFAULT_AGENT_IDENTITY`。
>    **验证时我把"长度"当成了"内容有没有变"的代理指标——它不成立。**
>
> 📌 **"我改了 A，B 也变了"不等于"A 引起了 B"。** 我一开始拿断言当证据，两次被干扰变量骗了；
> 第三次是拿代理指标当结论。**三次的根因是同一个：用间接信号代替直接观察。**

### mini 版的实现

`_inject_ephemeral_layers` 现在有两块（第 2 章只有预算告警）：

```python
# run_agent.py
# 6a. ephemeral_system_prompt：拼到「有效系统消息」的末尾
if self.ephemeral_system_prompt and api_messages:
    head = api_messages[0]
    if head.get("role") == "system":
        head = dict(head)                       # 再拷一层，防手滑改到历史
        head["content"] = (str(head.get("content","")) + "\n\n"
                           + self.ephemeral_system_prompt).strip()
        api_messages = [head, *api_messages[1:]]

# 6b. 预算告警 —— ★ **只改尾巴**，最后一行必须是工具结果，而且一次性
if (
    self.max_turns
    and not self._budget_warning_injected
    and remaining <= max(3, self.max_turns // 20)
    and api_messages
    and api_messages[-1].get("role") == "tool"      # ← 没有合法落点就放弃
):
    last = dict(api_messages[-1])
    last["content"] = str(last.get("content", "")) + "\n\n（系统提示：还剩 … 次迭代…）"
    api_messages = [*api_messages[:-1], last]       # ← 不新增行
    self._budget_warning_injected = True
```

> ⚠️ **6b 我第一版写错了**：原来是往末尾**插一条新的 user 行**，实测得到
> `['system', 'user', 'user']`——被 mini **自己的**交替校验判违规。
>
> Hermes 顶层 `AGENTS.md` 对这件事有明文约束：
>
> > *never two same-role messages in a row;
> > **never a synthetic user message injected mid-loop***
>
> 完整对照和修法见《源码讲解-03-PromptAssembly.md》§10 的第 ④ 条。

命令行入口：`./mini --system "这次回答尽量短"`，或设 `MINI_EPHEMERAL_SYSTEM_PROMPT`。

**还有一处相关的修复**：校验的时机。原来 `_validate_alternation` 只在
`_build_api_messages` 里跑（**注入之前**），所以"注入把序列弄违规"永远发现不了。
现在循环里**注入之后、发请求之前**再校验一次：

| 校验位置 | 校验对象 |
|---|---|
| `_build_api_messages` 里 | **历史本身**合法（防止历史被污染） |
| 循环第 7.5 步 | **最终请求**合法（含临时层） |

---

## 9. 记忆快照

原文：

> 本地记忆和用户画像数据被捕获进系统提示词的 **volatile 层**。
> **会话中期的写入只更新磁盘状态，不会改动已经构建好的缓存系统提示词**，
> 直到某个重建路径运行（新会话，或显式的失效/重建流程，比如压缩触发的重建）。

> 【源码】`invalidate_system_prompt()` 的 docstring 明确说了为什么重建时要重读磁盘：
>
> > 在上下文压缩事件之后调用。**同时从磁盘重新加载记忆**，
> > 好让重建的提示词捕获本会话里发生的写入；
> > 并清掉冻结的插件段快照，让插件在同一个边界上重新渲染。
>
> 所以记忆的更新路径是：
>
> ```text
> 模型调用 memory 工具写记忆
>        ↓
> 写入 ~/.hermes/MEMORY.md（立刻，磁盘）
>        ↓
> 但系统提示词里的那份是"冻结的快照"，本轮不变
>        ↓
> 什么时候生效？→ 新会话 / 压缩触发的重建 / 显式 invalidate
> ```
>
> **这个"延迟生效"看起来是个缺陷，其实是缓存约束下的必然选择。**

### 为什么这么设计 / 砍掉会怎样

如果写成"每次记忆写入就更新系统提示词"：

```text
① 模型写了一行记忆
② 系统提示词变了 → 从那一字节开始，整个缓存作废
③ 下一轮 API 调用全价重算
④ 而模型的写入可能每几轮就来一次
⑤ → 整个会话几乎没有缓存命中
```

**用"记忆立刻可见但下一轮才进提示词"换"缓存一直有效"。** 这个交换划算，
因为模型写记忆之后，**这一轮它自己当然还记得自己写了什么**——
不需要从系统提示词里重新读一遍。

---

## 10. 技能索引

原文只有两句：

> 当技能工具可用时，技能系统会往提示词里贡献一份**紧凑的技能索引**。

> 【源码】实际是个大块：`_skills_prompt()`（`system_prompt.py:300`）+
> `_auto_load_parts()`（钉住的技能）+ `SKILLS_GUIDANCE` 常量。
> 索引格式就是我们自己系统提示词里看到的 `<available_skills>` 块。

**这份索引为什么在 volatile 层打头**——见 §3 末尾那段源码注释的分析。

**"紧凑"的具体含义**：索引里**只有技能名 + 一句话描述**，没有正文。
正文要靠 `skill_view(name)` 按需加载。

**这是个典型的"索引 vs 内容"取舍**：

| | 全塞进去 | 只放索引（实际做法） |
|---|---|---|
| 模型知道有什么 | ✓ | ✓ |
| 上下文开销 | 每个会话几万 token | 几千 token |
| 模型能直接用 | ✓ | 要一次额外的 `skill_view` 调用 |

**选索引**，因为**大部分技能在大部分会话里用不上**——
和 §7 的"子目录 AGENTS.md 按需发现"是同一个思路。
（Hermes 的 `AGENTS.md` 明文禁止给"指导性工具"加 `offset`/`limit` 分页，
理由写得很直接：*models read page 1 and skip the rest*——
**所以技能正文要么全给，要么靠"按需加载"决定不给。**）

---

## 11. 定制面：该改什么、不该改什么

原文：

> 大多数用户应该把 `agent/prompt_builder.py` 当作**实现代码，而不是配置面**。
> 受支持的定制路径是**改变 Hermes 已经加载的提示词输入**，
> 而不是就地编辑 Python 模板。

### 优先用这些面

| 面 | 用来做什么 |
|---|---|
| `~/.hermes/SOUL.md` | 替换内建默认身份块，换上你自己的 agent 人格和常驻行为 |
| `~/.hermes/MEMORY.md` / `USER.md` | 提供持久的跨会话事实和用户画像数据 |
| 项目上下文文件（`.hermes.md` / `AGENTS.md` / `CLAUDE.md` / `.cursorrules`） | 注入仓库专属的工作规则 |
| 技能（Skills） | 打包可复用工作流和参考，**不用改核心提示词代码** |
| 可选的 system prompt 配置 / API 覆盖 | 加部署专属的指令文本，不必 fork Hermes |
| 临时覆盖层（`HERMES_EPHEMERAL_SYSTEM_PROMPT`、prefill messages） | 加**只作用于本轮**、不该进缓存前缀的指导 |

### 什么时候才该改代码

> 只有当你**有意维护一个 fork** 或**要向上游贡献行为变更**时，才编辑 `agent/prompt_builder.py`。
> 那个文件为每个会话装配提示词管道、缓存边界和注入顺序。
> **直接改它是全局产品变更，不是按用户的提示词定制。**

原文的收尾四句，写得很清楚：

> - 想要**不同的助手身份** → 改 `SOUL.md`
> - 想要**不同的仓库规则** → 改项目上下文文件
> - 想要**可复用的操作规程** → 加或改技能
> - 想要**改变 Hermes 给所有人装配提示词的方式** → 改 Python，并把它当作代码贡献

> 📌 **这一节和第 2 章"薄壳里不要加逻辑"是同一套思路的延伸：**
>
> | 第 2 章 | 这一章 |
> |---|---|
> | "壳"里不要放 agent 行为的逻辑 | 用户**不该靠改拼装代码来定制**提示词 |
> | 判定："从手机上使唤它还成立吗？" | 判定："这是**我**想要的，还是**所有人**都该改的？" |
>
> **两处都在防同一件事：把"一次性的个人需求"写进"所有人都走的代码路径"。**

---

## 12. 为什么这么拆

原文收尾只列了四条目标：

> 这个架构被有意优化以：
>
> - **保住提供商的 prompt 缓存**
> - **避免不必要地改动历史**
> - **让记忆的语义可理解**
> - **让 gateway/ACP/CLI 能加上下文而不污染持久的提示词状态**

**四条其实是一条**：前三条是"缓存友好"的三个侧面，第四条是"多个界面共存"。

---

## 13. 【动手】mini 版第 3 章要做什么

> ✅ **本章已完成**（实现结果见附录 A/B/C）。下面保留**当初的计划**，
> 方便对照"计划的和实际做的差在哪"——实测多做了一件（读文件的守护线程超时），
> 另外冒出两个计划外问题（启动器 cwd、截断警告没出口），都记在附录 B 里。

现状：`agent/prompt_builder.py` **41 行**，只有一个硬编码身份 + cwd + 工具名列表。
`run_agent.py` 里 `_build_system_prompt` / `_ensure_system_prompt` / `_inject_ephemeral_layers` 已存在。

### 要实现的（按依赖顺序）

| # | 做什么 | 对应真实版 |
|---|---|---|
| 1 | 拆出 `agent/system_prompt.py`，实现 **三层装配**（返回 `{"stable","context","volatile"}`） | `system_prompt.py:659` |
| 2 | `~/.mini-hermes/SOUL.md` 优先，缺省回退 `BASE_IDENTITY` | `load_soul_md()` |
| 3 | 项目上下文文件 **4 级优先级**（`.mini.md` / `AGENTS.md` / `CLAUDE.md` / `.cursorrules`） | `build_context_files_prompt()` |
| 4 | **安全扫描**（简版：不可见 unicode + "ignore previous instructions"），项目文件阻断、SOUL.md 只警告 | `_scan_context_content()` |
| 5 | **头 70 / 尾 20 截断** + 标记里带 `read_file` 补救指令 | `_truncate_content()` |
| 6 | `MEMORY.md` / `USER.md` 快照进 volatile 层 | `_memory_parts()` |
| 7 | 平台提示（mini 只有 CLI/TUI 两档，选一档实现即可） | `PLATFORM_HINTS` |
| 8 | 时间戳 + 运行时环境块（**cwd 放最后**） | `_timestamp_line()` / `build_environment_hints()` |
| 9 | `ephemeral_system_prompt`（`--system` 命令行参数 + `MINI_EPHEMERAL_SYSTEM_PROMPT` 环境变量） | §8 |
| 10 | **缓存稳定性测试**：同一会话连跑 3 轮，断言系统提示词**字节完全相同** | 这一章的验收标准 |

### 故意不做的（以及代价）

| 不做 | 代价 | 什么时候补 |
|---|---|---|
| 子目录 AGENTS.md 逐步发现（§7） | 大仓库里单文件 AGENTS.md 会膨胀 | 第 6 章（session-storage）之后 |
| 读文件的 5 秒守护线程超时 | 网络盘上可能卡启动 | 如果真在网络盘上跑再加 |
| 存储提示词校验（`_stored_prompt_matches_runtime`） | `--resume` 时无法判断存的那份还对不对 | 第 6 章 |
| 插件段（plugin sections） | 没有插件系统 | 第 10 章之后 |
| 外部记忆 provider 块 | 没有外部 provider | 不在路线内 |
| worktree 前缀优化（§2） | 多 worktree 场景下缓存命中率低 | 这个 mini 版大概永远不需要 |

### 验收标准（比"代码写完了"更重要）

1. **字节稳定性**：同一会话内连续 3 轮，系统提示词字符串**完全相等**（这是这一章的立章之本）
2. **分层正确**：打印三层，确认"共享文件在前、worktree/环境在后"
3. **SOUL.md 生效**：写一个 `~/.mini-hermes/SOUL.md`，确认身份被替换；删掉，确认回退
4. **优先级**：同时放 `AGENTS.md` 和 `CLAUDE.md`，确认只加载前者
5. **截断**：造一个超长 `AGENTS.md`，确认头 70 / 尾 20 + 标记，且标记里能读到补救指令
6. **扫描**：往 `AGENTS.md` 里塞 "ignore previous instructions"，确认变 `[BLOCKED]`；
   同样内容放进 `SOUL.md`，确认**只是警告、照常加载**
7. **临时层不污染**：注入 `ephemeral_system_prompt` 后，确认**历史里的 system 消息没变**

---

## 附录 A · mini 版代码结构对照（本章相关）

| mini 版文件 | 状态 | 对应 Hermes |
|---|---|---|
| `agent/prompt_builder.py` | ✅ **41 → 936 行**（内容层） | `agent/prompt_builder.py`（1745 行） |
| `agent/system_prompt.py` | ✅ **已新建，404 行**（排序层/三层装配） | `agent/system_prompt.py`（817 行） |
| `agent/subdirectory_hints.py` | ⬜ 故意不做 | 同名（236 行） |
| `run_agent.py` `_ensure_system_prompt` | ✅ 已接三层构建 + 缓存 | `conversation_loop.py` |
| `run_agent.py` `_inject_ephemeral_layers` | ✅ 已支持 `ephemeral_system_prompt` | `turn_context.py:1152` |
| `run_agent.py` `_emit_status` | ✅ **本章新增**（截断警告的出口） | 同名 |
| `cli.py` `/prompt` 命令 · `--system` · `--platform` | ✅ 新增 | `cli.py` 的 slash 命令 |
| `~/.mini-hermes/SOUL.md` | ✅ 已支持（含信任分级） | `~/.hermes/SOUL.md` |
| `~/.mini-hermes/MEMORY.md` / `USER.md` | ✅ 已支持 | 同名 |
| `~/.mini-hermes/config.json` | ✅ 新增（对应 config.yaml 的两个键） | `~/.hermes/config.yaml` |
| `tests/test_prompt_assembly.py` | ✅ **147 条**（46 个函数 / 13 组） | — |

规模变化（**用同一条命令分别量第 2 章末与第 3 章末**）：

```text
                          第 2 章末      第 3 章末
run_agent.py                744     →     906 行
agent/prompt_builder.py      41     →     936 行
agent/system_prompt.py        0     →     404 行
cli.py                      196     →     236 行
测试断言                     54     →     208 条（147 + 54 + 7）
测试代码行数                742     →    1980 行
源码（不含测试与 demo）    2156     →    3668 行
含测试与 demo 总行数       3174     →    5924 行
```

> ⚠️ **踩坑记录：这张表原来的数字是错的**，而且是**两个不同的错**。
> ① 行数是"代码检查修复**之前**"的（`prompt_builder.py` 788 → 实际 936 行：
> 那次修复加了 AGENTS.md 链式合并 + 安装树守卫，是实打实的 +148 行）；
> ② 更要紧的是**标签错了**——原来标着"3174（不含测试）"，但 3174 其实是
> **含测试与 demo 的整棵树**（2156 源码 + 742 测试 + 276 demo = 3174，
> 两个版本用同一条命令一量就露馅了）。标题里的"不含测试"和数字属于不同的统计口径。
>
> 教训：**数量声明必须连"统计口径"一起写清楚**（哪条命令、排除了什么），
> 否则它既可能过期，也可能从一开始就在描述另一个东西。

## 附录 B · 本章砍掉的东西（与何时补）

| 不做 | 代价 | 何时补 |
|---|---|---|
| 子目录 `AGENTS.md` 逐步发现 | 大仓库里单文件会膨胀 | 第 6 章之后 |
| 读文件的 5 秒守护线程超时 | **已做**（`read_text_with_timeout`，比计划多做了） | — |
| 存盘提示词校验 | `--resume` 时无法判断存的那份还对不对 | 第 6 章 |
| 插件段 / 外部记忆 provider | 没有插件系统 | 第 10 章之后 |
| 技能索引 + "先占位后回填" | volatile 层少一块，那个技巧没演示到 | 第 10 章 |
| worktree 前缀优化 | 多 worktree 场景下缓存命中率低 | 大概永远不需要 |

> **核心取舍一句话**：这一章的价值 80% 在"三层 + 缓存稳定"，
> 20% 在"`SOUL.md` / 上下文文件 / 安全扫描 / 截断"。
> 子目录发现、存储校验、插件段都是"等有真实需求再补"的东西。

### 实现过程中冒出来的两个计划外问题（值得单独记）

**① 启动器 `cd` 导致"用户的项目目录"丢失**（对应 Hermes 的 launch artifact 问题）

`mini` 启动脚本必须 `cd` 到项目根，否则 Python 的 `sys.path` 上加不到自己的模块。
但那一步会把**用户的目录换掉**，后果是：

```text
AGENTS.md 的发现 → 去看 mini-hermes 自己的目录（而不是用户的项目）
git 工作区快照   → 探到 mini-hermes 那个仓库
```

修法（抄 Hermes 的思路）：启动器在 `cd` **之前**把 `$PWD` 记进
`MINI_LAUNCH_CWD`，Python 侧用 `resolve_context_cwd()` 按
`MINI_CONTEXT_CWD` → `MINI_LAUNCH_CWD` → `os.getcwd()` 依次回退。

> 📌 **通用教训**："进程从哪个目录启动"和"用户的项目在哪个目录"是**两件事**。
> 只要你的启动脚本为了 import 而 `cd`，就必须显式把用户目录传下去。
> Hermes 里对应的是 `_context_cwd_is_launch_artifact`，而且它还会因此
> **拒绝加载自己安装树里的 `AGENTS.md`**——免得把自己的开发规则当成用户的项目规则。

**② 截断警告"无处可去"**

`build_system_prompt()` 会把截断警告取出来送进 `agent._emit_status()`——
但 mini 的 AIAgent 当时**根本没有这个面**，警告就静默消失了。

修法是补一个 `status_callback`（和 `text_callback` 分开）：

| 面 | 是什么 | 进历史吗 |
|---|---|---|
| `text_callback` | **模型说的话**（回答正文） | 进 |
| `status_callback` | **系统要说的话**（截断警告、状态提示） | 不进 |

混在一起的后果：截断警告会变成"助手说的话"被存进历史，
下一轮模型还以为自己说过这句话 —— **污染历史 + 破坏角色交替**。

> 这个缺口是**测试逼出来的**：我第一版测试断言"build 完再 drain 应该能取到警告"，
> 结果取到空——追下去才发现警告被 drain 走了却没人接收。
> **一次断言错误换来一个真实缺口的修复。**

## 附录 C · 实测输出

### C.1 三层装配的实际长相（在本项目目录里跑，是 git 仓库）

```text
═══ stable 层（整个会话不变）─── 489 字符 ═══
你是 mini-hermes，一个跑在用户电脑上的终端 AI agent。
...（身份 + 工具使用指导）
=== 注意：本层**没有**平台提示 —— 因为有工作区快照，它被挪去 context 层了 ===

═══ context 层（换目录/换 worktree 就变）─── 109 字符 ═══
# 工作区状态

当前分支：master
工作区改动：已修改 2、未跟踪 1
                    ← ★ 共享文件（AGENTS.md）在前、工作区快照在后
你在一个终端里和用户对话。用简单的纯文本，不要用 Markdown 语法…

═══ volatile 层（重建时最可能变）─── 347 字符 ═══
Conversation started: Monday, September 21, 2026 (CST, +0800)
Session ID: f0a7f616d481
Model: m
Provider: deepseek
Platform: cli

# mini-hermes runtime environment

主机操作系统：Darwin 25.2.0 (x86_64)
用户 home 目录：$HOME
主机名：qimourendeMacBook-Pro.local
当前工作目录：$HOME/Desktop/study/hermes学习/mini-hermes

<!-- End mini-hermes runtime environment -->     ← ★ 永远最后一个块（含 cwd，最易变）
```

### C.2 同一份代码的**条件式归属**（在非 git 目录里跑）

在 `/tmp/mh_e2e_project`（不是 git 仓库 → 没有工作区快照）里跑同一份代码：

```text
═══ stable 层（整个会话不变）─── 373 字符 ═══
你是一个说话极其简短的技术助手。
★★★ 硬性要求：不管用户问什么，你的回答**必须以「遵命」两个字开头**。★★★
                                        ← ★ SOUL.md 替换掉了硬编码身份，生效
## 工具使用
...
你在一个终端里和用户对话。用简单的纯文本…   ← ★ 平台提示**留在 stable 层**
                                            （没有快照，就没有"往后放能改善前缀"这回事）

═══ context 层（换目录/换 worktree 就变）─── 125 字符 ═══
# Project Context

以下项目上下文文件已加载，应当遵守：

## AGENTS.md
# 项目规则

★ 凡是与本项目有关的回答，**必须包含「项目规则已生效」这七个字**。
这个项目用 pytest，提交前跑 make lint。

═══ volatile 层（重建时最可能变）─── 321 字符 ═══
Conversation started: Monday, September 21, 2026 (CST, +0800)
...
当前工作目录：/tmp/mh_e2e_project        ← ★ 启动器 cd 走了，这里仍然是**用户的项目**
```

**同一个块（平台提示）在两份输出里归属不同层——这就是 §3 那条"条件式"分支的实证。**

### C.3 字节稳定性

```console
$ python3 -c "..."      # 同一个 agent 连续构建 3 次
三次拿到的字节数： [823, 823, 823]
三次完全相同： True
第二次起是缓存命中（同一对象）： True
```

而且是**构建之后写 `MEMORY.md` 也不影响本会话**——测试里断言了
"写入后同一会话内不立即出现，`invalidate` 之后才出现"。

### C.4 真实跑通（DeepSeek，端到端证明两块内容都到达了模型）

```console
$ cd /tmp/mh_e2e_project            # 用户的项目（有 AGENTS.md）
$ MINI_HOME=/tmp/mh_e2e mini --quiet -q "用一句话说明这个项目的测试框架是什么"
provider=deepseek model=deepseek-chat api_mode=chat_completions
遵命。项目规则已生效。这个项目用 pytest 做测试框架，提交前跑 make lint。
 ↑         ↑                      ↑
 │         │                      └─ AGENTS.md 里的正文（含具体规则）
 │         └── AGENTS.md 要求的话术
 └── SOUL.md 要求的话术
```

**三处都不是模型自己编的**——`遵命` 只在 `SOUL.md` 里，`项目规则已生效` 只在 `AGENTS.md` 里。
所以这一条同时证明了：SOUL.md 生效、AGENTS.md 生效、而且**启动器 `cd` 走之后仍然找对了目录**。

### C.5 测试

三个测试文件，**全量 208 条**（147 + 54 + 7）：

```console
$ python3 tests/test_prompt_assembly.py    # 147 条（46 个测试函数 / 13 组）
$ python3 tests/test_loop_mechanisms.py    # 54 条（第 2 章的 agent loop）
$ python3 tests/test_packaging.py          # 7 条（发布前自检）
```

第 3 章那个文件跑起来**按组打印**，一眼看得出"失败的是哪一类保证"：

```console
══ 1. 装配与分层 ══ 三层的存在 / 顺序 / 每块归属哪一层
   → 18 通过
══ 2. 字节稳定性 ★ ══ 这一章的立章之本：改动之前的字节不能变
   → 6 通过
══ 3. 身份（SOUL.md vs 默认身份） ══ 谁能当身份、什么时候跳过、什么时候强行加载
   → 11 通过
══ 4. 项目上下文文件 ══ 4 级优先级 / 链式合并 / override 文件 / frontmatter
   → 15 通过
══ 5. cwd 的解析与可信度 ══ 进程 cwd ≠ 用户项目目录；兜底值和显式值待遇不同
   → 11 通过
══ 6. 安全扫描（信任分级）★ ══ 按「谁写的」分级，不按「写了什么」分级
   → 6 通过
══ 7. 截断 ══ 头 70 / 尾 20 / 标记带补救指令 / 警告能浮现
   → 11 通过
══ 8. 平台提示与配置健壮性 ══ append / replace / 坏配置一律当没配
   → 10 通过
══ 9. 工作区快照的 pin ══ 别重探 git——重探就可能拿到不同的字节
   → 4 通过
══ 10. 时间戳 ══ 当天字节稳定；跨天才出现第二行
   → 8 通过
══ 11. 调用时临时层 ★ ══ 进请求、不进历史；只改尾巴不插行；最终请求要再校验一次；端到端抓真实请求
   → 35 通过
══ 12. 读文件与运行时环境块 ══ 读不返回也要能继续；环境块内部的边界标记必须转义
   → 7 通过
══ 13. 诊断命令 ══ /prompt 要能回答「我看到的就是正在用的那份吗」
   → 5 通过

147 通过 / 0 失败　（13 组）
```

**每组具体在守什么承诺**（代表性的断言）见《源码讲解-03-PromptAssembly.md》§12——
那里有一张逐组对照表。

两个设计值得记：

- **分组写在 `GROUPS` 表里，和函数在文件里的位置解耦**。
  我中途往文件尾追加了 12 个测试，**它们全落进了最后一个分组标题下面**
  （"身份矩阵"跑到"上下文文件读取的边界"里）——**注释分组会随追加而失真**。
- 配了一条**未注册自检**：定义了 `test_` 函数却没登记进 `GROUPS` → 直接报错退出。
  **"写了但从来没跑"是测试里最经典的静默失败**（测试数量看着在涨，覆盖的却是老东西）。

`test_packaging.py` 还**抓到过一次真阳性**：我在 `build_environment_hints` 的
docstring 里写了 `$HOME/project/x.py` 当例子，被"不许写死机器路径"那条拦下——
改成 `/app/project/x.py` 之后反而更贴题（那一段本来就在讲容器场景）。

