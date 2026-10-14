# 中文精译 · 第 7 章 · 扩展 7.2（旧称第 9 章）：多路复用网关（Multiplexing Gateway）

> **原文**：`website/docs/developer-guide/multiplexing-gateway.md`（228 行，官方 Developer Guide **第 11 位**，
> 紧跟第 8 章的 `gateway-session-lifecycle`）。
> **本篇定位**：翻译为主，目标是**看懂原理**。差异对比、源码深挖、实现日志都不在这里 ——
> 那些属于后面两个阶段（mini 实现 / 源码讲解）。
> 看不懂的地方或想深挖的点，随时问，我再往这里补。
>
> 📌 行号锚在上游 commit **`f5d1926110`**（`version = "0.21.3"`）；上游每合并一个 PR 行号就会漂。
> 译文里凡是**与源码不符**的地方，用 `⚠️ 上游文档出入` 标出来（本章已发现 **6 处**，见附录 C）。
> 🔗 各节原本附的 `【源码】要点` 已整体搬进《源码讲解-07c-MultiplexingGateway.md》（节号一一对应）；本篇只做翻译。
> 🧭 原文是**维护者设计说明**（通篇模块名/类名/issue 号），所以译文**先给白话**，
> 逐字标识符与 issue 号收进《源码讲解-07c》的【源码要点】；译文只把 issue 号降为「出处」一行。

> mini 当前维护说明见对应《源码讲解》的“发布前维护”节；原文翻译与历史实测不代替当前实现契约。

[TOC]

---

## 0. 这一章在回答什么

**它要解决的问题**：一开始，`hermes gateway` 这个常驻进程**只能服务一个 profile**。
"profile" = 一套自己的家（配置 / 密钥 / 会话 / 记忆 / 技能），所以你要是有三个 profile
（比如"工作 / 个人 / 给客户的那个 bot"），就得**同时跑三个网关进程**。

**它改成了什么**：一个进程服务**所有** profile —— 默认 profile + 每个具名 profile
各自有自己的适配器、密钥、会话、cron 心跳，但**共用**一个事件循环、一个 HTTP 监听口、
一把进程锁、一个状态面。

**一句话记法**：**"一个管家同时管几套房"** —— 钥匙串分开、账本分开、门牌分开，
但前厅和门卫只有一个。

**它最硬的一条约束**（后面所有设计都是为它服务的）：

> **profile A 的回合，永远不许看见 profile B 的状态。**

凡是**暂时做不到隔离**的东西，要么**失败关闭**（宁可报错不静默读错值），
要么在文末的「已知限制」里**点名写清**。

**一句话说清"多路复用"这个词**：同一个进程，**按来源/身份把请求分派到不同的 profile**，
就像一路电话线里复用出很多路通话。它隔离的是 **profile（配置）**，不是"人"（见 §13）。

---

## 1. Overview —— 一进程 vs 一 profile

不开多路复用的时候：一个网关进程**恰好服务一个 profile** —— 它的 `.env`、会话、技能、
平台适配器都是那一个的；多 profile 的安装方式就是**每个 profile 起一个进程**。

多路复用把这件事**收进一个进程**：默认 profile 加上每一个"被服务的"具名 profile，
各自拿到自己的适配器、密钥、会话和 cron 心跳；同时**共用**一个事件循环、一个 HTTP 监听、
一把进程锁和一个状态面。

**原文指出**：下面所有设计的出发点，就是那条硬约束 —— **A 的回合不许看见 B 的状态**。
密钥、home、会话、适配器 lane 都按 profile 隔离；**暂时隔离不了的，要么失败关闭，
要么在「已知限制」里点名**。

砍掉"一进程多 profile"（也就是永远一 profile 一进程）会怎样？**资源**上：N 个 profile =
N 个 Python 网关进程、各自的平台连接与 cron 调度会增加资源开销；具体内存和容量需测量，不能直接等同于 N 个 Chromium 进程；
**操作**上：加一个 profile 要动 supervisor 配置、要重启别的东西。
但反过来说，**一进程多 profile 的代价正是这一章全文**：所有"进程级"的东西
（环境变量、监听口、注册表、发现槽位）都会变成**跨 profile 的共享状态**，
每一处都得单独处理。所以这不是"更省事的架构"，是**"用隔离纪律换资源"**。

---

## 2. The mode flag —— 开关与它的三道来源

开关的配置项是 `gateway.multiplex_profiles`（也接受写在顶层），**默认开着**；
解析顺序是 **环境变量 > 配置文件 > 未设置**。

- 配置对象 `GatewayConfig` 把"未设置"保留成 `None`（读它的人按真值判断，所以读起来像"关"）。
  真正的判决发生在**启动时**：`resolve_multiplex_mode` 跑一遍
  `hermes gateway migrate` 的**预检**，然后给出启动判决 ——
  安静的"多 profile 默认安装"给 `True`；否则给 `False` **并记下原因**。
  显式写的值原样透传；**注入进来的 config 不参与判决**。
- **别的进程**读这个开关的方式是："先读**活着的那个网关**记录的 `served_profiles`，
  再读显式开关"，**绝不读合并后的默认值**（否则"其实还没迁移"会被读成"已经在多路复用"）。
- 环境变量覆盖 `GATEWAY_MULTIPLEX_PROFILES` **只接受明确的真/假词**；
  **空白或无法识别 = "没有覆盖"** —— 这样"部署时提供了但值为空"的密钥**不会**把
  配置文件里的明确选择盖掉。
- 启动时 `GatewayRunner.__init__` 调一次 `set_multiplex_active(...)`。它是个
  **模块级全局量，不是 contextvar** —— 它描述的是"这个部署的模式"，不是"某个任务的值"。
  它唯一的职责是**把 `get_secret()` 的失败关闭行为武装起来**。

三道来源（env / config / 预检）看着啰嗦，但它们对应**三种不同的人**：
运维（要能一键覆盖）、用户（配置文件里的偏好）、程序（自己判断这台机器能不能跑）。
砍掉预检、直接默认开，会出现最糟的一种：**安装里还有一个 profile 自己跑着网关**，
两个进程抢同一个 bot token / 同一个端口 —— 可能被锁冲突拒绝、连接失败，或出现平台侧竞争；预检用于提前识别冲突。

---

## 3. Scope composition —— 两个作用域是怎么套上去的

每一个入站事件，在**任何 profile 自己的代码跑起来之前**，都会先"合成"两个
**context-local（上下文局部）作用域**：

```text
平台事件
   │
   ▼
profile_routes 命中 ──► 服务集合检查 ──► SessionSource.profile 盖章
   │                                      （gateway/profile_routing.py）
   ▼
_profile_runtime_scope(profile_home)      （gateway/run.py）
   ├── set_hermes_home_override(home)     config / state.db / 技能 / 记忆 / 会话 都从这里解析
   └── set_secret_scope(profile 的 .env + 外部密钥源)
   │                                      provider 密钥、平台 token
   ▼
agent 回合（工作线程，靠 copy_context() 继承）
   │
   ▼
finally 里按相反顺序解开
```

`_profile_runtime_scope` 包住**每一处 profile 自己的代码会执行到的缝**：
二级适配器的启动/连接/重连、主平台事件处理器、入站预处理、`/model` 与会话信息解析、
后台任务、以及 agent 回合本身。配置重载**跑在默认 profile 的作用域下**，
好让"全局网关设置"解析得一致。

**两个作用域都是 `contextvar`** —— 所以它们会通过 `copy_context()` **传播进线程池的工作线程**，
并且**确定性地解开**。这两个运行期作用域管理器不改写共享的 `os.environ`；无作用域的启动期加载另有规则（见 §4）。

为什么不干脆"进事件时 `os.environ.update(profile_env)`"？
因为 `os.environ` 是**进程级**的：并发处理两个 profile 的事件时，
A 更新完、B 又更新，**两边都会读到 B 的值**；而且 `subprocess` 默认继承 `os.environ`，
所以子进程也会拿到**别人的密钥**。contextvar 之所以对，是因为它**天然按任务隔离**：
同一个线程里的不同任务各有各的上下文，解开还确定。

---

## 4. Workstream A —— 上下文局部的密钥作用域

`agent/secret_scope.py` 存在的原因很直白：**显然的写法**（把每个 profile 的 `.env`
**并进 `os.environ`**）会把 A 的密钥泄漏进 B 的回合，以及**每一个用
`env=dict(os.environ)` 起的子进程**。

它的三个入口：

- `build_profile_secret_scope(home)` —— 把该 profile 的 `.env` 与它配置的**外部密钥源**
  合并成一份映射（**跳过全局项**）。
- `set_secret_scope(mapping)` —— 为**当前任务**装上它。
- `get_secret(name)` —— 解析顺序：**全局白名单 → 当前作用域 → 兜底**。
  兜底才是承重的那部分：
  - **多路复用关**：读 `os.environ` —— 单 profile 网关与所有非网关调用方的行为**和以前完全一样**；
  - **多路复用开、且没装作用域**：**抛 `UnscopedSecretError`**，而不是静默读进程环境。
    这样一处"忘了迁移"的调用点会在**那一行**响亮地失败，而不是泄漏另一个 profile 的值。

原文只说"一份**小**白名单（`HERMES_HOME`、`HERMES_PROFILE`、代理设置、`API_SERVER_*` 监听设置 ——
**但故意不含 `API_SERVER_KEY`**）保持全局"。源码里的真实规模是 **36 个精确名 + 3 个前缀**，
分成五族（都描述**进程/部署**，不是某个 profile）：**进程身份**（`HERMES_HOME`、`HERMES_PROFILE`、
`HERMES_GATEWAY_LOCK_DIR`、`HERMES_MAX_ITERATIONS`、`HERMES_API_TIMEOUT`、`HERMES_REDACT_SECRETS`、
`HERMES_NOUS_TIMEOUT_SECONDS`、`_HERMES_GATEWAY`）· **shell/解释器**（`PATH`、`HOME`、`USER`、`LANG`、
`LC_ALL`、`TZ`、`PWD`、`SHELL`、`TMPDIR`、`VIRTUAL_ENV`、`PYTHONPATH`、`SSL_CERT_FILE`）·
**看板路径**（`HERMES_KANBAN_*`）· **监听设置**（`API_SERVER_ENABLED/HOST/PORT/CORS_ORIGINS`）·
**relay 路由戳**（`GATEWAY_RELAY_*` 九个）。前缀三个：`HERMES_KANBAN_`、`HERMES_TELEGRAM_`
（**调参用，不是 token**）、`TERMINAL_`。
**凭据一律不在名单里** —— 源码注释逐字：*API_SERVER_KEY is a credential: NOT here.*；
同一段还写着 relay 的 `GATEWAY_RELAY_SECRET/_ID/_DELIVERY_KEY` 与 `IDP_*` **刻意保持按 profile 作用域**。

**原文还澄清了三处相邻的 `os.environ` 缝**（都被同一条"作用域权威"规则接管）：

1. **轮转凭据的拾取路径**：多路复用下"每回合重读 `.env`"是**空操作**，凭据更新靠
   **下一回合的 profile 作用域**拾取，**永不经过 `os.environ`**。这条规则在**加载器边界**也成立：
   `load_hermes_dotenv` 在"多路复用开着**且**装了 profile-home 覆盖"时**跳过进程级加载**
   （import 期与 cron 调用方会在回合中途撞到它），同时仍把外部密钥源灌注进**该 profile 私有的快照**。
   **没有作用域的启动期加载不变。**
2. **配置里的 `${VAR}` / `${env:VAR}`**：装了作用域时通过 `get_secret` 解析。
3. **作用域下的 `.env` 写入**（`save_env_value`，例如配对授权的镜像写入）：
   **更新已装的作用域映射**，而不是进程环境。

这是整章**最能体现"失败关闭"价值**的一段。同一件事有两种失败方式：

| 做法 | 忘了迁移一处调用点时的表现 |
|---|---|
| 合并进 `os.environ` | **静默**读到**另一个 profile 的密钥** —— 可能是把 A 的账单算到 B 头上，或者让 B 用 A 的 token 发消息 |
| contextvar + **漏了就不许读** | 在那**一行**抛 `UnscopedSecretError`，栈里直接指出是哪个调用点没迁移 |

**"响亮地坏掉"胜过"安静地错"** —— 尤其是密钥这种错了也不报错的东西。

---

## 5. The HERMES_HOME override —— 让"家"也跟着 profile 走

`hermes_constants.py` 里有一个**上下文局部的覆盖**，`get_hermes_home()` 会**先查它、
再查 `HERMES_HOME` 环境变量**。于是**所有通过它解析路径的东西**都会自动跟着当前 profile 走：
配置、`state.db`、技能、记忆、SOUL、会话、看板、目标、插件发现、MCP 启动。

另有两个出口处理例外：

- `get_process_hermes_home()` —— 给**少数"机器级、不该跟着覆盖走"**的资源用；
- `hermes_home_key()` —— 给"按 home 分桶的注册表"一个**稳定的作用域键**。

还有一条**一次性告警**：如果**该有覆盖的地方**却在没有覆盖的情况下跑了 profile 自己的代码，
就警告一次（同一条不会反复刷）。

这一条是**"隔离"能否成立的地基**：密钥隔离只解决"别读错 key"，
但**会话、记忆、技能、日志**都是**按路径**找家的。只要有一条路径解析走的是
`os.environ["HERMES_HOME"]`（进程级），那个 profile 的数据就会**落到默认 profile 的家**里 ——
而且**不报错**（文件建出来了、内容也对，只是**在别人的家里**）。
`hermes_home_key()` 的存在也说明同一件事：**凡是"按 home 分桶的注册表"，
都必须用同一个稳定键**，否则"同一个 home 在两个注册表里被算成两个"。

---

## 6. Inbound routing —— 把共享 bot 收到的消息分给正确的 profile

`gateway/profile_routes` 把一组坐标 —— **(平台, 服务器/公会 id, 聊天 id, 线程 id)** ——
映射到一个 profile。匹配规则三条：

1. **合取**（所有列出来的字段都得对上，不是或）；
2. **最具体的先赢**（写了更多字段的规则优先）；
3. 线程（thread）会**顺着父聊天链**向上匹配（话题挂在频道下时也能命中）。

> ⚠️ **上游文档出入**：第 3 条这句「**向上**匹配」容易读成**多级回溯**；源码里是**单跳** ——
> 只把入站的 `(chat_id, parent_chat_id)` **两个值放进一个元组**做成员判断，**没有任何循环**
> （附录 C ⑥；`parent_chat_id` 本身也只取一跳）。

**只有多路复用开着时才会路由**。而且：一条命中的规则**如果目标不在"被服务的集合"里，
事件会被拒绝（丢弃），而不是投错地方**。

完整 schema 与匹配规则在用户向文档里：
[Routing shared-bot chats to profiles](https://hermes-agent.nousresearch.com/docs/user-guide/multi-profile-gateways#routing-shared-bot-chats-to-profiles-profile_routes)。

"共享 bot"是多路复用最现实的场景：**同一个 Telegram bot 同时给两个 profile 干活**
（比如团队群里 @ 一次服务"工作"，另一个群里服务"个人"）。没有路由表，
你只能靠"一个人一个 bot"，那就回到了多进程。
而"**目标不在服务集合里 → 丢弃**"这条选择很值得记：**丢弃是可以解释的**
（日志里说得出"命中了但没服务"），**投错是不可解释的**（消息跑到别人 profile 里，
两边都不知道发生过什么）。

---

## 7. Serving selected profiles —— 服务集合的唯一闸口

`profiles_to_serve(...)`（在 `hermes_cli/profiles.py`）是**"这个网关服务哪些 profile"的唯一闸口**：
默认 profile + `profiles/` 下每个**有效**的 profile 目录。

**被服务的集合**会同时给这几处把关：**适配器启动**、**cron 心跳**、**`/p/<profile>/` 的
HTTP 准入**、**路由资格**、以及**运行时状态面**。
被排除的 profile **仍然装在那儿**，而且**仍然可以自己单独跑一个网关**。

> ⚠️ **上游文档出入**：原文写的是 `profiles_to_serve(multiplex, profile_allowlist)`，还说
> "格式错的 allowlist 会安全地回退成只有默认" —— **源码签名没有 allowlist 参数**（附录 C ①）。

"**单闸口**"这个词在这里是硬要求：同一个"服务集合"概念如果被两处代码分别算出来，
就一定会漂 —— 一处算出来是"默认 + 3 个"，另一处算出来是"默认 + 2 个"，
症状是"某个 profile 的适配器起来了但路由不认它"（或反过来）。
这和第 4 章"环境事实只能有一个来源"、第 8 章"会话键只能有一个来源"是同一条纪律。

---

## 8. Per-profile persistence —— 每个 profile 自己的库

`SessionStore` **不把数据库句柄冻结在构造那一刻**（字段 `_db_pinned = _DB_UNPINNED`；
构造末尾只为当前作用域**预热**一个句柄，好让启动诊断照旧）。
真正的句柄是**每次读取时**按**当前的 HERMES_HOME 覆盖**解析出来的 ——
**每个解析到的 `profiles/<name>/state.db` 一个缓存句柄** ——
所以**即使 store 对象本身是共享的**，会话也会落进**拥有它的那个 profile** 的库里。

配对（pairing）相关的存储则**按被服务的 profile 各造一个**。

这条是**"数据落到谁家"的最后一道闸**。如果 store 在构造时就把句柄绑死，
那么"一个共享的 store 对象"必然指向**某一个** profile 的库 ——
于是 B 的会话被写进 A 的 `state.db`（而且**不报错**：表在、行在、时间线也对，
只是**在错的家**里）。改成"调用时按覆盖解析"，才和 §5 的 home 覆盖对得上。

---

## 9. Per-bot session lanes —— 每个 bot 一条会话线

会话键按 profile 加**命名空间**：默认 profile 是 `agent:main`，具名 profile 是 `agent:<名字>`。

适配器身上带着 `_owner_profile`（在**适配器配置时**就装好，**早于任何入站事件**），
原因是：**适配器入口的执行早于 `SessionSource.profile` 被盖章**。
`_session_key_profile` 的解析顺序是：**来源盖章 → owner profile → store 解析器**。

**文本/媒体批处理、活跃会话跟踪、忙会话守卫，全部按 lane 分键** ——
所以**两个 bot 共享同一个聊天时，不会共享同一条会话线**。

一句话：**"同一把锁不能锁两条会话线"**。第 7/8 章里的锁、忙时队列、agent 缓存、
`resume_pending` 状态**全是按会话键分桶**的 —— 如果两个 bot 因为"共享同一个聊天 id"
而算出**同一个键**，它们就会互相排队、互相覆盖、互相打断（第 8 章的"单槽覆盖"
会把另一个 bot 的消息顶掉）。**命名空间就是把这两条线拆开的那一刀。**

---

## 10. Control plane —— 桌面插件怎么管 profile

桌面插件只能通过 **ws JSON-RPC 那扇门**够到网关，所以**profile 的枚举与配置**
住在 `tui_gateway/methods_profiles.py` 里，原文列出六个核心方法；该源码快照另有 `profiles.remember_onboarding`：

```text
profiles.list      profiles.create     profiles.describe
profiles.configure profiles.set_asset  profiles.get_asset
```

需要配置解析的读写在目标 home 覆盖之下；资产等方法直接使用显式解析出的目标目录。
**资产（asset）的写入是原子的，且按类型与大小设上限。**

"读写都跑在目标 profile 的作用域下"这条，正是 §3/§5 那套机制**在非回合路径上的复用**：
桌面端点一次"改 profile X 的配置"，**没有 agent 回合**，如果不套作用域，
它就会在**默认 profile 的家里**改配置 —— 这正是第 8 章 `scope is bound per profile
activity, not per turn` 那句话的另一半（作用域绑在"**活动**"上，不是"回合"上）。

---

## 11. Failure modes —— 四种处置

| 类别 | 具体 |
|---|---|
| **启动即致命** | 多路复用配置错误；以及**二级 profile 启用了"要占端口"的平台** —— 因为**一个共享 HTTP 监听归默认 profile 所有** |
| **跳过而非致命** | **单个**配置错的二级适配器 → 带警告跳过，而不是把整个多路复用器拖下水 |
| **失败关闭** | 多路复用下**没有作用域的 `get_secret()` 会抛**；路由命中但目标**不在服务集合** → 事件丢弃；**没有作用域的 `/p/` 请求** → 进入**默认 profile** 的作用域，而不是"未定义作用域" |
| **回退** | 外部的 `cron.provider` **不支持多路复用** → 带警告回退到内置 ticker |

> ⚠️ **上游文档出入**：原文点名的启动致命错误 `SecondaryPortBindingConfigError` ——
> **源码里没有这个类**（机制在、名字没了；附录 C ②）。而且「二级启用占端口平台 → **启动即致命**」
> 这半句**也不准确**：真实行为是把二级的占端口适配器改成**共享监听模式**（走 `/p/<profile>/…`，
> 自己不 bind）；启动时真正会被拒绝（`MultiplexConfigError`）的是**开放策略却没开 allow-all**。
> 「启用了占端口平台但**没有** `/p/<profile>/` ingress」只在**迁移预检**里算 blocker（附录 C ⑤）。

四种处置对应**四种不同的"坏"**，不能互换：

- 配置错 → **别启动**（配置错是"这个部署根本不该跑起来"）；
- 一个适配器错 → **跳过**（一个 bot token 过期不该让所有 profile 掉线）；
- 隔离缺失 → **抛**（宁可这一行坏，也不要读错别人的密钥）；
- 能力不支持 → **回退 + 警告**（降级但可用，且**说出来**）。

把这四种拍成一种（比如"全都抛"/"全都跳过"），要么可用性崩、要么隔离性崩。

---

## 12. Known limitations —— 还没 profile 化的进程级状态

原文在这里是一张五行表 —— 下面逐行翻译 + 标注当前状态：

| 面 | 原文说的状态 |
|---|---|
| **MCP 发现与工具注册** | 进程级；**第一个建 agent 的 profile 赢下发现槽位**。按 profile 的完整 MCP 注册表在 `#67605` 里跟踪 |
| **终端 / 沙箱环境（`TERMINAL_*`）** | 按白名单保持全局；工具从**进程环境**读它 |
| **内置工具注册表** | 内置的是进程级；**插件注册的工具按 profile 叠加**（靠 `hermes_home_key()`） |
| **provider / 能力注册表** | 同一种"混合叠加"模式（浏览器、图像生成、TTS、转写、视频生成、网络搜索、密钥源） |
| **HTTP 监听、relay 入口、进程锁** | 原文描述为一个进程一份，归默认/活动 profile；源码的多路复用状态写到启动 home 的 `gateway_state.json`，次级平台合并在同一记录里（⚠️ 原文写 `runtime_status.json` ——
**代码里没有这个文件名**，见附录 C ④） |

> ⚠️ **上游文档出入**：**MCP 那条已经不是原文说的样子了** —— 它已经被修掉（详见附录 C ③）。
源码里 `hermes_cli/mcp_startup.py` 的开头注释逐字写着：

```text
# Discovery slot per profile home (``hermes_home_key()`` follows the context-local HERMES_HOME
# override): a shared Desktop/dashboard backend serving several profiles runs one discovery per
# profile instead of the first profile to build an agent claiming the slot for everybody (#67605).
```

也就是说：**"第一个建的赢"已经修掉了** —— 现在是**每个 profile home 一个发现槽位**
（用 `_mcp_discovery_started: Set[str]` / `_mcp_discovery_thread: Dict[str, Thread]` 分桶）。

"**已知限制要点名**"这件事本身就是设计的一部分：多路复用最难的不是写代码，
而是**"哪些东西还没隔离"要说得清**。说不清，运维就会在"以为隔离了"的前提下部署 ——
而**以为隔离了**比"知道没隔离"危险得多。这条纪律和第 4 章"注册 ≠ 暴露"、
第 8 章"记债而不是静默跳过"是同一种：**把残留的共享状态写成一张可核对的表。**

---

## 13. Non-goals —— 不是目标的东西

多路复用隔离的是 **profile**；它**不负责认证或授权"终端用户"**。
**一个 profile 是一份配置，不是一个人**：网关**信任它的传输层和它的路由表**
来决定"这个事件属于哪个 profile"。**请求级的身份**、以及**profile 层之上的按用户授权**，
**不在本文范围内**。

这句话是**防止把安全责任放错层**。一个常见误读是"上了多路复用，就等于做了多租户隔离" ——
**不是**：多路复用保证的是"**两个 profile 之间**不串味"，
它**不保证**"同一个 profile 里的两个用户之间不串味"（授权要靠配对/白名单，会话上下文隔离还要靠按用户构造会话键）。
把这两件事混起来，会出现"以为做了租户隔离，其实只是配置隔离"的部署事故。

---

## 附录 A · 术语表（21 条）

| 术语（原文） | 一句话意思 |
|---|---|
| **多路复用**（multiplexing） | 一个进程服务多个 profile；按来源/身份把请求分派到不同 profile |
| **profile** | 一套自己的家：配置 / 密钥 / 会话 / 记忆 / 技能。**是配置，不是人**（§13） |
| **作用域合成**（scope composition） | 每个入站事件在处理前先装好两个 context-local 作用域（home + 密钥） |
| **context-local / contextvar** | Python 的"按任务隔离"的变量；线程池里要靠 `copy_context()` 显式传播 |
| `copy_context()` | 把当前上下文复制进另一个线程/任务，作用域才会跟着过去 |
| **失败关闭**（fail closed） | 拿不到可信值时**报错**，而不是静默用别的值 |
| `UnscopedSecretError` | "多路复用开着但没有作用域"时抛的错；专治"忘了迁移的调用点" |
| **全局白名单** | 少数"描述进程而非 profile"的环境变量（`HERMES_HOME`、`API_SERVER_*` 等）；**不含凭据** |
| **home 覆盖**（HERMES_HOME override） | 上下文局部的"家在哪"；`get_hermes_home()` 先查它再查环境变量 |
| `hermes_home_key()` | 按 home 分桶的注册表用的稳定键 |
| `get_process_hermes_home()` | 少数"机器级、不该跟着覆盖走"的资源用的出口 |
| **`profile_routes`** | `(平台, 公会, 聊天, 线程)` → profile 的路由表；合取匹配、最具体优先、线程匹配当前聊天或直接父聊天（单跳） |
| **服务集合**（served set） | 这个网关当前服务的 profile 集合；适配器/cron/路由/HTTP 准入都看它 |
| **单闸口**（chokepoint） | "只能有一份实现"的那个函数（这里是 `profiles_to_serve`） |
| **lane（会话线）** | 一个 bot 在某个聊天里的那条会话；锁/队列/缓存都按它分桶 |
| **命名空间** | 会话键里的 profile 段（`agent:main` / `agent:<name>`），永不共享 |
| **`_owner_profile`** | 装在适配器上的"我属于哪个 profile"，供**早于来源盖章**的入口使用 |
| **控制面**（control plane） | 桌面插件管 profile 的那六个 JSON-RPC 方法 |
| **资产**（asset） | profile 的可写附件（头像之类）；写入原子 + 类型/大小上限 |
| **tombstoned profile** | 已被删除标记标明失效的 profile 目录；枚举时跳过 |
| **进程级 / 混合叠加**（process-global / overlay） | 已知限制表里的两类：完全共享、或"内置全局 + 插件按 profile 叠" |

## 附录 B · 章节位置与本章的债

```text
官方 website/sidebars.ts 的 Architecture 分组（14 项）
  3  agent-loop                     → 第 2 章
  4  prompt-assembly                → 第 3 章
  5  context-compression-and-caching → 第 5 章
  6  micro-compaction               （跳过：独立支线）
  7  gateway-internals              → 第 7 章
  8  gateway-session-lifecycle      → 第 8 章
  9  multiplexing-gateway           → **第 9 章（本章）**
 10  completion-backlog-delivery    （第 7 章差距 #6 挂着）
 11  session-storage                → 第 6 章
 12  state-db-recovery              （与第 6 章配对）
 13  provider-runtime               （★ 官方"推荐 10 篇"里排第 4）
```

**本章和第 7、8 章的关系**：同一个 `gateway/` 地盘，但**时间尺度**不同 ——
第 7 章讲"一条消息进来怎么走"，第 8 章讲"两轮之间什么还活着"，
**本章讲"一个进程怎么同时服务几套房"**。

**第 7 章侧线已经做掉本章的一半**（对我们这个 mini 项目而言）：
`-p <profile>` 入口层 / `profile_runtime_scope` / 密钥作用域 fail-closed /
会话键命名空间 —— 分别对应原文的 `Serving selected profiles`、`Scope composition`、
`Workstream A`、`Inbound routing`。

**本扩展开工时的清单（历史）**：当时尚未实现真正的“同进程多 profile 服务”
（per-bot session lanes 的完整落地）、按 profile 的持久化句柄缓存、
控制面方法、失败模式的四类处置 —— 后续已在 `f34fdaa` / `9e7c8e4` 实现作用域、按 home 缓存句柄与每 profile 适配器；当前剩余差距见源码讲解 §十七。

## 附录 C · 上游文档出入（本次核实 6 处；另附已核实/未核清单）

| # | 原文说什么 | 源码实测 | 性质 |
|---|---|---|---|
| ① | `profiles_to_serve(multiplex, profile_allowlist)`，且"**格式错的 allowlist 会安全地回退成只有默认**" | 源码签名是 **`profiles_to_serve(multiplex: bool)`**（`hermes_cli/profiles.py:802`）—— **没有 allowlist 参数**，docstring 里也没有 allowlist 语义（`profiles.py` 里 `allowlist` 只出现在注释里，讲的是 clone 会剥掉平台白名单） | **能力已不在**（原文留了个"配置层再裁一刀"的能力，现在没有） |
| ② | 启动致命错误之一是 `SecondaryPortBindingConfigError` | **源码里没有这个类**（全 `gateway/` grep 无 `class SecondaryPortBinding…`）。**语义在**：`gateway/run_adapters.py:1139`、`gateway/platforms/base.py:1905`、`gateway/platforms/shared_ingress.py:4` 三处注释都在讲"二级 profile 的占端口适配器永不绑定，那个监听归默认 profile" | **类名过时/不存在**（机制在、名字没了） |
| ③ | 「已知限制」表：**MCP 发现与工具注册是进程级，第一个建 agent 的 profile 赢下发现槽位** | **已经不成立**：`hermes_cli/mcp_startup.py:12-17` 逐字注释说明发现槽位**按 profile home 分桶**（`hermes_home_key()`），并且明说这就是 **#67605** 修掉的；代码里也是 `Set[str]` / `Dict[str, Thread]` 分桶 | **限制表过期**（方向少见：**源码比文档更先进**） |

| ④ | 「已知限制」表里按 profile 写的状态文件叫 `runtime_status.json` | **代码里没有 `runtime_status.json`**：常量是 `_RUNTIME_STATUS_FILE = "gateway_state.json"`（`gateway/status.py:32`），路径 `_get_process_hermes_home() / "gateway_state.json"`（`:166`）；消费者按 profile 读的是 `profile_dir / "gateway_state.json"`（`hermes_cli/web_routers/status.py:264`） | **文件名过时**（照抄会写出一个不存在的文件） |
| ⑤ | 「启动即致命：二级 profile 启用了**要占端口**的平台」 | 真实**不拒绝**：二级的占端口适配器被改成 **shared-listener 模式**（`adapter._shared_listener_profile = profile_name`，`gateway/run_adapters.py:1139`；只有 `api_server`/`webhook` 两个 MIRROR 平台被直接跳过，`:551`）。`MultiplexConfigError`（`run.py:1636`）全仓唯一 raise 点是 `run_adapters.py:926`，条件是**开放 dm/group 策略却没开 allow-all**（`_own_policy_open_startup_violation`，`run.py:2184`）。「没有 `/p/<profile>/` ingress」是**迁移预检**的 blocker（`gateway_migrate.py:456`） | **语义错位**（把"迁移预检的 blocker"读成"启动期的致命错"，还把平台名单写进错了地方） |
| ⑥ | 路由匹配"with parent-chain chat matching"（我译成「顺着父聊天链向上匹配」） | 源码是**单跳**：`self.chat_id not in (chat_id, parent_chat_id)`（`gateway/profile_routing.py:93`），整个模块**没有任何 `while` 循环**；`parent_chat_id` 由适配器只取一跳（Discord `channel.parent`，`plugins/platforms/discord/adapter.py:5494`） | **措辞暗示多级**（照抄时别自己加回溯） |

**出处引用方式的说明**：本章有几处 issue 号（`#88532`、`#61276`）在**源码里找不到注释**，
只在**测试文件**里能查到（分别见 `tests/hermes_state/test_named_profile_session_db.py`、
`tests/gateway/test_api_server_multiplex_secret_scope.py`）—— 这不影响结论，
但"顺着 issue 号去源码找注释"时找不到是正常的。

**本轮补核实（原先列在"未核"里的三条，现在有源码级证据了）**：

1. ✅ **cron 回退确实存在**：`scheduler_for_profile_mode()`（`cron/scheduler_provider.py:391-402`）逐字
   *"External providers own one unscoped remote registry and cannot reconcile several profile stores:
   fail closed to the built-in multiplex ticker until the API carries profile identity."* ——
   命中就 `logger.warning("cron.provider '%s' does not support multiplex_profiles; using built-in ticker")`
   并返回 `InProcessCronScheduler()`；多路复用 ticker 本体在 `:496 _start_multiplex`（每个 profile 用
   `_profile_cron_scope(home)` 分别打心跳，`#69377`）。
2. ✅ **配对存储确实按 profile 各造一个**：`run_adapters.py:895-899` 逐字
   `self.pairing_stores[name] = (self.pairing_store if name == active else PairingStore(profile=name))`。
3. ✅ **资产上限**：`2_000_000` 字节（`tui_gateway/methods_profiles.py:654`，超了报 `4069 asset too large`）；
   只收 `png/jpg/webp`（`_ASSET_EXTS`，`:15`）；**格式靠 magic bytes 嗅探、不信声明的 mime**（`_ASSET_MAGIC`，`:17-18`）；
   落盘是**临时文件 + `Path.replace()`**（`:657-659`）。

**仍未逐字核**（别当成已核实）：

1. 已知限制表里**除 MCP 那条以外**的其余四行（终端环境、内置注册表、能力注册表、监听/锁）；
2. `implicit_multiplex_blocker()` 依赖的 `build_migration_plan()` / `_host_supports_migration()` 的完整构造。

