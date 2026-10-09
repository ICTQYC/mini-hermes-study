# 源码讲解 · 第 7 章：网关内部（Gateway Internals）

> **真实 Hermes**：`$HOME/.hermes/hermes-agent/`，锚上游 commit **`f5d1926110`**（`version = "0.21.3"`）
> **mini 版**：`mini-hermes/`，本章末状态见文末「本章提交」
> **官方文档**：《中文精译-07-GatewayInternals.md》（本篇是它的深挖版）
>
> **本篇的读法（四步，一步都不许跳）**：
> ① **Hermes 怎么做的** → ② **读 Hermes 的代码**（`文件:行号` + 逐字引注释）→
> ③ **mini 怎么做的** → ④ **mini 为什么没做成这样**（原因要具体，不许写"为了简洁"）+ 值不值得改。
>
> 读完一节你应该能回答三问：**Hermes 原来长什么样 / mini 做没做 / 没做为什么。**
> 差异表的列序固定为 `| 真实 Hermes | mini 版 | 性质 | 值得改 |`，
> 且 **「mini 版」一列记的是【当前代码】**（不是交付时的状态）。

[TOC]

---

## 两份代码的路径与规模

| | 真实 Hermes | mini 版 |
|---|---|---|
| 网关本体 | `gateway/`（门面 `run.py` + **21 个** `run_*.py` 兄弟 + `platforms/`） | `gateway/`（门面 `run.py` + **3 个**兄弟） |
| 本章规模 | `gateway/` **160 个 .py / 86,785 行** | 全项目代码 **8,200 → 10,615 行**（本章新增文件 2,030 行 + 改动既有 +70 行 + **侧线 profile 入口层 +315 行**，见下表） |
| 平台适配器 | `plugins/platforms/` **22 个**平台 | `gateway/adapters/` **1 个**（console） |
| 存储 | `state.db`（第 6 章） | 同（本章加 `session_key` 列） |
| 测试 | ~39k 条 | 新增 `tests/test_gateway.py` **693 行 / 13 组 / 99 条断言** |

**本章 mini 新增/改动**（`wc -l` 实测）：

```text
gateway/__init__.py            21      gateway/run.py                242
gateway/session_keys.py        82      gateway/run_inbound.py        220
gateway/authz.py               89      gateway/run_turn.py           143
gateway/delivery.py            63      gateway/run_shutdown.py        59
gateway/hooks.py              126      gateway/adapters/base.py      231
gateway/status.py             225      gateway/adapters/console.py    81
gateway/profile_scope.py       74
──────────────────────────────  gateway/ 包合计 1,666 行 ──────────────────────────────
hermes_cli/commands.py         93      agent/secret_scope.py          91
hermes_cli/profiles.py         56      tests/test_gateway.py         693
hermes_cli/gateway_cmd.py     124
──────────────────  新增代码 2,030 行 + 新增测试 693 行  ──────────────────

侧线（多 profile 入口层）：
hermes_cli/profile_cmd.py     112      hermes_cli/profiles.py  56 → 225（+169）
hermes_cli/main.py      141 → 171（+30：`-p/--profile` 在构造 MiniCLI 前钉住 home；profile 子命令）
hermes_cli/gateway_cmd.py 124 → 128（+4：provider 让出 `-p`；命名空间参数改名 --namespace）
tests/test_gateway.py   693 → 903（+210：组 14）
──────────────────────  侧线代码 +315 行  ──────────────────────
改动既有（数字是 `git show HEAD:<file> | wc -l` vs 现在，实测）：
          hermes_constants.py      109 → 136（加 contextvar 覆盖）
          hermes_state_common.py   185 → 191（sessions 加 session_key 列 + 索引）
          hermes_state_sessions.py 223 → 248（create_session 收 session_key；
                                             新增 latest_session_id_for_key；list_sessions 补列）
          hermes_cli/main.py       129 → 141（gateway 子命令）
          tests/test_packaging.py        （PROJECT_MODULES += gateway；一处带理由的例外）
```

---

## 导言 · 机制总览：Hermes 具体怎么做的（先读这一节）

这一节只讲 **Hermes 一侧**。每条都给 `文件:行号` + 逐字引注释 —— 先建立"它到底长什么样"，
再看 §一 之后的逐条对齐。

### 0.1 谁在管：进程与角色

```text
平台事件（Telegram / Discord / Slack / … 22 个）
   ↓  ①收     plugins/platforms/<name>/adapter.py   → 归一化成 MessageEvent
   ↓  ②守1    gateway/platforms/base.py::handle_message()   适配器级守卫（按会话分锁）
   ↓  ③守2    gateway/run_inbound.py（门面 gateway/run.py）   runner 级守卫（按命令策略）
   ↓  ④路     gateway/session.py::build_session_key()        会话键（**唯一来源**）
   ↓  ⑤一轮   gateway/run_turn.py                            绑 profile 作用域 → 建 AIAgent → 出站
   ↓  ⑥身份   gateway/status.py                              PID + 启动时间指纹 + gateway_state.json
   ↓  ⑦扩     gateway/hooks.py                               生命周期事件
```

**关键角色只有三个**：**适配器**（只会翻译与发消息）、**runner**（唯一能决定"说不"的地方）、
**存储**（第 6 章的库，负责"两轮之间还活着"）。

### 0.2 什么时候：一条消息的时序

| 时刻 | 谁动 | 代码 |
|---|---|---|
| 平台事件到达 | 适配器 `connect()` 后的循环 | `gateway/platforms/base.py` |
| 归一化成 `MessageEvent` | 适配器 | 同上 |
| **第 1 级守卫**：这个会话忙吗？ | 适配器 | `platforms/base.py:1884-1885`（`_active_sessions`/`_pending_messages`）、`:3583`（放锁）、`:3585`（陈旧检测） |
| **第 2 级守卫**：这个命令现在能跑吗？ | runner | `gateway/run_busy.py:758-797`（两张表 + 查表） |
| 授权 | runner | `gateway/authz_mixin.py`（五层） |
| 算会话键 | runner | `gateway/session.py:654 build_session_key` |
| 读历史 | runner → `SessionStore` | `gateway/run_turn.py:2002` |
| 跑一轮 | per-turn `AIAgent` | `gateway/run_turn.py:1328/2053` |
| 出站 | 适配器 `send()` | `gateway/delivery.py` |

### 0.3 做什么：网关的五件事

1. **准入**：谁能进来（§五 授权，默认拒绝）；
2. **路由**：归哪个会话（§一 会话键，单一来源）；
3. **串行化**：同一会话不许并发跑两轮（§二 两级守卫）；
4. **承上启下**：绑 profile 作用域、读历史、落库（§九 + 第 5/6 章）；
5. **活着**：常驻、单实例、可被判活（§八）。

### 0.4 不变式（变了就是 bug）

| 不变式 | 为什么 |
|---|---|
| **会话键只有一个产出点**（`build_session_key`） | 手拼 = 两处实现一份事实；错了不报错，只是"悄悄开了第二个会话" |
| **同一会话同时只有一轮在跑** | 两条消息交错写进同一份历史 → 角色交替直接坏掉（第 2 章不变量） |
| **授权默认拒绝** | 新装的网关默认什么都不接受；`open` 那条路真实版明令禁止（见 §五） |
| **密钥读取不带作用域 = 报错（多路复用下）** | 否则次级 profile 静默用默认 profile 的凭据 |
| **`os.environ` / 模块全局量永不被 profile 作用域改写** | 改了就**子进程会继承**别的 profile 的秘密 |
| **"PID 存在" ≠ "那是我的网关"** | 号会被回收；真实版因此吃过 #109680 |
| **钩子路径在调用时解析** | import 期看到的 home 永远是启动 profile 的 |

### 0.5 出错：失败模式与处置

| 失败 | 处置 | 代码 |
|---|---|---|
| 历史读不出来 | **拒发**（*"An unreadable store is not an empty conversation…"*） | `gateway/run_turn.py` |
| 启动时撞上活的外部持有者 | 配置冲突 → **退出码 78**（`EX_CONFIG`），让监督者别重启 | `gateway/restart.py:14/:61` |
| 运行中途撞上锁 | **可重试**（`retryable=True`），持有者退出后自动恢复 | `gateway/status.py` |
| 会话锁的主进程死了 | 陈旧检测自愈放锁（**没有 owner 记录时不算陈旧**） | `platforms/base.py:3585` |
| agent 忙时来了新消息 | 普通消息**排队 + 打断**；命令按 `busy_policy` 处置 | `platforms/base.py` + `run_busy.py` |
| 运行中途一轮失败 | 网关**不许死**，只回一条错误（进程还得服务别的会话） | mini 也照此实现 |

### 0.6 跨层：与第 2~6 章的接口

| 章 | 挂到网关的东西 | 本章位置 |
|---|---|---|
| 第 2 章 | 角色交替不变量（决定了"同会话不许并发"） | §二 |
| 第 3 章 | 系统提示词按平台变化（`platform` 传给 agent） | `run_turn.py` |
| 第 4 章 | 工具确认（网关里必须**异步**："确认"变成一次带状态的对话） | §六 |
| 第 5 章 | 网关侧会话维护（85% 兜底压缩）→ **mini 未做**，理由与代价见 §十四 #7 |
| 第 6 章 | `SessionStore` / `state.db` / 血缘 / 检索 | §一、§九 |

### 0.7 时间线：进程的一生

```text
启动   单实例检查（活的外部持有者 → 78）→ 写 pid 记录 + gateway_state.json(running)
       → set_multiplex_active(多路复用才开) → 钩子 discover_and_load（按 home 解析）
       → 适配器 connect() → emit gateway:startup
常驻   入站循环（第 1/2 级守卫）· 后台维护（cron 滴答 / 会话清理 / 记忆落盘 / 缓存刷新）
收尾   结束会话（写 end_reason）→ 摘 pid 文件 → gateway_state.json(stopped)
```

**验收四问**（读完这节应该答得出）：

1. 一条平台消息从进来到被答，中间**最少**经过哪两个守卫？（为什么要两级？）
2. 会话键错了会出什么事 —— **报错还是静默出错**？
3. "这个 home 的网关还活着吗"要用**几件事**证明？为什么一件事不够？
4. 一个进程服务两个 profile 时，最危险的一类 bug 是什么形状？（提示：不报错的那种）

---

## §〇 差异表（16 条 · 「mini 版」列 = 当前代码）

| # | 真实 Hermes | mini 版 | 性质 | 值得改 |
|---|---|---|---|---|
| 1 | 会话键 `agent:<ns>:<platform>:…`，`build_session_key()` 单一来源；ns 里含 `agent:` 前缀 | 同机制，键形 `<ns>:<platform>:<chat_type>:[:chat_id][:thread]`（**无 `agent:` 前缀**，因为 mini 没有历史键要逐字节兼容） | 已对齐（形状有意简化） | 不必 |
| 2 | 命名空间：默认→`main`；具名→名字；**真名 `main` → `main~`**（`~` 不在 profile id 字母表里） | 完全一致 + 有测试与反向验证 | 已对齐 | 不必 |
| 3 | 反向映射 `profile_from_session_key_namespace()` | 无 | 未做（mini 的键→profile 反查只靠 namespace 段字符串） | 做得起（~10 行）；有消费者时再加 |
| 4 | 五层授权：平台全放行 → 平台白名单 → **私聊配对** → 全局全放行 → 默认拒 | 四层（跳过配对）+ 默认拒 | 已对齐（少一层，理由见 §五） | 配对：单用户无消费者 → 不做 |
| 5 | `dm_policy` 四态（open/allowlist/disabled/pairing），`open` 被安全文档明令禁止 | 只有 allow-all / allowlist / 默认拒 | 未做（mini 无平台 YAML 配置） | 接真实平台时补 |
| 6 | 第 1 级守卫：`_active_sessions` + `_pending_messages` + `_session_tasks`（**owner + 陈旧检测**） | 同三件套（threading 版，语义一致） | 已对齐 | 不必 |
| 7 | 陈旧判定：`done and done()` —— **没有 owner 记录时 NOT stale** | 同（`not task.is_alive()` 且无 owner 时返回 False） | 已对齐 | 不必 |
| 8 | 忙时策略在 `CommandDef.busy_policy`（reject/dispatch/interrupt_then_dispatch） | 同三态 + 未知命令按 reject | 已对齐 | 不必 |
| 9 | 命令分发 = 两张表（`_PLAIN_COMMANDS`/`_IDLE_COMMANDS`）+ `_command_handler_table()`，无 if 链 | 一张声明表 `GATEWAY_COMMANDS` + `_command_handler_table()`，无 if 链 | 已对齐（规模小所以一张表） | 不必 |
| 10 | 单实例：PID 记录 + **启动时间指纹** + `gateway_state.json` 双判据 | 同三件（`ps -o lstart=` 取指纹） | 已对齐 | 不必 |
| 11 | 退出码 78 + `is_global_startup_conflict()`（`*_lock`/`lock_conflict`） | 同；冲突时写 `startup_failed` + `conflict_with` | 已对齐（少了监督者映射，见 §八） | 不做（mini 不装服务） |
| 12 | 钩子：`<home>/hooks/<name>/{HOOK.yaml,handler.py}` + 8 事件 + 内置桩为空壳 | 同布局 + 7 事件（多了 `gateway:tick`，少了 `command:*` 通配） | 已对齐（HOOK.yaml 用自带 3 行解析器，零依赖） | 不必 |
| 13 | 交付四种：直接回复 / 主频道 / 指定目标 / 跨平台 | 两种：直接回复 / 指定目标（`platform:chat_id`） | 部分对齐（写明了缺的两种） | 主频道：无 cron 消费者 → 不做 |
| 14 | profile 作用域：`_profile_runtime_scope`（home contextvar + 密钥作用域 + **终端作用域**）+ 7 个绑定点的同类函数 | `profile_runtime_scope`（home contextvar + 密钥作用域，**无终端作用域**）+ `served_profile_child_env` | 部分对齐（终端作用域见 §九） | 值得（mini 的 terminal 工具目前无作用域概念） |
| 15 | 多路复用 + `set_multiplex_active` → `UnscopedSecretError` | 同（有测试与反向验证） | 已对齐 | 不必 |
| 16 | 22 个平台适配器 + `plugin.yaml` + `register_deferred` 延迟加载 | 1 个 console 适配器（stdin/stdout），**接口形状照抄** | 规模差距（mini 零依赖无 SDK） | 不做（要 SDK/凭据） |
| 17 | CLI：`-p/--profile` 选 profile（`hermes_cli/_parser.py:16`）+ `hermes profile create/…` | `-p/--profile` **对齐** + `mini profile create\|list\|show`（只做三个动作：delete/rename 没做，因为 mini 没有回收站/确认流程） | 已对齐（入口层，规模小） | 不必（delete/rename 要消费者时再加） |

---

## §一 会话键：单一事实来源

**① Hermes 怎么做的**：会话键同时是**路由键**（消息归哪个会话）、**并发键**（哪个 agent 在跑）、
**存储键**（`sessions.session_id` 那一行的归属）。所以它必须只有一个产出点。

**② 读 Hermes 的代码**（`gateway/session.py:654-662`，逐字）：

> ```python
> def build_session_key(
>     source: SessionSource, group_sessions_per_user: bool = True,
>     thread_sessions_per_user: bool = False, profile: Optional[str] = None,
> ) -> str:
>     """Build a deterministic session key from a message source (single source of truth).
>     Layout: ``<ns>:<platform>:<chat_type>[:<slack scope_id>][:<chat_id>][:<thread_id>][:<user>]``.
> ```
>
> 官方文档那句同样直接：**Never construct session keys manually.**

命名空间那条（`gateway/session.py:625-634`，逐字）：

> ```python
> def _session_key_namespace(profile: Optional[str]) -> str:
>     """``agent:<ns>`` prefix for a session key: default/None profile → ``agent:main``
>     (BYTE-IDENTICAL to every historical key); named profile → ``agent:<name>`` so two
>     profiles serving the same chat never collide. A profile literally named ``main`` would
>     otherwise produce the default's namespace and share every session (routing index, agent
>     cache, store) with it, so it is marked ``main~``: ``~`` is outside the profile-id alphabet,
>     so the marked form can never be another profile's id."""
>     if not profile or profile == "default":
>         return "agent:main"
>     return "agent:main~" if profile == "main" else f"agent:{profile}"
> ```

**两条要记住的东西**：

1. **`main~` 的手法**：不靠约定（"记得别把 profile 起名叫 main"），靠**字母表** —— `~` 不可能
   出现在合法 profile id 里，所以这个标记形态**不可能撞上**任何真 profile。
2. **"BYTE-IDENTICAL to every historical key"**：默认 profile 的键形状**一个字节都不能变**。
   这不是洁癖：历史键已经在用户的库里，改一个字节 = 全部历史会话"丢了"。
   mini 没有这类历史包袱，所以键形少一个 `agent:` 前缀 —— 这是**有意的简化**，
   代价写在 `gateway/session_keys.py` 的模块 docstring 里（真接多平台时要一次定死，之后不能再改）。

**③ mini 怎么做的**（`gateway/session_keys.py`，82 行）：

- `SessionSource`（平台/chat_id/chat_type/user_id/thread_id）；
- `session_key_namespace(profile)`：默认 `main`、`main` → `main~`、具名 → 名字；
- `build_session_key(source, profile=None)`：**唯一产出点**；
- 没有 `chat_id` 时退到发送者 id、再退到"每平台一个"（照真实版 DM 分支的理由：
  否则所有无 chat_id 的 DM 会**共享一个 agent**）。

**归属问题（mini 踩出来的）**：适配器的**第 1 级守卫**就要按会话分锁 → 它必须**在调 runner 之前**
就有键；但"命名空间是哪个 profile"是网关状态。mini 的解法是**注入函数**
（`runner._key_for_source` → 适配器 `_session_key_fn`），而不是注入 profile 名：
两边都只调 `build_session_key`，**没有任何一方自己拼键**。
（踩坑过程见 §十二 ①。）

**④ mini 为什么没做成这样**：本节**做成了**。少的只有反向映射
（`profile_from_session_key_namespace`）—— 它的消费者是"给一个已存在的会话键，反查它属于哪个
profile"（真实版用在做账号清单、诊断）。mini 目前没有这种只读诊断面，所以**做得起但现在没有用**；
等有消费者再加（差异表 #3）。

---

## §二 两级消息守卫

**① Hermes 怎么做的**

先看它要解决的那个**具体场景**（带时间戳）：

```text
10:00:00  用户：「帮我重构 utils.py」
          → agent 开始跑：读文件、想改动、可能还要等你确认一条终端命令
10:00:05  用户改主意了：「算了先别动，帮我看另一件事」
          ← 这一条进来时，上一轮还在跑
```

这五秒里，那条新消息只有**三种朴素处理方式**，而三种都会出事：

| 处理方式 | 具体会发生什么（症状） |
|---|---|
| **① 并发跑第二轮** | 两条线同时往**同一个会话历史**里写。新的一轮基于"第一轮还没写回的旧历史"起跑 → 它**看不到第一轮已经做的事**（同一个文件被改两遍、都基于旧内容）；两轮各写各的 assistant 消息 → 库里出现 `user:A, user:B, assistant:A, assistant:B` 这种**交错**，而"严格角色交替"是第 2 章的不变量 → 下一轮请求直接被 provider 400（或要靠 repair 收拾）。更隐蔽的是它们还抢同一份状态：todo 存储、终端确认的等待位。 |
| **② 静默丢消息** | 用户看到消息发出去了（平台显示已发送），但**永远没有回复**。他以为 agent 卡死了 → 5 分钟后重发 → 要么这次丢了、要么那次丢了，而**丢掉的可能是"停下别动"**。 |
| **③ 无脑排队** | "无脑"= **两边都不通知**：不通知**正在跑的那一轮** → 它继续干十分钟的长任务，用户点了停止却排在一个跑不完的活后面；不通知**用户** → 他不知道自己这条被受理了没有，于是重复发。 |

**关键洞察**：用户发新消息的真实意图，往往不是"追加一条"，而是**"我改主意了 / 打断"** ——
而"排队"这个动作本身**不表达**这个意图。这是三种朴素做法的共同盲点。

Hermes 的做法拆成五条，每条对应防住上面哪一种错：

| # | 做法 | 防住 | 代码 |
|---|---|---|---|
| 1 | 忙时**每个会话一把锁**，同会话不许并发 | ① | `platforms/base.py:1884-1885`（`_active_sessions`）；`run.py` 侧即第 2 级 |
| 2 | 消息**入队而不是丢**（`_pending_messages`） | ② | `platforms/base.py` 同处；一轮结束后由 `_drain_pending` 放出来（mini 侧真踩过顺序坑，见 §十二 ②） |
| 3 | 入队的同时**设置中断事件** → 正在跑的那一轮在**下一个安全点**（两次迭代之间）看到它并及早收尾 | ③（前半） | 适配器设事件 → 网关把它路由给正在跑的 agent；mini: `_on_interrupt` → `agent.interrupt()` |
| 4 | **命令与普通消息分开处置**（`busy_policy`）：`/stop` `/new` 穿透并先打断；`/status` `/approve` 忙碌时也能到；其余**明确拒绝并说出口** | ③（后半） | `run_busy.py:758-797` + `CommandDef.busy_policy`（见 §三） |
| 5 | 主（owner task）死了 → **陈旧检测自愈**放锁 | ① 的次生灾害（锁永久挂着 = 会话永久卡死） | `platforms/base.py:3585`（下一段逐字引） |

**为什么要两级**（这一段最容易看漏，值得单独说）：

- **第 1 级在适配器**：它挡在"消息进 runner **之前**"，所以它**能早**；但它只知道"忙 / 不忙"。
- **第 2 级在 runner**：它手里有**命令表**，所以它**能判策略**（该执行 / 该拒 / 该先打断）；
  但它已经晚了（消息已经在后台队列那条路上）。
- 只留第 1 级 → `/stop` 和普通消息一个待遇（要么都拒、要么都排队，**打断不了**）；
  只留第 2 级 → 普通消息也得先"走一趟"才知道该排队，而且"拒绝"要在更靠里的位置才说出口。

一句话：**忙时不是"排队就完事"，而是分两件事做 —— 把消息存住（不丢），同时把"改主意"这个意图
告诉正在跑的那一轮（可打断）**；命令还要额外按自己的策略走。

**② 读 Hermes 的代码**：

第 1 级在**适配器**里（`gateway/platforms/base.py`）：

- `_active_sessions[session_key]` = 这个会话的锁（`guard` Event，`:1884-1885`、`:3610`）；
- `_pending_messages` = 忙时消息的去处（**排队，不是丢**）；
- `_session_tasks[session_key]` = **谁是主**（owner task，`:3611-3615`）。

放锁与陈旧检测（`:3583` 与 `:3585-3589`，逐字）：

> ```python
> def _session_task_is_stale(self, session_key: str) -> bool:
>     """True if the recorded owner task for ``session_key`` has exited. No owner task at all is
>     NOT stale (guards installed outside handle_message, as tests do, must not be healed)."""
>     done = getattr(self._session_tasks.get(session_key), "done", None)
>     return bool(done and done())
> ```

**为什么"没有 owner 记录不算陈旧"值得单独引**：这是给自己的测试留的路 ——
测试可以在 `handle_message` 之外装一把守卫来模拟"忙"，如果那也算陈旧，
测试就会**自己把自己治好**，于是什么东西都测不出来。

第 2 级在 **runner** 里（`gateway/run_busy.py:758-797`，逐字）：

> ```python
>     _PLAIN_COMMANDS = (
>         "status", "context", "restart", "approve", "deny", "pause", "agents", "bg", "btw", …
>     )
>     # Dispatched only on the idle path (busy dispatch has its own allowlist).
>     _IDLE_COMMANDS = (
>         "topic", "whoami", "platform", "stop", "reasoning", "memory", "skills", …
>     )
>
>     def _command_handler_table(self, names) -> Dict[str, Any]:
>         return {
>             name: getattr(
>                 self, self._COMMAND_HANDLER_ALIASES.get(name, f"_handle_{name.replace('-', '_')}"),
>             )
>             for name in names
>         }
> ```

**③ mini 怎么做的**（`gateway/adapters/base.py` 231 行 + `gateway/run_inbound.py` 220 行）：

| 机制 | 真实 | mini |
|---|---|---|
| 会话锁 | asyncio Event + owner task | `threading.Event` + owner `Thread`（`is_alive()` 代替 `done()`） |
| 忙碌时的**普通消息** | 排队 + 设置中断事件 | 排队 + `_on_interrupt` 回调 → `agent.interrupt()` |
| 忙碌时的**命令** | 第 2 级按表处置 | 内联分发到 runner，按 `busy_policy` 处置 |
| 自愈 | owner 结束 → 放锁 | 同 |

**一条 mini 侧的设计说明（写在代码注释里）**：第 1 级的职责边界是
"**只分类，不判策略**" —— 它看得到的只有"忙"，看不到"命令表"。
所以命令一律内联交给第 2 级；普通消息排队 + 打断。
"拒绝"也由第 2 级说出口（用户可见的话归 runner 管）。

**④ mini 为什么没做成这样**：本节**做成了**（含陈旧自愈、含"没有 owner 不算陈旧"这条边界）。
唯一的简化：真实版的守卫是在 asyncio 里跑的，mini 用线程 —— 这条差别影响的是
**并发模型**（第 2 章的 CLI 就是线程模型），落进差距清单 §十四 #3。

---

## §三 忙时策略：命令的属性，不是调用点的属性

**① Hermes 怎么做的**：一个命令在"agent 正在跑"时该怎么办，写在**命令自己的声明**里。

**② 读 Hermes 的代码**（`hermes_cli/commands.py:21-37`，逐字）：

> ```python
> class CommandDef:
>     """Definition of a single slash command."""
>     name: str                          # canonical name without slash: "background"
>     ...
>     cli_only: bool = False             # only available in CLI
>     gateway_only: bool = False         # only available in gateway/messaging
>     gateway_config_gate: str | None = None  # config dotpath; truthy overrides cli_only for gatew
>     # Mid-run (agent busy) gateway behavior (gateway/run.py Guard-2 dispatcher): "dispatch" = run
>     # while busy (normal handler or the ``busy_handler`` variant); "reject" = refuse mid-run
>     # (generic "Agent is running" unless ``busy_handler`` names a reject message);
>     # "interrupt_then_dispatch" = interrupt first (/stop, /new, /reset; Guard 1, platforms/base.p
>     busy_policy: str = "reject"
>     busy_handler: str | None = None  # key of a special mid-run handler in Guard-2 table
> ```

注释里那句 **"(/stop, /new, /reset; Guard 1, platforms/base.p…)"** 直接点明：
`interrupt_then_dispatch` 那三个命令是在**第 1 级**就处理的（打断），其余交第 2 级。

**③ mini 怎么做的**：`hermes_cli/commands.py:40-51` 的 `GATEWAY_COMMANDS` 八条声明，
每条带 `busy_policy`；`/new` `/stop` 是 `interrupt_then_dispatch`，`/status` `/help` `/approve`
`/deny` `/queue` 是 `dispatch`，`/sessions` 是 `reject`（默认）。

**顺序不变量（mini 踩出来的，见 §十二 ③）**：忙时判定必须**先于**命令分发。
反过来的话，`reject` 策略的命令会在 agent 跑着时**照样执行** ——
"改一个正在被使用的会话的元数据"正是忙时策略要防的事。

**④ mini 为什么没做成这样**：本节**做成了**。少的是 `busy_handler`（"拒绝时换一条专门的拒绝文案"）
与 `gateway_config_gate`（按配置决定命令可不可见）—— 前者是文案细化、后者需要配置系统，
mini 都是单条文案 + 全命令可见（差异表 #8 已注明）。

---

## §四 命令分发：表驱动，没有 if 链

**① Hermes 怎么做的**：上游代码形状规则里有一条硬规定 ——
**「键在名字/种类上的 `if/elif` 阶梯 ≥ 4 个分支，必须改成 字典/表 → 处理器」**，
并点名 `_SLASH_DISPATCH`（cli）与 `_command_handler_table`（gateway）是标准形状。

**② 读 Hermes 的代码**：`_command_handler_table()` 的实现只有 5 行（见 §二 的逐字引），
它做的事是**按名字算出处理器函数名**（`_handle_<name>`），别名走 `_COMMAND_HANDLER_ALIASES`。
加一个命令 = 加一条声明 + 一个 `_handle_*` 方法；分发逻辑一个字不改。

**③ mini 怎么做的**：`resolve_command()`（规范名 / 别名 / **唯一**前缀）+ `_command_handler_table()`
（八个名字 → 方法）。加到新命令的成本 = 表里加一行 + 写一个方法。

**一条 mini 特意加严的边界**：前缀匹配必须**唯一**。`/s` 同时可能是 `stop`/`status`/`sessions`
—— 这时**不猜**（返回 None，当普通消息处理）。猜错的后果是**打断一个正在跑的回合**，
而"什么都不做"的后果只是用户多打几个字。

**④ mini 为什么没做成这样**：本节**做成了**。
一处**有意的不一致**要写清楚：`mini-hermes/cli.py`（第 2~6 章的产物）的命令分发**仍是 if 链** ——
它是已冻结章节的交付物，回头改它要重跑那一章的验证。**两处并存的代价**是：
同一个概念在仓库里有两种写法（新读者会问"该照哪个抄"）。答案写在 `hermes_cli/commands.py`
的模块 docstring 里：**新代码照表写**。

---

## §五 授权：默认拒绝

**① Hermes 怎么做的**：五层，顺序求值，最后落到"未授权 → 拒"。**默认是拒绝**，
新装的网关默认什么都不接受。

**② 读 Hermes 的代码**（`gateway/authz_mixin.py`）：

平台策略（`:347-350`，逐字）：

> ```python
>     def _adapter_policy(self, platform, kind: str, profile) -> str:
>         """Lowercased effective ``dm_policy`` (open/allowlist/disabled/pairing) or ``group_policy``
>         (open/allowlist/disabled) for *kind* in {"dm", "group"}; ``""`` if unknown."""
> ```

而 `open` 那一态是被**点名禁止**的（`:390-398`，逐字）：

> ```python
>     def _own_policy_authorizes(self, source, user_id, is_group, adapter_profile) -> Optional[bool]:
>         """Own-policy adapter verdict when no env allowlist exists; None = no verdict.
>
>         Trusted only when the effective policy for THIS chat type is ``allowlist``: ``open`` forwards
>         EVERY sender (the fail-open SECURITY.md §2.6 forbids), ``disabled`` never forwards, ``pairing``
>         forwards unpaired DMs for the handshake (already denied by the pairing-store check).
>         Anything else → default-deny.
>         """
> ```

**这三行是整个第 7 章最该记住的一句**：*open forwards EVERY sender (the fail-open
SECURITY.md §2.6 forbids)*。一个网关如果"默认放行、让人事后配"，它就是一个**公开的 agent 入口**。
注意 "Anything else → default-deny"：**兜底是拒，不是放**。

**③ mini 怎么做的**（`gateway/authz.py`，89 行）：四层（平台全放行 → 平台白名单 → 全局全放行 →
**默认拒**），顺序照抄、不可重排（全放行必须能压过白名单，否则运维改了开关却被旧白名单挡住，
只能靠猜）。另有 `explain()`：**拒绝时要说得出理由与补救方法** ——
`⛔ 未授权：默认拒绝：请设置 CONSOLE_ALLOWED_USERS=<你的 id>（或 CONSOLE_ALLOW_ALL_USERS=1）`。
（写不出补救方法的拒绝，运维只能翻代码。）

**授权在命令分发之前**（顺序不变量）：所以未授权用户**连 `/help` 都被拒** ——
不泄露命令面。

**④ mini 为什么没做成这样**：少的是**私聊配对**（第 3 层）与 `dm_policy` 四态配置。
原因具体：配对的存在意义是"已授权用户给**新用户**发配对码" —— mini 是单用户单平台，
**没有第二个用户可配对**；`dm_policy` 需要每平台的 YAML 配置（mini 只有环境变量）。
代价写明：接真实平台、要开多人使用时，**这两件必须补**，而且必须保持"默认拒"。

---

## §六 交付与"确认"：为什么网关里连审批都得异步

**① Hermes 怎么做的**：四种交付（直接回复 / 主频道 / 指定目标 / 跨平台）。
另外一条容易被忽略：**cron 任务的投递不会镜像进网关会话历史** —— 它们只活在自己的 cron 会话里。

**② 读 Hermes 的代码**：`gateway/delivery.py`；cron 不镜像的理由是**消息角色交替**
（把后台产出塞进用户会话会让两条同角色消息相邻 —— 第 2 章那条不变量）。

**③ mini 怎么做的**：`gateway/delivery.py`（63 行）实现两种：直接回复（回来源）与指定目标
（`platform:chat_id`）。目标必须**自带平台**（不允许只给一个 chat id，否则投递要靠猜平台，
猜错 = 把消息发给另一个平台的同号聊天）；没有该平台适配器就**报错**（不静默丢弃）。

**网关里的"确认"必须异步** —— 这是 mini 做出来才看清的一条：

```text
agent 要跑一条危险命令
  → approval_callback 发一条消息出去："🔐 需要确认：<命令>，回复 /approve 或 /deny"
  → 用 threading.Event 等（最多 approval_timeout 秒）
  → 用户的下一条消息 /approve 命中 runner 的 _resolve_approval → 放行
```

**为什么不能像 CLI 那样阻塞着问**：网关**没有 TTY**。真实版为此有整套
`approval` 表 + `delivery_obligations`（"这条确认消息必须被送达"是一笔要记账的债）。
mini 用 `Event` + 超时（超时按**拒绝**处理，不是放行）。

**④ mini 为什么没做成这样**：主频道投递需要"主频道"配置 + cron 这类后台产出的消费者；
跨平台投递在 mini 里退化成"指定平台"（registry 里只有一个适配器）。
真实版的 `delivery_obligations`（投递债记账）mini 没做 —— 代价是：
**网关崩在"确认消息已发、回答还没来"的中间状态时，那笔确认就无声消失了**（进程内状态）。
做持久化投递债需要一个"待办"表，mini 现在没有消费者会去读它。

---

## §七 钩子：扩展点，与被忽视的"解析时机"

**① Hermes 怎么做的**：钩子 = `<profile home>/hooks/<名字>/{HOOK.yaml, handler.py}`。
出厂**一个都没有**（`builtin_hooks/` 只有 `__init__.py`，`_register_builtin_hooks()` 是空壳桩）——
它是**扩展点**，不是"已经有什么东西"。

**② 读 Hermes 的代码**（`gateway/hooks.py:89-107`，逐字）：

> ```python
>     def discover_and_load(self) -> None:
>         """Register built-in hooks, then load every valid hook dir under the active profile's …"""
>         self._register_builtin_hooks()
>         hooks_dir = _resolve_hooks_dir()
>         if not hooks_dir.exists():
>             return
>         for hook_dir in sorted(hooks_dir.iterdir()):
>             if not hook_dir.is_dir():
>                 continue
>             try:
>                 loaded = _load_hook_dir(hook_dir)
>             except Exception as e:
>                 print(f"[hooks] Error loading hook {hook_dir.name}: {e}", flush=True)
>                 continue
> ```

★ 两个细节：

1. `_resolve_hooks_dir()` 在**调用时**解析 —— 多 profile 时 import 期看到的 home 永远是启动 profile 的；
2. **坏钩子不致命**：加载失败 `continue`（还打印原因），运行时单个钩子抛异常不影响别的钩子、也不影响网关主流程（钩子是**用户代码**）。

事件表（`:5-6`，8 个）：`gateway:startup` / `session:start` / `session:end` / `session:reset` /
`agent:start` / `agent:step` / `agent:end` / `command:*`。

**③ mini 怎么做的**（`gateway/hooks.py`，126 行）：同布局；7 个事件（6 个同名 + **新增 `gateway:tick`**，
因为 mini 没有 `agent:step` 的消费者、却有一个空壳维护 tick）；`HOOK.yaml` 用自带 3 行解析器
（零依赖）；`discover_and_load()` 返回加载到的名字、把失败记进 `errors`（可观测）；
`emit()` 逐个 `try/except`。

**④ mini 为什么没做成这样**：`command:*` 通配事件（真实版可以订阅"所有命令"）mini 只支持精确
`command` —— 通配匹配要一套模式匹配，mini 就一个消费者形态，**等真有人要"审计所有命令"时再加**。

---

## §八 进程身份：PID + 启动时间指纹

**① Hermes 怎么做的**：这是本章最"反直觉"的一节 —— 判"网关活着吗"需要**两件事**，
因为只看一件事的两种错都被踩过。

**② 读 Hermes 的代码**（`gateway/status.py:1112-1135`，逐字，这段话信息量极大）：

> ```python
> def live_gateway_pid_for_home(home: Path) -> Optional[int]:
>     """Verified PID of the gateway owned by ``home`` (pid file + runtime lock first, then the runtim
>     status record), or None. Every reader of another home's gateway identity goes through this so
>     they all prove the same thing: the PID passes the start-time reuse guard, its live command line …
>     a gateway's belonging to ``home``, and the record is not ``stopped``. Bare PID existence is not
>     identity -- a stale record whose PID was recycled by an unrelated process lent it ``served_profi…
>     and put phantom gateways into the update inventory (#109680) -- while a launch-service gateway w…
>     ``gateway.pid`` was unlinked is still live (#110166). Never unlinks ``home``'s identity files."""
> ```

拆成三条可迁移的教训：

| 教训 | 出处 |
|---|---|
| **"PID 存在" ≠ "那是我的网关"** | 号被回收 → 把**别人的进程**当成自己的（`#109680`：幽灵网关进了更新清单） |
| **文件不在 ≠ 死了** | launchd 启动的网关 `gateway.pid` 被删掉，但进程**还活着**（`#110166`）→ 第二判据：`gateway_state.json` |
| **读方不清理** | `Never unlinks home's identity files` —— 读的代码不许删文件（否则一个诊断命令就能把活网关的"身份"抹掉） |

退出码那半边（`gateway/restart.py:14/:61`）：`GATEWAY_FATAL_CONFIG_EXIT_CODE = 78`（`EX_CONFIG`）、
`is_global_startup_conflict()` 认 `*_lock` / `lock_conflict` 两族错误码。
**同一个错误，时机不同，处置相反**：启动时 = 配置冲突（停放 + 退 78，让监督者别重启）；
运行中途 = 可重试的瞬时故障（持有者退出后自动恢复）。

**③ mini 怎么做的**（`gateway/status.py`，225 行）：

- `process_start_fingerprint(pid)` = `ps -o lstart= -p <pid>`（macOS/Linux 都有）；
- `write_pid_record` / `get_running_pid`（记录 + 存活 + **指纹一致** + home 一致）；
- `write_runtime_status` / `runtime_status_running_pid`（第二判据）；
- `live_gateway_pid_for_home()` = **唯一入口**（真实版那条纪律：所有读者都走它，才能"证明同一件事"）；
- `looks_like_gateway_command_line()`：按**完整 token** 匹配（真实版的明文禁令：
  **绝不用 argv 子串推断进程身份** —— 那是十几起"更新时杀错进程"事故的根因）；
- 单实例冲突 → 写 `startup_failed` + `conflict_with` + **返回 78**。

**④ mini 为什么没做成这样**：**监督者映射**没做（systemd `RestartPreventExitStatus=78` /
s6 finish→125 / launchd `KeepAlive.SuccessfulExit=false`）—— mini 不装服务，
所以退出码 78 目前**没有消费者**。它仍然值得存在：**退出码也是接口**，
将来谁写个 wrapper/plist，语义已经定好了。

---

## §九 profile 作用域：一个进程服务多 profile（第 6 章欠的债）

**① Hermes 怎么做的**：一个进程可以服务多个 profile（`gateway.multiplex_profiles: true`）。
难点在于：`os.environ`、模块全局量、import 期算出的常量里，装的**永远是启动那个 profile 的值**。
于是每一个"替次级 profile 干活"的动作都必须**显式绑定作用域**。

**② 读 Hermes 的代码**（`gateway/run.py:1751-1780`，逐字，这是本章第二段最该记住的注释）：

> ```python
> def _profile_runtime_scope(
>     profile_home: "Path", prepared_secret_scope: Optional[dict] = None, *,
>     hydrate_secrets: bool = True):
>     """Scope config/skills/memory AND credentials to a profile for one turn (multiplexed path only).
>     ``set_hermes_home_override`` is a contextvar (reaches the agent worker via ``copy_context()``);
>     ``set_secret_scope`` makes the profile ``.env`` the credential source without mutating
>     ``os.environ``, so subprocesses never inherit cross-profile secrets."""
>     ...
>     # Install the routed profile's COMPLETE terminal policy, never ambient TERMINAL_* a prior turn s…
>     # Without it terminal_tool reads the process-global TERMINAL_* vars a previous profile's turn ma…
>     # pinned (first-writer-wins backend leak; #68559).
> ```

三条要点：

1. **home 用 contextvar，不用 `os.environ`** —— 并发跑的两个回合各自看到自己的 home；
2. **密钥用作用域，不写 `os.environ`** —— 于是**子进程永远不继承**跨 profile 的密钥；
3. **终端作用域要"整套安装"**，不能沿用上一轮留下的 `TERMINAL_*`（first-writer-wins 泄漏，#68559）。

配套的 fail-closed（`agent/secret_scope.py:27/:52`）：只有 `set_multiplex_active(True)` 之后，
**没有作用域**的密钥读取才会抛 `UnscopedSecretError`。单 profile 时不抛（那时 `os.environ` 是对的）。

还有一张**绑定表**（哪些执行点必须绑）：真实版列了 7 类 —— 被路由的回合、agent 释放/淘汰、
关闭、回合后媒体投递、cron 滴答、子进程、后台线程。mini 的对应清单见差异表 #14。

**③ mini 怎么做的**：

- `hermes_constants.py`：`_HOME_OVERRIDE` contextvar + `set/reset_mini_home_override`，
  `get_mini_home()` 解析顺序 = **作用域 → `MINI_HOME` → 默认**；
- `agent/secret_scope.py`（91 行）：`set_secret_scope` / `get_secret` / `UnscopedSecretError` /
  `set_multiplex_active` / `build_profile_secret_scope`（读 `.env` 成**只读字典**，不碰 `os.environ`）；
- `gateway/profile_scope.py`（74 行）：`profile_runtime_scope(home)` 上下文管理器
  （绑 home + 绑密钥，**复位顺序与绑定相反**，异常路径也不留残留作用域）+
  `served_profile_child_env(home)`（给子进程：**摘掉** `MINI_HOME`/各种 `*_API_KEY`，再钉上目标 home）；
- `hermes_cli/profiles.py`（56 行）：`profiles_to_serve(multiplex)`，默认 profile **永远排第一**；
- `gateway/run.py::_db()` 用 `get_mini_home()` 而不是 `self.home`：
  **作用域绑好之后，"替谁干活"这件事就不用传参了 —— 于是也没法传错**。

**A→B→A 证明**（真实版 AGENTS.md 明确要求的验证方式，一个临时 home 证明不了作用域）：
`tests/test_gateway.py::test_profile_scope_aba` —— A 作用域下 home 与密钥都是 A、B 下是 B、
**回到 A 还是 A**（残留作用域 = 那个静默的 bug 类）。

**④ 入口层（侧线，已做）**：光有作用域基座还看不见"多 profile"——还得能**选**。
第 7 章侧线补了最小可用的一层（+315 行）：

| 动作 | 实现 | 与真实版的对照 |
|---|---|---|
| 选：`mini -p lab` | `main.py` 在**构造 MiniCLI 之前**把 `MINI_HOME` 钉到那个 home | 真实版 `-p` 就是 `--profile`（`hermes_cli/_parser.py:16`）；mini 以前把 `-p` 给了 provider，侧线让回来（provider 保留 `--provider`） |
| 建：`mini profile create lab` | 建 home + `logs/` `hooks/`（mini 真会读写的那两个）+ **播种空 `.env`** | 真实版建 9 个目录（`:32`）；"从第一天起拥有自己的凭据文件"（`:74-78`） |
| 克隆：`--clone` | 拷 `.env` / `SOUL.md` / `config.json`，**不拷 `MEMORY.md`** | 真实版 `_CLONE_CONFIG_FILES = ["config.yaml", ".env", "SOUL.md"]`（`:35`）—— **清单里没有记忆文件，这是设计**：克隆只带配置与身份 |
| 看：`list` / `show` | 列出/显示每个 home 的家底 | 真实版这一族更大（create/list/show/delete/rename…） |

三条**故意做严**的地方（每条都配了测试与反向验证）：

1. **id 字母表**：拒绝 `main~`、`default`、空格、中文 —— `~` 不在字母表里是 §一 那招的前提，
   允许建一个叫 `main~` 的 profile 就等于把"不可能撞"变成"可能撞"；
2. **解析不静默回落**：`-p ghost` **报错**并列出已有的 + 给补救命令。
   静默回落默认 home 的后果是"你以为在沙箱里乱来，实际在主 profile 上乱来"，**且不报错**；
3. **clone 不带记忆**（上面第 3 行）。

**④' mini 为什么没做成这样（终端作用域）**：**终端作用域**没做。原因具体：mini 的 terminal 工具
没有"后台/沙箱后端"这个概念（一次 `subprocess.run`，无 `TERMINAL_*` 配置族），
所以没有"上一轮钉住的全局量"可泄漏。**代价**：mini 的 terminal 工具一旦要支持
docker/ssh 这类后端，就必须补上这一层，否则会出现与 `#68559` 同形状的
"第一个 profile 赢了"泄漏（**不报错**）。

---

## §十 单实例与运行时状态（mini 侧的形状）

这一节没有真实版的直接对应物（真实版的单实例靠适配器 **token 锁**：同一个 bot token
被两个 profile 抢时抛 `{scope}_lock`）。mini 只有一个适配器、没有 token，
所以单实例落在 **home 级**：一个 home 同时只允许一个网关进程。

| 环节 | 行为 |
|---|---|
| 启动检查 | `live_gateway_pid_for_home(home)` 有活的外部 pid → 写 `startup_failed` + `conflict_with` + **返回 78** |
| 正常启动 | 写 `gateway.pid`（pid + 启动指纹 + home + cmdline）与 `gateway_state.json`（`running`） |
| 收尾 | 结束会话 → 摘 pid 文件 → 状态 `stopped` |
| 重启 | 已结束的会话**不会**被"找回"（`latest_session_id_for_key` 排除 `ended_at`），于是开新会话 |

**`session_key` 落库**是本章对第 6 章 schema 的唯一改动（`hermes_state_common.py` 加一列 + 一个索引，
走第 6 章的**声明式加列**机制 —— 老库自动补上，不写迁移）。真实版 `sessions` 表里
本来就有 `session_key` 列和 `idx_sessions_session_key`（`hermes_state_common.py:338` / `:659`），
所以这一步是**对齐**，不是发明。

---

## §十一 交付路径与"指定目标"的边界

见 §六。这里补一条 mini 的实现细节：`Delivery.deliver(source, text, target=None)` 返回
**实际用的 `platform:chat_id`**（可断言），并把每次投递记进 `Delivery.sent`（观察面）。
`parse_target("console:c9")` 只认 `platform:chat_id` 形状，其它一律 `DeliveryError`。

---

## §十二 真问题与修法（本章抓出 5 个，全部是**跑出来**的）

### ① 会话键算晚了 → 所有会话共用一把锁（静默）

**症状**：冒烟测试里 `/help` 的结论取不到（`last_result` 是 `None`），debug 打印发现
`_last_results` 的键是 **空字符串**。

**根因**：`build_session_key` 是在 runner 的 `_handle_message()` 里调的，而那是**后台线程**的事；
适配器的**第 1 级守卫**在**调用之前**就要用键来分会话 → 它拿到的是 `MessageEvent.session_key`
的默认值 `""`。后果：**所有会话的守卫键都是空串** → 一个会话忙，全部会话一起忙。

**修法**：键函数（不是键值）**注入**给适配器（`runner._key_for_source`），
适配器在进守卫之前调用它；runner 侧改为 `event.session_key or self._key_for_source(...)`（兜底复用）。
**两边都只调 `build_session_key`，没有任何一方自己拼键。**

### ② 先 drain 再放锁 → 队列永久卡死（静默）

**症状**：无 —— 只是"排队的那条消息再也不被处理"。

**根因**：一轮结束后我的 finally 里先 `_drain_pending(key)` 再 `_release_session_guard(key)`。
被放出来的那条消息调 `handle_message` 时，锁**还握在自己手里**（owner 线程还活着，所以不算陈旧）→
它又被排回队列。**队列就此卡死，且不报错。**

**修法**：顺序反过来 —— **先放锁，再 drain**。代码里留了注释说明为什么不能反
（"反过来的话……队列就此卡死，且不报错"），测试里有一条直接守这条顺序
（反向验证 ② 就是把顺序改回去 → 干净变红）。

### ③ 忙时判定在命令分发之后 → `reject` 命令照样执行

**根因**：我先写"斜杠命令 → 查表分发"，再写"agent 在跑吗"。顺序反了：
`/sessions` 这类 `busy_policy=reject` 的命令在 agent 跑着时**照样执行**。

**修法**：忙时处置**优先于**命令分发（空闲路径才走命令表）；
真实版的形状正是"不在忙时可执行白名单里的命令**只在空闲路径分发**"。

### ④ 一条**死代码**：忙时普通消息的"已排队"回复

**症状**：测试断言"排队时回复了'排队'"失败。

**根因**：我写了"普通消息在忙时回一条'已经排队'"。但**普通消息在第 1 级就被排队了**，
永远到不了第 2 级 —— 那条回复是**不可达的死代码**。
真实语义也不是"回一条排队提示"：新消息**打断旧回合**（这才是用户要的效果），
要看队列用 `/queue`。

**修法**：删掉那条回复；把 runner 侧的分支改成明确的"不该发生"注释 + 一个中性的动作码。

### ⑤ 用量回填"静默写 0" + `/sessions` 少一列

**症状**（**只有真跑才看得出来**）：真跑收据里 `tok in/out = 0/0`；
`/sessions` 显示的会话键是 `-`。

**根因**两个：

- `AIAgent.usage` 的键是 `prompt_tokens` / `completion_tokens`（照 OpenAI 响应字段），
  而存储层的列叫 `input_tokens` / `output_tokens` —— 没做映射就是**静默写 0**；
- `list_sessions()` 的 SELECT 里没有 `session_key` 列（第 6 章写这个函数时还没有这一列）。

**修法**：`_sync_session_meta` 做键名映射（两个名字都认）；`list_sessions` 补列（顺带补 `end_reason`）。
测试里的假 agent 改用**真实键名**，这样那条断言守的是"映射"本身，而不是"随便什么键都能填"。
再跑真跑：`tok in/out = 6065/24`、`/sessions` 显示 `main:console:dm:console-1` ✓。

**这五个的共性**：四个是**静默**错误（不报错、只错数据或卡住），
而"静默"正是第 7 章反复出现的主题 —— 所以它们的防线不是"小心点"，
而是**给每个静默失败配一条测试**（§十三）。

---

## §十三 测试与收据

**测试**：`tests/test_gateway.py`（903 行 / **14 组 / 135 条断言**），双解释器
（`/usr/local/bin/python3` = 3.13.14、`/usr/bin/python3` = 3.9.6）**135 通过 / 0 失败**。

| 组 | 守的承诺 |
|---|---|
| 1 会话键 / 2 命名空间 | 唯一来源 · 布局 · 退路 · `main~`（`~` 不在 profile id 字母表里） |
| 3 授权 | 四层顺序 · **默认拒绝** · 拒绝说得出理由 |
| 4 命令解析 | 规范名/别名/**唯一**前缀（歧义不猜） |
| 5 第 1 级守卫 | 排队 · 命令内联 · **陈旧锁自愈**（含"无 owner 不算陈旧"） |
| 6 第 2 级守卫 | `busy_policy` 三态 |
| 7 顺序不变量 | **忙时处置先于命令分发** |
| 8 会话归属 | 键落库 · 按键找回 · `/new` 之后找回新的 · **全结束 → None** |
| 9 交付 | 直接回复 · 指定目标 · 缺适配器报错 |
| 10 钩子 | 发现 · 分发 · **坏钩子隔离** · 路径按调用时 home 解析 |
| 11 进程身份 | PID+**指纹** · 状态记录双判据 · 全 token 匹配 · **78** |
| 12 profile 作用域 | contextvar · **fail-closed** · 不改 `os.environ` · **A→B→A** |
| 13 profile 枚举 | **HOME 锚定** · `profiles_to_serve` |
| 14 profile 入口层（侧线） | `create` 建什么/播种什么 · id 字母表（**`~` 必须被拒**）· 重名 · `--clone` 带配置**不带记忆** · 解析**不静默回落** · 两 profile **物理分家**（A→B→A）· 真 CLI 子进程 |

**全套**（7 个测试文件 × 2 解释器）：**710 通过 / 0 失败**
（压缩 152 · 网关 99 · 打包 18 · 提示词 155 · 存储 107 · 工具 89 · 循环 54）。

**反向验证 9 项，9/9 干净变红**（"干净"= 目标用例变 ✗ 且不是整个套件崩）：

| 项 | 改回旧写法 | 结果 |
|---|---|---|
| ① 会话键交给后台算 | 适配器不再算键 | 8 条变红 |
| ② 先 drain 再放锁 | 顺序反 | 2 条 |
| ③ 忙时判定挪到分发之后 | 顺序反 | 4 条 |
| ④ 去掉陈旧锁自愈 | 返回 False | 1 条 |
| ⑤ 授权默认放行 | `return False` → `True` | 3 条 |
| ⑥ 去掉 `main~` 特例 | 删掉那两行 | 1 条 |
| ⑦ 不校验启动时间指纹 | 只看"号存在" | 3 条 |
| ⑧ 多路复用不 fail-closed | 回落 `os.environ` | 1 条 |
| ⑨ 找回会话不排除已结束 | 去掉 `ended_at IS NULL` | 2 条 |

**侧线（profile 入口层）另做 4 项，4/4 干净变红**：

| 项 | 改回旧写法 | 结果 |
|---|---|---|
| ① 解析静默回落默认 home | 加一句 `return default_home()` | 2 条 |
| ② `create` 不校验 id | 删掉 `validate_profile_name()` | 6 条 |
| ③ `-p` 解析了但不用 | 把设 `MINI_HOME` 那行改成 `pass` | 1 条 |
| ④ `--clone` 顺手带走记忆 | `CLONE_FILES` 里加 `MEMORY.md` | 1 条 |

> 第 ① 与第 ③ 项各暴露了一个**验证脚手架问题**（跟第 7 章 ⑨ 同类）：
> ① 的期望串写成了"异常分支"的断言名，而实际失败的是"没抛异常"那条；
> ③ 最初只断言了"子进程没报错"，而**没报错不等于 home 换了** ——
> 补了一条"在本进程跑 `main(["-p","lab",…])` 后 `MINI_HOME` 等于 lab 的 home"才算真的测到。
> **反向验证第二次证明自己不只是"检查测试会红"，它还会告诉你测试测偏了。**

> 第 ⑨ 项的过程值得记：第一次改法是让 SQL 少一个绑定参数 → 12 条变红**但那是崩**（不可诊断）；
> 改成"去掉过滤条件本身"才拿到干净的红。同时给测试补了一条**真正覆盖这个机制**的断言
> （"所有会话都已结束 → 找回必须是 None"）—— **原来的断言其实测不到这个过滤器**。

**真跑**（真 DeepSeek，走真实 CLI 路径 `./mini gateway run`，stdin 喂 6 行）：

```text
you> 用一句话说明你现在是通过哪个平台在跟我说话。
  你现在是通过 mini-hermes 的 console（终端命令行）平台在跟我说话。
you> 我刚才问了你什么？一句话回答。
  你问我「你刚才问了我什么」，让我一句话回答。      ← 历史从库里重建（per-turn 新建 agent 但对话不断）
you> /status
  会话键 main:console:dm:console-1 · 会话 id 20261008_123808_7b174d · 消息数 4 · agent 空闲 · 排队 0
you> /sessions
  ● 20261008_123808_7b174d  main:console:dm:console-1  4 条
you> /new
  🆕 新会话已开始（20261008_123954_7067ac）
you> 新会话里回我一个字：好
  好
```

收据：退出码 0 · 会话行 2 条（`end_reason` = `reset` / `gateway_stop`）· 消息行 6 条 ·
用量 `6065/24`、`3030/1`（in/out）· `messages_fts` 行数 = `messages` 行数（索引同步）·
钩子触发顺序完整：`gateway:startup, gateway:tick, session:start, agent:end, agent:end,
command:status, command:sessions, command:new, session:end, session:start, agent:end, session:end` ·
pid 文件已摘掉 · 状态 `stopped` · `live_gateway_pid_for_home` = None。

---

## §十四 差距清单（本章未做，每条带代价）

| # | 未做 | 为什么没做 | 代价（什么时候必须补） |
|---|---|---|---|
| 1 | **22 个平台适配器 + 延迟加载** | 零依赖框架 + 需要各家 SDK 与真实凭据 | 没有真实平台接入；"平台领域知识各自成文件"这条规模教训只能靠文档记 |
| 2 | **relay / connector 契约**（出站 WebSocket 通用适配器） | 需要 connector 端 | 无 |
| 3 | **asyncio 版守卫** | mini 是线程模型（第 2 章的 CLI 也是） | 高并发/多平台时线程模型的伸缩性不如 asyncio；语义已对齐，换实现不影响契约 |
| 4 | **私聊配对 / `dm_policy` 四态** | 单用户单平台，配对的消费者不存在 | 开多人使用前必须补，且必须保持"默认拒" |
| 5 | **主频道投递 / 跨平台投递** | 需要"主频道"配置 + cron 消费者；registry 只有一个适配器 | 有后台产出（cron/心跳）时必补 |
| 6 | **`delivery_obligations`（投递债记账）** | 需要一张待办表 + 消费者 | **网关崩在"确认消息已发、回答未到"之间 → 那笔确认无声消失**（进程内状态） |
| 7 | **网关侧会话维护（85% 兜底压缩）** | mini 的压缩在 agent 内部（第 5 章），且 mini **每条消息新建 agent**（没有"缓存 agent 要淘汰"这回事） | 长会话 + 网关的兜底保护缺失；真实版这道防线服务的是"两轮之间长起来的会话" |
| 8 | **跨进程压缩锁**（`compression_locks` / `session_turn_leases`） | mini 单进程 | 支持多进程/多实例时必补，否则两个进程会同时压同一个会话 |
| 9 | **终端作用域（`TERMINAL_*` 整套安装/复位）** | mini 的 terminal 工具无后台后端概念 | 加 docker/ssh 后端时必补，否则出现 `#68559` 同形状的"第一个 profile 赢了"泄漏 |
| 10 | **反向映射 `profile_from_session_key_namespace()`** | 没有消费者（缺只读诊断面） | 诊断"这个会话属于哪个 profile"时要补（约 10 行） |
| 11 | **`command:*` 通配钩子事件** | 只有一个消费者形态 | 要"审计所有命令"时补 |
| 12 | **监督者映射（systemd/s6/launchd 对 78 的处置）** | mini 不装服务 | 退出码目前无消费者；语义已定好（**退出码也是接口**） |
| 13 | **多 profile 的 YAML 桥（`apply_yaml_bridge`）** | mini 的平台配置就是一个 dict | 有平台 YAML 配置且多路复用时必补（否则次级 profile 污染进程环境） |
| 14 | **profile 的 delete / rename / 批量迁移** | 删一个 home 是破坏性操作，mini 没有回收站/确认流程（用户 `rm -rf` 比我们写一个更清楚）；rename 还要处理"库里已有的键指向旧名字" | 要做运维级功能时补；真做 delete 前必须先想清"删掉的会话去哪" |

**mini 反过来多做的三件**（真实版没有对应物）：① `gateway:tick` 钩子事件（维护 tick 的形状）；
② `console` 适配器让全部机制**真能端到端跑**；③ `_key_for_source` 的**函数注入**形状
（真实版是把手写的键传给底层，mini 把"唯一的键函数"注入进去，结构上更难写错）。

---

## 附录 A · 代码位置对照（mini → 真实）

| mini | 真实 Hermes |
|---|---|
| `gateway/run.py`（门面 + 装配 + 生命周期） | `gateway/run.py`（门面，"继承 15 个 mixin"那层） |
| `gateway/run_inbound.py` | `gateway/run_inbound.py` + `gateway/run_busy.py` |
| `gateway/run_turn.py` | `gateway/run_turn.py`（真实侧还拆出 4 个兄弟：followup_ack / runner / approval_settle…） |
| `gateway/run_shutdown.py` | `gateway/run_shutdown.py`（真实版收尾函数名是 `_finalize_session_off_loop`，见精译-07 附录 C ①） |
| `gateway/session_keys.py` | `gateway/session.py`（`build_session_key` / `_session_key_namespace` / `SessionStore`） |
| `gateway/authz.py` | `gateway/authz_mixin.py` |
| `gateway/delivery.py` | `gateway/delivery.py` |
| `gateway/hooks.py` | `gateway/hooks.py` |
| `gateway/status.py` | `gateway/status.py`（另有 `gateway/restart.py` 的退出码判定） |
| `gateway/adapters/base.py` | `gateway/platforms/base.py`（`BasePlatformAdapter`） |
| `gateway/adapters/console.py` | `plugins/platforms/<name>/adapter.py`（22 个之一） |
| `gateway/profile_scope.py` | `gateway/run.py::_profile_runtime_scope` + `agent/secret_scope.py` + `tools/terminal_scope.py` + `tools/environments/local.py::served_profile_child_env` |
| `hermes_cli/commands.py` | `hermes_cli/commands.py`（`CommandDef` / `resolve_command`） |
| `hermes_cli/profiles.py` | `hermes_cli/profiles.py`（`profiles_to_serve`） |
| `hermes_cli/gateway_cmd.py` | `hermes_cli/gateway*.py`（CLI 侧一族） |
| `hermes_cli/profile_cmd.py` + `profiles.py` | `hermes_cli/profiles.py`（create / list / show / `--clone` / id 字母表 / HOME 锚定） |

## 附录 B · 上游文档出入

上游 `gateway-internals.md` 本章只核出 **1 处**（函数名 `_finalize_session` vs
源码的 `_finalize_session_off_loop`），另有 2 条"文档没写但值得知道"的实测数字 ——
详见《中文精译-07-GatewayInternals.md》**附录 C**（不在此重复，避免两处不一致）。

## 附录 C · 前几章挂过来的三笔债的处置

| 债 | 来自 | 处置 |
|---|---|---|
| **多 profile 多库** | 第 6 章差距清单 #1（"支持多 profile 时必须补，否则静默串库"） | ✅ **本章还了**：`profile_runtime_scope` + `_db()` 走 `get_mini_home()` + `profiles_to_serve` + A→B→A 测试。仍缺终端作用域（§十四 #9） |
| **跨进程压缩锁** | 第 5/6 章 | ⬜ 未做（mini 单进程）→ §十四 #8 |
| **网关的输入归属**（`observed` / `platform_message_id` / 异常写入方仲裁） | 第 6 章差距清单 #6 | ⚠️ **部分**：本章有了"第 1/2 级守卫 + 授权"，但**没有**把"这条消息是谁写的"记进库（真实版的 `observed` 列用于"压缩期间来了新消息"的仲裁）。代价：将来做压缩轮转仲裁时必补 |

## 本章提交

```text
9015f61   第 7 章开工：中文精译-07-GatewayInternals.md（636 行）+ README 进度表
858d1f2   第 7 章：网关实现 + 源码讲解 + 侧线（多 profile 入口层）
          ← 标签 ch07-gateway 指这里（本章文档后续修改会让标签往后移）
```

**本章的产出清单**（对照上游同名文档的 297 行）：

| 交付物 | 位置 | 规模 |
|---|---|---|
| 中文精译 | `中文精译-07-GatewayInternals.md` | 636 行（含附录 A 术语表 24 条 / C 上游出入） |
| 源码讲解（本篇） | `源码讲解-07-GatewayInternals.md` | 917 行（导言 + 差异表 17 + 逐机制四步 + 真问题 5 + 差距 14） |
| 实现 | `gateway/` + `agent/secret_scope.py` + `hermes_cli/{commands,profiles,profile_cmd,gateway_cmd}.py` | 新增 2,345 行 |
| 测试 | `tests/test_gateway.py` | 903 行 / 14 组 / 135 条 |
| 侧线 | 多 profile 入口层（`-p` / `profile create\|list\|show`） | 含在实现行数里 |
