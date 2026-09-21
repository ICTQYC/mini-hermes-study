# 源码讲解 · 第 3 章 Prompt Assembly

> **直接读。** 每节结构一样：**mini 版怎么做的 → 真实 Hermes 怎么做的 → 差在哪 / 为什么。**
> 所有结论都带 `文件:行号` 来源。行号会漂，**按方法名 grep 更可靠**。
>
> 这份文档的重点是**架构层面**的差异。哪些算架构、哪些只是细节，
> 见最后一节「怎么划线」——那里有一条可操作的标准。

## 两份代码的路径

```bash
# mini 版
$HOME/Desktop/study/hermes学习/mini-hermes/

# Hermes 源码
~/.hermes/hermes-agent/
```

**一键打印本文提到的 mini 侧位置**：

```bash
cd "$HOME/Desktop/study/hermes学习/mini-hermes"
echo "① 三层装配"    && grep -n "def build_system_prompt_parts\|def _workspace_parts\|def _post_workspace_parts" agent/system_prompt.py
echo "② 上下文文件"  && grep -n "def load_agents_md_chain\|def build_context_files_prompt\|def project_context_suppressed" agent/prompt_builder.py
echo "③ 身份"        && grep -n "def _identity_parts" agent/system_prompt.py && grep -n "def load_soul_md" agent/prompt_builder.py
echo "④ 扫描/截断"    && grep -n "def scan_context_content\|def truncate_content\|def read_text_with_timeout" agent/prompt_builder.py
echo "⑤ 时间戳"      && grep -n "def timestamp_line" agent/prompt_builder.py
echo "⑥ 临时的层"    && grep -n "_inject_ephemeral_layers" run_agent.py
echo "⑦ cwd 解析"    && grep -n "def resolve_context_cwd_source" agent/prompt_builder.py
```

---

# 0. 先说结论：差异一览

先说你的判断对不对。

> **你说**：第三章主要实现的就是 prompt 的构建，三个主要部分 stable / context / volatile，
> 重点是让缓存尽量命中；内部细节就不是架构层面的问题了。

**主干判断完全正确**——三层 + 缓存命中率就是这一章的架构。但有**四处我不同意归到"细节"**，
它们改的是系统的对外保证，不是观感：

| 看起来像细节的东西 | 为什么其实是架构 |
|---|---|
| **上下文文件从哪些目录加载**（链式 vs 只看 cwd） | 它决定"规则从哪来"——**仓库根的全局规则会不会被静默丢掉**是行为差异，不是排版 |
| **一个块放哪一层** | 直接决定缓存前缀有多长（§2 的 100 倍差距） |
| **安全扫描按"来源"分级** | 这是**安全边界**，改它就是改谁能影响模型 |
| **"兜底值"能不能获得提示词话语权** | 这是**可信度模型**，错了会把开发规则当用户规则注入 |

**而下面这些确实只是细节**（改了对任何对外保证都没影响）：
截断的 70/20 比例、20K/500K 上下限、5 秒超时、空文件跳过、frontmatter 剥离、
`AGENTS.override.md` 这个文件名、BOM 处理、时区后缀格式。

---

# 1. 三层的存在与排序 —— 抄对了的部分

**来源**：`agent/system_prompt.py:659`（`build_system_prompt_parts`）

## mini 版怎么做的

```python
# agent/system_prompt.py
def build_system_prompt_parts(agent, system_message=None) -> Dict[str, str]:
    # stable  ← 身份 + 工具指导
    # context ← 调用方 system_message + 项目上下文文件 + 工作区快照 + 平台提示
    # volatile ← 记忆 + 用户画像 + 时间戳 + 运行时环境块
    return {"stable": ..., "context": ..., "volatile": ...}
```

字符串拼装是另一个函数（`build_system_prompt`），**返回分层 dict 而不是一个字符串**——
因为 stable 那一层要能单独取出来。

## 真实 Hermes 怎么做的

同一个签名，同样的三层。额外的：

```python
def build_system_prompt(agent, system_message=None) -> str:
    """...cached on ``agent._cached_system_prompt`` and only rebuilt after compression.
    Tiers are ordered stable -> context -> volatile so implicit longest-prefix caches
    keep the unchanged scaffold."""
    parts = build_system_prompt_parts(agent, system_message=system_message)
    agent._cached_system_prompt_static = parts["stable"]      # ← stable 单独存一份
    ...
```

`_cached_system_prompt_static` 的用途很有意思：**stable 那一层本来就不该变**，
所以把它单独存起来，"存盘的提示词还对不对"的校验只需要比这一层
（`_stored_prompt_matches_runtime`）。

## 差在哪

**排序和分层：抄对了。** 这是这一章最核心的东西，mini 版和真实版一致。

**mini 版缺的是"重建路径"**（见 §3）——它只有一个 `invalidate_system_prompt()`，
没有压缩触发的重建，也没有"存盘提示词的校验"。

---

# 2. ★ 最大的架构差异：上下文文件是链式的

**这条是代码检查时才发现的，而且我第一版是错的。**

## 文档怎么说的 vs 源码怎么做的

| 来源 | 说法 |
|---|---|
| **官方文档的表** | `AGENTS.md` —— **CWD only** |
| **源码 docstring**<br>`agent/prompt_builder.py:1652` | *AGENTS.md — **merged directory chain from git root down to cwd**. Each directory on the chain contributes its own provenance-labelled section.* |

**我第一版照文档实现 → 错了。**

## 准确的说法是两个层次

```text
┌─ ① 跨类型：首个匹配者胜出，只加载一种 ─────────────────────┐
│  .hermes.md / HERMES.md  （走到 git root）                │
│      ↓ 没有就试下一档                                       │
│  AGENTS.md               （★ 这一档内部是链式的）            │
│      ↓ 没有就试下一档                                       │
│  CLAUDE.md               （真的只看 cwd）                   │
│      ↓ 没有就试下一档                                       │
│  .cursorrules            （真的只看 cwd）                   │
└───────────────────────────────────────────────────────────┘
┌─ ② AGENTS.md 类型内部：链式合并（git root → cwd）──────────┐
│  每个目录各贡献一个文件，每个都带**来源标签**                  │
│  同一目录里：AGENTS.override.md > AGENTS.md > agents.md      │
│             （首个非空者胜出）                                │
└───────────────────────────────────────────────────────────┘
```

**"跨类型只取一个"和"同类型链式合并"是两条不同层次的规则**——文档把它们混成了一句
"CWD only"，于是两件事都描述错了。

## 为什么链式才是对的

看 Hermes 自己的仓库结构就明白了：

```text
~/.hermes/hermes-agent/AGENTS.md          ← 全局规则 + 一张"路由表"
~/.hermes/hermes-agent/agent/AGENTS.md    ← 在 agent/ 里干活时，这一份**同时**加载
~/.hermes/hermes-agent/tools/AGENTS.md    ← 在 tools/ 里时加载这一份
```

根 `AGENTS.md` 里那张路由表写着"working in `agent/` → read `agent/AGENTS.md`"——
**链式合并让"走到哪加载到哪"变成自动的**，不需要模型自己去读那张表。

**如果只加载 cwd 那一份**：根目录的全局规则（"Never give up on the right solution"、
依赖版本策略、提交纪律…）在你进入任何子目录时就**静默丢失**了。
除非每个子目录都把全局规则重复一遍——那就是维护灾难。

## mini 版现在的实现

```python
# agent/prompt_builder.py  load_agents_md_chain()
def load_agents_md_chain(cwd: Path, max_chars: int) -> List[str]:
    sections = []
    for directory in _agents_md_directory_chain(cwd):     # git root → cwd
        for name in AGENTS_MD_NAMES:                      # override > 普通 > 小写
            candidate = directory / name
            if not candidate.is_file():
                continue
            raw = read_text_with_timeout(candidate)
            if raw is None or not raw.strip():
                continue                                  # 空文件 fall through
            ...
            label = name if directory == cwd else os.path.relpath(candidate, cwd)
            sections.append(f"## {label}\n{body}")
            break                                          # 每目录一个
    return sections
```

**实测输出**（在 `/tmp/mh_chain/pkg/sub` 跑，仓库根有 `AGENTS.md`、`pkg/` 也有）：

```text
# Project Context

以下项目上下文文件已加载，应当遵守：

## ../../AGENTS.md          ← ★ 来源标签：看得出这条是仓库根来的
# 全局规则（仓库根）
★ 每个回答都必须出现「全局规则」四个字（位置不限）。

## ../AGENTS.md             ← ★ 来源标签：pkg 层
# pkg 目录规则
★ 凡是问到 pkg 的回答，必须包含「pkg规则」四个字。

# 工作区状态
当前分支：master
```

**两层都加载了，顺序从 git root 到 cwd，各带来源标签。**

> 📌 **来源标签这个设计也值得学**。它不是装饰——**模型需要知道"这条规则是谁定的"**：
> 仓库根的规则 vs 当前目录的规则，冲突时该听谁的，标签给了它判断依据。
> 没有标签的话，两段规则就是两团无从排序的文本。

---

# 3. 缓存策略：真正的架构在"什么时候允许重建"

**这正是你说对的"重点是让缓存尽量命中"** —— 但**让缓存命中的手段不只是分层**，
另一半是"**严格限制什么时候能重建**"。

## mini 版怎么做的

```python
# agent/system_prompt.py  build_system_prompt()
cached = getattr(agent, "_cached_system_prompt", None)
cached_for = getattr(agent, "_cached_system_prompt_key", "__unset__")
if cached is not None and cached_for == system_message:
    return cached                 # 命中就原样返回，一个字节都不重算
...
agent._cached_system_prompt = prompt
```

**重建时机（mini 现状）**：

| 时机 | 触发点 |
|---|---|
| 新会话 | `MiniCLI` 构造 / `/new` |
| 显式失效 | `invalidate_system_prompt()` |

## 真实 Hermes 的重建时机

| 时机 | 位置 |
|---|---|
| 新会话 | — |
| **上下文压缩之后** | `invalidate_system_prompt()` 的调用点 |
| **会话边界** | `reset_session_state()` 清掉工作区快照的 pin |
| **存盘提示词校验失败** | `_stored_prompt_matches_runtime()` |

而且 Hermes 的 `invalidate_system_prompt` 做了一件 mini 没有的事：

> 在上下文压缩事件之后调用。**同时从磁盘重新加载记忆**，好让重建的提示词捕获
> 本会话里发生的写入；并清掉冻结的插件段快照，让插件在同一个边界上重新渲染。

## 差在哪

| | mini | Hermes |
|---|---|---|
| 缓存本身 | ✅ 有（单槽，和 Hermes 一样） | 单槽 `agent._cached_system_prompt` |
| 重建时机 | 只有 2 个 | 4 个（多出压缩 / 校验） |
| 跨进程恢复 | 继续用存盘那份（**对**，缓存前缀能复用） | 同，但会**校验** | 
| 记忆重载 | `invalidate` 时会重读（因为每次都重读文件） | 显式重载 |

> ⚠️ **`--resume` 时有一个真实的边界行为**（mini 里写成了注释）：
>
> 历史里 index 0 已经有一条 system 消息（上次会话存的），
> 所以这次新构建的那份**会被丢掉**，继续用旧的。
>
> **这是对的**——继续用旧的，缓存前缀才能跨进程复用。
> 代价是 `SOUL.md` / `AGENTS.md` 在上次会话后改了的话，**恢复的会话看不到新版本**。
>
> Hermes 用 `_stored_prompt_matches_runtime()` 来判断"存的那份还对不对"
> （靠运行时环境块的边界当锚点），不对就重建。**mini 还没做这个校验** → 差距清单第 2 条。

---

# 4. "什么算身份"：按缓存影响定，不按语义

**来源**：`agent/surface_switch.py`、`agent/conversation_loop.py`

## 真实 Hermes 的设计

> `Platform:` **故意不算身份字段**：一次界面切换（桌面 ↔ TUI）保留存储的字节，
> 把当前界面的指导作为**一次性说明**送到**每轮的 user 消息通道**，于是缓存前缀活下来了（#104414）。

## 这条为什么是架构级的

因为它的判据**不是"语义上属不属于身份"**，而是"**改了之后值不值得让缓存失效**"：

```text
语义直觉：界面上写着"你是终端还是桌面" → 明显属于"我是谁" → 算身份
实际判据：桌面切 TUI 是常见操作 → 如果算身份，每次切换就重建提示词 → 缓存全废
        ⇒ 不算身份。改走"每轮 user 消息的一次性说明"。
```

**同一个原则在别处也出现**：`platform_hints` 覆盖在**构建时**解析一次，
所以对一份固定配置它产出**字节稳定**的提示 → 可以住在 stable 层。
如果每轮去读配置文件，用户中途改一下就全废。

> 📌 **可迁移的判据**：**一个字段能不能进"身份/缓存"区，取决于它变的频率和变的代价，
> 不取决于它语义上属于什么。** 变了会导致大范围作废的东西，就该被挪到"每轮临时的层"去。

## mini 版的情况

**mini 还没碰到这个问题**：它只有一个 `platform` 参数，构造时固定，不会中途切换。
但 `--platform cli|tui` 已经存在了——**将来如果做成"运行中切换界面"，
这个坑就会露出来**。

**这里能学到的**：`--platform` 现在写进 volatile 层的 trailer（`Platform: cli`），
如果它变成可变的，那它就该**从提示词里挪走**，改成每轮临时层。

---

# 5. 条件式分层：一个块放哪层，取决于前缀影响

**来源**：`agent/system_prompt.py`（`build_system_prompt_parts` 里的 `if coding_workspace_parts:`）

## mini 版怎么做的

```python
workspace_parts = _workspace_parts(agent)
post_workspace_parts = _post_workspace_parts(agent)      # 平台提示
if workspace_parts:
    context_parts.extend([*workspace_parts, *post_workspace_parts])
else:
    # ★ 没有快照时**留在更稳定的 stable 层**
    stable_parts.extend(post_workspace_parts)
```

## 为什么这样

Hermes 的注释写得很清楚：

> *Preserve the stable placement for non-workspace sessions; there is no worktree snapshot
> whose later position would improve their prefix.*

翻成人话：**"把它往后放"这件事的收益，来自"前面有一段更长的不变前缀"。
没有 worktree 快照就没有那个前缀，"往后放"就纯粹是白挪。**

> 📌 这是全章最容易被忽略的一课：
> **一个块放哪一层，取决于「把它放前面/后面，对共享前缀有没有影响」，
> 不是"它语义上算配置还是算环境"。**

## 实测：同一份代码，两种归属

mini 版实现了这个分支，所以能实测出来：

```text
① 在 mini-hermes 目录里跑（是 git 仓库 → 有工作区快照）
   context 层：  [工作区快照] + [平台提示]
   stable 层：   身份 + 工具指导（**不含**平台提示）

② 在 /tmp/mh_e2e_project 里跑（不是 git 仓库 → 没有快照）
   stable 层：   身份 + 工具指导 + **[平台提示]**
   context 层：  只有项目上下文文件
```

**同一个"平台提示"块，因为环境不同，归属的层不同。**

---

# 6. 信任分级：安全扫描按"来源"而不是按"内容"

**来源**：`agent/prompt_builder.py:81`（`_scan_context_content`）

## mini 版怎么做的

```python
def scan_context_content(content, filename, *, user_authored=False) -> str:
    hits = [name for name, pattern in _INJECTION_PATTERNS if pattern.search(content)]
    if not hits:
        return content
    if user_authored:
        logger.warning("...命中注入模式 %s；因为是用户自己写的，仍然加载。", filename, hits)
        return content                                   # ★ 警告 + 照常加载
    logger.warning("...命中注入模式 %s；已阻断。", filename, hits)
    return f"[BLOCKED: {filename} 命中提示注入模式（{'、'.join(hits)}），内容未加载。...]"
```

| 来源 | 信任等级 | 命中时 |
|---|---|---|
| 用户自己的 `SOUL.md` | 写入要过文件工具的审批门 | **警告 + 照常加载** |
| 项目里的 `AGENTS.md` / `.cursorrules` | 跟着仓库克隆进来的 | **阻断，替换成 `[BLOCKED]`** |

## 为什么这是架构而不是细节

因为它改的是**安全边界**：改这个判据 = 改"谁能影响模型的行为"。

Hermes 的完整理由（#112570），值得整段读：

> `*user_authored*`（用户自己 `HERMES_HOME` 里的 SOUL.md）：命中时**警告但仍然加载**。
> SOUL.md 和 `config.yaml` 属于同一信任等级——文件工具对它的写入要过
> **受保护指令的审批门**，而且**项目检出永远无法提供它**——
> 所以一个用户如果在自己的安全指引里**记录**了"忽略之前的指令"这句话，
> **不该因此丢掉整个身份文件**。
> 项目目录里的文件是**跟着检出进来的，继续阻断**；
> 由 profile 分发（`hermes profile install <git-url>`）拥有的 SOUL.md 也继续阻断。

**注意最后一句**：同样是 `SOUL.md`，**用户自己写的**和**从 git 装来的**待遇不同。
**判据是"谁写的、能不能被陌生人替换"，不是"文件里写了什么"。**

## mini 版的差距

mini 只做了两档（用户 / 项目），**没有第三档"分发来源的 SOUL.md"**
（因为 mini 没有 `profile install` 这种机制）。有机制的时候要记得补。

还有一处细节 mini 抄到了：**开头的 UTF-8 BOM 是 Windows 编辑器的产物，不是注入**。
Hermes 明确处理它，理由是一行 BOM 就会让用户的身份文件被替换成 `[BLOCKED]`——
**这种误报的代价（agent 失去人格）远高于收益。**

---

# 7. 快照与延迟生效：缓存约束下的两个必然选择

这两件事看起来无关，其实是**同一个约束的两种表现**。

## ① 工作区快照：钉住（pin），不重探

**来源**：`agent/coding_context.py`（`_frozen_workspace_snapshot`）

Hermes 的 docstring：

> *The workspace block is a live git probe after project context, ahead of the whole
> volatile band; **re-probing at the compaction rebuild re-emits different bytes for any
> repo that moved and defeats the keep-prompt fast path**. So the bytes are pinned per
> session on the agent, keyed by the resolved cwd (a gateway serves many cwds), and
> replayed on rebuilds.*

```python
# mini 版（agent/system_prompt.py  _workspace_parts）
pinned = getattr(agent, "_frozen_workspace_snapshot", None)
if pinned is not None and pinned[0] == cwd:
    replay = pinned[1]           # "" 也是真实值
else:
    replay = pb.workspace_snapshot(cwd)
    agent._frozen_workspace_snapshot = (cwd, replay)
```

**三个细节都和 Hermes 一致**：

| 细节 | 为什么 |
|---|---|
| 按 **cwd 作 key** | 一个网关进程同时服务多个会话 |
| `""` 算**真实值** | "这里不是 git 仓库"也是一个结论 |
| 会话边界才清 pin | 只有新会话才该重新探 git |

## ② 记忆快照：写入立即落盘，但**不进本会话的提示词**

**来源**：`agent/system_prompt.py`（`invalidate_system_prompt`）

```text
模型调用 memory 工具写记忆
       ↓
写入 MEMORY.md（立刻，磁盘）
       ↓
但系统提示词里那份是"冻结的快照"，本会话不变
       ↓
什么时候生效？→ 新会话 / 压缩触发的重建 / 显式 invalidate
```

**这个"延迟生效"看起来是缺陷，其实是缓存约束下的必然选择**：
如果每次记忆写入就更新提示词，那一字节之后整个缓存作废，
而模型可能每几轮就写一次记忆 → **整个会话几乎没有缓存命中**。

而且**这个交换是划算的**：模型写完记忆之后，**这一轮它自己当然还记得自己写了什么**，
不需要从系统提示词里重新读一遍。

## 两件事的共同点

```text
① 工作区快照：别重探（重探就可能得到不同的字节）
② 记忆快照：  别实时更新（更新就让缓存作废）

⇒ 同一个约束（提示词字节稳定）的两种表现
```

> 🎓 **这是这一章最值得带走的一条思考方式**：
> 凡是"内容会变但必须进缓存前缀"的东西，都要回答：
> **它在什么时机刷新？** 答案必须是"一个已经存在的失效事件"（新会话 / 压缩），
> 不能是"它自己变了就刷"。

---

# 8. mini 完全没有的块

这些不是"漏了"，是**依赖还没有的东西**：

| 缺的块 | 依赖什么 | 什么时候补 |
|---|---|---|
| **技能索引**（volatile 层打头） | 技能系统 | 第 10 章 |
| **帮助指引的"先占位后回填"** | 技能索引（用哪版取决于它） | 第 10 章 |
| **插件段** | 插件系统 | 第 10 章之后 |
| **外部记忆 provider 块** | provider 抽象 | 不在路线内 |
| **profile 行** | 多 profile | 不在路线内 |
| **bot-mode 协议 / 环境探测行** | 对应功能 | 不在路线内 |
| **子目录 AGENTS.md 的逐步发现** | 无（可以现在做） | 第 6 章之后 |

## 其中两个特别值得记住

**① volatile 层以技能索引打头**（`agent/system_prompt.py`）：

> *Skills are runtime-mutable, so the index leads the volatile band: on a longest-prefix
> backend an unchanged index stays inside the reused prefix; a changed one re-prefills from here.*

**变化最频繁的东西，放在它那一层的最前面**——这样它一变，只有它自己和它后面的重新预填充。
**变化点放在层的开头 = 爆炸半径最小。**

**② "先占位、后回填"**（同文件）：

```python
_help_guidance_slot = len(stable_parts)
stable_parts.append(HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS)
...
skills_prompt = _skills_prompt(agent)          # ← 索引是后面才构建的
if "skill_view" in (agent.valid_tool_names or set()) and "- hermes-agent:" in skills_prompt:
    stable_parts[_help_guidance_slot] = HERMES_AGENT_HELP_GUIDANCE
```

**原因**：用哪版帮助指引取决于**技能索引里有没有 `hermes-agent` 这一项**，
而技能索引属于 **volatile** 层、是后面才构建的。所以只能**先放占位符，等索引建好再回填那个下标**。

注释里那句 *pure string check — inherits the index's stability* 是在说：
这个判断**只看字符串**，不引入新的不确定性——同样的索引永远得出同样的结论，
**所以 stable 层还是稳定的**。

---

# 9. 兜底值 vs 显式值：安装树守卫

**来源**：`agent/prompt_builder.py:1634`（`_project_context_suppressed`，issue #64590）

## 真实 Hermes 的判据

```python
def _project_context_suppressed(cwd, cwd_path, allow_install_tree_fallback) -> bool:
    """A FALLBACK-picked cwd inside the Hermes install tree must not gain system-prompt
    authority (the desktop default would load this repo's contributor AGENTS.md).
    An explicitly configured cwd is honored verbatim — the Hermes tree is a legitimate
    workspace when the user deliberately points a session at it — and CLI-style surfaces
    pass allow_install_tree_fallback=True because their launch dir IS the user's shell cwd."""
    return cwd is None and not allow_install_tree_fallback and _is_install_tree(cwd_path)
```

**三个条件同时成立才抑制**：① cwd 是**兜底**挑的 ② 这个壳不允许兜底 ③ 目录在安装树里。

## 这条的架构含义：可信度模型

```text
用户/调用方显式指定       → 照用（即使它就是安装树：用户在开发 mini-hermes 本身）
MINI_LAUNCH_CWD（用户的 shell cwd） → 照用（那就是他的目录）
os.getcwd()（系统猜的）    → 落在安装树里 → 抑制
```

> 📌 **核心区分：「兜底值」和「显式值」的可信度不同。**
>
> 这条不只适用于 cwd。任何"系统猜一个默认值"的地方都要问：
> **如果这个猜出来的值恰好指向了我自己的东西，会发生什么？**
>
> 这里的后果是：**仓库里的 `AGENTS.md` 被当成"用户的项目规则"注入进提示词。**

## mini 版的实现

```python
# agent/prompt_builder.py  resolve_context_cwd_source()
for env_key, source in (("MINI_CONTEXT_CWD", "context_env"), ("MINI_LAUNCH_CWD", "launch_env")):
    value = os.environ.get(env_key)
    if value and Path(value).is_dir():
        return value, source
return os.getcwd(), "getcwd"        # ← 来源也一起返回，让调用方能判可信度
```

**返回"来源"而不只是路径**，就是这一节的关键设计。

```python
# agent/system_prompt.py  _context_files_part()
block = pb.build_context_files_prompt(
    cwd=cwd, skip_soul=soul_loaded, context_length=ctx_len,
    allow_install_tree_fallback=(cwd_source != "getcwd"),
)
```

**实测**：在 mini-hermes 目录里放一个 `AGENTS.md`，从外部目录跑 → 不泄漏；
但如果绕过启动脚本直接 `python3 -m hermes_cli.main`（cwd 就成了安装树）→ 被抑制。

---

# 10. 代码检查发现的三个真问题

这三个都是**检查时才发现的**，值得单独记——它们的共同点是"**没有场景覆盖所以没被测出来**"。

## ① `and` / `or` 写反了（真 bug）

```python
# Hermes 原样：
wants_soul = agent.load_soul_identity or not agent.skip_context_files
#                                        ^^ 是 or
# 我第一版写的：
soul = load_soul_md() if load_soul_identity and not skip_context_files else None
#                                            ^^^ 写成了 and
```

后果（配合默认值 `load_soul_identity = False`）：

| 场景 | skip | load | 应该 | 我第一版 |
|---|---|---|---|---|
| 普通 CLI 会话 | False | False | 加载 | 加载（**碰巧对**） |
| 子 agent 委派 | True | False | 不加载 | 不加载 |
| **cron 任务** | True | **True** | **加载** | ✗ **丢了人格** |
| 显式强行加载 | False | True | 加载 | 加载 |

**第三行露不出来，因为 mini 还没有 cron / 子 agent。**

Hermes 的 docstring 说 *cron keeps the persona while skipping cwd instructions*——
`load_soul_identity` 是一个**独立的强行开关**：cron 要"跳过项目上下文文件（没有 cwd 可言）
**但保留人格**"，这两件事本来就该能分开控制。

**修法**：改成 `or`，默认值对齐成 `False`，并**补一条参数化契约测试把四种组合钉死**。

> 🎓 教训：**没有场景覆盖的代码就是没被测过的代码。**
> "正常场景碰巧对"是最危险的——它让你的 bug 一直藏着，直到某个边缘场景上线。

## ② 截断警告"无处可去"

`build_system_prompt()` 把截断警告取出来送进 `agent._emit_status()`——
但 mini 的 AIAgent **根本没有这个面**，警告就静默消失了。

**这个是测试逼出来的**：我第一版测试断言"build 完再 drain 应该能取到警告"，
结果取到空 → 追下去才发现警告被 drain 走了却没人接收。

**修法**：补 `status_callback`，和 `text_callback` 分开：

| 面 | 是什么 | 进历史吗 |
|---|---|---|
| `text_callback` | **模型说的话**（回答正文） | 进 |
| `status_callback` | **系统要说的话**（截断警告、状态提示） | 不进 |

混在一起的后果：截断警告会变成"助手说的话"被存进历史，
下一轮模型还以为自己说过这句话 —— **污染历史 + 破坏角色交替**。

## ③ 启动器 `cd` 导致"用户的项目目录"丢失

`mini` 启动脚本必须 `cd` 到项目根（否则 Python 的 `sys.path` 上加不到自己的模块），
但那一步会把**用户的目录换掉**：

```text
AGENTS.md 的发现 → 去看 mini-hermes 自己的目录（而不是用户的项目）
git 工作区快照   → 探到 mini-hermes 那个仓库
```

**修法**：启动器在 `cd` **之前**把 `$PWD` 记进 `MINI_LAUNCH_CWD`。

> 📌 **通用教训**：**"进程从哪个目录启动"和"用户的项目在哪个目录"是两件事。**
> 只要你的启动脚本为了 import 而 `cd`，就必须显式把用户目录传下去。
> （Hermes 管这个叫 launch artifact，见 §9。）

## 附：一个观察（不是 bug）

三条"必须包含 X 个字"的人工规则同时喂进去时（SOUL.md 一条 + 根 AGENTS.md 一条 +
子目录 AGENTS.md 一条），**模型会随机漏掉一条**：

```console
第一次：遵命。pkg 目录是存放可复用包/模块代码的地方……pkg规则要求我这么答。
        （漏了根目录的「全局规则」）

第二次：遵命。全局规则：这仓库目前只是个空壳……按 pkg规则，pkg 目录里也只有规则文件。
        （三条都在）
```

**打印出来的 context 层证明三块内容都进了提示词**，所以这是**模型遵循度**问题，
不是注入失败。值得记住：**规则越多越具体，小模型掉规则的概率越高**——
这也解释了 Hermes 为什么把"必须做 X"这类强制指导写成**独立的、模型门控的块**
（`TOOL_USE_ENFORCEMENT_GUIDANCE` 只发给特定模型），而不是到处散着写。

---

# 11. 差距清单

判断标准：**这条差距在新的需求出现之前值不值得补？**

| # | 差距 | 性质 | 值不值得改 |
|---|---|---|---|
| 1 | 存盘提示词校验（`_stored_prompt_matches_runtime`） | 架构（`--resume` 的保证） | **中**——`--resume` 场景真实存在 |
| 2 | 压缩触发的重建 | 架构（重建时机） | **高**——第 5 章做压缩时必然要加 |
| 3 | 子目录 AGENTS.md 逐步发现 | 架构（规则的到达方式） | 中——大仓库才需要 |
| 4 | 技能索引 + 占位回填 | 架构（volatile 打头 + 依赖顺序） | **高**——第 10 章做技能时会正面碰到 |
| 5 | 分发来源的 SOUL.md 也阻断（第三档信任） | 架构（安全边界） | 低——mini 没有分发机制 |
| 6 | `Platform:` 不算身份字段 | 架构（但 mini 未触发） | 低——要等"运行中切界面" |
| 7 | 插件段 / 外部记忆 / profile 行 / bot-mode | 依赖别的功能 | 低 |
| 8 | 截断的 70/20 具体比例 | 细节 | 不用改 |
| 9 | 5 秒超时的具体值 | 细节 | 不用改 |
| 10 | `AGENTS.override.md` 之外的命名 | 细节 | 不用改 |

> 📌 **一条和"用法"有关的补充**：用户想加"一些要求"时该放哪（`SOUL.md` / `MEMORY.md` /
> 项目 `AGENTS.md` / `--system` 四种放法，生效时机完全不同）——
> 那张对照表在《中文精译-03-PromptAssembly.md》**§8 末尾**。
> 它讲的是**同一条架构约束的用户面**：改缓存态要等重建，想立刻生效只能走临时层。

---

# 12. 测试覆盖了什么

`tests/test_prompt_assembly.py` —— **43 个测试函数 / 125 条断言 / 13 组**。
跑一次就按组打印，一眼看得出"失败的是哪一类保证"：

```console
$ python3 tests/test_prompt_assembly.py
MINI_HOME = /var/folders/.../mini_test_home_xxx

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
══ 11. 调用时临时层 ★ ══ 进请求、不进历史；缓存键要区分调用方传的 system_message
   → 13 通过
══ 12. 读文件与运行时环境块 ══ 读不返回也要能继续；环境块内部的边界标记必须转义
   → 7 通过
══ 13. 诊断命令 ══ /prompt 要能回答「我看到的就是正在用的那份吗」
   → 5 通过

125 通过 / 0 失败　（13 组）
```

**★ 标的三组是这一章最核心的承诺**，改代码时最容易破坏。

## 每一组具体在守什么

| 组 | 代表性的断言（不是全部，挑最能说明意图的） |
|---|---|
| **1. 装配与分层** | 「共享文件在工作区快照之前」「运行时环境块是 volatile 层最后一个块（它含 cwd）」「有快照时平台提示进 context 层，没有时留在 stable 层」 |
| **2. 字节稳定性 ★** | 「连续三次构建字节完全相同」「第二次是缓存命中（同一对象）」「重建后仍相同」「写入 MEMORY.md 后同一会话内**不**立即出现，invalidate 之后才出现」 |
| **3. 身份** | 「SOUL.md 全文只出现一次」「四种组合：普通会话加载 / 子 agent 不加载 / cron 跳过 cwd 但保留人格 / 显式强行加载」 |
| **4. 项目上下文文件** | 「根目录和中间层的 AGENTS.md 都被加载」「顺序从 git root 到 cwd」「非 cwd 的层带来源标签」「AGENTS.override.md 优先于 AGENTS.md」「优先级低的**整档**被忽略，不是合并」「空文件被跳过 → 落到下一个候选」 |
| **5. cwd 的解析与可信度** | 「MINI_LAUNCH_CWD 压过进程的 cwd」「无效路径被忽略」「按 launch cwd 找到用户项目的 AGENTS.md」「兜底 cwd 落在安装树里 → 抑制；显式放行 → 不抑制」 |
| **6. 安全扫描 ★** | 「项目文件命中 → BLOCKED 且原文不进入提示词」「**用户 SOUL.md 命中 → 不被 BLOCKED、原文在**」「不可见 unicode 命中」「开头 BOM 只被剥掉、不算命中」 |
| **7. 截断** | 「保留前 70% / 后 20%」「中间大段被去掉」「标记里带 read_file + 完整路径」「没超限就原样返回不加标记」「警告走 status 面，**没有**混进模型说的话」 |
| **8. 平台提示与配置** | 「append 保留默认 + 追加」「replace 完全替换」「裸字符串 = append」「坏配置（数字/列表/None）回退默认」「config.json 坏掉不抛异常」 |
| **9. 工作区快照的 pin** | 「cwd 没变时不重复探 git（pin 生效）」「cwd 变了才重探」「非 git 目录也钉住空值」 |
| **10. 时间戳** | 「同一天只输出一行」「不含时刻」「跨天时出现第二行并给出重建当天」「同一天内两次渲染字节完全相同」 |
| **11. 调用时临时层 ★** | 「请求里的 system 消息含临时提示」「**历史里的不含**」「历史原样未动」「没设时请求与历史逐字相同」「调用方 system_message 进 context 层」「切回旧 key 会重建（单槽缓存的已知取舍）」 |
| **12. 读文件与环境块** | 「读不存在的文件返回 None」「内部的边界标题被转义成引用行」「远端后端不给主机 cwd」 |
| **13. 诊断命令** | 「预览标出与当前生效的一致」「输入变了但没重建 → 预览报出不一致，并告诉用户 /new」 |

## 为什么测试这么组织

**① 测试函数在文件里的位置和"它属于哪一组"是解耦的。**

分组写在文件末尾的 `GROUPS` 表里：

```python
GROUPS: List[tuple] = [
    ("2. 字节稳定性 ★", "这一章的立章之本：改动之前的字节不能变",
     [test_byte_stability, test_stability_across_invalidate, test_memory_write_is_delayed]),
    ...
]
```

为什么要这样：我中途往文件尾部追加了 12 个新测试，结果它们**全落进了最后一个分组标题下面**
（"身份矩阵"跑到"上下文文件读取的边界"里去了）——**文件里的注释分组会随追加而失真**。
把分组做成一张显式的表，就不依赖函数的物理位置了。

**② 配一条"未注册自检"：**

```python
registered = {t.__name__ for _, _, tests in GROUPS for t in tests}
defined = {name for name, obj in globals().items()
           if name.startswith("test_") and callable(obj)}
missing = sorted(defined - registered)
if missing:
    print(f"⚠️  有测试函数没登记进 GROUPS（写了但不会跑）：{missing}")
    raise SystemExit(2)
```

**"写了但从来没跑"是测试里最经典的静默失败**——测试数量看着在涨，覆盖的却是老东西。
这条自检让它变成**启动时的硬错误**。

> 📌 这和这整个项目一直在防的是同一件事：
> **别让失败静默发生。**
> 第 2 章那个"turn 抛异常但退出码 0"、这里"测试写了但没跑"、§10 的"截断警告无处可去"——
> 都是同一个 bug 类的不同表现。

**③ `MINI_HOME` 指向临时目录**（在 `import` 之前就设好）：

```python
_TEST_HOME = tempfile.mkdtemp(prefix="mini_test_home_")
os.environ["MINI_HOME"] = _TEST_HOME     # ← 必须在 import 之前
...
from agent import prompt_builder as pb
```

否则测试会去读用户真实的 `~/.mini-hermes/SOUL.md` / `MEMORY.md`——
**测试结果就依赖"跑测试的这个人家里有什么文件"了**。

---

# 附录：怎么划线——"架构"和"细节"的可操作标准

你问的那个问题（"内部的一些细节就不是架构层面的问题了"）值得给个**能复用的判据**。

## 三个测试，任何一个通过就是架构

**测试 1：改它会不会改变对外的保证？**

```text
✅ 架构：上下文文件从哪些目录加载   → 改它，仓库根的全局规则可能被静默丢掉
✅ 架构：安全扫描怎么分级           → 改它，谁能影响模型就变了
❌ 细节：截断是 70/20 还是 60/30    → 只影响"哪些字被留下"，保证不变
❌ 细节：超时是 5 秒还是 3 秒       → 只影响数字
```

**测试 2：改它会不会牵动别处的决策？**

```text
✅ 架构：平台提示放哪一层
   → 和"排序原则"绑着（有快照就往后挪），改它要同时改排序逻辑
❌ 细节：时区后缀的格式
   → 只在 timestamp_line 一处
```

**测试 3：它是不是一个"反复出现的模式的一次应用"？**

```text
✅ 架构：按变化频率排序    → 全章出现三次（层内 / 层间 / volatile 内）
✅ 架构：快照要钉住        → 工作区快照、记忆快照都是它的应用
❌ 细节：BOM 要不要剥       → 只此一处
```

## 反过来的特征：细节长什么样

```text
· 改它只需要动一个地方，且没有别处依赖它的取值
· 它是个数字 / 名字 / 格式，不是一条规则
· 改完之后"系统对外的承诺"一字不变
· 你没法用它去解释别处的设计
```

## 用这一章的例子对照

| | 架构 | 细节 |
|---|---|---|
| **是这个** | 三层与排序、重建时机、条件式分层、信任分级、链式合并、兜底 vs 显式、快照 pin | 70/20、20K/500K、5 秒、空文件跳过、frontmatter 剥离、`AGENTS.override.md` 这个名字 |
| **为什么** | 通过上面三个测试 | 三个测试全不通过 |

> 🎓 **最后一句**：**"细节"不是"不重要"，是"不牵动别处"。**
> 70/20 这个比例写错了不会让系统出问题（只是信息损失多一点）；
> 而"上下文文件只看 cwd"写错了，会让**仓库根的规则静默消失**——
> 没有报错、没有异常，只有"规则好像没生效"。
>
> **判断架构的一个实用信号：改错了会不会静默出错。**
> 静默出错的地方，基本都是架构。
