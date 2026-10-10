# 源码讲解 · 第 9 章：多路复用网关（Multiplexing Gateway）

> **上游锚**：`f5d1926110`（Hermes `v0.21.3`）。本文所有 `文件:行号` 都对这个 commit。
> **真实源码**：`$HOME/.hermes/hermes-agent/`（`gateway/` 包 = **160 个 `.py` / 86,785 行**）
> **对照实现**：`mini-hermes/`（全项目 **11,643 行**代码 / **5,984 行**测试，本章开工前）
> **本文状态**：🧱 **Hermes 侧完整、mini 侧待实现** —— 本章按"**先读源码、再写代码**"的工序写：
> 差异表与逐节四步里 mini 那一栏先写**现状**并标「本章待实现」；实现完成后补齐 mini 列、
> 真问题（实现时炸过的）与真跑收据。**§十六 差距清单就是实现清单。**
> **材料来源**：5 份取源码笔记（`/tmp/ch09-notes-{routing,secrets,runner,persistence,mini}.md`，共 2,230 行）
> ＋ `/tmp/ch09-checklist.md`（抄 / 不抄清单）。原《精译-09》里的 `【源码】要点` 块已并入本文。

[TOC]

---

## 两份代码的路径与规模

| 关注点 | 真实 Hermes | mini-hermes |
|---|---|---|
| **开关与裁决** | `hermes_cli/gateway_multiplex_mode.py`（**149 行**）：`resolve_multiplex_mode` / `implicit_multiplex_blocker` / `MultiplexDecision` | **无** —— 只有一个 `--multiplex` 布尔（`hermes_cli/gateway_cmd.py`）→ 本章新建 `gateway/multiplex_mode.py` |
| **共享 bot 路由** | `gateway/profile_routing.py`（**176 行**）：`ProfileRoute` / `parse_profile_routes` / `match_profile_route` / `ProfileRouteRejected` | **无**（`grep profile_route` 零命中）→ 本章新建 `gateway/profile_routing.py` |
| **服务集合** | `hermes_cli/profiles.py:802 profiles_to_serve(multiplex)` | `hermes_cli/profiles.py:211 profiles_to_serve(multiplex=False)` ✅ 已有，缺墓碑过滤 |
| **home 覆盖** | `hermes_constants.py`（**1,326 行**）：`set_hermes_home_override` / `get_hermes_home` / `hermes_home_key` / `get_process_hermes_home` | `hermes_constants.py`（**59 行**）：`set_mini_home_override` / `get_mini_home` ✅，缺 `hermes_home_key` 与进程级出口 |
| **密钥作用域** | `agent/secret_scope.py`（**280 行**）：`get_secret` / `_GLOBAL_ENV_EXACT`（36 项）/ `UnscopedSecretError` / `build_profile_secret_scope` | `agent/secret_scope.py`（**91 行**）：fail-closed ✅，**无全局白名单** |
| **作用域装配** | `gateway/run.py:1750 _profile_runtime_scope`（**30+ 调用点**） | `gateway/profile_scope.py:35 profile_runtime_scope`（**1 个调用点**：`run_turn.py:76`） |
| **每 profile 适配器** | `gateway/run_adapters.py:832 / :1004`（建、连、跳过、重连） | `gateway/run.py:59-71`（按**适配器名**分键）＋ `:198-199` 裸 `connect()` 循环 |
| **会话库句柄** | `gateway/session_persistence.py:43 _open_session_db_for_active_scope` + `:89 _db` property | `gateway/run.py:109-124 _db()/_db_for(home)` ✅ 已按 home 缓存 |
| **配对存储** | `gateway/run_adapters.py:895 PairingStore(profile=name)` | `gateway/authz.py`（单一 store，无 profile 维度） |
| **状态面** | `gateway/status.py:32 gateway_state.json` / `:819 write` / `:895 read` / `:852 served_profiles` | `gateway/status.py`（PID + 指纹 + 干净退出标记）✅，缺 `served_profiles` |
| **控制面** | `tui_gateway/methods_profiles.py`（**686 行**，6 个 JSON-RPC 方法） | argparse 子命令（`profile create|list|show`） |
| **cron 心跳** | `cron/scheduler_provider.py:391`（按 profile tick + 回退）/ `cron/jobs.py:1212` | **无 cron 子系统** |

---

## 导言 · 机制总览：Hermes 具体怎么做的（先读这一节）

> 本节只讲 **Hermes 一侧**：谁在管、什么时候发生、具体做什么、哪些是圣线、错了怎么办、
> 跨层的那一半、以及一轮的完整时间线。每条给 `文件:行号`，优先引注释（WHY 都在注释里）。

### 0.1 谁在管这件事

| 职责 | 接口 | 实现 | 兜底 |
|---|---|---|---|
| **开关的裁决** | `resolve_multiplex_mode(config) -> MultiplexDecision` | `hermes_cli/gateway_multiplex_mode.py:110` | 预检抛异常 → 记 warning、按 standalone 起（`:117-119`） |
| **启动预检** | `implicit_multiplex_blocker() -> Optional[str]` | `gateway_multiplex_mode.py:75`（**六条**） | 返回原因串即"别开"，`None` = 可以开 |
| **服务集合** | `profiles_to_serve(multiplex) -> List[(name, home)]` | `hermes_cli/profiles.py:802`（**唯一闸口**） | 未开多路复用 → 只返回 active 那一个 |
| **作用域装配** | `_profile_runtime_scope(profile_home)` | `gateway/run.py:1750`（async 版 `:1784`） | 加载密钥失败 → 作用域仍安装（`hydrate_secrets=False` 路径） |
| **home 覆盖** | `set_hermes_home_override / get_hermes_home` | `hermes_constants.py:25 / :101` | 无覆盖 → `HERMES_HOME` env → 平台默认 |
| **密钥读取** | `get_secret(name, default)` | `agent/secret_scope.py:143` | 见 0.5（无作用域直接抛） |
| **路由匹配** | `match_profile_route(routes, platform, …)` | `gateway/profile_routing.py:166` | 无命中 → 默认 profile |
| **路由的拒绝/丢弃** | `ProfileRouteRejected` | 判定 `gateway/run.py:4348-4352`；丢弃 `gateway/run_inbound.py:173-178` | 匹配过程抛异常 → 记 warning 当"无命中"（`run.py:4335`） |
| **会话库句柄** | `_open_session_db_for_active_scope()` | `gateway/session_persistence.py:43` | 打开失败 → 有界退避 + JSONL 降级 |
| **每 profile 适配器** | `_start_secondary_profile_adapters()` | `gateway/run_adapters.py:832` | 单 profile 失败 → 跳过继续（`:865`） |
| **状态面** | `write_runtime_status / read_runtime_status` | `gateway/status.py:819 / :895` | 读不到就当作"没有记录" |

### 0.2 什么时候发生

**（a）开关值从哪来 —— 三道来源 + 真值词表**（`gateway/config.py:29-64`）

```text
env:    GATEWAY_MULTIPLEX_PROFILES=1|true|yes|on  → True
                                   0|false|no|off → False
                                   空白 / 未识别   → None（= 不算覆盖；未识别额外打 warning）
config: config.yaml 顶层 multiplex_profiles，或 gateway.multiplex_profiles
默认:   hermes_cli/config_defaults.py:1998  "multiplex_profiles": True
```

★ 三态（`True` / `False` / `None`）**不是洁癖**。`gateway/config.py:552` 的注释逐字：
*"UNSET here is ``None``: a request the gateway settles at boot, not a verdict."* ——
`None` = "待裁决"，`True/False` = "操作者或迁移已经定了"。
空白值的处理写在同一处 docstring：*"a provisioned-but-unpopulated Fly secret arrives as `""`
and must NOT shadow a config.yaml opt-in."*

**（b）预检六条（按短路顺序）**（`gateway_multiplex_mode.py:75-107`）

| # | 条件 | 逐字理由 |
|---|---|---|
| 1 | 本进程是**非 default profile 自己的网关** | *"this is profile '<x>'s own gateway; only the default profile's gateway multiplexes"* |
| 2 | 服务集合 **< 2** | `SINGLE_PROFILE_REASON = "only one profile exists (nothing to multiplex)"` |
| 3 | **host 不支持迁移** | `_host_supports_migration()` 返回的原文（s6 slots / Windows 计划任务这类"看不见 per-profile 网关"的宿主） |
| 4 | **有 secondary 自己还在跑网关** | *"profile(s) 'x' (pid 123) still run their own gateway; fold them with `hermes gateway migrate --multiplex` or pin gateway.multiplex_profiles: false"* |
| 5 | **迁移预检有 blocker** | `"; ".join(plan.blockers)`（重复 bot 凭证 / 占端口平台没有 `/p/<profile>/` ingress） |
| 6 | 全过 | 返回 `None` → `MultiplexDecision(True, "default", "gateway.multiplex_profiles unset; default applies")` |

第 2 条的注释解释了为什么它排在最前：*"Cheap and first: a single-profile install has nothing to
multiplex, and the fail-closed secret scope the multiplexer arms buys it nothing."*
（顺带让**内嵌/测试 runner 不去触达**下面那两条昂贵的 service-manager 探测。）

**（c）每次入站事件**：路由**只在多路复用开着时**才跑（`profile_routing.py:matches()` 的调用前置条件写在
`run.py:4320` 一带）；匹配是**纯本地**计算，不出网。

**（d）每次读密钥 / 读会话库 / 建适配器**：都按"**当前作用域**"解析，而不是按进程启动时的值
（`get_secret` `:143`、`_db` property `session_persistence.py:89`、`_profile_runtime_scope` 的 30+ 调用点）。

### 0.3 具体做什么

| 阶段 | 做什么 | 出网？ |
|---|---|---|
| **boot**：读配置 | `load_gateway_config_for_runner()`（`run.py:1791`）→ 裁决 → **在多路复用开启时于默认 profile 作用域下再 reload 一次**（让默认 profile `.env` 里的平台 token 与次级 profile 一样走作用域解析） | 否（读文件；外部密钥源 = 是） |
| **boot**：建适配器 | 主 profile 的适配器 + `_start_secondary_profile_adapters()` 逐个 profile、逐个平台建连 | **是**（连平台） |
| **boot**：记录 | `write_runtime_status(served_profiles=[...])` + 裁决理由 | 否 |
| **入站**：守卫 | 适配器第 1 级（`_active_sessions` / `_pending_messages`） | 否 |
| **入站**：路由 | `match_profile_route` → 盖章 `source.profile` → **目标不在服务集合就丢弃** | 否 |
| **回合**：装作用域 | `_profile_runtime_scope(home)`：home 覆盖 + 密钥映射 + 终端策略 | 否（密钥源可能出网） |
| **回合**：跑 agent | 正常 agent 循环（第 2~8 章） | **是** |
| **回合**：落盘 | `_db` 按当前 home 解析 → 写对应 profile 的 `state.db` | 否 |
| **跨轮/巡检** | cron tick 按 profile 各打心跳；状态面按 profile 可读 | 任务是=是 |

**三个易看漏的设计**：

1. **配置 reload 在默认作用域下做**（`run.py:1791-1817` 逐字注释：*"reload under the default profile's
   `_profile_runtime_scope` so platform tokens in its `.env` resolve via the secret scope"*），
   而且 reload 完把裁决结果**写回**（`scoped.multiplex_profiles = cfg.multiplex_profiles`）——
   否则"再读一次"会把已裁决的值又读成 unset。
2. **枚举服务集合是纯目录读**（`#94590`）：*"never creates a profile dir"* —— "列一下服务谁"不许有副作用。
3. **预检失败不算致命**：`resolve_multiplex_mode` 把异常转成 blocker（*"a broken preflight must not
   take the gateway down with it"*），结果仍是"老实起 standalone 并记日志"。

### 0.4 不变式（四条圣线 + 一条顺序约定）

| # | 圣线 | 依据 |
|---|---|---|
| ① | **作用域绝不写进 `os.environ`** | `hermes_constants.py:25` 逐字 *"Deliberately does not mutate ``os.environ`` (shared by every thread in the process)"*；`_profile_runtime_scope` 逐字 *"so subprocesses never inherit cross-profile secrets"* |
| ② | **密钥解析顺序固定**：全局白名单 → 当前作用域 → 兜底；**多路复用开着且没有作用域 = 抛**，不是回退 | `agent/secret_scope.py:143-171` 四分支；`UnscopedSecretError` docstring 逐字 *"The fix is to wrap the call path in ``set_secret_scope(...)`` …, not to widen the global allowlist."* |
| ③ | **"服务集合"只有一份实现** | `profiles_to_serve` docstring 逐字 *"the single chokepoint for "which profiles does the inbound gateway handle""* |
| ④ | **数据库句柄不冻结**：每个解析出的 path 一个句柄；**没命中不缓存** | `gateway/session.py:789-800`（#88532 逐字）；`session_persistence.py:135` 逐字 *"Only hits are memoized: a profile directory can be provisioned *after* startup … and a cached miss would pin that profile's rows to the ambient store for life."* |
| ⑤ | （顺序）**默认 profile 永远排第一** | `profiles_to_serve` 的 `serve = [("default", …)]` 先建；mini 侧 docstring 也记了"共享入口的平台只在默认 profile 上跑 / 在默认 profile 停网关会停掉全部" |

### 0.5 出错了怎么办（四类处置，各有逐字出处）

| 类别 | 触发 | 行为 | 出处 |
|---|---|---|---|
| **启动即失败** | 二级 profile 的**开放 dm/group 策略却没开 allow-all** | `raise MultiplexConfigError` → 启动中止（`_startup_fail_fatal_config`） | raise：`run_adapters.py:926`；判定：`run.py:2184-2205`；消费：`run_startup.py:1190-1196` |
| **跳过而非致命** | **单个** profile 的适配器建/连失败；该 profile **没有**该平台的凭据；relay/whatsapp（共享入口）；mirror 平台（api_server/webhook）；重复认领同一 token/监听 | `logger.error/warning` 后 `continue`，其它 profile 照起 | `run_adapters.py:865`（循环级）、`:1004-1100`（平台级各分支） |
| **失败关闭** | 多路复用开 + **没有作用域**读密钥；路由命中但**目标不在服务集合**；未带作用域的 `/p/` 请求 | 依次：抛 `UnscopedSecretError`；`raise ProfileRouteRejected` → 入站**丢弃**；落进**默认 profile** 的作用域 | `secret_scope.py:162`；`run.py:4352` + `run_inbound.py:173-178`；`#61276` |
| **回退 + 警告** | 外部 `cron.provider` 不支持多路复用；预检自身抛异常 | 回退内置 ticker 并 warning；按 standalone 起并 warning | `cron/scheduler_provider.py:391-402` 逐字；`gateway_multiplex_mode.py:117-119` |
| （热重载的第五类） | 运行中某个二级 profile 配置坏了 | **只把那个 profile 停下来**（park），不拖垮进程 | `run_profile_reconcile.py:125-129` |

### 0.6 跨层的那一半：哪些东西仍然是"进程级"

| 仍然是进程级 | 归属与出处 |
|---|---|
| `os.environ` 里的**白名单项**（进程身份 / shell / 监听设置 / relay 戳 …36 项 + 3 前缀） | 全局，`agent/secret_scope.py:98-135` |
| **HTTP 监听**、relay 入口、**进程锁** | 一份；归默认/活动 profile（`gateway/platforms/shared_ingress.py`）；次级的占端口适配器走 `/p/<profile>/…` |
| **MCP 发现槽位** | 曾进程级，现按 **profile home 分桶**（`hermes_cli/mcp_startup.py:12-17`，`#67605`） |
| **内置工具注册表** | 进程级；**插件注册的工具按 profile 叠加**（靠 `hermes_home_key()`） |
| **provider / 能力注册表** | 混合叠加（浏览器、图像、TTS、转写、网络搜索、密钥源…） |
| **`gateway_state.json`** | 多路复用器只写**启动 home 那一份**；次 profile 的平台以 `<profile>:<platform>` 键并进同一份（`gateway/platforms/base.py:2105`） |

对应地，这些都是 **profile 级**：密钥、home、会话库、配对存储、cron store、适配器实例、
会话键命名空间、终端策略、`pairings` 白名单。

### 0.7 一轮的完整时间线

```text
进程启动
  │  load_gateway_config_for_runner()            run.py:1791
  │    └─ resolve_multiplex_mode()                multiplex_mode.py:110
  │         ├─ 显式值 → 直接用（config/env）
  │         └─ 未设置 → implicit_multiplex_blocker() 六条     :75
  │              └─ 抛异常？ → warning + standalone（不致命） :117
  │    └─ multiplex 开 → 在默认 profile 作用域下再 reload 一次 :684
  │  set_multiplex_active(bool)                   run.py:3435
  │  set_hermes_home_override(get_hermes_home())  run.py:684
  │
  │  profiles_to_serve(multiplex)   → [("default",…), ("lab",…), …]   profiles.py:802
  │  _start_secondary_profile_adapters()          run_adapters.py:832
  │    └─ for profile_home:                       （跳过 active）
  │         _start_one_profile_adapters()         :1004
  │            ├─ 无凭据 → 跳过 + info
  │            ├─ relay/whatsapp → 记"没服务" + 跳过
  │            ├─ api_server/webhook → "默认监听已镜像" + 跳过
  │            ├─ _profile_runtime_scope(home, hydrate_secrets=False)
  │            │     └─ adapter = self._create_adapter(...)
  │            ├─ 重复认领？ → 拒绝 + 跳过
  │            └─ connect → 失败：warning + 断开 + 排重连
  │  write_runtime_status(served_profiles=[...])  run_adapters.py:900
  │
  ▼
入站事件（共享 bot 的一条消息）
  │  适配器第 1 级守卫（忙 → 单槽覆盖 / 溢出 FIFO）
  │  _profile_name_for_source()                   run.py:4309
  │    ├─ 路由匹配（合取 / 最具体优先 / 单跳父链）profile_routing.py:166
  │    ├─ 目标不在服务集合 → ProfileRouteRejected  :4352
  │    └─ 未命中 → 默认 profile
  │  拒绝标记 → source.profile_route_rejected = True
  │  _hm_admit_event()：为 True → warning + return None（丢弃）  run_inbound.py:173
  │  授权（默认拒绝的四层）
  │
  ▼
回合
  │  _profile_runtime_scope(profile_home)         run.py:1750
  │    ├─ set_hermes_home_override(home)          hermes_constants.py:25
  │    ├─ set_secret_scope(build_profile_secret_scope(home))   secret_scope.py:80
  │    └─ install_and_reset_profile_terminal_scope(home)
  │  agent 循环（读密钥走 get_secret → 作用域）    secret_scope.py:143
  │  落盘：_db → _open_session_db_for_active_scope(path)       session_persistence.py:43
  │  finally：反序复位（密钥 → home → 终端）       run.py:1780-1782
  ▼
跨轮 / 关闭
   cron ticker 按 profile 各打心跳（外部 provider 不支持则回退内置）
   状态面：gateway_state.json（served_profiles / multiplex_standalone_reason）
```

### 读完这一节的验收 —— 不看后面的差异表，能说清这四件事

1. **开关的三种状态**分别是什么、为什么"未设置"不能当成 `False`，以及**预检六条**里哪一条最贵、为什么排最后；
2. **服务集合**为什么必须只有一个实现，以及"枚举不许建目录"防的是什么；
3. **两条失败关闭**（无作用域读密钥 / 路由目标不在服务集合）分别在哪儿抛、在哪儿被消费，为什么"丢弃"比"投错"可接受；
4. **哪些东西仍然进程级**（至少举三样），以及为什么"默认 profile 永远排第一"是顺序约定而不是习惯。

---

## §〇 差异表（本章 34 条）

> 列序：`真实 Hermes | mini 版 | 性质 | 值得改`。「mini 版」列一律指**当前代码**；
> 本章待实现的行标 `⬜ 本章待实现`。

| # | 关注点 | 真实 Hermes | mini 版 | 性质 | 值得改 |
|---|---|---|---|---|---|
| 1 | 开关三态 | `None`=待裁决 / `True` / `False`（`config.py:552`） | `--multiplex` 单个布尔 | 语义缺失 | ✅ 抄（A1，~40 行） |
| 2 | env 覆盖 | `GATEWAY_MULTIPLEX_PROFILES` + 真值词表 + **空白不算覆盖** | 无 | 能力缺失 | ✅ 抄（A1） |
| 3 | 启动预检 | 六条（`multiplex_mode.py:75`） | 无 | ★ 机制缺失 | ✅ 抄三条（A3，~60 行） |
| 4 | 裁决不许致命 | 预检异常 → standalone + warning（`:117`） | 无 | 防御缺失 | ✅ 抄（A3） |
| 5 | 裁决理由落盘 | `write_runtime_status(multiplex_standalone_reason=…)` | 无 | 可观测性 | ✅ 抄（A4，~20 行） |
| 6 | 服务集合 | `profiles_to_serve(multiplex)` 单闸口 + 墓碑过滤 + 纯目录读 | 同名函数 ✅ 但无墓碑过滤 | 细节差异 | ✅ 抄差额（A2，~10 行） |
| 7 | 服务集合顺序 | 默认永远第一 | 已遵守 ✅ | 已对齐 | — |
| 8 | 作用域装配 | `_profile_runtime_scope`：home + 密钥 + 终端策略 | `profile_runtime_scope`：home + 密钥（无终端策略） | 少一件 | ⚠️ 见 #9 一起 |
| 9 | 作用域覆盖范围 | **30+ 调用点**（适配器/启动/入站/回合/slash/通知/热重载…） | **1 个**（`run_turn.py:76`） | ★ 架构成型问题 | ✅ 抄（A5，~30 行） |
| 10 | 密钥失败关闭 | mux 开 + 无作用域 → 抛 | 同 ✅ | 已对齐 | — |
| 11 | 全局白名单 | 36 项 + 3 前缀（`secret_scope.py:98-130`） | **无** | ★ 缺层（会读到空值/误抛） | ✅ 抄（A9，~25 行） |
| 12 | home 覆盖 | 覆盖 → env → 默认 | 同 ✅ | 已对齐 | — |
| 13 | `hermes_home_key()` | `hermes_constants.py:119`，给注册表稳定分键 | 无（`tools/registry.py` 的 check_fn 缓存**不分键**） | ★ 缺层（多 profile 结果互串） | ✅ 抄（A10，~35 行） |
| 14 | 进程级 home 出口 | `get_process_hermes_home()`（身份文件不受作用域影响） | 无 | 缺层 | ✅ 抄（A10） |
| 15 | 一次性告警 | `#18594`：声明了 profile 却在默认 home 上跑 | 无 | 缺层（低优先） | ⬜ 可抄（~15 行） |
| 16 | 配置 reload 策略 | mux 开 → 默认作用域下 reload + 写回裁决 | 无 reload 概念 | 规模差距 | ⬜ 不抄（照抄会与 mini 的一次性加载冲突） |
| 17 | 路由表 | `profile_routing.py` 176 行（合取/最具体/单跳父链/enabled/bot_profile） | **无** | ★ 核心缺失 | ✅ 抄（A7，~150 行） |
| 18 | YAML 数字 id 强转 | `_coerce_route_id`：int→str，bool/float 只警告 | 无 | 细节但真会踩（snowflake 读成 int） | ✅ 抄（含在 A7） |
| 19 | 路由拒绝 | `ProfileRouteRejected` + served 集合判定 | 无 | 缺层 | ✅ 抄（含在 A7） |
| 20 | 拒绝 → 丢弃 | 入站咽喉 `return None` + warning | 无 | 缺层 | ✅ 抄（含在 A7） |
| 21 | per-bot lane | `_owner_profile` + `_session_key_profile` 三段解析 | 只有 `session_key_namespace(profile)` ✅ | 缺一层（同聊天两 bot 会共用 lane） | ✅ 抄（A8，~30 行） |
| 22 | 每 profile 适配器 | `_profile_adapters[profile][platform]` | `adapters[name]`（与 profile 无关） | ★ 架构成型问题 | ✅ 抄（A6，~150 行） |
| 23 | fail-soft 启动 | 单 profile/平台失败 → 跳过 + 警告 + 排重连 | 裸 `connect()` 循环：**一个失败整进程挂** | ★ 可用性 | ✅ 抄（A6） |
| 24 | 跳过要说出来 | 每种跳过都有专属日志（info/warning 分明） | 无 | 可观测性 | ✅ 抄（含在 A6） |
| 25 | 重复认领拒绝 | `_refuse_duplicate_claim`（同 token/同监听） | 无 | 基座缺失（mini 适配器无 token） | ⬜ 不抄（B8） |
| 26 | mirror 平台跳过 | api_server/webhook 走默认监听镜像 | 无（mini 无 HTTP 监听） | 基座缺失 | ⬜ 不抄（B2） |
| 27 | 每 profile 持久化句柄 | 不冻结 + 按 path 缓存 + **只 memoize 命中** | 已按 home 缓存 ✅，无"只缓存命中" | 细节 | ✅ 抄差额（A11，~15 行） |
| 28 | 配对存储 per profile | `PairingStore(profile=name)` | 单一 store | 缺层 | ⬜ 可抄（mini 的配对白名单目前是全局一份；本章不动） |
| 29 | 状态面 | `gateway_state.json` + `served_profiles` + 按 profile 可读 | 有状态文件，缺 `served_profiles` | 可观测性 | ✅ 抄（A12，~45 行） |
| 30 | 控制面 | 6 个 JSON-RPC 方法（686 行） | argparse 子命令 | 基座缺失 | ⬜ 不抄（B7） |
| 31 | 资产原子写 | tmp + `Path.replace()` + magic 嗅探 + 2MB | 无资产概念 | 基座缺失 | ⬜ 不抄，但**手法**记注释（B7） |
| 32 | cron per profile + 回退 | `_start_multiplex` / 外部 provider → 内置 | **无 cron** | 基座缺失 | ⬜ 不抄（B6） |
| 33 | open-policy 启动拒绝 | `_own_policy_open_startup_violation` | 无（授权是默认拒绝） | 概念不存在 | ⬜ 不抄（B9） |
| 34 | 终端作用域桥 | `install_and_reset_profile_terminal_scope` | 无（无沙箱后端） | 基座缺失 | ⚠️ 抄一个"占位 + 注释"（避免以后误以为做了隔离） |

**统计**：34 行 = **✅ 要抄 18 项**（A 批 11 条机制）+ **⬜ 不抄 9 项** + **⚠️ 2 项** + **— 已对齐 2 项**
＋ 3 项（#15/#28/#34）属"可抄但本章不动"。

---

## §一 Overview —— 一进程 vs 一 profile（精译 §1）

### ① Hermes 怎么做的

一个网关进程服务**所有** profile：默认 profile 加上每个被服务的具名 profile，各有自己的
**适配器、凭据、会话库、cron 心跳**；同时**共用**一个事件循环、一个 HTTP 监听、一把进程锁、
一个状态面。全部设计的出发点是一条硬约束：

> **profile A 的回合，永远不许看见 profile B 的状态。**

### ② 读 Hermes 的代码

```text
gateway/run.py:288   # "Non-default profiles' adapters by profile then Platform;
                     #  self.adapters stays the default's map."
gateway/run.py:282   self.adapters: Dict[Platform, BasePlatformAdapter] = {}
gateway/run.py:288   self._profile_adapters: Dict[str, Dict[Platform, BasePlatformAdapter]] = {}
gateway/AGENTS.md    "run.py is the facade; phases live in run_*.py (startup, adapters, inbound,
                      turn, busy, goals, notifications, shutdown, ...)"
gateway/run.py:1646  def _multiplex_profile_homes(config)   ← 服务集合的消费点
```

★ 注意**两张适配器表**：`self.adapters` 永远是**默认 profile 的**那一张；次级 profile 的进
`self._profile_adapters[profile][platform]`。这个"默认在明、次级在暗"的布局后面到处出现
（配对 store、状态面、平台状态键都是同一套）。

### ③ mini 的实现（当前）

- `gateway/run.py:59-71`：`self.adapters: Dict[str, Any] = {}` —— 键是**适配器名字**，与 profile 无关；
- `gateway/run.py:190-191`：`set_multiplex_active(self.multiplex)` + `self.served_homes = profiles_to_serve(self.multiplex)`；
- `gateway/run.py:255-257`：`served_homes` 只用来在 banner/status 里显示"服务了几个 home"。

### ④ mini 为什么没做成这样 / 值不值得改

**原因具体**：① mini 只有**一个** console 适配器（`gateway/adapters/console.py`），不存在"多 bot"；
② 没有控制面（桌面插件那扇门），所以没有"运行中增删 profile"的需求；③ 第 7 章做的是**入口层**（`-p <profile>`
选一个家），那时还没有"一个进程服务多个家"的消费者 —— 于是 `--multiplex` 停在了"设标志 + 显示条数"。

**是不是 bug**：不是错，但是**半成品语义**（`served_homes` 没有决定任何消息归属）。
**值不值得改**：值得，而且这就是本章 A5/A6/A7 —— 没有它们，`--multiplex` 只是"密钥变严 + 横幅变长"。

---

## §二 The mode flag —— 开关的三道来源（精译 §2）

### ① Hermes 怎么做的

`gateway.multiplex_profiles` 也接受顶层写法，**默认开**；解析顺序 **env > config.yaml > unset**。
关键在**三态**：`None`（未设置，等 boot 裁决）/ `True` / `False`；**空白与未识别都不算覆盖**。

### ② 读 Hermes 的代码

```text
gateway/config.py:29-30  _TRUTHY_STRINGS = frozenset({"1","true","yes","on"})
                         _FALSY_STRINGS  = frozenset({"0","false","no","off"})
gateway/config.py:33-36  def _bool_token(value) -> Optional[bool]:
                             """True/False for a recognized truthy/falsy token, else None."""
gateway/config.py:45-64  def _env_multiplex_profiles_override() -> "bool | None":
    """… ``None`` when unset, blank, or unrecognized so the caller keeps the config.yaml
    value (env > config > default). Blank is deliberately ``None``, not ``False``: a
    provisioned-but-unpopulated Fly secret arrives as ``""`` and must NOT shadow
    a config.yaml opt-in."""
gateway/config.py:552-558  multiplex_profiles: Optional[bool] = None
    # "On by default (DEFAULT_CONFIG), but UNSET here is ``None``: a request the gateway
    #  settles at boot, not a verdict. … Every reader tests truthiness, so an unresolved
    #  ``None`` never multiplexes by accident."
hermes_cli/config_defaults.py:1998   "multiplex_profiles": True
hermes_cli/gateway_multiplex_mode.py:27-50  explicit_multiplex_flag(default_home)
hermes_cli/gateway_multiplex_mode.py:53-63  default_gateway_multiplexes(default_home)
    # 先读 **活着的那个网关** 记录的 served_profiles，再读显式开关 ——
    # 别的进程**不猜** unset 的默认（否则"还没迁移"会被读成"已经在多路复用"）
```

### ③ mini 的实现（当前）

`gateway/run.py:42/48 multiplex: bool = False`，CLI 侧 `--multiplex`（`hermes_cli/gateway_cmd.py:111/118`）；
**没有** env 覆盖、没有真值词表、没有"未设置"这一态。

### ④ mini 为什么没做成这样 / 值不值得改

**原因具体**：mini 的配置面只有 `config.json` + 命令行旗标，**没有托管部署**（没人往容器里塞
`MINI_*` 空值），所以"空白 ≠ False"这条防御在 mini 里暂时没有受害者。
**值不值得改**：**值得**（A1，~40 行）：三态区分"操作者的决定"与"待裁决"，是后面 A3/A4 的地基 ——
没有 `None`，"boot 期守卫"就无处安放。env 覆盖也顺手抄（mini 已有 `MINI_` 前缀约定）。

---

## §三 启动预检与裁决（原文散落，本章新增小节）

### ① Hermes 怎么做的

"默认开"必须配一个**预检**，否则一次安装里两个进程会抢同一个 bot token —— 那不是报错，是
**间歇性错乱**。预检六条（见 0.2），**短路顺序**本身就是设计：最便宜、最能排除大类的排前面。
裁决结果记三种来源（`config` / `default` / `guard`），并**落盘 + 分级打日志**。
预检崩了**不许拖垮网关**。

### ② 读 Hermes 的代码

```text
hermes_cli/gateway_multiplex_mode.py:75-107   implicit_multiplex_blocker() 六条（逐字见 §0.2）
hermes_cli/gateway_multiplex_mode.py:24       SINGLE_PROFILE_REASON = "only one profile exists (nothing to multiplex)"
hermes_cli/gateway_multiplex_mode.py:100-102  # "Cheap and first: a single-profile install has nothing to
                                              #  multiplex, and the fail-closed secret scope the multiplexer
                                              #  arms buys it nothing. (Also keeps every embedded/test runner
                                              #  off the service-manager probes below.)"
hermes_cli/gateway_multiplex_mode.py:110-123  resolve_multiplex_mode(config) -> MultiplexDecision
hermes_cli/gateway_multiplex_mode.py:117-119  except Exception as exc:  # a broken preflight must not take
                                              # the gateway down with it
hermes_cli/gateway_multiplex_mode.py:46-51    MultiplexDecision: enabled / source / reason
                                              # source ∈ {"config","default","guard"}
hermes_cli/gateway_multiplex_mode.py:126-133  record_multiplex_decision() → write_runtime_status(
                                                  multiplex_standalone_reason=decision.reason if source=="guard" else None)
hermes_cli/gateway_multiplex_mode.py:136-149  log_multiplex_decision()  # guard+单profile → info；其它 guard → warning
```

### ③ mini 的实现（当前）

**没有** —— `--multiplex` 是纯布尔，没有"该不该开"的判断，也没有"为什么没开"的记录。

### ④ mini 为什么没做成这样 / 值不值得改

**不抄的两条**（当场写清理由）：
- 第 3 条 **host 可迁移性**：mini 没有 s6 / Windows 计划任务 / systemd 这些"宿主看不见 per-profile 网关"
  的概念 —— ① 类基座（`_host_supports_migration()` 的判据全是服务管理器）。
- 第 5 条 **迁移预检 blocker**：mini 没有 `gateway migrate` 命令，"把整个 fleet 折进一个进程"这个动作不存在。

**要抄的三条**（A3，~60 行）：① 本进程是非默认 profile 自己的网关 → 不多路复用；
② 服务集合 < 2 → 不开（省得白开 fail-closed）；④ **有 secondary 自己还在跑网关 → 不开** ——
mini 第 7 章已有"PID + 启动时间指纹"的存活判据（`gateway/status.py`），这条**判得出来**，抄。
另外**异常兜底 + 裁决落盘**（A3/A4）一起抄：理由写代码注释里（"预检崩了按 standalone 起"）。

---

## §四 Scope composition —— 作用域绑在"活动"上（精译 §3）

### ① Hermes 怎么做的

每个入站事件，在**任何 profile 自己的代码跑起来之前**，先合成**三个**上下文局部作用域：

```text
平台事件 → 路由命中 → source.profile 盖章
        → _profile_runtime_scope(home)
             ├─ set_hermes_home_override(home)      配置/库/技能/记忆/会话 都按它解析
             ├─ set_secret_scope(profile 的 .env)   provider 密钥、平台 token
             └─ install_and_reset_profile_terminal_scope(home)   终端/沙箱策略
        → agent 回合（工作线程靠 copy_context() 继承）
        → finally 反序复位
```

**作用域绑在"profile 的活动"上，不是"回合"上** —— 这是本章最容易看漏的一句：它不仅包住 agent 回合，
还包住**适配器的建/连/重连、入站预处理、slash 命令、通知、热重载、平台监听**。

### ② 读 Hermes 的代码

```text
gateway/run.py:1750-1783  @_contextmanager def _profile_runtime_scope(profile_home, prepared_secret_scope=None, *, hydrate_secrets=True)
    """Scope config/skills/memory AND credentials to a profile for one turn (multiplexed path only).
    ``set_hermes_home_override`` is a contextvar (reaches the agent worker via ``copy_context()``);
    ``set_secret_scope`` makes the profile ``.env`` the credential source without mutating
    ``os.environ``, so subprocesses never inherit cross-profile secrets."""
    # 末端还要装"该 profile 的**完整**终端策略"：
    # "never ambient TERMINAL_* a prior turn set … (first-writer-wins backend leak; #68559)"
gateway/run.py:1739-1750  _load_profile_secret_scope(profile_home)  # 先 hydrate 外部密钥源，再 build
gateway/run.py:1784-1789  _async_profile_runtime_scope(profile_home)
    """Enter a profile scope without loading secret files on the event loop."""
    #   → secrets = await asyncio.to_thread(_load_profile_secret_scope, …)
```

**调用点分布（15 个文件、30+ 处，抄几个代表）**：

| 文件 | 用途 |
|---|---|
| `gateway/run_adapters.py`（:459/:517/:908/:1008/:1046/:1149/:1355…/:1475） | 建适配器、连接、重连、平台握手 |
| `gateway/run_startup.py`（:119/:122/:933/:962） | 启动期（async 版在这里用） |
| `gateway/run_turn.py`（:2173/:2208/:2458） | 回合与后处理 |
| `gateway/run_inbound.py:1709` | 入站预处理 |
| `gateway/slash_commands*.py`（`slash_commands.py:305`、`_session.py:464`、`_status.py:418`、`_model.py:383`） | 每条 slash 命令 |
| `gateway/run_notifications.py`（:1354/:1636） | 通知派发 |
| `gateway/run_agent_cache.py:711`、`run_goals.py:463`、`run_profile_reconcile.py:168` | 缓存淘汰、目标、热重载 |
| `gateway/platforms/api_server.py:1492`、`shared_ingress.py:142` | HTTP 入口 |

### ③ mini 的实现（当前）

`gateway/profile_scope.py:35-54 profile_runtime_scope()`：**两件**（home + 密钥），`finally` **反序**复位；
docstring 已引真实版那句话解释"为什么不改 `os.environ`"。
**唯一产品级调用点是 `gateway/run_turn.py:76`**（一个回合）—— 适配器连接、入站预处理、
REPL 命令都不在作用域里。

### ④ mini 为什么没做成这样 / 值不值得改

**原因具体**：mini 的 profile 活动路径只剩"回合"这一条 —— 适配器按名字分键、不绑 profile（§一），
命令面是 REPL 内的本地命令（不跑回合），也没有通知/热重载子系统。所以**不是忘了**，是**没有第二条路径**。
**值不值得改**：值得（A5，~30 行）—— 但必须先有 per-profile 适配器（A6），否则作用域无处可套。
顺序：**A6 → A5**。

---

## §五 Workstream A —— 密钥作用域（精译 §4）

### ① Hermes 怎么做的

显然的写法（把 profile 的 `.env` 并进 `os.environ`）会把 A 的密钥泄漏进 B 的回合与**每个子进程**。
所以：作用域是 **contextvar**，读取走一个**四分支**的解析器，且**多路复用开着又没有作用域时直接抛**。
异常本身也分了**两层信息**：`str(exc)` 是给终端用户的一句可执行建议，开发者诊断挂 `__notes__`。

### ② 读 Hermes 的代码

```text
agent/secret_scope.py:24    _MULTIPLEX_ACTIVE: bool = False     # "Process-global (describes the
                            #  deployment mode, not a per-task value): set once at gateway startup"
agent/secret_scope.py:27/33 set_multiplex_active() / is_multiplex_active()
agent/secret_scope.py:37-46 serves_routed_profile()
    """True when the current task runs for a profile other than the process's own … The MCP registry
    scope and the check_fn cache key both follow this predicate so a served profile's view never
    aliases the launch profile's (#111151)."""
agent/secret_scope.py:49    _SECRET_SCOPE: ContextVar[Optional[Mapping[str, str]]] = ContextVar("_SECRET_SCOPE", default=None)
agent/secret_scope.py:52-77 class UnscopedSecretError(RuntimeError)
    """A secret was read in multiplex mode with no scope installed. The fix is to wrap the call path
    in ``set_secret_scope(...)`` (the per-turn / per-adapter profile scope), not to widen the global
    allowlist. ``str(exc)`` is the ONE sentence an end user can act on; the developer diagnosis
    (which secret, which doc) rides ``__notes__``…"""
agent/secret_scope.py:80-96 set_secret_scope / reset_secret_scope / current_secret_scope
agent/secret_scope.py:143-171 get_secret(name, default=None)
    """… Global vars always read ``os.environ``. With a scope installed, a miss returns ``default``
    under multiplexing (never another profile's ``os.environ`` value) but falls through to
    ``os.environ`` otherwise — single-profile deployments inject credentials via the process env
    (systemd, ``op run``), so the scope must stay a ``.env`` overlay, not a blindfold (otherwise
    cron 401s). With no scope: multiplex INACTIVE reads ``os.environ``; ACTIVE raises (fail closed)."""
agent/secret_scope.py:174-176 get_secret_str(...)
    """… Still raises ``UnscopedSecretError`` — swallowing it hides a spawn-site bug."""
agent/secret_scope.py:254-257 build_profile_secret_scope(hermes_home)
    """… Global vars are NOT copied in — ``get_secret`` reads those from ``os.environ`` — so the
    scope holds only profile secrets."""
```

**四分支速查**（`:153-171`）：白名单 → `os.environ`；有作用域命中 → 返回；有作用域未命中 →
`default if _MULTIPLEX_ACTIVE else os.environ`；**无作用域** → mux 开则 `raise`，否则 `os.environ`。

### ③ mini 的实现（当前）

`agent/secret_scope.py`（91 行）：`_MULTIPLEX_ACTIVE` / `set_multiplex_active` / `multiplex_active` /
`UnscopedSecretError` / `get_secret` **都在**；逻辑是三分支（作用域命中 → 返回；无作用域 + mux 开 → 抛；
否则 `os.environ`）。**没有全局白名单**（`_GLOBAL_ENV_EXACT` / `_is_global_env` 不存在）。

### ④ mini 为什么没做成这样 / 值不值得改

**原因具体**：mini 的 `get_secret` 是照着真实版的 fail-closed 骨架写的，但**漏了"豁免层"**。
当时的理由（我回顾第 7 章的决定）：mini 的密钥消费者少，"没有需要豁免的进程级键"。
**这个判断是错的**：`HOME`、`PATH`、`MINI_HOME` 这类描述进程的值，在多路复用下：
① 无作用域读取 → **抛**（`PATH` 都没法读）；② 有作用域但映射里没有 → 返回 `default`（**空值**）
而不是进程真值 —— 这正是白名单要挡的"读到空/误抛"。
**值不值得改**：**值得，优先做**（A9，~25 行）。名单精简到 mini 真有的两族（进程身份 + shell/解释器），
并照抄那句注释（*"API_SERVER_KEY is a credential: NOT here."* 的等价物：**凭据一律不进名单**）。

---

## §六 Workstream A 的三处"相邻缝"（精译 §4 后半）

### ① Hermes 怎么做的

同一条"作用域权威"规则要管的还有三处**不是回合**的缝：

| 缝 | 规则 |
|---|---|
| **(a) dotenv 加载器边界** | 多路复用**开**且装了 profile-home 覆盖 → **跳过进程级加载**（import 期/cron 调用方会在回合中途撞到它）；外部密钥源仍灌注进该 profile 的私有快照。**没有作用域的启动期加载不变** |
| **(b) 配置里的 `${VAR}` / `${env:VAR}`** | 装了作用域时**经 `get_secret`** 解析；多路复用下"未命中就是未命中"，**绝不读另一个 profile 的 `os.environ`**（#84079） |
| **(c) 作用域下的 `.env` 写入** | `save_env_value` 的镜像发布更新**已装的作用域映射**，而不是进程环境（#88441 / #77490） |

### ② 读 Hermes 的代码

```text
hermes_cli/env_loader.py:26    _SCOPED_SKIP_LOGGED: set[str]   # 已打过日志的 routed home（防刷）
hermes_cli/env_loader.py:334-336  # "Multiplex gateway: while a routed profile-home override is active,
                                  #  copying that profile's .env into os.environ would expose its credentials
                                  #  to sibling turns and every spawned child. Unscoped startup loads keep
                                  #  the normal path; external sources still refresh against the mapping."
hermes_cli/env_loader.py:340-350  if is_multiplex_active() and get_hermes_home_override() is not None:
                                      … hydrate_profile_secret_sources(home_path); return []
hermes_cli/config.py:1595-1611 _env_ref_lookup(name)   # ${VAR} 解析
    """… Inside a scope (a multiplexed gateway turn, a secondary profile's config load, a cron job) the
    read goes through ``agent.secret_scope.get_secret`` so the ref resolves against *that* profile's
    ``.env``: under multiplexing a miss is a miss, never another profile's ``os.environ`` value
    (#84079 — every profile "had" the default profile's ``${MATRIX_ACCESS_TOKEN}`` and fanned out)."""
hermes_cli/config.py:2586-2612 _publish_env_value(key, value)   # .env 写入后的进程内发布
    """… Under a multiplexed gateway a routed profile's write must not land in the SHARED ``os.environ``
    where every profile sees it; the installed scope mapping is updated instead so same-turn reads see
    the change. … See #77490, #88441."""
```

### ③ mini 的实现（当前）

- **(a) 有隐患**：`hermes_cli/runtime_provider.py:105 load_env_file()` 在 `:135` **直接 `os.environ[key] = val`**
  —— mini 的 `.env` 加载**没有**"mux 开 + 有覆盖就跳过"的闸。
- **(b) 无消费者**：mini 没有 `${VAR}` / `${env:VAR}` 展开能力（全仓 grep 零命中）→ 这一缝在 mini 不存在。
- **(c) 未取证的半件**：mini 的 `.env` 写入路径（`profile_cmd` / `runtime_provider`）本轮没核它是否也写 `os.environ`。

### ④ mini 为什么没做成这样 / 值不值得改

- (a) **抄**（新列 A13，~15 行）：mini 现在的加载路径在"多路复用 + 有 home 覆盖"时会把**别人的 .env**
  灌进进程环境 —— 与 §五的 fail-closed 精神直接冲突（fail-closed 只管读取，不管加载）。
  **判据**：这是 A9 的同一根因（"进程环境不是 profile 存储"），一起修最省。
- (b) **不抄**：没有表达式展开能力 → 没有受害者；写进文档即可（"mini 无 `${VAR}`"）。
- (c) **实现时先核**，核完再决定（若也写 `os.environ`，与 (a) 合并成一条改）。

---

## §七 The HERMES_HOME override（精译 §5）

### ① Hermes 怎么做的

`hermes_constants.py` 里有一个**上下文局部的覆盖**，`get_hermes_home()` **先查它、再查 `HERMES_HOME`**，
于是所有走它解析路径的东西（配置、`state.db`、技能、记忆、SOUL、会话、看板、插件发现、MCP 启动）
自动跟着当前 profile 走。另有两个**出口**处理例外：`get_process_hermes_home()`（进程级资产）与
`hermes_home_key()`（按 home 分桶的注册表键）。另有一条**一次性告警**（声明了 profile 却在默认 home 上跑）。

### ② 读 Hermes 的代码

```text
hermes_constants.py:15-17  _profile_fallback_warned = False
                           _UNSET = object()
                           _HERMES_HOME_OVERRIDE: ContextVar[str | object] = ContextVar(..., default=_UNSET)
hermes_constants.py:25-31  set_hermes_home_override(path)
    """Set a context-local Hermes home override and return its reset token.
    Deliberately does not mutate ``os.environ`` (shared by every thread in the process)."""
hermes_constants.py:101-108 get_hermes_home()
    """Hermes home: context-local override → ``HERMES_HOME`` env var → platform default."""
    #   override 非空即胜（`if override:`）；env 为空时先调 _warn_profile_fallback_once()
hermes_constants.py:119-142 hermes_home_key(path=None)   # normcase + resolve；已存在目录才进缓存
hermes_constants.py:150-157 get_process_hermes_home()
    """Hermes home of the running process, ignoring task overrides. For process-level assets (theme
    YAML, dashboard plugin manifests) that must stay visible while a request is scoped to another
    profile…"""
hermes_constants.py:73-98  _warn_profile_fallback_once()
    """Warn once when HERMES_HOME is unset but a non-default profile is sticky-active (wrong fallback)."""
    #  消息逐字结尾："… The subprocess spawner should pass HERMES_HOME explicitly (see issue #18594)."
    #  行 86-87 注释：直接写 stderr，不走 logging —— 因为它跑在 import 期（logging 还没配好）
```

### ③ mini 的实现（当前）

`hermes_constants.py:43-48`：覆盖 → `MINI_HOME` → `~/.mini-hermes` ✅（与真实版同序）。
**缺**：`hermes_home_key()` 等价物（全仓只有一处**注释**提到它）、`get_process_hermes_home()` 等价物、
一次性告警。另有一处细节不同：**只有** env/默认分支会 `mkdir`，override 分支不建目录（`:43-45`）。

### ④ mini 为什么没做成这样 / 值不值得改

- `hermes_home_key()` **值得抄**（A10）：它不是一个"辅助函数"，而是**按 home 分桶的注册表的唯一稳定键**。
  缺了它的直接后果已经在 mini 里有了：`tools/registry.py` 的 `check_fn` 缓存**不按 profile 分键**
  （第 4 章注释里写着"真实版按 `hermes_home_key()` 分键"，实现里没有）→ 多 profile 下
  A 的"终端可用"结论会被 B 复用。**成本 ~20 行**，判据清楚。
- `get_process_hermes_home()` **值得抄**（A10）：mini 的 PID/锁/状态文件现在用 `get_mini_home()` 解析 ——
  一旦作用域铺开（A5），**身份文件会跟着"当前作用域"跑**（把 A 的 PID 写进 B 的家）。
  这正是真实版那句注释防的事。
- 一次性告警：**低优先**（#15，可抄 ~15 行）—— mini 目前没有"子进程 spawner 传 home"的路径。

---

## §八 Inbound routing —— 共享 bot 路由表（精译 §6）

### ① Hermes 怎么做的

`gateway.profile_routes` 把 **(平台, 公会 id, 聊天 id, 线程 id)** 映射到 profile：**合取**匹配、
**最具体优先**、线程走**单跳**父聊天。只有多路复用开着才路由；命中的规则**目标不在服务集合 → 丢弃**。
还有一个不显眼但要命的判别项 **`bot_profile`**：路由只对**它指定的那个 bot** 收的消息生效。

### ② 读 Hermes 的代码

```text
gateway/profile_routing.py:1-12  模块 docstring（逐字）：
    """Profile-based routing: route guilds/channels/threads to different profiles.
    Matching priority, most specific first (``gateway.profile_routes`` in config.yaml):
    platform + chat_id + thread_id (14) → platform + chat_id (6) → platform + guild_id (2)
    → default profile. For Discord threads/forum posts ``parent_chat_id`` carries the
    direct parent, so a channel route also matches any thread/post under it.
    A route applies only to messages received by the bot of its ``bot_profile`` (default: the
    default profile's shared bot). Telegram DM ``chat_id == user_id`` for EVERY bot, so without
    this a ``chat_id`` route meant for the shared bot would re-home the same user's DM with a
    dedicated secondary bot into another profile (#104933)."""
gateway/profile_routing.py:55-66  ProfileRoute 字段：name / platform / profile /
                                  guild_id / chat_id / thread_id / enabled / bot_profile
gateway/profile_routing.py:68-71  @property specificity: 2*bool(guild) + 4*bool(chat) + 8*bool(thread)
gateway/profile_routing.py:85-98  matches() 关键五行逐字：
    if not self.enabled or self.platform != platform: return False
    if _bot_profile_key(self.bot_profile) != _bot_profile_key(adapter_profile): return False
    if self.thread_id and self.thread_id != thread_id: return False
    if (self.chat_id and self.chat_id not in (chat_id, parent_chat_id)
            and not _whatsapp_user_chat_ids_match(platform, self.chat_id, chat_id)
            and not _whatsapp_user_chat_ids_match(platform, self.chat_id, parent_chat_id)): return False
    return not (self.guild_id and self.guild_id != guild_id)
gateway/profile_routing.py:161    routes.sort(key=lambda r: r.specificity, reverse=True)
gateway/profile_routing.py:107-127 _coerce_route_id()   # YAML 把 unquoted snowflake 读成 int → 转 str；
                                                        # bool/float 只 warning，不静默"修"（#86470）
gateway/profile_routing.py:144     # "Validate profile name to prevent path traversal (lazy import: cycle)."
gateway/run.py:4348-4352          if matched.profile not in served: raise ProfileRouteRejected(matched.name)
gateway/run_inbound.py:173-178     if source.profile_route_rejected is True: … return None   # ← 丢弃
```

★ 最后那行 `return not (…)` 是**合取**的关键写法：前面的 `chat_id` 分支**只提前返回 False**，
不会提前返回 True —— 所以 guild 与 chat 是真 AND（测试 `test_guild_and_chat_are_conjunctive`
的注释写着这是回归修复：*"previously chat_id was checked first and returned True before guild_id
was ever consulted"*）。
★ 父链是**单跳**（`(chat_id, parent_chat_id)` 元组判断，全模块**无 `while`**）—— 与文档措辞的出入见精译附录 C ⑥。

### ③ mini 的实现（当前）

**没有**（`grep -rn "profile_route\|ProfileRoute"` 零命中）。mini 里唯一的 `route()` 是
`gateway/session_state.py:95 SessionRouter.route()` —— 那是**会话归属**（复用 / 软恢复 / 硬擦除后新建），
**不是跨 profile 的消息路由**，两者同名但不同层。

### ④ mini 为什么没做成这样 / 值不值得改

**原因具体**：没有受害者 —— mini 只有**一个** console 适配器，"一个共享 bot 服务两个 profile"
这个场景还不存在。所以第 7/8 章都没做。
**值不值得改**：**值得，而且这是本章的定义性机制**（A7，~150 行）。判据：等 mini 有了 per-profile
适配器（A6），"两个 bot 共享一个聊天"立刻变成真场景 —— 那时没有路由表，就只能靠"一个人一个 bot"，
等于回到多进程。抄的时候**照抄三处容易漏的细节**：
① `bot_profile` 判别（默认 profile 归一成 `None`）；② `_coerce_route_id` 的 int 强转（YAML 真会把
snowflake 读成 int，而入站是 str → **永远匹配不上且不报错**）；③ 拒绝 → **丢弃**（不投错）。
WhatsApp 别名折叠**不抄**（mini 无 WhatsApp，B1）。

---

## §九 Serving selected profiles —— 服务集合的单闸口（精译 §7）

### ① Hermes 怎么做的

`profiles_to_serve(...)` 是**"这个网关服务哪些 profile"的唯一闸口**：默认 profile + 每个**有效**的
具名 profile 目录。它同时给**五个面**把关：**适配器启动**、**cron 心跳**、**`/p/<profile>/` HTTP 准入**、
**路由资格**、**运行时状态面**。被排除的 profile **仍装在那儿**，也**仍能自己单独跑一个网关**。

### ② 读 Hermes 的代码

```text
hermes_cli/profiles.py:802-815  profiles_to_serve(multiplex: bool) -> List[Tuple[str, Path]]
    """``(profile_name, hermes_home)`` pairs a gateway should serve — the single chokepoint
    for "which profiles does the inbound gateway handle".

    ``multiplex=False``: exactly one entry for the *active* profile (byte-for-byte the
    historical single-profile behavior; name is ``"default"`` or the named profile's id).
    ``multiplex=True``: default plus every live named profile under ``profiles/`` (tombstoned
    profiles skipped). Pure directory read: never creates a profile dir (#94590)."""
gateway/run_adapters.py:499     profile_homes = _multiplex_profile_homes(self.config)   ← 适配器面
gateway/run.py:4342             served = {name for name, _home in _multiplex_profile_homes(config)}   ← 路由面
gateway/run_adapters.py:886-900 _record_served_profiles() → write_runtime_status(served_profiles=served) ← 状态面
hermes_cli/profiles.py:942      # clone 会剥掉源 profile 的 bot token / allowlist / 平台段
```

📌 `#94590` 那条（*"never creates a profile dir"*）值得单独记：**枚举本身不许有副作用** ——
否则"列一下服务谁"这个只读动作会凭空造出目录，而目录一存在，下一次枚举就会把它当活 profile。

### ③ mini 的实现（当前）

`hermes_cli/profiles.py:211-225 profiles_to_serve(multiplex: bool = False)` ✅ 已有，docstring 也记了
"默认永远排第一"的两条后果（共享入口的平台只在默认 profile 上跑 / 在默认 profile 停网关会停掉全部）。
`list_profiles()`（`:196-200`）是裸 `iterdir()`。

### ④ mini 为什么没做成这样 / 值不值得改

**缺的只有两件**：① **墓碑过滤**（mini 没有 `tombstoned`/`.deleted` 概念 → 残留/半删目录会被当活 profile）；
② **"枚举不建目录"的不变量注释**（mini 现在确实不建，但没有任何东西挡住以后有人加一句 `mkdir`）。
**值不值得改**：值得（A2，~10 行 + 2 条断言），成本极低。
**mini 额外要守的一条**：`profiles_root()` 是 **HOME 锚定**（`Path.home()`，刻意不看 `MINI_HOME`）——
测试必须**同时**钉 `HOME` 与 `MINI_HOME`，否则会写进用户真实 home（第 7 章已踩过）。

---

## §十 Per-profile persistence —— 每个 profile 自己的库（精译 §8）

### ① Hermes 怎么做的

`SessionStore` **不把句柄冻结在构造那一刻**：句柄按解析出的 `state.db` 路径缓存，
**每次读取都经 `_db` 重新按当前 HERMES_HOME 覆盖解析**。配对（pairing）存储**按被服务的 profile 各造一个**。

### ② 读 Hermes 的代码

```text
gateway/session.py:789-800   （#88532，逐字）
    # "SQLite handles are cached per path and resolved through ``_db`` per call, never bound
    #  once: a multiplexed gateway serves every profile from ONE process and a handle frozen to
    #  the root home would land every profile's rows in the root state.db. …
    #  The row still carries the right ``profile_name``, so the damage is invisible in the data
    #  and shows up only as the desktop listing a profile's session under the default bot …"
gateway/session.py:802-822   self._db_pinned = _DB_UNPINNED
                             self._db_handles: Dict[Path, Any] = {}
                             self._db_handle_cache = RecoverableHandleCache(handles=…, lock=…)
                             self._open_session_db_for_active_scope()     # ← 只"预热"，不冻结
gateway/session_persistence.py:43-81  _open_session_db_for_active_scope(db_path=None)
    """SessionDB for the active profile scope. ``db_path`` pins the store; otherwise
    ``_default_db_path()`` follows the context-local HERMES_HOME (resolved per call so multiplexed
    profiles reach their own store). Handles are cached per path; failed opens enter a bounded
    backoff during which callers keep using the JSONL fallback.
    Resolving here rather than once in ``__init__`` is the whole fix for #88532…"""
gateway/session_persistence.py:89-101  @property _db   # 未 pinned → 每次 _open_session_db_for_active_scope()
gateway/session_persistence.py:118-138 _profile_home_for_key()
    # "Only hits are memoized: a profile directory can be provisioned *after* startup (enrollment
    #  bridge), and a cached miss would pin that profile's rows to the ambient store for life."
gateway/run_adapters.py:895-899  self.pairing_stores[name] = (self.pairing_store if name == active
                                                             else PairingStore(profile=name))
```

### ③ mini 的实现（当前）

- 网关侧 ✅ **已经**按 home 缓存句柄：`gateway/run.py:109-116 _db()` → `_db_for(get_mini_home())`；
  `:118-124 _db_for(home)` → `self._dbs[str(home)]`。docstring 写明"让替次级 profile 干活的代码没法传错 home"。
- `hermes_state.SessionDB` 自己**构造时把路径固化**（`hermes_state.py:83-95`）—— 在 mini 的结构里没问题，
  因为 `_db_for` 每次显式传路径。
- **缺**：`_profile_home_for_key` 的"**只 memoize 命中**"（mini 没有 profile→home 的 miss 语义），
  以及配对存储的 profile 维度（mini 的配对白名单是全局一份）。

### ④ mini 为什么没做成这样 / 值不值得改

- "只 memoize 命中"**值得抄**（A11，~15 行）：它防的是**启动之后才被建出来的 profile**
  （mini 有 `profile create`，完全可能运行中新建）—— 缓存一个 miss 会把那个 profile 的行**钉到 ambient store 一辈子**。
- 配对存储的 profile 维度：**本章不动**（#28）—— mini 的授权是"默认拒绝 + 配对白名单"，
  白名单现在是全局一份；等 mini 真有"每个 profile 一套白名单"的需求再拆（拆的关键点已在真实版给出：
  `PairingStore(profile=name)`）。

---

## §十一 Per-bot session lanes —— 每个 bot 一条会话线（精译 §9）

### ① Hermes 怎么做的

会话键按 profile 加**命名空间**（默认 `agent:main`、具名 `agent:<name>`、**名字恰为 `main` 的 profile 用
`main~`**）。适配器身上带 `_owner_profile`（**配置时**就装好，早于任何入站事件），
`_session_key_profile` 的解析顺序是 **来源盖章 → owner profile → store 解析器**。
批处理、活跃会话跟踪、忙会话守卫**全部按 lane 分键**。

### ② 读 Hermes 的代码

```text
gateway/session.py:625-634  _session_key_namespace(profile)
    """``agent:<ns>`` prefix for a session key: default/None profile → ``agent:main``
    (BYTE-IDENTICAL to every historical key); named profile → ``agent:<name>`` so two profiles
    serving the same chat never collide. A profile literally named ``main`` would otherwise
    produce the default's namespace and share every session (routing index, agent cache, store)
    with it, so it is marked ``main~``: ``~`` is outside the profile-id alphabet, so the marked
    form can never be another profile's id."""
gateway/session.py:654        build_session_key(source, …, profile=None)   # 唯一构造入口
gateway/platforms/base.py:1904  self._owner_profile: Optional[str] = None
gateway/run_adapters.py:1115    adapter.set_owner_profile(profile_name)   ← 配置适配器时装上
gateway/platforms/base.py:2306-2325  _session_key_profile(source)
    """Profile namespace for an adapter-derived session key. Ingress … stamps ``source.profile``,
    so without this every bot in a multiplex … ``agent:main:`` lane. Order: ``source.profile`` →
    ``_owner_profile`` → store resolver; getattr-guarded (object.__new__ in tests), type-checked…"""
gateway/session_recovery.py:35-46    _resolve_profile_for_key(source)
    """Profile namespace for session keys: None when multiplexing is off (legacy ``agent:main``),
    else ``source.profile`` or the active profile."""
```

### ③ mini 的实现（当前）

`gateway/session_keys.py:45-63 session_key_namespace(profile)` ✅ **已对齐**（含 `main~` 规则、`~` 不在 id 字母表）；
`build_session_key(source, *, profile=None)` ✅ 是唯一入口。
**缺**：`_owner_profile` / `set_owner_profile` 那一层（`grep _owner_profile` 零命中）。

### ④ mini 为什么没做成这样 / 值不值得改

**原因具体**：mini 的会话键现在**只从 `source` 推导**（加一个调用方传的 `profile` 参数）。
在没有 per-profile 适配器之前，没有"这个键是谁的 bot 算出来的"这个问题 —— 所以那一层无处安放。
**值不值得改**：**值得**（A8，~30 行），而且**必须在 A6 之后**：一旦一个 home 一个适配器，
"同一个聊天的两个 bot"就会算出**同一个键**，于是第 8 章那套按键分桶的东西（锁、单槽覆盖、
agent 缓存、`resume_pending`）会互相踩 —— **单槽覆盖会把另一个 bot 的消息顶掉**。
抄的时候照抄三段解析与 `_bot_profile_key` 的归一（默认 profile ↔ `None`）。

---

## §十二 每 profile 适配器与 fail-soft 启动（原文散落，本章新增小节）

### ① Hermes 怎么做的

- **主 profile（默认/活动）**：自己的适配器照常起，但**缺凭据的平台要跳过**（而不是起一个永远重连的坏适配器）；
- **次级 profile**：逐 profile、逐平台建连，**每种失败都有专属日志与"跳过"**；
- **只有两类错会中止启动**：`MultiplexConfigError`（配置错，见 §十四）与"共享监听被抢"。

### ② 读 Hermes 的代码

```text
gateway/run_adapters.py:832-873  _start_secondary_profile_adapters()
    """Bring up adapters for every non-active profile (multiplex only); returns connected count.
    Each profile connects under its own HERMES_HOME + secret scope; credential/listener collisions
    are refused here — the only point seeing every profile's credentials together."""
    # 关掉多路复用时先清状态面：
    #   "``write_runtime_status`` re-stamps the previous writer's record in place, so a multiplexer's
    #    ``served_profiles`` would outlive it into this single-profile run and `hermes -p X ...` would
    #    keep refusing (exit 78) / reporting "served" for profiles nobody serves."
    for profile_name, profile_home in profile_homes:          # :855
        if profile_name == active: continue                   # 主循环负责它
        try:    connected += await self._start_one_profile_adapters(profile_name, profile_home, claimed)
        except MultiplexConfigError: raise                    # :862  配置错 → 上抛，让启动中止
        except Exception as e:                                # :865  其它错 → 记日志、跳过这个 profile
            logger.error("Failed to start adapters for profile '%s': %s", profile_name, e, exc_info=True)
    self._record_served_profiles(active, profile_homes)       # :512  → 状态面
    self._restore_secondary_completion_ledgers(profile_homes)
gateway/run_adapters.py:1004-1100  _start_one_profile_adapters()  逐平台跳过分支：
    无凭据          → logger.info("[MULTIPLEX] Profile '%s': skipping %s - no bot credential in this profile's secrets")
    relay/whatsapp  → self._note_unserved_secondary_platform(...)   # 共享进程级入口，次级会重试循环
                      # 注释："four profiles with WHATSAPP_ENABLED=true and nothing in the log is a silent dead channel."
    mirror 平台     → logger.info("[MULTIPLEX] Profile '%s': %s is served by the default profile's listener
                      at /p/%s/ — not starting a second listener")   # api_server/webhook，#100397
    适配器建不出    → logger.warning("… skipping platform '%s' - adapter creation returned None")
    重复认领        → _refuse_duplicate_claim(...) → continue        # :932-963
    connect 失败    → logger.warning("✗ %s failed to connect (profile: %s)") → _safe_adapter_disconnect
                      → _schedule_secondary_profile_startup_reconnect(profile_name, platform, adapter) → continue
    成功            → profile_map[platform] = adapter; logger.info("✓ %s connected (profile: %s)")
gateway/run_startup.py:1020-1062  _start_prefilter_platforms()（主 profile 侧）
    # "Multiplex: a platform enabled in the shared config.yaml may hold its token only in a secondary
    #  profile's .env; an empty primary would queue a reconnect loop that never heals. … skip the empty
    #  primary instead of failing loudly."  (#64674)
gateway/run_startup.py:1186-1210  _start_secondary_profiles()
    #   except MultiplexConfigError as e: logger.error("Gateway multiplexer config error: %s", str(e))
    #                                    → self._startup_fail_fatal_config(str(e)) → 启动中止
```

### ③ mini 的实现（当前）

```text
gateway/run.py:59-71   self.adapters: Dict[str, Any] = {}   # 键 = 适配器名字（与 profile 无关）
gateway/run.py:198-199 for a in self.adapters.values():
                           a.connect()                      # ← 裸循环：无 try/except
```
对照：`run_shutdown.py:80-84` 的 disconnect 循环**有** try/except —— 所以"连接没有、断开有"**不是疏忽**，
是当年没人为它写消费者。

### ④ mini 为什么没做成这样 / 值不值得改

**原因具体**：mini 只有 console 一个适配器，且它的 `connect()` 只是打印一行 —— 没有**会失败**的连接
（没有网络、没有 token），所以没有 fail-soft 的受害者。
**值不值得改**：**值得，这是本章最大的一块**（A6，~150 行）。判据：本章要引入"每个 home 一个适配器"，
一旦有了多个实例，"一个失败要不要拖垮全部"立刻从假问题变真问题。落地形态（mini 版）：
`_profile_adapters[profile][name]` + 循环里 try/except + **每种跳过都打日志**（照抄真实版的
info/warning 分级，理由：**跳过了不说 = 静默死掉的通道**）。

---

## §十三 Control plane（精译 §10）

### ① Hermes 怎么做的

桌面插件只能通过 ws JSON-RPC 够到网关，所以 profile 的枚举与配置住在 `tui_gateway/methods_profiles.py`：
六个方法（`list` / `create` / `describe` / `configure` / `set_asset` / `get_asset` + 一个 `remember_onboarding`），
**读写跑在目标 profile 的 HERMES_HOME 覆盖之下**，资产写入**原子 + 类型/大小上限**。

### ② 读 Hermes 的代码

```text
tui_gateway/methods_profiles.py:21-30  def _profile_handler(name: str, code: int):
    """``@method(name)`` whose body's uncaught exception becomes ``_err(rid, code, str(e))``."""
tui_gateway/methods_profiles.py:59-67  @contextlib.contextmanager def _hermes_home_scope(path):
    """Scope config/auth resolution to ``path`` for the block."""   # set/reset_hermes_home_override
tui_gateway/methods_profiles.py:248/362/429/592/624/662  六个 handler（错误码 5061…5066 + 5067）
    #  局部错误码：4061 name required / 4063-4064 resolve/not found / 4066 unknown asset /
    #  4067 data required / 4068 not valid base64 / 4069 asset too large / 4070 unsupported format
tui_gateway/methods_profiles.py:14-19  _ASSET_EXTS = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
    # "ext -> [(start, end, magic bytes)]; format is sniffed, the declared mime is never trusted."
tui_gateway/methods_profiles.py:654  if len(blob) > 2_000_000: return _err(rid, 4069, "asset too large …; max 2MB")
tui_gateway/methods_profiles.py:657-659  tmp = assets_dir / f"{asset}.{ext}.tmp"; tmp.write_bytes(blob)
                                         tmp.replace(assets_dir / f"{asset}.{ext}")     # ← 原子：tmp + rename
tui_gateway/methods_profiles.py:475  if len(json.dumps(incoming)) > 65536: return             # ui_meta 64KB 帽
```

★ `_hermes_home_scope` 的调用点只有 **5 处**（`:294/:313/:327` create 的辅助、`:436` describe、
`:574` configure 的 config 段）—— 这不是遗漏，而是**诚实的不一致**：`set_asset`/`get_asset`/`ui_meta`/`model`
直接读写 `profile_dir`（它们的目标本来就是"那个 profile 的目录"，不依赖 config 解析）。

### ③ mini 的实现（当前）

**没有**控制面：mini 的命令面是 argparse 子命令（`gateway run|status|stop|hooks` + `profile create|list|show`），
没有 ws/HTTP 服务、没有方法表、没有"资产"概念。

### ④ mini 为什么没做成这样 / 值不值得改

**不抄**（B7）：① 基座 —— mini 没有桌面插件这扇门（真实版这条链是"桌面 → ws → 网关"）；
② 复杂度 —— 686 行的 RPC 层对 mini 是净负担；③ 没有资产（头像）概念。
**但有一条手法要留**：**原子写 = 临时文件 + `Path.replace()`**（真实版 `:657-659`）——
建议在 mini 的 `profile create`/配置写入处照这个写法（mini 现在直接 `write_text`）。
理由写进注释：**半写的配置文件比没有配置文件更难查**。

---

## §十四 Failure modes —— 四种处置（精译 §11）

### ① Hermes 怎么做的

四种"坏"对应四种处置，不能互换（表见 §0.5）。这里补三个实现细节：

1. **`MultiplexConfigError` 的真实触发不是"占端口平台"**（那是文档的错，见精译附录 C ②⑤），
   而是**开放策略没有 allow-all opt-in**：
   ```text
   gateway/run.py:2184-2205  _own_policy_open_startup_violation(config)
       """Return a startup-abort reason when open policy lacks allow-all opt-in."""
       #  逐条：平台启用 → 取该平台的 (dm_env, group_env, allow_all_env)
       #        dm_policy/group_policy 都为非 open → 跳过
       #        GATEWAY_ALLOW_ALL_USERS 或平台级 allow-all 为真 → 跳过
       #        否则 return f"{platform.value}: open policy without allow-all opt-in"
   gateway/run_adapters.py:926  raise MultiplexConfigError(
       f"Profile '{profile_name}' enables {violation}. Enable GATEWAY_ALLOW_ALL_USERS or the platform
       allow-all flag for that profile, or change dm_policy/group_policy away from 'open'.")
   ```
2. **占端口平台不是"拒绝"，是"改成共享监听模式"**：`run_adapters.py:1139` 给二级的占端口适配器打
   `adapter._shared_listener_profile = profile_name`（**不 bind**），只有两个 MIRROR 平台被直接跳过（`:551`）。
3. **热重载只 park 坏 profile**：`run_profile_reconcile.py:125-129`
   `except MultiplexConfigError as exc: logger.error("[MULTIPLEX] Profile '%s' not served: %s", name, exc)`。

### ② 读 Hermes 的代码（补一条"失败关闭"的消费链）

```text
agent/secret_scope.py:162          raise UnscopedSecretError(...)                     ← 抛
gateway/run.py:4348-4352           raise ProfileRouteRejected(matched.name)           ← 抛
gateway/run_inbound.py:173-178     if source.profile_route_rejected is True: warning + return None   ← 消费=丢弃
gateway/platforms/base.py:4361-4376  build_source 捕 ProfileRouteRejected → source.profile_route_rejected = True
                                     # 注释：这个拒绝标记在**授权之前**就被消费掉
gateway/run_adapters.py:1430-1437    _stamp_routed_profile 捕到 → return False（不丢弃，交给上层）
```

### ③ mini 的实现（当前）

`authz.py` 的四层默认拒绝（第 7 章）**不覆盖**这些：mini 没有 `MultiplexConfigError` 等价物
（`grep "Multiplex"` 零命中），没有"启动期配置错 → 中止"这条路径，也没有"拒绝 → 丢弃"这条链
（因为还没有路由表）。

### ④ mini 为什么没做成这样 / 值不值得改

- **"配置错 → 启动中止"这条原则值得抄**（落在 A3 的守卫里）：mini 现在的 `--multiplex` 即使配得毫无意义
  （只有一个 home）也照样"启动成功"。抄法：守卫给出 blocker 就**降级 standalone + 记状态**，
  而"开放策略"这类真错（mini 没有）用等价物替代：**读不到的 profile 目录/坏 config.json → 启动即报错**。
- **"拒绝 → 丢弃"必须抄**（含在 A7）：它是整条路由链的兜底语义。
- `_own_policy_open_startup_violation` **不抄**（B9）：mini 的 `authz.py` 是"默认拒绝"，
  没有 `dm_policy=open` 这个可配置项 —— 概念不存在，抄了就是空壳。

---

## §十五 Known limitations —— 还没 profile 化的进程级状态（精译 §12）

### ① Hermes 怎么做的

原文五行表（MCP 发现 / `TERMINAL_*` / 内置工具注册表 / provider·能力注册表 / HTTP 监听·relay·进程锁）。
**其中一行已经被修掉了**：MCP 发现槽位现在**按 profile home 分桶**：

```text
hermes_cli/mcp_startup.py:12-17   # "Discovery slot per profile home (``hermes_home_key()`` follows the
                                  #  context-local HERMES_HOME override): a shared Desktop/dashboard backend
                                  #  serving several profiles runs one discovery per profile instead of the
                                  #  first profile to build an agent claiming the slot for everybody (#67605).
                                  #  A single-profile process has exactly one key, so behaviour is the old
                                  #  single-slot form."
                                  _mcp_discovery_started: Set[str] = set()
                                  _mcp_discovery_thread: Dict[str, threading.Thread] = {}
```

**另一处**（文档没写、但属于同一族）：`serves_routed_profile()` 的 docstring 逐字说
**MCP 注册表作用域与 `check_fn` 缓存键都跟随这个判据**（*"so a served profile's view never aliases the
launch profile's (#111151)"*）—— 也就是说这两处的"按 profile 分家"是**显式对齐**过的。

### ② mini 的实现（当前）

| 面 | 真实 | mini |
|---|---|---|
| MCP 发现 | 按 home 分桶（#67605） | mini 无 MCP |
| `TERMINAL_*` | 按白名单保持全局 | 无沙箱后端 |
| 内置工具注册表 | 进程级；插件工具按 profile 叠 | **进程级**（mini 无插件系统） |
| `check_fn` 缓存 | 按 `hermes_home_key()` 分键 | **不分键** ← 真隐患（缺口 #7，A10 解决） |
| 监听/relay/进程锁 | 一进程一份 | 进程锁 ✅（mini 有 PID 指纹） |

### ④ mini 为什么没做成这样 / 值不值得改

唯一的真问题是 **`check_fn` 缓存不分键** —— 它现在没爆是因为 mini 的 `check_fn` 只查"这台机器有没有
`bash`/`grep`"这类**与 profile 无关**的事实。但 mini 的第 4 章已经给了 `check_fn` 一个**带 state 的**
形态（TTL + 抖动保护 + 失败宽限），一旦将来有"按 profile 变化的可用性"（比如"这个 profile 配了搜索 key"），
**A 的结论会被 B 复用**。→ **随 A10 一起修**（`hermes_home_key` 进缓存键），成本 ~10 行。

---

## §十六 Non-goals（精译 §13）

### ① 原文的边界声明（翻译要点的三句）

- 多路复用隔离的是 **profile**，**不负责认证或授权"终端用户"**；
- **一个 profile 是一份配置，不是一个人**：网关**信任传输层与路由表**决定事件归属；
- **请求级身份**与**profile 层之上的按用户授权**不在本文范围。

### ②/③ Hermes 与 mini 各自把这件事放在哪

| | 授权层 | 位置 |
|---|---|---|
| Hermes | 四层 `authz_mixin`（平台全放行 → 平台白名单 → 私聊配对 → 全局放行 → **默认拒**）+ 每 profile 一份 `PairingStore` | `gateway/authz_mixin.py`、`gateway/pairing.py:318` |
| mini | 四层默认拒绝（第 7 章）＋**单一**配对白名单 | `gateway/authz.py` |

### ④ mini 为什么没做成这样 / 值不值得改

这节**不是机制，是边界**：mini 照抄它的价值在于**别把安全责任放错层** ——
"多路复用让两个 profile 不串味"≠"同一 profile 里两个用户不串味"。
**本章不动**（mini 的授权线在第 7 章已定性）；只在文档里保留这句判断。

---

## §十七 差距清单（= 本章实现清单）

> 状态列：`⬜` 待做（本章）/ `✅` 已完成 / `⏭` 不做（附理由）。实现完成后逐条回填 commit 与收据。

### A 批 · 要抄（13 条）

| # | 条目 | 真实出处 | 落点 | 规模 | 状态 |
|---|---|---|---|---|---|
| A1 | 开关三态 + env 覆盖 + 真值词表 + 空白不算覆盖 | `config.py:29-64/552-558` | 新 `gateway/multiplex_mode.py` + `gateway_cmd.py` | ~40 | ⬜ |
| A2 | 服务集合补墓碑过滤 + "枚举不建目录"不变量 | `profiles.py:802` | `hermes_cli/profiles.py` | ~10 | ⬜ |
| A3 | 启动守卫三条（非默认网关 / 服务集合<2 / 有 secondary 在跑）+ 异常兜底不致命 | `multiplex_mode.py:75-123` | `gateway/multiplex_mode.py` | ~60 | ⬜ |
| A4 | 裁决理由落状态文件 + 分级日志 | `multiplex_mode.py:126-149` | `gateway/status.py` + `run.py` | ~20 | ⬜ |
| A5 | 作用域铺到"活动"（适配器/入站/命令/通知），不只回合 | `run.py:1750` + 30 调用点 | `run_inbound.py` / `run.py` / `commands` | ~30 | ⬜ |
| A6 | per-profile 适配器 + fail-soft（每类跳过都有日志） | `run_adapters.py:832/1004` | 新 `gateway/run_adapters.py` | ~150 | ⬜ |
| A7 | 共享 bot 路由表（合取/最具体/单跳父链/bot_profile/int 强转/拒绝→丢弃） | `profile_routing.py`(176) + `run.py:4348` + `run_inbound.py:173` | 新 `gateway/profile_routing.py` | ~150 | ⬜ |
| A8 | per-bot lane（`_owner_profile` + 三段解析） | `base.py:1904/2306`、`run_adapters.py:1115` | `gateway/adapters/base.py` + `session_keys.py` | ~30 | ⬜ |
| A9 | 全局白名单（进程身份 + shell/解释器两族）+ `_is_global_env` | `secret_scope.py:98-135` | `agent/secret_scope.py` | ~25 | ⬜ |
| A10 | `hermes_home_key` + 进程级 home 出口（顺带修 `check_fn` 缓存分键） | `hermes_constants.py:119/150` | `hermes_constants.py` + `tools/registry.py` + `gateway/status.py` | ~35 | ⬜ |
| A11 | 句柄缓存"只 memoize 命中" | `session_persistence.py:118-138` | `gateway/run.py` | ~15 | ⬜ |
| A12 | 状态面写 `served_profiles` + 按 profile 可读 | `status.py:32/852/895` | `gateway/status.py` | ~45 | ⬜ |
| A13 | dotenv 加载器边界：mux 开 + 有 home 覆盖 → 跳过进程级加载 | `env_loader.py:340-350` | `hermes_cli/runtime_provider.py` | ~15 | ⬜ |

**A 批合计：~630 行代码 + ~180-250 行测试（新增测试组 18~22）**

### B 批 · 不抄（11 条，理由见 `/tmp/ch09-checklist.md`）

| # | 条目 | 不抄理由（三类） |
|---|---|---|
| B1 | WhatsApp 身份别名折叠 | ② 无消费者 |
| B2 | 占端口平台 shared-listener + `/p/<profile>/` ingress | ① 基座（无 HTTP 监听） |
| B3 | relay 路由戳 | ① 基座 |
| B4 | `hermes gateway migrate --multiplex` 迁移命令 | ① 基座（无服务管理器） |
| B5 | 迁移预检的 port-binder blocker | ① 基座 |
| B6 | cron 多路复用 ticker + 外部 provider 回退 | ① 基座（mini 无 cron） |
| B7 | 控制面 JSON-RPC 686 行 + 资产机制 | ① 基座 + ③ 复杂度（**但原子写手法留注释**） |
| B8 | 重复凭据/监听认领拒绝 | ① 基座（mini 适配器无 token） |
| B9 | open-policy 启动拒绝 | ③ 概念不存在（mini 默认拒绝） |
| B10 | 句柄打开退避 + JSONL 降级 | ① 基座（无 live-system guard） |
| B11 | 终端作用域桥（`tools/terminal_scope.py`） | ① 基座（无沙箱后端） |

### mini 自身的两件（本章顺手修）

| # | 问题 | 证据 | 状态 |
|---|---|---|---|
| M1 | **真 bug**：`self._last_active_key` 无赋值点，只在 `run_turn.py:181/186` 被读 → 网关里一旦审批就 `AttributeError` | `grep -rn _last_active_key` 只有两处读 | ⬜ 本章"真问题"候选 |
| M2 | `--multiplex` 半成品语义（只设标志 + 横幅） | `run.py:190/191/255` | ⬜ 由 A5/A6/A7 解决 |

### 实现顺序建议（依赖决定）

```text
第一批（地基）：A1 → A3+A4（守卫要三态）           A9+A13（同一根因：进程环境不是 profile 存储）
第二批（成型）：A6（per-profile 适配器） → A8（lane） → A5（作用域铺开）
第三批（核心）：A7（路由表，依赖 A6 的 owner profile 与 A8 的键）
第四批（配套）：A2 / A10 / A11 / A12（服务集合、分键、缓存、状态面）
最后：M1 修复 + 反向验证 + 真跑（两个 home + 一条共享 bot 路由）+ 回填本文 mini 列与收据
```

---

## 附录 A · 真实侧常量与函数索引（本章用到的）

| 文件:行号 | 名字 | 一句话 |
|---|---|---|
| `gateway/config.py:29-30` | `_TRUTHY_STRINGS` / `_FALSY_STRINGS` | 真值词表（各 4 个） |
| `gateway/config.py:45` | `_env_multiplex_profiles_override()` | env 覆盖；空白/未识别 → `None` |
| `gateway/config.py:552` | `multiplex_profiles: Optional[bool] = None` | 三态字段 |
| `gateway/config.py:251-255` | `PORT_BINDING_PLATFORM_VALUES` | 10 个占端口平台（B2，不抄） |
| `hermes_cli/config_defaults.py:1998` | `"multiplex_profiles": True` | 默认开 |
| `hermes_cli/gateway_multiplex_mode.py:24/46/75/110/126/136` | `SINGLE_PROFILE_REASON` / `MultiplexDecision` / `implicit_multiplex_blocker` / `resolve_multiplex_mode` / `record_*` / `log_*` | 裁决全家 |
| `hermes_cli/profiles.py:802` | `profiles_to_serve(multiplex)` | 服务集合单闸口 |
| `gateway/profile_routing.py:51/55/68/73/101/107/130/166` | `ProfileRouteRejected` / `ProfileRoute` / `specificity` / `matches` / `_bot_profile_key` / `_coerce_route_id` / `parse_profile_routes` / `match_profile_route` | 路由全家（176 行） |
| `gateway/run.py:1750/1784` | `_profile_runtime_scope` / `_async_…` | 作用域装配 |
| `gateway/run.py:1636` | `MultiplexConfigError` | 唯一启动致命类 |
| `gateway/run.py:2184` | `_own_policy_open_startup_violation` | 它的真实触发条件 |
| `gateway/run.py:4309/4348` | `_profile_name_for_source` / 拒绝判定 | 路由消费 |
| `gateway/run_adapters.py:832/886/926/932/1004/1139/1432` | 次级启动 / 状态记录 / 配置错 / 认领拒绝 / 逐平台循环 / shared-listener / 盖章 | 适配器全家 |
| `hermes_constants.py:25/101/119/150/73` | 覆盖 / 解析 / key / 进程 home / 一次性告警 | home 全家 |
| `agent/secret_scope.py:24/49/80/98/126/133/143/174/254` | 全局标志 / 作用域 / 安装 / 白名单 / 前缀 / 判定 / 读取 / str 版 / 构建 | 密钥全家（280 行） |
| `gateway/session.py:625/654/789-822` | 命名空间 / 建键 / 句柄不冻结 | 会话键与句柄 |
| `gateway/session_persistence.py:43/89/118` | 按作用域开库 / `_db` property / 只缓存命中 | 持久化 |
| `gateway/session_recovery.py:35` | `_resolve_profile_for_key` | 键→profile |
| `gateway/platforms/base.py:1904/2306/2105/4361` | owner profile / 三段解析 / 平台状态键 / 拒绝标记 | 适配器侧 |
| `gateway/status.py:32/166/819/852/895` | `gateway_state.json` / 路径 / 写 / served_profiles / 读 | 状态面 |
| `tui_gateway/methods_profiles.py:21/59/248…662` | 装饰器 / scope / 六个 handler | 控制面（不抄） |
| `cron/scheduler_provider.py:391/496`、`cron/jobs.py:1212` | 回退 / 多路复用 tick / 心跳 | cron（不抄） |
| `hermes_cli/mcp_startup.py:12-17` | MCP 发现按 home 分桶 | #67605（已修） |
| `hermes_cli/env_loader.py:340`、`hermes_cli/config.py:1595/2586` | 三处相邻缝 | §六 |

## 附录 B · 文档矩阵（本仓三份 + 笔记）

| 精译节 | 讲解节 | 清单条目 | 取证笔记 |
|---|---|---|---|
| §1 Overview | §一 | A5/A6（差异 #21/#22） | `notes-mini.md` §1(f)、`notes-runner.md` §3a |
| §2 The mode flag | §二 | A1 | `notes-runner.md` §1-2 |
| （原文散落） | §三 | A3/A4 | `notes-runner.md` §1.3 |
| §3 Scope composition | §四 | A5 | `notes-runner.md` §3b |
| §4 Workstream A | §五+§六 | A9/A13 | `notes-secrets.md` §1-5 |
| §5 HERMES_HOME override | §七 | A10 | `notes-secrets.md` §4 |
| §6 Inbound routing | §八 | A7 | `notes-routing.md` 全文 |
| §7 Serving selected profiles | §九 | A2 | `notes-mini.md` §2(a) |
| §8 Per-profile persistence | §十 | A11 | `notes-persistence.md` §1 |
| §9 Per-bot session lanes | §十一 | A8 | `notes-persistence.md` §1(c)(d) |
| §10 Control plane | §十三 | B7 | `notes-persistence.md` §3 |
| §11 Failure modes | §十二+§十四 | A3/A6、B5/B9 | `notes-runner.md` §3c-3f |
| §12 Known limitations | §十五 | A10（check_fn 分键） | `notes-mini.md` §3 + `mcp_startup.py` |
| §13 Non-goals | §十六 | —（边界） | — |

## 附录 C · 上游文档出入

本章共 **6 处**，完整表格与证据在《中文精译-09》附录 C（① `profiles_to_serve` 的 allowlist 参数不存在 ·
② `SecondaryPortBindingConfigError` 无此类 · ③ 限制表 MCP 那条已被 #67605 修掉 · ④ `runtime_status.json`
实为 `gateway_state.json` · ⑤ "二级启用占端口平台 = 启动即致命"语义错位 · ⑥ 父链是单跳不是多级）。
本文 §十二/§十四 已把 ②⑤ 讲透，§十五 讲了 ③，§五/§七 引了 ④ 的落点。

**仍然未核的**（诚实清单）：① 原文"已知限制"表除 MCP 外的其余四行（`TERMINAL_*` / 内置注册表 /
能力注册表 / 监听·锁）没有逐条去源码验证；② `build_migration_plan()` 与 `_host_supports_migration()`
的完整构造（只会走 B4/B5 的"不抄"结论，不影响本章实现）。

