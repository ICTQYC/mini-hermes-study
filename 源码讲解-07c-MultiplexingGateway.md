# 源码讲解 · 第 7 章 · 扩展 7.2（旧称第 9 章）：多路复用网关（Multiplexing Gateway）

> **上游锚**：`f5d1926110`（Hermes `v0.21.3`）。本文所有 `文件:行号` 都对这个 commit。
> **真实源码**：`~/.hermes/hermes-agent/`（`gateway/` 包 = **160 个 `.py` / 86,785 行**）
> **对照实现**：`mini-hermes/`（全项目 **12,883 行**代码 / **6,355 行**测试）
> **mini 侧现状**：✅ **已交付两批** —— 地基（A1/A3/A4/A9/A13）提交 `f34fdaa`、
> 成型（A6/A8/A5）提交 `9e7c8e4`；收据见 §十七。没做的只剩 §十七 里标 `⬜` 的那些。
> **本文的写法**：mini 那一栏**一律写现状** —— 做过的写 `已对齐` / `✅ 已抄（<commit>）`，
> 没做的写 `⬜ 没做（量级）`。**不写「本章做的 / 本章待实现」这类两态说法**（那要读者自己拼两段时间线）。
> **材料来源**：5 份取源码笔记（`/tmp/ch09-notes-{routing,secrets,runner,persistence,mini}.md`，共 2,230 行）
> ＋ `/tmp/ch09-checklist.md`（抄 / 不抄清单）。原《精译-09》里的 `【源码】要点` 块已并入本文。

[TOC]

---

---

## 两份代码的路径与规模

| 关注点 | 真实 Hermes | mini-hermes |
|---|---|---|
| **开关与裁决** | `hermes_cli/gateway_multiplex_mode.py`（**149 行**）：`resolve_multiplex_mode` / `implicit_multiplex_blocker` / `MultiplexDecision` | `gateway/multiplex_mode.py`（**298 行**）：三态开关 + 真值词表 + 预检三条 + 裁决落盘 ✅ |
| **共享 bot 路由** | `gateway/profile_routing.py`（**176 行**）：`ProfileRoute` / `parse_profile_routes` / `match_profile_route` / `ProfileRouteRejected` | **还没做**（A7，~150 行）—— 现在靠「收它的那个 bot 自己的 owner」兜底（§十一） |
| **服务集合** | `hermes_cli/profiles.py:802 profiles_to_serve(multiplex)` | `hermes_cli/profiles.py:235 profiles_to_serve(multiplex=False)` ✅ 已有，缺墓碑过滤 |
| **home 覆盖** | `hermes_constants.py`（**1,326 行**）：`set_hermes_home_override` / `get_hermes_home` / `hermes_home_key` / `get_process_hermes_home` | `hermes_constants.py`（**59 行**）：`set_mini_home_override` / `get_mini_home` ✅，缺 `hermes_home_key` 与进程级出口 |
| **密钥作用域** | `agent/secret_scope.py`（**280 行**）：`get_secret` / `_GLOBAL_ENV_EXACT`（36 项）/ `UnscopedSecretError` / `build_profile_secret_scope` | `agent/secret_scope.py`（**135 行**）：fail-closed ✅ + **白名单 19 项（前缀 0）** + `is_global_env` ✅ |
| **作用域装配** | `gateway/run.py:1750 _profile_runtime_scope`（**30+ 调用点**） | `gateway/profile_scope.py:35 profile_runtime_scope`（**4 处**：入站 `run_inbound.py:46`、回合 `run_turn.py:84`、适配器建/连 `run_adapters.py:150/162`）✅ |
| **每 profile 适配器** | `gateway/run_adapters.py:832 / :1004`（建、连、跳过、重连） | `gateway/run_adapters.py`（**213 行**）：`servable_homes` / 逐 profile 建连 / fail-soft / console 的 `@<profile>` ✅ |
| **会话库句柄** | `gateway/session_persistence.py:43 _open_session_db_for_active_scope` + `:89 _db` property | `gateway/run.py:137/146 _db()/_db_for(home)` ✅ 已按 home 缓存 |
| **配对存储** | `gateway/run_adapters.py:895 PairingStore(profile=name)` | `gateway/authz.py`（单一 store，无 profile 维度） |
| **状态面** | `gateway/status.py:32 gateway_state.json` / `:819 write` / `:895 read` / `:852 served_profiles` | `gateway/status.py`（PID + 指纹 + 干净退出标记）✅，缺 `served_profiles` |
| **控制面** | `tui_gateway/methods_profiles.py`（**686 行**，6 个 JSON-RPC 方法） | argparse 子命令（`profile create|list|show`） |
| **cron 心跳** | `cron/scheduler_provider.py:391`（按 profile tick + 回退）/ `cron/jobs.py:1212` | **无 cron 子系统** |

---

---

## 节 ↔ 精译 对照

| 本文 | 讲什么 | 对应《精译-09》 |
|---|---|---|
| `§一` | Overview —— 一进程 vs 一 profile | §1 |
| `§二` | The mode flag —— 开关的三道来源 | §2 |
| `§三` | 启动预检与裁决（原文散落，本文归拢） | —（原文散落，本文归拢） |
| `§四` | Scope composition —— 作用域绑在"活动"上 | §3 |
| `§五` | Workstream A —— 密钥作用域 | §4 |
| `§六` | Workstream A 的三处"相邻缝" | §4 后半 |
| `§七` | The HERMES_HOME override | §5 |
| `§八` | Inbound routing —— 共享 bot 路由表 | §6 |
| `§九` | Serving selected profiles —— 服务集合的单闸口 | §7 |
| `§十` | Per-profile persistence —— 每个 profile 自己的库 | §8 |
| `§十一` | Per-bot session lanes —— 每个 bot 一条会话线 | §9 |
| `§十二` | 每 profile 适配器与 fail-soft 启动（原文散落，本文归拢） | —（原文散落，本文归拢） |
| `§十三` | Control plane | §10 |
| `§十四` | Failure modes —— 四种处置 | §11 |
| `§十五` | Known limitations —— 还没 profile 化的进程级状态 | §12 |
| `§十六` | Non-goals | §13 |

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
| **MCP 发现槽位** | 曾进程级，现按 **profile home 分桶**（`hermes_cli/mcp_startup.py:13-16`，`#67605`） |
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

---

## §〇 差异表（本章 34 条）

> 列序：`真实 Hermes | mini 版 | 性质 | 值得改`。「mini 版」列一律指**当前代码**；
> 「值得改」列写**现状**：`已对齐` / `✅ 已抄（<commit>）` / `⬜ 没做（量级）` / `⏭ 不抄（理由）`。

| # | 关注点 | 真实 Hermes | mini 版 | 性质 | 值得改 |
|---|---|---|---|---|---|
| 1 | 开关三态 | `None`=待裁决 / `True` / `False`（`config.py:552`） | 三态齐（`--multiplex` / `--no-multiplex` / 不给=待裁决），`gateway/multiplex_mode.py:38-95` | 语义缺失 | ✅ 已抄（`f34fdaa`·A1） |
| 2 | env 覆盖 | `GATEWAY_MULTIPLEX_PROFILES` + 真值词表 + **空白不算覆盖** | `MINI_MULTIPLEX_PROFILES` + 同一套真值词表 + 空白不算覆盖 | 能力缺失 | ✅ 已抄（`f34fdaa`·A1） |
| 3 | 启动预检 | 六条（`multiplex_mode.py:75`） | 三条 + **异常兜底降级**（`multiplex_mode.py:132 implicit_multiplex_blocker()`） | ★ 机制缺失 | ✅ 已抄（`f34fdaa`·A3） |
| 4 | 裁决不许致命 | 预检异常 → standalone + warning（`:117`） | 同 ✅（预检抛异常 → standalone + warning） | 防御缺失 | ✅ 已抄（`f34fdaa`·A3） |
| 5 | 裁决理由落盘 | `write_runtime_status(multiplex_standalone_reason=…)` | `multiplex_status_fields()` 单一来源，running/stopped 两次写都带裁决栏 | 可观测性 | ✅ 已抄（`f34fdaa`·A4） |
| 6 | 服务集合 | `profiles_to_serve(multiplex)` 单闸口 + 墓碑过滤 + 纯目录读 | 同名函数 ✅ 但无墓碑过滤 | 细节差异 | ⬜ 没做（A2，~10 行） |
| 7 | 服务集合顺序 | 默认永远第一 | 已遵守 ✅ | 已对齐 | — |
| 8 | 作用域装配 | `_profile_runtime_scope`：home + 密钥 + 终端策略 | `profile_runtime_scope`：home + 密钥 ✅；**终端策略没做** | 少一件 | 已对齐 home+密钥；终端策略 ⏭ 不抄（B11） |
| 9 | 作用域覆盖范围 | **30+ 调用点**（适配器/启动/入站/回合/slash/通知/热重载…） | **4 处**：入站（一律）/ 回合（按消息的家）/ 适配器建与连（`run_inbound.py:46`、`run_turn.py:84`、`run_adapters.py:150/162`） | ★ 架构成型问题 | ✅ 已抄（`9e7c8e4`·A5） |
| 10 | 密钥失败关闭 | mux 开 + 无作用域 → 抛 | 同 ✅ | 已对齐 | — |
| 11 | 全局白名单 | 36 项 + 3 前缀（`secret_scope.py:98-130`） | **19 项精确名 + 0 前缀**（`secret_scope.py:36-52`）—— `MINI_*` 是凭据，**故意不给前缀** | ★ 缺层（会读到空值/误抛） | ✅ 已抄（`f34fdaa`·A9） |
| 12 | home 覆盖 | 覆盖 → env → 默认 | 同 ✅ | 已对齐 | — |
| 13 | `hermes_home_key()` | `hermes_constants.py:119`，给注册表稳定分键 | 无（`tools/registry.py` 的 check_fn 缓存**不分键**） | ★ 缺层（多 profile 结果互串） | ⬜ 没做（A10，~35 行） |
| 14 | 进程级 home 出口 | `get_process_hermes_home()`（身份文件不受作用域影响） | 无 | 缺层 | ⬜ 没做（A10） |
| 15 | 一次性告警 | `#18594`：声明了 profile 却在默认 home 上跑 | 无 | 缺层（低优先） | ⬜ 没做（~15 行） |
| 16 | 配置 reload 策略 | mux 开 → 默认作用域下 reload + 写回裁决 | 无 reload 概念 | 规模差距 | ⬜ 不抄（照抄会与 mini 的一次性加载冲突） |
| 17 | 路由表 | `profile_routing.py` 176 行（合取/最具体/单跳父链/enabled/bot_profile） | **无** | ★ 核心缺失 | ⬜ 没做（A7，~150 行，第三批） |
| 18 | YAML 数字 id 强转 | `_coerce_route_id`：int→str，bool/float 只警告 | 无 | 细节但真会踩（snowflake 读成 int） | ⬜ 没做（含在 A7） |
| 19 | 路由拒绝 | `ProfileRouteRejected` + served 集合判定 | 无 | 缺层 | ⬜ 没做（含在 A7） |
| 20 | 拒绝 → 丢弃 | 入站咽喉 `return None` + warning | 无 | 缺层 | ⬜ 没做（含在 A7） |
| 21 | per-bot lane | `_owner_profile` + `_session_key_profile` 三段解析 | `_owner_profile` + `_session_key_profile` 三段解析（`adapters/base.py:111/131`） | 缺一层（同聊天两 bot 会共用 lane） | ✅ 已抄（`9e7c8e4`·A8） |
| 22 | 每 profile 适配器 | `_profile_adapters[profile][platform]` | `_profile_adapters[名字][平台]` + `servable_homes()` 边界（`run_adapters.py:63/102`） | ★ 架构成型问题 | ✅ 已抄（`9e7c8e4`·A6） |
| 23 | fail-soft 启动 | 单 profile/平台失败 → 跳过 + 警告 + 排重连 | 活动与次级适配器**都** fail-soft（`run.py:250`、`run_adapters.py:138`） | ★ 可用性 | ✅ 已抄（`9e7c8e4`·A6） |
| 24 | 跳过要说出来 | 每种跳过都有专属日志（info/warning 分明） | `_adapter_note()` → `[MULTIPLEX]` 日志 + `adapters_log()` 观测面 | 可观测性 | ✅ 已抄（`9e7c8e4`·A6） |
| 25 | 重复认领拒绝 | `_refuse_duplicate_claim`（同 token/同监听） | 无 | 基座缺失（mini 适配器无 token） | ⬜ 不抄（B8） |
| 26 | mirror 平台跳过 | api_server/webhook 走默认监听镜像 | 无（mini 无 HTTP 监听） | 基座缺失 | ⬜ 不抄（B2） |
| 27 | 每 profile 持久化句柄 | 不冻结 + 按 path 缓存 + **只 memoize 命中** | 已按 home 缓存 ✅，无"只缓存命中" | 细节 | ⬜ 没做（A11，~15 行） |
| 28 | 配对存储 per profile | `PairingStore(profile=name)` | 单一 store | 缺层 | ⬜ 没做（可抄；配对白名单目前仍是全局一份，本章不动） |
| 29 | 状态面 | `gateway_state.json` + `served_profiles` + 按 profile 可读 | 有状态文件（含裁决栏）✅，缺 `served_profiles` | 可观测性 | ⬜ 没做（A12，~45 行，第四批） |
| 30 | 控制面 | 6 个 JSON-RPC 方法（686 行） | argparse 子命令 | 基座缺失 | ⬜ 不抄（B7） |
| 31 | 资产原子写 | tmp + `Path.replace()` + magic 嗅探 + 2MB | 无资产概念 | 基座缺失 | ⬜ 不抄，但**手法**记注释（B7） |
| 32 | cron per profile + 回退 | `_start_multiplex` / 外部 provider → 内置 | **无 cron** | 基座缺失 | ⬜ 不抄（B6） |
| 33 | open-policy 启动拒绝 | `_own_policy_open_startup_violation` | 无（授权是默认拒绝） | 概念不存在 | ⬜ 不抄（B9） |
| 34 | 终端作用域桥 | `install_and_reset_profile_terminal_scope` | 无（无沙箱后端） | 基座缺失 | ⬜ 没做（只留「占位 + 注释」，避免以后误以为做了隔离） |

**统计**：34 行 = ✅ **已抄 11** ＋ ⬜ **没做 12**（A2/A7/A10/A11/A12 ＋ #15/#28/#34 等）
＋ ⏭ **不抄 8**（含 #8 的"终端策略"那一半）＋ — **已对齐 3**（#7/#10/#12）＝ 34。

---

---

## §一 Overview —— 一进程 vs 一 profile

**① Hermes 怎么做的**

一个网关进程**服务所有 profile**：默认 profile 之外，每个被服务的具名 profile 各持一套 **适配器、凭据、会话库、cron 心跳**；与此同时，全进程**共用**一个事件循环、一个 HTTP 监听、一把进程锁、一个状态面。「各自持有」与「全局共用」的边界，全部由一条硬约束划出：

> **profile A 的回合，永远不许看见 profile B 的状态。**

把这条约束落地的关键布局是**两张适配器表**：`self.adapters` 永远是**默认 profile 的**那一张；次级 profile 的适配器进 `self._profile_adapters[profile][platform]`。这个「默认在明、次级在暗」的分层后面到处复现 —— 配对 store、状态面、平台状态键都照这套摆。

**② 读 Hermes 的代码**

两张表的声明，以及点明要害的那句注释（逐字）：

> ```python
> gateway/run.py:3444  # "Non-default profiles' adapters by profile then Platform;
>                      #  self.adapters stays the default's map."
> gateway/run.py:3439  self.adapters: Dict[Platform, BasePlatformAdapter] = {}
> gateway/run.py:3445  self._profile_adapters: Dict[str, Dict[Platform, BasePlatformAdapter]] = {}
> ```

要害是后半句 **`self.adapters stays the default's map`**：默认 profile 不是「第一个 profile」，而是一个**轴心** —— 次级全都挂在它下面。

**③ mini 的实现（现状）**

两张适配器表**已对齐**：`self.adapters` 是活动 profile 的，次级进 `self._profile_adapters[名字][平台]`（`gateway/run.py:64 / :82`；表逻辑在 `gateway/run_adapters.py`，**213 行**）。服务集合先过一道边界再落账 —— `gateway/run.py:242` 的 `self.served_homes = self.servable_homes()`；启动次级适配器、以及给所有适配器设 `multiplex_note`（`@<profile>` 提示语**只在多路复用开着时出现**）在 `gateway/run.py:259-268`。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 「默认在明、次级在暗」的布局 | 两张表（`self.adapters` / `_profile_adapters`） | ✅ 有 |
| 运行中增删 profile | 控制面（JSON-RPC 686 行） | ⬜ 没做（基座缺失 B7；mini 的 `profile create\|list\|show` 是 argparse 子命令，§十三） |
| cron 心跳 | 每 profile 一套 | ⬜ 没做（无 cron 子系统 B6） |

另有一条 mini 自己的判据：`served_homes` 现在**真的决定消息归属**（`_profile_home_for_source` → 作用域 → 会话库），不再只是横幅上的一个数字。

---

## §二 The mode flag —— 开关的三道来源

**① Hermes 怎么做的**

多路复用开关 `gateway.multiplex_profiles` 也接受顶层写法，**默认开**；解析顺序是 **env > config.yaml > unset**。要害在**三态**而不是布尔：`None`（未设置，等 boot 裁决）/ `True` / `False`。**空白与未识别都不算覆盖** —— 它们回落成 `None`，把决定权交回下层，而不是被当成 `False` 把功能关掉。配置里留 `None` 是刻意的：它是一个**待 boot 裁决的请求**，不是结论；所有读者都按真值判断，所以未决的 `None` 永远不会「不小心」开启多路复用。

另一处易错点在 `unset` 的处理：别的进程**不猜** unset 的默认，而是先读**活着的那个网关**记录的 `served_profiles`，再读显式开关 —— 否则「还没迁移」会被误读成「已经在多路复用」。

**② 读 Hermes 的代码**

env 覆盖函数把「空白 ≠ False」这条防御写进了 docstring（逐字）：

> ```python
> gateway/config.py:45-64  def _env_multiplex_profiles_override() -> "bool | None":
>     """… ``None`` when unset, blank, or unrecognized so the caller keeps the config.yaml
>     value (env > config > default). Blank is deliberately ``None``, not ``False``: a
>     provisioned-but-unpopulated Fly secret arrives as ``""`` and must NOT shadow
>     a config.yaml opt-in."""
> ```

要害是 **`Blank is deliberately ``None``, not ``False``**：托管部署里一个只被 provision、还没填值的 secret 到达时是 `""`；若把 `""` 当成 `False`，它就会**盖掉** config.yaml 里的显式开启。

**③ mini 的实现（现状）**

`gateway/multiplex_mode.py`（**298 行**）实现了同形状的三态：命令行 `--multiplex` / `--no-multiplex` / 不给（=`None`），外加 `MINI_MULTIPLEX_PROFILES` 覆盖（用真值词表，**空白与未识别词不算覆盖**），再由裁决函数 `resolve_multiplex_mode()` 收口；`gateway_cmd.py` 的旗标是同一个 dest、`default=None`。

**④ 与真实版的差别**

三态与「空白不算覆盖」这一层，mini 与真实版对齐；差的只是**来源渠道**与**防御对象**：mini 的配置面只有 `config.json` + 命令行旗标，**没有托管部署**（没人往容器里塞 `MINI_*` 空值），所以「空白 ≠ False」在 mini 里**暂时没有受害者** —— 这条防御是照着机制抄的，不是被踩出来的。

---

## §三 启动预检与裁决（原文散落，本文归拢）

**① Hermes 怎么做的**

「默认开」必须配一个**预检**，否则一次安装里两个进程会抢同一个 bot token —— 那不是报错，是**间歇性错乱**。预检的六条（逐字见 §0.2）按**短路顺序**排：最便宜、最能排除大类的排前面，把昂贵的宿主探测留到后面。裁决结果要记下**来源**（`config` / `default` / `guard` 三种之一），并**落盘 + 分级打日志**：`guard` 且单 profile 记 info，其它 `guard` 记 warning。最后一条纪律：**预检崩了不许拖垮网关** —— 异常被兜住，网关退回 standalone，而不是陪着一起挂。

**② 读 Hermes 的代码**

「为什么单 profile 那条排第一」写在注释里（逐字）：

> ```python
> hermes_cli/gateway_multiplex_mode.py:100-102
> # "Cheap and first: a single-profile install has nothing to
> #  multiplex, and the fail-closed secret scope the multiplexer
> #  arms buys it nothing. (Also keeps every embedded/test runner
> #  off the service-manager probes below.)"
> ```

要害是 **`Cheap and first`**：单 profile 的第 1 条既最省钱，又能把嵌入式 / test runner 挡在后面那些**昂贵的 service-manager 探测**之前 —— 顺序本身就是设计。

**③ mini 的实现（现状）**

`gateway/multiplex_mode.py`（**298 行**）以 `resolve_multiplex_mode()` 定三段来源（显式 → env → 预检），`implicit_multiplex_blocker()` 只有三条检查（① 非默认 profile 自己的网关 ② 服务集合 < 2 ③ 有 secondary 还在跑网关），`record_multiplex_decision()` 把裁决与理由写进状态文件；**预检抛异常 → standalone + warning**，同样不许把网关带走。

★ mini 另有一条**真实版没有**的检查 **1b**：本进程 home 不是本机默认家 → 只服务自己（理由见 §九）。

**④ 与真实版的差别**

**真实版六条预检，mini 三条** —— 两条不适用、一条自加，理由都写在代码注释里：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 预检条数 | 六条（短路排序） | 三条（同上 ① / ② / ③） |
| 第 3 条 · host 可迁移性 | 判据是 s6 / systemd / Windows 计划任务这类「宿主看不见 per-profile 网关」的概念 | ⬜ 没做 —— ① 类基座缺失 |
| 第 5 条 · 迁移预检 blocker | 针对「把整个 fleet 折进一个进程」这个迁移动作 | ⬜ 没做 —— mini 没有 `gateway migrate`，这个动作不存在 |
| 检查 1b | 没有 | ★ mini 自加 |

★ **1b 为什么必须自加**：`profiles_root()` 是 **HOME 锚定**的 —— `MINI_HOME` 指到别处时（测试 / 演示 / 子进程），若顺手服务 HOME 那边的 profile，就等于**往用户真实的家写东西**（实测踩过），所以先拦下来。

---

## §四 Scope composition —— 作用域绑在「活动」上

**① Hermes 怎么做的**

每个入站事件，在**任何 profile 自己的代码跑起来之前**，先合成**三个**上下文局部作用域，回合结束再反序拆掉：

```text
平台事件 → 路由命中 → source.profile 盖章
        → _profile_runtime_scope(home)
             ├─ set_hermes_home_override(home)      配置/库/技能/记忆/会话 都按它解析
             ├─ set_secret_scope(profile 的 .env)   provider 密钥、平台 token
             └─ install_and_reset_profile_terminal_scope(home)   终端/沙箱策略
        → agent 回合（工作线程靠 copy_context() 继承）
        → finally 反序复位
```

**作用域绑在「profile 的活动」上，不是「回合」上** —— 这是本章最容易看漏的一句：它不仅包住 agent 回合，还包住**适配器的建 / 连 / 重连、入站预处理、slash 命令、通知、热重载、平台监听**。

**② 读 Hermes 的代码**

核心那个 contextmanager 的 docstring，把「三个作用域 + 为什么不改 `os.environ`」一并说清（逐字）：

> ```python
> gateway/run.py:1750-1783
> """Scope config/skills/memory AND credentials to a profile for one turn (multiplexed path only).
> ``set_hermes_home_override`` is a contextvar (reaches the agent worker via ``copy_context()``);
> ``set_secret_scope`` makes the profile ``.env`` the credential source without mutating
> ``os.environ``, so subprocesses never inherit cross-profile secrets."""
> # 末端还要装"该 profile 的**完整**终端策略"：
> # "never ambient TERMINAL_* a prior turn set … (first-writer-wins backend leak; #68559)"
> ```

要害是两个词：**`contextvar`**（所以能穿过 `copy_context()` 抵达 agent 工作线程）与 **`without mutating os.environ`**（所以子进程永远不会继承到跨 profile 的凭据）。调用点铺得很开 —— **15 个文件、30+ 处**，从适配器建 / 连 / 重连、启动期、回合与后处理、入站预处理、每条 slash 命令、通知派发，一直铺到缓存淘汰 / 目标 / 热重载、HTTP 入口（完整清单见附录 A）。

**③ mini 的实现（现状）**

`gateway/profile_scope.py:35` 的 `profile_runtime_scope()` 只做**两件**（home + 密钥），`finally` **反序**复位；docstring 已引真实版那句话解释「为什么不改 `os.environ`」。产品级调用点**4 处**：入站 `run_inbound.py:46`（**一律**套，含活动 profile）、回合 `run_turn.py:84`（按**这条消息的家**）、次级适配器的**建**与**连** `run_adapters.py:150/162`（`hydrate_secrets=False`）。

**④ 与真实版的差别**

真实版 **30+ 调用点**（适配器 / 启动 / 入站 / 回合 / slash / 通知 / 热重载……），mini **4 处** —— 差额全部来自「mini 没有那个子系统」（通知 / 热重载 / 控制面 / 终端策略），不是漏套。

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 调用点 | 15 个文件、30+ 处 | 4 处 |
| 终端策略 | `install_and_reset_profile_terminal_scope` | ⬜ 没做（无终端子系统） |
| 缓存 spec | 按 home 分键 | 按 home 分键（`gateway/run.py:129-133`） |

★ **「只在次级 profile 才套」是个真陷阱**：那条路会漏掉**回合之前**读凭据的地方（`_open_session` 要 provider spec），fail-closed 会当场抛 `UnscopedSecretError` —— mini 实测踩过，改成入站**一律**套（嵌套同名作用域无害）。★ **缓存不能跨 profile 共享**：`_provider_spec()` 原来只缓存**一份** spec（第 7/8 章的写法），多路复用下等于「把默认 profile 的密钥 / base_url / 模型发给所有人」→ 已改成**按 home 分键**（`gateway/run.py:129-133`）。这条是 A5 的直接推论，第 7/8 章看不出来。

---

## §五 密钥作用域：一份凭据该不该被另一个 profile 读到

**① Hermes 怎么做的**

多路复用的网关让**一个进程服务多个 profile**。这里最诱人也最危险的一步，是把某个 profile 的 `.env` 直接并进 `os.environ` —— 一旦这么做，**A 的密钥会泄漏进 B 的回合**，而且进程启动的**每一个子进程**都会继承它。真实版于是把「作用域」做成**上下文局部**的东西，而不是往进程环境里写：

- **作用域是 contextvar**：`set_secret_scope(mapping)` 装的这份密钥只对**当前任务**可见（contextvar 会经 `copy_context()` 传进 agent 的工作线程），所以子进程**永远不继承**别的 profile 的密钥；
- **读取走一个四分支解析器** `get_secret(name)`：① 命中**全局白名单** → 读 `os.environ`；② 有作用域、命中 → 返回作用域里的值；③ 有作用域、未命中 → 多路复用下返回 `default`（**绝不回落到另一个 profile 的 `os.environ`**），单 profile 下才回落 `os.environ`；④ **没有作用域** → 多路复用开着就**抛** `UnscopedSecretError`（fail closed），否则读 `os.environ`；
- **白名单里放的是「真正全局」的键**：`PATH`、`HOME` 这类描述**进程/部署**的值并不是 profile 私密；多路复用下仍要照常读 `os.environ`，否则会把一次正确的读当成违规读、直接崩。判据是**精确名或前缀**任一命中，且刻意保持精简（拿不准就当它是 profile 密钥）；
- **异常本身分两层信息**：`str(exc)` 是给终端用户的**一句可执行建议**，开发者诊断（哪个密钥、哪份文档）挂 `__notes__`。

一句话因果：「用错别 profile 的凭据」是一种**静默故障，肉眼看不出来**；这套机制要的就是「要么读到正确的那份、要么响亮地炸」，**绝不允许悄悄回落到进程环境**。

**② 读 Hermes 的代码**

上面四条，真实版压在 `agent/secret_scope.py:143-171` 的一段 docstring 里（逐字）：

> ```python
> def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
>     """Resolve a credential by env-var name, honoring the active profile scope.
>
>     Global vars always read ``os.environ``. With a scope installed, a miss returns
>     ``default`` under multiplexing (never another profile's ``os.environ`` value)
>     but falls through to ``os.environ`` otherwise — single-profile deployments
>     inject credentials via the process env (systemd, ``op run``), so the scope
>     must stay a ``.env`` overlay, not a blindfold (otherwise cron 401s). With no
>     scope: multiplex INACTIVE reads ``os.environ``; ACTIVE raises (fail closed).
>     """
> ```

要害在最后三行 —— *"must stay a `.env` overlay, not a blindfold"*：作用域是**叠加层、不是眼罩**，单 profile 部署靠 systemd / `op run` 把凭据注进进程环境，所以没作用域时**仍该读** `os.environ`（不读会害得 cron 401）。真正把关死的是 *"With no scope … ACTIVE raises (fail closed)"*：**多路复用开着又没有作用域 = 直接抛**，不给回落的机会。

**③ mini 的实现（现状）**

`agent/secret_scope.py`（**135 行**）把骨架照抄了：`GLOBAL_ENV_EXACT`（**19 项精确名**，两族：进程身份 / shell 与解释器）＋ `GLOBAL_ENV_PREFIXES`（**0 个**）＋ `is_global_env()`；`get_secret` 是**四分支**（全局白名单 → 当前作用域 → 兜底 `os.environ`；多路复用下无作用域则抛 `UnscopedSecretError`）。两处刻意的取舍：

- ★ **前缀 0 个是刻意的**（真实版有 3 个前缀，这里相反）：mini 的凭据就住在 `MINI_*`（`MINI_SEARCH_API_KEY`、`MINI_TAVILY_API_KEY`）—— **加一个 `MINI_` 前缀 = 把凭据放行**，这是这份名单最容易犯的错，代码注释里逐字写了理由（`API_SERVER_KEY is a credential: NOT here.` 的等价物：**凭据一律不进名单**）。
- ★ **fail-closed 得真有消费者才算数**：`get_secret` 原先**没有产品级调用点**（凭据都直读 `os.environ`）；现在 `hermes_cli/runtime_provider.py` 与 `tools/web_search.py` 的凭据读取都走作用域，于是「多路复用下没有作用域」从理论变成**会当场炸的真路径**。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 作用域载体 | contextvar（不进 `os.environ`） | ✅ 有（`_SECRET_SCOPE`，同形） |
| 四分支 `get_secret` | 白名单 / 有命中 / 有未命中 / 无作用域 | ✅ 有（同序） |
| 全局白名单前缀 | 3 个（`HERMES_KANBAN_` / `HERMES_TELEGRAM_` / `TERMINAL_`） | ✅ 名单已建（**0 个前缀**，刻意） |
| 进程级键的豁免 | 白名单挡（`PATH` / `HOME` 类不崩） | ✅ 有（`GLOBAL_ENV_EXACT` 19 项） |
| fail-closed 的消费者 | 遍布各读取点 | ✅ 有（`runtime_provider.py` / `tools/web_search.py`） |

为什么名单长这样：豁免层的判据是「**描述进程/部署的键**」，与「密钥消费者多不多」无关。`HOME`、`PATH`、`MINI_HOME` 这类键在多路复用下若不当豁免，会二选一地坏掉：① 无作用域读取 → **抛**（连 `PATH` 都读不了）；② 有作用域但映射里没有 → 返回 `default`（**空值**）而非进程真值 —— 这正是白名单要挡的「读到空 / 误抛」。名单只收 mini 真有的两族（进程身份 ＋ shell/解释器），**凭据一律不进**（差距清单 A9，已落地）。

---

## §六 三处「相邻缝」：作用域权威还得管住的三处非回合边界

**① Hermes 怎么做的**

「作用域权威」这条规则不只管**回合**（一轮对话），还要管三处**不是回合**的边界 —— 它们平时不在视线里，一旦漏掉就是同一种病：凭据在进程里串了。三处的机制分别是：

- **(a) dotenv 加载器边界**：多路复用**开着**且装了 profile-home 覆盖时，**跳过进程级加载**（import 期、cron 调用方都会在回合中途撞到它）；外部密钥源仍灌注进该 profile 的私有快照。**没有作用域的启动期加载不变** —— 否则默认 profile 自己的 `.env` 都读不进来，网关起不来。
- **(b) 配置里的 `${VAR}` / `${env:VAR}`**：装了作用域时**经 `get_secret`** 解析；多路复用下「未命中就是未命中」，**绝不读另一个 profile 的 `os.environ`**（`#84079` —— 否则每个 profile 都「拥有」默认 profile 的 `${MATRIX_ACCESS_TOKEN}`，一次配错全体扇出）。
- **(c) 作用域下的 `.env` 写入**：`save_env_value` 落盘后的**镜像发布**更新**已装的作用域映射**，而不是进程环境（`#88441` / `#77490`）。

**为什么必须有 (a) 这道闸**：(b)/(c) 都建立在「读取经作用域」之上，可一旦别人的 `.env` 已经进了 `os.environ`，**任何直接读 `os.environ` 的代码（包括子进程）就绕过了作用域** —— fail-closed 管的是「读」，管不了「灌」。所以闸必须落在**加载器**这一层，而不是等读取时再拦。

**② 读 Hermes 的代码**

(a) 那条规则，真实版写在 `hermes_cli/env_loader.py:334-336` 的注释里（逐字）：

> ```python
> # Multiplex gateway: while a routed profile-home override is active, copying that profile's .env
> # into os.environ would expose its credentials to sibling turns and every spawned child. Unscoped
> # startup loads keep the normal path; external sources still refresh against the profile mapping.
> ```

要害是 **"expose its credentials to sibling turns and every spawned child"** —— 一句话点明「灌进 `os.environ`」为什么不只是当下这一轮的事：**兄弟回合和每个子进程都会看到它**。后半句 "Unscoped startup loads keep the normal path" 是边界：**只在有覆盖时跳**，普通的启动期加载照旧。

**③ mini 的实现（现状）**

- **(a) 已加闸**：`hermes_cli/runtime_provider.py` 的 `_multiplex_dotenv_boundary_active()` —— **多路复用开 且 有 profile-home 覆盖** → 跳过进程级 `.env` 加载（每个 home 只记一次跳过日志，防刷屏）；判据照抄真实版 `env_loader.py:340` 那一行 `is_multiplex_active() and get_hermes_home_override() is not None`。
- **(b) 无消费者**：mini 没有 `${VAR}` / `${env:VAR}` 展开能力（全仓 grep 零命中）→ 这一缝在 mini 不存在。
- **(c) 未取证的半件**：mini 的 `.env` 写入路径（`profile_cmd` / `runtime_provider`）**没核**它是否也写 `os.environ`。

**④ 与真实版的差别**

| 缝 | 真实 Hermes | mini 现状 |
|---|---|---|
| (a) dotenv 加载器边界 | 多路复用 + 有 profile-home 覆盖 → 跳过进程级加载 | ✅ 有（`_multiplex_dotenv_boundary_active()`，A13，~15 行） |
| (b) 「${VAR}」/「${env:VAR}」展开 | 装了作用域时经 `get_secret` 解析 | ⬜ 没做（mini 无展开能力，全仓 grep 零命中） |
| (c) 作用域下「.env」写入 | 镜像发布更新已装的作用域映射（`#88441` / `#77490`） | ⬜ 未取证（`profile_cmd` / `runtime_provider` 是否也写 `os.environ` 没核） |

为什么现在这样：(a) 与 §五 的 fail-closed 是**同一根因**（「进程环境不是 profile 存储」）—— fail-closed 只管「读」不管「灌」，所以这道闸只能落在加载器这一层，一起修最省。(b) 没有表达式展开能力 → 没有受害者，无需实现。(c) 属于「实现时先核」，核完再定：若也写 `os.environ`，与 (a) 合并成一条改。

---

## §七 HERMES_HOME 覆盖：这一轮到底在哪个家干活

**① Hermes 怎么做的**

`hermes_constants.py` 里有一个**上下文局部的覆盖**（一个 `ContextVar`）：`get_hermes_home()` **先查它、再查 `HERMES_HOME`**、最后落平台默认。于是所有走它解析路径的东西（配置、`state.db`、技能、记忆、SOUL、会话、看板、插件发现、MCP 启动）都自动跟着当前 profile 走。用 contextvar 而不是改 `os.environ`，理由很直接：**`os.environ` 是进程里所有线程共享的**，改它等于把「现在是谁」变成一份全局状态，并发跑的两个回合会互相看到对方的 home。

另有两个**出口**处理例外：

- `get_process_hermes_home()`：给**进程级资产**（主题 YAML、dashboard 插件清单）用 —— 它们必须在「请求被绑到别的 profile」时仍然可见；
- `hermes_home_key()`：**按 home 分桶的注册表键**（normcase + resolve；已存在的目录才进缓存，因为 `resolve()` 是文件系统调用）。

还有一条**一次性告警**：声明了 profile 却没设 `HERMES_HOME` 时会**静默落到默认 home**，这条路径发出一次警告。

**② 读 Hermes 的代码**

两条函数各自的 docstring 把「顺序」和「为什么用 contextvar」一次说清（逐字）：

> ```python
> def set_hermes_home_override(path: str | Path | None) -> Token:
>     """Set a context-local Hermes home override and return its reset token.
>
>     Deliberately does not mutate ``os.environ`` (shared by every thread in the process).
>     """
>
> def get_hermes_home() -> Path:
>     """Hermes home: context-local override → ``HERMES_HOME`` env var → platform default."""
> ```

要害两句：`get_hermes_home()` 那句就是**解析顺序**（覆盖 → 环境变量 → 平台默认）；`set_hermes_home_override()` 那句 *"Deliberately does not mutate `os.environ` (shared by every thread in the process)"* 是**为什么非得 contextvar** —— 进程里所有线程共用 `os.environ`，改它就不是「这一轮的 home」而是「所有人的 home」了。

**③ mini 的实现（现状）**

`hermes_constants.py:43-48` 的解析顺序 = 覆盖 → `MINI_HOME` → `~/.mini-hermes` ✅（与真实版同序）。**缺**三件：`hermes_home_key()` 的等价物（全仓只有一处**注释**提到它）、`get_process_hermes_home()` 的等价物、那条一次性告警。另有一处细节不同：**只有** env/默认分支会 `mkdir`，override 分支不建目录（`:43-45`）。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 解析顺序 | 覆盖 → `HERMES_HOME` → 平台默认 | ✅ 有（覆盖 → `MINI_HOME` → `~/.mini-hermes`，同序） |
| 按 home 分桶的注册表键 | `hermes_home_key()` | ⬜ 没做（A10，~20 行） |
| 进程级 home | `get_process_hermes_home()` | ⬜ 没做（A10） |
| 一次性告警 | `_warn_profile_fallback_once()`（`#18594`） | ⬜ 没做（`#15`，~15 行） |
| override 分支建目录 | — | 只有 env/默认分支 `mkdir`，override 分支不建（`:43-45`） |

差在哪、为什么现在这样：

- `hermes_home_key()` 不是「辅助函数」，而是**按 home 分桶的注册表的唯一稳定键**。缺它的后果已经在 mini 里：`tools/registry.py` 的 `check_fn` 缓存**不按 profile 分键**（第 4 章注释里写着「真实版按 `hermes_home_key()` 分键」，实现里没有）→ 多 profile 下 A 的「终端可用」结论会被 B 复用。
- `get_process_hermes_home()`：mini 的 PID/锁/状态文件现在用 `get_mini_home()` 解析 —— 一旦作用域铺开（A5），**身份文件会跟着「当前作用域」跑**（把 A 的 PID 写进 B 的家）。这正是真实版那句注释防的事。
- 一次性告警低优先：mini 目前没有「子进程 spawner 传 home」这条路径。

---

## §八 共享 bot 路由表：一条消息该进哪个 profile

**① Hermes 怎么做的**

多路复用的前提是「**一条消息进来时得先判它是哪个 profile 的**」。真实版把这层判断放在一张**配置里的路由表**上，规则很朴素：

- **合取**：平台、公会、聊天、线程**每一项都满足**才算命中（不是「匹配到一项就赢」）；
- **最具体优先**：线程(14) > 聊天(6) > 公会(2) > 默认(0) —— 打分排序，不是「配置里谁在前谁赢」；
- **线程走单跳**：Discord 的线程只带**直接父聊天**，所以一条「频道级」规则**也管得住它下面所有线程**；
- 只在多路复用开着时才路由；命中但**目标不在服务集合 → 丢弃**（不投错、不乱投）；
- 还有一项不看就漏的判别项 **`bot_profile`**：规则**只对「它指定的那个 bot 收到的消息」生效**。

最后那条为什么要命：Telegram 里**每个 bot 看到的私聊 `chat_id` 都是同一个 `user_id`**。没有 `bot_profile` 这层，一条原本写给「共享 bot」的 `chat_id` 规则，会把同一个用户**发给专属 bot 的私聊**也一并改判到别的 profile（上游 `#104933` 就是这么发现的）。

**② 读 Hermes 的代码**

模块 docstring 一句就把上面五条说全了（逐字）：

> ```python
> """Profile-based routing: route guilds/channels/threads to different profiles.
> Matching priority, most specific first … platform + chat_id + thread_id (14) →
> platform + chat_id (6) → platform + guild_id (2) → default profile. …
> A route applies only to messages received by the bot of its ``bot_profile`` …
> Telegram DM ``chat_id == user_id`` for EVERY bot, so without this a ``chat_id`` route
> meant for the shared bot would re-home the same user's DM with a dedicated secondary
> bot into another profile (#104933)."""
> ```

打分与「合取」各落一处：`specificity = 2*公会 + 4*聊天 + 8*线程`（所以线程最重）；`matches()` 的**最后一行**是 `return not (guild_id and guild_id != guild)` —— 前面的 `chat_id` 分支**只提前返回 False、从不提前返回 True**，公会与聊天才是真 AND（这是回归修复，注释写着 *"previously chat_id was checked first and returned True before guild_id was ever consulted"*）。

**③ mini 的实现（现状）**

**还没有路由表**：现在「一个进程多个 bot 各走各的」靠的是**收它的那个 bot 自己的身份**（适配器的 `owner_profile`，§十一 的三段解析）—— 默认 bot 的消息归默认 profile，`lab` bot 的归 `lab`。这覆盖了「一个人一个 bot」，覆盖不了「**按聊天/线程把消息改判到别的 profile**」。

> 📌 别被同名函数骗到：`SessionRouter.route()`（`gateway/session_state.py:95`）是**会话归属**（复用 / 软恢复 / 硬擦除后新建），跟「跨 profile 的消息路由」是两层东西。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 按聊天/线程改判 | 路由表 176 行（合取 + 最具体 + 单跳父链） | ⬜ 没做（A7，~150 行） |
| 判别项 `bot_profile` | 归一成 `None` | ✅ 有（`bot_profile_key`，但用在 lane 上、还没用在路由上） |
| 命中但不在服务集合 | 丢弃 + warning | ⬜ 没做（要先有路由表） |
| YAML 数字 id | `_coerce_route_id`：int→str，bool/float 只警告 | ⬜ 没做（真会踩：snowflake 读成 int → **永远匹配不上且不报错**） |

**为什么现在没做**：没有受害者 —— mini 只有 console 一个平台，「两个 bot 共享一个聊天」还不存在；有了 per-profile 适配器（A6）之后这层才有意义，所以 A7 排在第三批。

---

## §九 服务集合的单闸口：哪些 profile 被网关服务、由谁说了算

**① Hermes 怎么做的**

`profiles_to_serve(...)` 是「这个网关服务哪些 profile」的**唯一闸口** —— 它返回「默认 profile ＋ 每一个**有效**的具名 profile 目录」。这一个函数同时给**五个面**把关：**适配器启动**、**cron 心跳**、**`/p/<profile>/` HTTP 准入**、**路由资格**、**运行时状态面**。

关键不在「怎么筛」，而在**「谁来筛」只有一处**：被排除的 profile **仍装在那儿**，也**仍能自己单独跑一个网关** —— 排除只是说「这个进程不接它」，不是「把它删了」。

还有一条必须记住的边界：**枚举本身不许有副作用**（`#94590`）。「列一下服务谁」这个动作必须**纯粹是读目录** —— 否则它凭空造出一个目录，而**目录一存在，下一次枚举就会把它当活 profile**。

**② 读 Hermes 的代码**

函数 docstring 一句就说全了上面所有事（逐字）：

> ```python
> ``(profile_name, hermes_home)`` pairs a gateway should serve — the single chokepoint
> for "which profiles does the inbound gateway handle".
>
> ``multiplex=False``: exactly one entry for the *active* profile (byte-for-byte the
> historical single-profile behavior; name is ``"default"`` or the named profile's id).
> ``multiplex=True``: default plus every live named profile under ``profiles/`` (tombstoned
> profiles skipped). Pure directory read: never creates a profile dir (#94590).
> ```

要害是两个词：**single chokepoint**（判据只此一处，别在别的地方再写一遍「哪些该服务」），以及 **"never creates a profile dir"**（这个只读动作**不许有副作用**）。

**③ mini 的实现（现状）**

`hermes_cli/profiles.py:235-249 profiles_to_serve(multiplex=False)` **已有**，docstring 也记了「默认永远排第一」的两条后果 —— **共享入口的平台只在默认 profile 上跑**，**在默认 profile 停网关会把所有被服务的 profile 一起停掉**。`list_profiles()`（`:196-200`）是裸 `iterdir()`。

★ 网关侧另加了一层**边界**（mini 自有，真实版没有对应物）：`gateway/run_adapters.py:63 servable_homes()` —— **home 不是本机默认家 → 只服务自己**，且只加 `profiles/` 下的具名 profile。理由具体：`profiles_root()` 是 **HOME 锚定**的（`Path.home()`，刻意不看 `MINI_HOME`），`MINI_HOME` 指到别处时照抄真实版那句 `profiles_to_serve(True)` 会**越界到用户真实的家**（实测踩过：测试进程替用户的 `default` ＋ `lab` 建了适配器）。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 墓碑过滤 | 跳过 `tombstoned` profile | ⬜ 没做（没有 `.deleted` 概念 → 残留/半删目录会被当活 profile） |
| 「枚举不建目录」的不变量 | 写在 docstring（`#94590`） | ⬜ 无（mini 现在确实不建，但没有任何东西挡住以后有人加一句 `mkdir`）（A2，~10 行 ＋ 2 条断言） |
| 服务集合的边界 | 无（`profiles_to_serve(True)` 直接读目录） | ✅ 有（`servable_homes()` 一层，防 HOME 锚定越界） |

mini 额外要守的一条：`profiles_root()` 是 HOME 锚定，所以测试必须**同时**钉 `HOME` 与 `MINI_HOME`，否则会写进用户真实 home（第 7 章已踩过）。

---

## §十 每个 profile 自己的库：句柄不许冻结在构造那一刻

**① Hermes 怎么做的**

`SessionStore` **不把数据库句柄冻结在构造那一刻**。句柄按解析出的 `state.db` 路径缓存，**每一次读取都经 `_db` 重新按当前 HERMES_HOME 覆盖解析**。配对（pairing）存储**按被服务的 profile 各造一个**。

为什么不能冻结：多路复用的网关用**一个进程**服务所有 profile。句柄如果在 `__init__` 里绑死到进程自己的 root home，那么**每个 profile 的行都会落进 root 的 `state.db`**。更糟的是这**不报错** —— 行里仍带着正确的 `profile_name`，数据上看不出来，只是桌面端会把某个 profile 的会话列在默认 bot 底下（`#88532`）。

**② 读 Hermes 的代码**

（`gateway/session.py:789-800`，`#88532` 注释，逐字）：

> ```python
> # SQLite handles are cached per path and resolved through ``_db`` per call, never bound
> # once: a multiplexed gateway serves every profile from ONE process and a handle frozen to
> # the root home would land every profile's rows in the root state.db.
> ```

要害是 **never bound once**：句柄**只缓存、不冻结**，路径在**每次调用时**重解 —— 这就是 `#88532` 的整个修法。

**③ mini 的实现（现状）**

网关侧**已经**按 home 缓存句柄：

```python
def _db(self):
    return self._db_for(get_mini_home())                     # gateway/run.py:137
def _db_for(self, home: Path):
    key = str(home)
    if key not in self._dbs:
        self._dbs[key] = hermes_state.SessionDB(db_path=Path(home) / "state.db")
    return self._dbs[key]
```

docstring 写明它的目的：作用域绑好之后，「替次级 profile 干活」的代码不用自己传 home，于是**没法传错**。`hermes_state.SessionDB` 自己**构造时把路径固化**（`hermes_state.py:83-95`），在 mini 的结构里没问题 —— 因为 `_db_for` 每次**显式**传路径。**缺**两件：`_profile_home_for_key` 的「**只 memoize 命中**」（mini 没有 profile→home 的 miss 语义），以及配对存储的 profile 维度（mini 的配对白名单是全局一份）。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 句柄按路径解析、不冻结 | `_db` 每次重解（`#88532`） | ✅ 有（`_db_for` 显式传路径） |
| 「只 memoize 命中」 | 只缓存命中的 profile→home | ⬜ 没做（A11，~15 行） |
| 配对存储按 profile 分家 | 每个被服务 profile 一个 `PairingStore(profile=name)` | ⬜ 没做（白名单全局一份，#28） |

「只 memoize 命中」防的是**启动之后才被建出来的 profile**（mini 有 `profile create`，完全可能运行中新建）—— 缓存一个 miss 会把那个 profile 的行**钉到 ambient store 一辈子**。配对存储的 profile 维度要等 mini 真有「每个 profile 一套白名单」的需求再拆（拆的关键点真实版已经给出：`PairingStore(profile=name)`）。

---

## §十一 每条会话线：两个 bot 同处一个聊天为什么会互相顶掉

**① Hermes 怎么做的**

先讲清它防的那件事。一个进程多路复用时，**同一个人可以同时跟两个 bot 说话**。会话键如果只按「平台＋聊天」算，两个 bot 会算出**同一个键** —— 于是它们共用：

- 一把**会话锁**（一个 bot 在忙，另一个 bot 的消息被当忙时消息，排队或打断）；
- 一条**会话历史 / 一条 lane**（互相覆盖对方的消息）。

所以会话键必须**按 profile 加命名空间**：默认 profile → `agent:main`（**与历史上每一个键逐字节一致**）、具名 profile → `agent:<name>`、**名字恰为 `main` 的 profile 用 `main~`**（`~` 不在 profile id 字母表里，所以这个标记形态**不可能**撞上任何真 profile）。

命名空间从哪来：适配器身上带一个 `_owner_profile`（**配置适配器时就装好，早于任何入站事件**），`_session_key_profile` 的解析顺序是 **① 来源盖章（`source.profile`）→ ② owner profile → ③ store 解析器**。批处理、活跃会话跟踪、忙会话守卫**全部按 lane 分键** —— 键不同，三条线才分得开。

**② 读 Hermes 的代码**

那段 docstring 把「为什么要有命名空间」一次讲透（逐字）：

> ```python
> ``agent:<ns>`` prefix for a session key: default/None profile → ``agent:main``
> (BYTE-IDENTICAL to every historical key); named profile → ``agent:<name>`` so two
> profiles serving the same chat never collide. A profile literally named ``main`` would
> otherwise produce the default's namespace and share every session (routing index, agent
> cache, store) with it, so it is marked ``main~``: ``~`` is outside the profile-id alphabet,
> so the marked form can never be another profile's id.
> ```

要害是 **"so two profiles serving the same chat never collide"** —— 命名空间存在的**唯一理由**就是这一句；`main~` 那一段说的是**怎么保证不撞**（不靠约定、靠字母表）。

**③ mini 的实现（现状）**

mini 把「这条消息归哪个 bot」放在两处解析：`gateway/adapters/base.py:111 _session_key_profile()` 做**三段解析**（路由章 → bot owner → `None`），`gateway/adapters/base.py:131` 在**入站口盖章**（`handle_message` 的第一步，早于任何业务逻辑）。平台语法那一端是 `gateway/adapters/console.py:100 _maybe_feed_profile()` —— `@<profile> 文本` = 「这条消息是那个 bot 收到的」。两个 bot 同处一个聊天时，各自的 `@<profile>` 让它们算出**两个不同的键**，于是互不顶掉。

**④ 与真实版的差别**

- 真实版按 `(平台, 聊天, 线程, **接收它的 bot**)` 查**路由表**；mini **还没有路由表**（A7 没做），所以「哪个 bot 收到」由**平台语法**表达（console 的 `@<profile>`）—— **同一个判别项，不同入口**。
- 两条必须守住的性质（都有断言 ＋ 反向验证）：**同聊天两个 bot = 两个键**（否则第 8 章的单槽覆盖会把另一个 bot 的消息顶掉）；**默认 profile 的键逐字节不变**（`main:…`，归一后仍是 `None`）。

---

## §十二 每 profile 适配器与 fail-soft 启动：哪些失败只该跳过、不该带走网关

**① Hermes 怎么做的**

多路复用的网关要为**每个被服务的 profile** 起适配器，失败处理分三档：

- **主 profile（默认/活动）**：自己的适配器照常起，但**缺凭据的平台要跳过** —— 而不是起一个**永远重连、永不痊愈**的坏适配器；
- **次级 profile**：逐 profile、逐平台建连，**每一种失败都有专属日志 ＋ 「跳过」**；
- **只有两类错会中止启动**：`MultiplexConfigError`（配置错，见 §十四）与「**共享监听被抢**」。

为什么必须 fail-soft：适配器是**进程级共享资源**。某个平台凭据缺失、监听端口被别处占了，这些都**只影响那一个平台**；如果它们能中止整个启动，一个坏平台就会**连带**把其它所有 profile 一起拖死。反过来，`MultiplexConfigError` 是**配置本身错了**（例如两个 profile 抢同一个凭据/监听）—— 继续跑只会造出一个**行为不明**的网关，所以必须**上抛、中止**。

**② 读 Hermes 的代码**

`_start_secondary_profile_adapters()` 的 docstring 一句话说清它的位置（逐字）：

> ```python
> """Bring up adapters for every non-active profile (multiplex only); returns connected count.
> Each profile connects under its own HERMES_HOME + secret scope; credential/listener collisions
> are refused here — the only point seeing every profile's credentials together."""
> ```

要害在 **"the only point seeing every profile's credentials together"**：凭据/监听的冲突**只有这里**看得全，所以「拒绝重复认领」只能落在这里；函数返回的是**连上的数量**（不是列表）。逐平台的跳过分支（无凭据 / relay-whatsapp 共享入口 / mirror 平台 / 适配器建不出 None / 重复认领 / connect 失败）都写在这一族函数里 —— 行号与日志原文全部见附录 A 追加。

**③ mini 的实现（现状）**

mini 有两张适配器表：活动 profile 的 `self.adapters`（`gateway/run.py:64`）与每 profile 的 `self._profile_adapters`（`gateway/run.py:82`）。活动适配器连接是 **fail-soft**（`gateway/run.py:250-256`：连不上只跳过 ＋ 说出来），随后 `self._start_secondary_profile_adapters()`（`gateway/run.py:259`）逐 profile 建连 —— `gateway/run_adapters.py:102` 逐个走「跳过活动/已建 → 建 → 连」，每个 profile 都**在自己的作用域里**连。`@<profile>` 的落点是 `gateway/run_adapters.py:193 profile_feed()`，未知 profile 返回 `False`（**调用方必须说出来**，不许静默丢）。

**④ 与真实版的差别**

- **跳过要说出来**：mini 是 `_adapter_note()`（`[MULTIPLEX]` 前缀 ＋ `adapters_log()` 观测面），没有真实版那么细的 info/warning 分级 —— 差在**日志系统**的规模，**机制一致**。
- **重复认领拒绝 / 占用端口的 shared-listener**：⬜ 没做（B8/B2，**基座缺失**：mini 适配器没有 token，也没有 HTTP 监听）。
- ★ **服务集合的边界是 mini 自加的**：`servable_homes()` 一层 ＋ **预检 1b** 一层。理由：真实版那句 `profiles_to_serve(True)` 在 mini 里会**越界到用户真实的家**（`profiles_root()` 是 HOME 锚定的）。

---

## §十三 Control plane：桌面插件隔着 ws 够到网关的那扇门

**① Hermes 怎么做的**

桌面插件**没有别的门** —— 它只能通过 **ws JSON-RPC** 够到网关，于是 profile 的枚举与配置必须落在一层 RPC 方法上，住在 `tui_gateway/methods_profiles.py`。这一层一共**六个方法**（`list` / `create` / `describe` / `configure` / `set_asset` / `get_asset`，外加一个 `remember_onboarding`），每个方法自带方法级错误码（5061…5066 + 5067）。

它管两件事，判据都写得很死：

- **读写跑在目标 profile 的 HERMES_HOME 覆盖之下**：`_hermes_home_scope(path)` 把 config/auth 的解析临时钉到那个 profile 的 home，块里的读写就像"在那个 home 里"跑，出块自动复位。
- **资产写入 = 原子 + 类型/大小上限**：写入前先**嗅探文件头的魔数**判定格式（声明的 mime 从不采信），超过 2MB 直接拒（错误码 4069），落盘走**临时文件 + rename**。`ui_meta` 另有 64KB 帽（`tui_gateway/methods_profiles.py:466` 的 docstring：*"64KB cap (rides every roster paint)"*）。

★ 一处**诚实的不一致**：`_hermes_home_scope` 的调用点只有 **5 处**（`:294/:313/:327` create 的辅助、`:436` describe、`:574` configure 的 config 段）—— 这不是遗漏：`set_asset`/`get_asset`/`ui_meta`/`model` 直接读写 `profile_dir`（它们的目标本来就是"那个 profile 的目录"，不依赖 config 解析）。

**② 读 Hermes 的代码**

资产那几行的纪律（逐字）：

> ```text
> tui_gateway/methods_profiles.py:15   _ASSET_EXTS = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
> tui_gateway/methods_profiles.py:16   # ext -> [(start, end, magic bytes)]; format is sniffed, the declared mime is never trusted.
> tui_gateway/methods_profiles.py:649      if len(blob) > 2_000_000:
> tui_gateway/methods_profiles.py:650          return _err(rid, 4069, f"asset too large ({len(blob)} bytes; max 2MB)")
> tui_gateway/methods_profiles.py:656-658  tmp = assets_dir / f"{asset}.{ext}.tmp"; tmp.write_bytes(blob)
>                                          tmp.replace(assets_dir / f"{asset}.{ext}")     # ← 原子：tmp + rename
> ```

要害是那行注释 **"format is sniffed, the declared mime is never trusted"** —— 上传者给的 mime 会撒谎、扩展名也会撒谎，所以**只认魔数**；三行连起来就是资产写入的全部顺序：**先验真、再限量、最后原子落盘**。

**③ mini 的实现（现状）**

**还没有控制面**：mini 的命令面是 argparse 子命令（`gateway run|status|stop|hooks` + `profile create|list|show`），**没有 ws/HTTP 服务、没有方法表、也没有"资产"这个概念**。profile 的枚举与配置走 CLI 参数与本机目录扫描，不经任何 RPC。

**④ 与真实版的差别**

差的不是几个方法，是**整扇门**：真实版这条链是"桌面 → ws → 网关"，mini 没有桌面插件这扇门（基座不存在）；它也没有"资产（头像）"这类二进制写入（概念不存在）。`tui_gateway/` 那层 **686 行**的 RPC 层，对一个没有 RPC 消费者的 CLI 是**净负担**（B7）。

**顺带记一条手法**：**原子写 = 临时文件 + `Path.replace()`**（真实版 `:657-659`）—— mini 现在直接 `write_text`。理由一句话：**半写的配置文件比没有配置文件更难查**。

---

## §十四 Failure modes：一个「坏东西」的四条出路（降级 / 丢弃 / 跳过 / 中止）

**① Hermes 怎么做的**

四种"坏"对应**四种处置，不能互换**（表见 §0.5）。把这四条出路摊开看，判据其实是同一条线：**它坏在哪、坏得多彻底** ——

| 处置 | 判据（什么时候用） | 动作 |
|---|---|---|
| **中止** | 开放策略没有 allow-all opt-in —— 这是**配置错**，继续起就等于开一个公开的 agent 入口 | 抛 `MultiplexConfigError`，**启动中止** |
| **降级** | 一个平台要独占端口，但进程里已有别的 profile 在听 | **不拒绝**：给二级适配器打 `_shared_listener_profile`（**不 bind**），改成共享监听模式 |
| **跳过** | 这东西坏得**只影响自己**（热重载时某个 profile 配置坏了；或两个 MIRROR 平台） | 只 **park 坏 profile**（落一条 error 日志），其余照常服务 |
| **丢弃** | 一条消息命中了"拒绝"判定 | 标记置位 → 消费端 **warning + 丢弃**，**不投错、不乱投** |

三个实现细节值得单说：

- **中止的真触发不是"占端口平台"**（那是文档的错，见精译附录 C ②⑤），而是**开放策略缺 allow-all opt-in**：`_own_policy_open_startup_violation(config)` 逐平台查 —— 平台启用、`dm_policy`/`group_policy` 都为非 `open` 就跳过；`GATEWAY_ALLOW_ALL_USERS` 或平台级 allow-all 为真就跳过；否则返回 `"{platform.value}: open policy without allow-all opt-in"`，交给 `run_adapters.py` 抛 `MultiplexConfigError`。
- **降级不是"拒绝"**：`run_adapters.py:1139` 给二级的占端口适配器打 `adapter._shared_listener_profile = profile_name`（**不 bind**）；只有两个 MIRROR 平台被直接**跳过**（`:551`）。
- **跳过只罚坏的、不带连坐**：热重载时 `run_profile_reconcile.py:125-129` 捕到 `MultiplexConfigError` 只落一条 `[MULTIPLEX] Profile '%s' not served: %s` 的 error 日志，别的 profile 照常服务。

**② 读 Hermes 的代码**

"拒绝 → 丢弃"是一条**跨文件的消费链** —— 抛点、翻译点、消费点各在一处（**代码行逐字**，行尾中文是位置摘要）：

> ```text
> agent/secret_scope.py:162          raise UnscopedSecretError(...)                     ← 抛
> gateway/run.py:4348-4352           raise ProfileRouteRejected(matched.name)           ← 抛
> gateway/run_inbound.py:173         if getattr(source, "profile_route_rejected", False) is True:   ← 消费=丢弃
>                                     …（warning + `return None`）
> gateway/platforms/base.py:4361-4376  build_source 捕 ProfileRouteRejected → source.profile_route_rejected = True
>                                      # 注释：这个拒绝标记在**授权之前**就被消费掉
> gateway/run_adapters.py:1430-1437    _stamp_routed_profile 捕到 → return False（不丢弃，交给上层）
> ```

要害在 `platforms/base.py` 那条注释：**这个拒绝标记在"授权之前"就被消费掉**。抛点有两处（密钥一处、路由一处），消费点也有两处：`run_inbound.py` 见到标记就 **warning + return None（丢弃）**；`run_adapters.py` 的 `_stamp_routed_profile` 捕到则 **return False（不丢弃，交给上层）**。

**③ mini 的实现（现状）**

`authz.py` 的四层默认拒绝（第 7 章）**不覆盖**这些。**mini 的处置与真实版不同，而且是刻意的**：

- **没有 `MultiplexConfigError` 等价物**：真实版"配置错 → 启动中止"，mini 是**降级为 standalone 继续起**（`gateway/multiplex_mode.py:132 implicit_multiplex_blocker()` 的预检 + `:222-260` 的裁决落盘）—— 判据：mini 的配置来源是 env 与目录扫描，**没有"把 fleet 折进一个进程"那种需要中止的配置错**，能降级就不该让网关起不来。
- **"拒绝 → 丢弃"这条链还没有**（因为还没有路由表，A7 没做）—— 现在只有"**未知 profile 而被丢弃**"这一种：console 的 `@<profile>` 命中不到适配器时**明说被丢弃**（`adapters/console.py:100`），绝不静默吞掉。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 配置错 | **中止**（`MultiplexConfigError`） | **降级 standalone 继续起**（没有等价的中止判据） |
| 拒绝 → 丢弃 | 完整消费链（抛 → 翻译成标记 → 丢弃 / 上交） | ⬜ 没做（要先有路由表，A7） |
| 未知 profile | —— | 明说被丢弃（`adapters/console.py:100`，不静默） |
| 开放策略 `open` | `_own_policy_open_startup_violation` 把关 | ⬜ 没做 —— mini 的 `authz.py` 是"默认拒绝"，没有 `dm_policy=open` 这个可配置项，概念不存在（B9） |

**为什么现在这样**：mini 的配置来源（env + 目录扫描）里**没有**"需要中止"的那类配置错，所以"配置错就该起不来"这条原则落成的是**降级 standalone + 记状态**（A3），不是中止；而 `--multiplex` 即使配得毫无意义（只有一个 home）也照样"启动成功"，这是这条原则**还没走完**的那半。至于"拒绝 → 丢弃"，它是整条路由链的兜底语义，**没有路由表（A7）就无从落地**。

---

## §十五 Known limitations：还没 profile 化的进程级状态

**① Hermes 怎么做的**

有些状态天生是**进程级**的，不按 profile 分家 —— 文档列了五行（MCP 发现 / `TERMINAL_*` / 内置工具注册表 / provider·能力注册表 / HTTP 监听·relay·进程锁）。它们的共同危险是：**一个进程服务多个 profile 时，先到的那个 profile 会把这格状态占为己有**，后面的 profile 看到的是别人的东西 —— 而且**不报错**。

**其中一行已经被修掉了**：MCP 发现槽位现在**按 profile home 分桶** —— 一个服务多个 profile 的共享后端，会**每个 profile 跑一次发现**，而不是第一个建 agent 的 profile 把槽位独占给所有人（#67605）；单 profile 进程只有一个 key，行为退回旧的单槽形态。

**另一处**（文档没写、但属于同一族）：`serves_routed_profile()` 的 docstring 逐字说 **MCP 注册表作用域与 `check_fn` 缓存键都跟随这个判据**（*"so a served profile's view never aliases the launch profile's (#111151)"*）—— 这两处的"按 profile 分家"是**显式对齐**过的。

**② 读 Hermes 的代码**

"每 profile 一次发现"就写在这段注释里（逐字）：

> ```text
> hermes_cli/mcp_startup.py:13-16   # "Discovery slot per profile home (``hermes_home_key()`` follows the
>                                   #  context-local HERMES_HOME override): a shared Desktop/dashboard backend
>                                   #  serving several profiles runs one discovery per profile instead of the
>                                   #  first profile to build an agent claiming the slot for everybody (#67605).
>                                   #  A single-profile process has exactly one key, so behaviour is the old
>                                   #  single-slot form."
>                                   _mcp_discovery_started: Set[str] = set()
>                                   _mcp_discovery_thread: Dict[str, threading.Thread] = {}
> ```

要害是 **`hermes_home_key()`**：它跟着 context-local 的 HERMES_HOME 覆盖走，所以"分桶的键"表达的是**当前正在替谁干活**，而不是进程启动时的那个 home。

**③ mini 的实现（现状）**

这五格在 mini 的现状（对照真实版）：

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| MCP 发现 | 按 profile home 分桶（#67605） | ⬜ 没做 —— mini 无 MCP |
| `TERMINAL_*` | 按白名单保持全局 | ⬜ 没做 —— 无沙箱后端 |
| 内置工具注册表 | 进程级；插件工具按 profile 叠 | **进程级**（mini 无插件系统） |
| `check_fn` 缓存 | 按 `hermes_home_key()` 分键 | **不分键** ← 真隐患（缺口 #7，A10 解决） |
| 监听 / relay / 进程锁 | 一进程一份 | 进程锁**有**（mini 有 PID 指纹） |

用大白话把这张表念一遍：

- **MCP 发现**：真实版曾经是"谁先建 agent 谁占坑"，现在按 profile home 分桶；mini 根本没有 MCP，这一格是空的。
- **`TERMINAL_*`**：真实版把这些终端策略变量**保持全局**、只靠白名单管，所以多 profile 时会串味；mini 没有沙箱后端，也就没有这些变量。
- **内置工具注册表**：真实版是**进程级**的，只有插件工具那一层按 profile 叠；mini 也**进程级**，而且**没有插件系统**可叠。
- **`check_fn` 缓存**：真实版按 `hermes_home_key()` 分键；mini **不分键** —— 这才是真隐患（见 ④）。
- **监听 / relay / 进程锁**：真实版一进程一份；mini 的**进程锁已经按 home 分**（用 PID 指纹判活），这一格是齐的。

**④ 与真实版的差别**

唯一的真问题是 **`check_fn` 缓存不分键**。它现在没爆，是因为 mini 的 `check_fn` 只查"这台机器有没有 `bash`/`grep`"这类**与 profile 无关**的事实。但 mini 第 4 章已经给了 `check_fn` 一个**带 state 的形态**（TTL + 抖动保护 + 失败宽限）—— 一旦将来出现"按 profile 变化的可用性"（比如"这个 profile 配了搜索 key"），**A 的结论会被 B 复用**。所以这一格迟早要跟 `hermes_home_key()` 对齐（A10，`hermes_home_key` 进缓存键，~10 行）。

---

## §十六 Non-goals：这条边界管的是 profile，不是人

**① Hermes 怎么做的**

这一节不是机制，是**边界声明**。三句话划清：

- 多路复用隔离的是 **profile**，**不负责认证或授权"终端用户"**；
- **一个 profile 是一份配置，不是一个人**：网关**信任传输层与路由表**决定事件归属；
- **请求级身份**与**profile 层之上的按用户授权**不在本文范围。

一句话：**"多路复用让两个 profile 不串味" ≠ "同一 profile 里两个用户不串味"** —— 前者归这一章，后者归授权层。

**② 读 Hermes 的代码**

Hermes 把"谁能不能进"这件事**放在了多路复用之外的授权层**：

- **四层 `authz_mixin`**（平台全放行 → 平台白名单 → 私聊配对 → 全局放行 → **默认拒**）住在 `gateway/authz_mixin.py`；
- **每 profile 一份 `PairingStore`** 住在 `gateway/pairing.py:318`。

也就是说：多路复用只负责"这条事件归哪个 profile"，"谁是终端用户、能不能进"一律由授权层判。

**③ mini 的实现（现状）**

mini 的授权是**四层默认拒绝**（第 7 章）＋**单一**配对白名单，落在 `gateway/authz.py` —— 形状与真实版的四层 `authz_mixin` 对齐，少的只是"每 profile 一份 `PairingStore`"这一层（mini 只有一份）。

**④ 与真实版的差别**

两边其实都**把授权放在多路复用之外**（Hermes 在 `authz_mixin` + `PairingStore`，mini 在 `authz.py`），差别只在粒度。要记住的不是"谁多一层"，而是**别把安全责任放错层**：`authz_mixin.py` / `pairing.py` / `authz.py` 管的是"人和权限"，多路复用管的是"事件归哪个 profile"。mini 的授权线在第 7 章已定性，**本章不动**，这里只把这条边界的判断留在文档里。

---

---

## §十七 差距清单（= 本章实现清单）

> 状态列：`✅` **已做**（带 commit）/ `⬜` **没做**（带量级）/ `⏭` **不抄**（带理由）。

### A 批 · 要抄（13 条）

| # | 条目 | 真实出处 | 落点 | 规模 | 状态 |
|---|---|---|---|---|---|
| A1 | 开关三态 + env 覆盖 + 真值词表 + 空白不算覆盖 | `config.py:29-64/552-558` | 新 `gateway/multiplex_mode.py` + `gateway_cmd.py` | ~40 | ✅ `f34fdaa` |
| A2 | 服务集合补墓碑过滤 + "枚举不建目录"不变量 | `profiles.py:802` | `hermes_cli/profiles.py` | ~10 | ⬜ |
| A3 | 启动守卫三条（非默认网关 / 服务集合<2 / 有 secondary 在跑）+ 异常兜底不致命 | `multiplex_mode.py:75-123` | `gateway/multiplex_mode.py` | ~60 | ✅ `f34fdaa` |
| A4 | 裁决理由落状态文件 + 分级日志 | `multiplex_mode.py:126-149` | `gateway/status.py` + `run.py` | ~20 | ✅ `f34fdaa` |
| A5 | 作用域铺到"活动"（适配器/入站/命令/通知），不只回合 | `run.py:1750` + 30 调用点 | `run_inbound.py` / `run_turn.py` / `run_adapters.py` | ~30 | ✅ `9e7c8e4` |
| A6 | per-profile 适配器 + fail-soft（每类跳过都有日志） | `run_adapters.py:832/1004` | 新 `gateway/run_adapters.py` | ~150 | ✅ `9e7c8e4` |
| A7 | 共享 bot 路由表（合取/最具体/单跳父链/bot_profile/int 强转/拒绝→丢弃） | `profile_routing.py`(176) + `run.py:4348` + `run_inbound.py:173` | 新 `gateway/profile_routing.py` | ~150 | ⬜ |
| A8 | per-bot lane（`_owner_profile` + 三段解析） | `base.py:1904/2306`、`run_adapters.py:1115` | `gateway/adapters/base.py` + `session_keys.py` | ~30 | ✅ `9e7c8e4` |
| A9 | 全局白名单（进程身份 + shell/解释器两族）+ `_is_global_env` | `secret_scope.py:98-135` | `agent/secret_scope.py` | ~25 | ✅ `f34fdaa` |
| A10 | `hermes_home_key` + 进程级 home 出口（顺带修 `check_fn` 缓存分键） | `hermes_constants.py:119/150` | `hermes_constants.py` + `tools/registry.py` + `gateway/status.py` | ~35 | ⬜ |
| A11 | 句柄缓存"只 memoize 命中" | `session_persistence.py:118-138` | `gateway/run.py` | ~15 | ⬜ |
| A12 | 状态面写 `served_profiles` + 按 profile 可读 | `status.py:32/852/895` | `gateway/status.py` | ~45 | ⬜ |
| A13 | dotenv 加载器边界：mux 开 + 有 home 覆盖 → 跳过进程级加载 | `env_loader.py:340-350` | `hermes_cli/runtime_provider.py` | ~15 | ✅ `f34fdaa` |

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
| M2 | `--multiplex` 半成品语义（只设标志 + 横幅） | `run.py:242/250-256` | ✅ 已解决（A5/A6 已落地；A7 待做） |

### 已交付的两批（收据）

| 批次 | 内容 | commit | 收据 |
|---|---|---|---|
| 第一批（地基） | A1 三态＋env 覆盖 · A3 预检三条＋异常兜底 · A4 裁决落盘 · A9 白名单 · A13 dotenv 边界 | `f34fdaa` | 全套件 **823 通过 / 0 失败**（双解释器）· 反向验证 **14/14 干净变红** · 真跑 **10/0** |
| 第二批（成型） | A6 per-profile 适配器＋fail-soft · A8 per-bot lane · A5 作用域铺开 | `9e7c8e4` | 全套件 **844 通过 / 0 失败** · 反向验证 **8/8** · 真跑 **4/0**（真 CLI ＋ 真 DeepSeek） |

**第二批逼出来的 4 个真问题**（都不是读代码读出来的，是写测试 / 真跑逼出来的）：

1. ★ `resolve_provider` **直读 `os.environ`** → 次级 profile 明明自己 `.env` 里有 key 却报"环境变量为空"；
   更要紧的是它会静默用**别人的** key 调用。修完这条，第一批的 fail-closed ＋ 白名单才**第一次真的开始拦人**
   （此前是"武装好但还没有受害者"）。搜索工具同理（`tools/web_search.py`）。
2. **`_provider_spec` 把一份 spec 发给所有 profile** → 用错凭据（改成按 home 分键）。
3. **服务集合越界**：`profiles_root()` 是 HOME 锚定的，而测试只钉 `MINI_HOME` → 进程顺手续了
   **用户真实的** `default` ＋ `lab` 两个家（真发生了）→ 预检 1b ＋ `servable_homes()` 两层拦住。
4. **入站只在"次级 profile"才套作用域** → `_open_session` 在回合前要 provider spec，
   fail-closed 当场抛 `UnscopedSecretError` → 改成**一律套**。

★ 真跑的做法：**故意把 `DEEPSEEK_API_KEY` 从子进程环境里摘掉** —— 两边仍各自从自己的 `.env`
经作用域取到（`@lab` 的回合落在 lab 的 `state.db`、默认家 0 行）。这是"凭据走作用域"的硬证据；
环境里留值会掩盖"凭据没走作用域"这类 bug。

### 实现顺序建议（依赖决定）

```text
第一批（地基）：A1 → A3+A4（守卫要三态）           A9+A13（同一根因：进程环境不是 profile 存储）
第二批（成型）：A6（per-profile 适配器） → A8（lane） → A5（作用域铺开）
第三批（核心）：A7（路由表，依赖 A6 的 owner profile 与 A8 的键）
第四批（配套）：A2 / A10 / A11 / A12（服务集合、分键、缓存、状态面）
最后：M1 修复 + 反向验证 + 真跑（两个 home + 一条共享 bot 路由）+ 回填本文 mini 列与收据
```

---

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
| `hermes_cli/mcp_startup.py:13-16` | MCP 发现按 home 分桶 | #67605（已修） |
| `hermes_cli/env_loader.py:340`、`hermes_cli/config.py:1595/2586` | 三处相邻缝 | §六 |

### 从正文挪过来的引用（本轮重写：正文只留证据，清单归这里）

### 附录 A 追加（g1）

从正文挪走的 `文件:行号`（原样）：

**§一**

- `gateway/AGENTS.md` — 「run.py is the facade; phases live in run_*.py (startup, adapters, inbound, turn, busy, goals, notifications, shutdown, ...)」
- `gateway/run.py:1646` — `def _multiplex_profile_homes(config)` ← 服务集合的消费点

**§二**

- `gateway/config.py:29-30` — `_TRUTHY_STRINGS = frozenset({"1","true","yes","on"})`；`_FALSY_STRINGS = frozenset({"0","false","no","off"})`
- `gateway/config.py:33-36` — `def _bool_token(value) -> Optional[bool]`：「True/False for a recognized truthy/falsy token, else None.」
- `gateway/config.py:552-558` — `multiplex_profiles: Optional[bool] = None`；注释「On by default (DEFAULT_CONFIG), but UNSET here is ``None``: a request the gateway settles at boot, not a verdict. … Every reader tests truthiness, so an unresolved ``None`` never multiplexes by accident.」
- `hermes_cli/config_defaults.py:1998` — `"multiplex_profiles": True`
- `hermes_cli/gateway_multiplex_mode.py:27-50` — `explicit_multiplex_flag(default_home)`
- `hermes_cli/gateway_multiplex_mode.py:53-63` — `default_gateway_multiplexes(default_home)`：先读**活着的那个网关**记录的 `served_profiles`，再读显式开关 —— 别的进程**不猜** unset 的默认（否则「还没迁移」会被读成「已经在多路复用」）

**§三**

- `hermes_cli/gateway_multiplex_mode.py:75-107` — `implicit_multiplex_blocker()` 六条（逐字见 §0.2）
- `hermes_cli/gateway_multiplex_mode.py:24` — `SINGLE_PROFILE_REASON = "only one profile exists (nothing to multiplex)"`
- `hermes_cli/gateway_multiplex_mode.py:110-123` — `resolve_multiplex_mode(config) -> MultiplexDecision`
- `hermes_cli/gateway_multiplex_mode.py:117-119` — `except Exception as exc:` # a broken preflight must not take the gateway down with it
- `hermes_cli/gateway_multiplex_mode.py:46-51` — `MultiplexDecision: enabled / source / reason` # `source ∈ {"config","default","guard"}`
- `hermes_cli/gateway_multiplex_mode.py:126-133` — `record_multiplex_decision()` → `write_runtime_status(multiplex_standalone_reason=decision.reason if source=="guard" else None)`
- `hermes_cli/gateway_multiplex_mode.py:136-149` — `log_multiplex_decision()` # guard+单profile → info；其它 guard → warning

**§四**

- `gateway/run.py:1739-1750` — `_load_profile_secret_scope(profile_home)` # 先 hydrate 外部密钥源，再 build
- `gateway/run.py:1784-1789` — `_async_profile_runtime_scope(profile_home)`：「Enter a profile scope without loading secret files on the event loop.」→ `secrets = await asyncio.to_thread(_load_profile_secret_scope, …)`
- 调用点分布（15 个文件、30+ 处，抄几个代表）：
  - `gateway/run_adapters.py`（:459/:517/:908/:1008/:1046/:1149/:1355…/:1475）— 建适配器、连接、重连、平台握手
  - `gateway/run_startup.py`（:119/:122/:933/:962）— 启动期（async 版在这里用）
  - `gateway/run_turn.py`（:2173/:2208/:2458）— 回合与后处理
  - `gateway/run_inbound.py:1709` — 入站预处理
  - `gateway/slash_commands*.py`（`slash_commands.py:305`、`_session.py:464`、`_status.py:418`、`_model.py:383`）— 每条 slash 命令
  - `gateway/run_notifications.py`（:1354/:1636）— 通知派发
  - `gateway/run_agent_cache.py:711`、`run_goals.py:463`、`run_profile_reconcile.py:168` — 缓存淘汰、目标、热重载
  - `gateway/platforms/api_server.py:1492`、`shared_ingress.py:142` — HTTP 入口

### 附录 A 追加（g2）

真实侧（每节 ② 只引了一段 docstring/注释，以下为从四节正文挪走的 `文件:行号`）：

- `agent/secret_scope.py:24` — `_MULTIPLEX_ACTIVE: bool = False`（注释：*"Process-global (describes the deployment mode, not a per-task value): set once at gateway startup"*）。
- `agent/secret_scope.py:27/33` — `set_multiplex_active()` / `is_multiplex_active()`。
- `agent/secret_scope.py:37-46` — `serves_routed_profile()`（docstring：MCP registry scope 与 `check_fn` 缓存键都跟它走，免得被服务 profile 的视图别名到启动 profile 的，`#111151`）。
- `agent/secret_scope.py:49` — `_SECRET_SCOPE: ContextVar[Optional[Mapping[str, str]]] = ContextVar("_SECRET_SCOPE", default=None)`。
- `agent/secret_scope.py:52-77` — `UnscopedSecretError`（docstring：修法是包进 `set_secret_scope(...)`，不是放宽白名单；`str(exc)` 是给用户的一句可执行建议，开发者诊断挂 `__notes__`）。
- `agent/secret_scope.py:80-96` — `set_secret_scope` / `reset_secret_scope` / `current_secret_scope`。
- `agent/secret_scope.py:174-176` — `get_secret_str`（*"Still raises ``UnscopedSecretError`` — swallowing it hides a spawn-site bug."*）。
- `agent/secret_scope.py:254-257` — `build_profile_secret_scope`（*"Global vars are NOT copied in … so the scope holds only profile secrets."*）。
- `hermes_cli/env_loader.py:26` — `_SCOPED_SKIP_LOGGED: set[str]`（已打过日志的 routed home，防刷屏）。
- `hermes_cli/env_loader.py:340-350` — `if is_multiplex_active() and get_hermes_home_override() is not None: … hydrate_profile_secret_sources(home_path); return []`。
- `hermes_cli/config.py:1595-1611` — `_env_ref_lookup(name)`（`${VAR}` 解析；docstring：作用域内经 `agent.secret_scope.get_secret` 解析，多路复用下「miss 就是 miss」，`#84079`）。
- `hermes_cli/config.py:2586-2612` — `_publish_env_value(key, value)`（docstring：多路复用下被路由 profile 的写不许落进**共享** `os.environ`，改为更新已装的作用域映射；`#77490` / `#88441`）。
- `hermes_constants.py:15-17` — `_profile_fallback_warned = False` / `_UNSET = object()` / `_HERMES_HOME_OVERRIDE: ContextVar[str | object]`（default=`_UNSET`）。
- `hermes_constants.py:119-142` — `hermes_home_key(path=None)`（normcase + resolve；已存在目录才进缓存）。
- `hermes_constants.py:150-157` — `get_process_hermes_home()`（*"For process-level assets (theme YAML, dashboard plugin manifests) that must stay visible while a request is scoped to another profile…"*）。
- `hermes_constants.py:73-98` — `_warn_profile_fallback_once()`（`#18594`；直接写 stderr、不走 logging，行 86-87 注释：它跑在 import 期、logging 还没配好）。
- `gateway/profile_routing.py:1-12` — 模块 docstring（§八 ② 已引其逐字段）。
- `gateway/profile_routing.py:55-66` — `ProfileRoute` 字段：`name` / `platform` / `profile` / `guild_id` / `chat_id` / `thread_id` / `enabled` / `bot_profile`。
- `gateway/profile_routing.py:68-71` — `@property specificity`：`2*bool(guild) + 4*bool(chat) + 8*bool(thread)`。
- `gateway/profile_routing.py:85-98` — `matches()` 关键五行（`enabled`/`platform` 硬门槛、`_bot_profile_key` 比较、thread、`chat_id in (chat_id, parent_chat_id)` **单跳**、`return not (...)` 保真合取）。
- `gateway/profile_routing.py:161` — `routes.sort(key=lambda r: r.specificity, reverse=True)`。
- `gateway/profile_routing.py:107-127` — `_coerce_route_id()`（YAML unquoted snowflake 读成 int → 转 str；bool/float 只 warning，不静默「修」，`#86470`）。
- `gateway/profile_routing.py:144` — 注释 *"Validate profile name to prevent path traversal (lazy import: cycle)."*
- `gateway/run.py:4348-4352` — `if matched.profile not in served: raise ProfileRouteRejected(matched.name)`。
- `gateway/run_inbound.py:173-178` — `if source.profile_route_rejected is True: … return None`（**丢弃**）。

mini 侧（正文已见，此处汇总）：

- `agent/secret_scope.py`（**135 行**）：`GLOBAL_ENV_EXACT`（19 项）/ `GLOBAL_ENV_PREFIXES`（0 个）/ `is_global_env()` / `get_secret` 四分支。
- `hermes_cli/runtime_provider.py` — `_multiplex_dotenv_boundary_active()`（§六 (a)）；凭据读取走 `get_secret`（§五 ③）。
- `tools/web_search.py` — 凭据读取走作用域（§五 ③）。
- `hermes_constants.py:43-48` — 覆盖 → `MINI_HOME` → `~/.mini-hermes`（§七 ③）。
- `gateway/adapters/base.py:111` — `_session_key_profile()` 三段解析（§八 ③ 关联，见 §十一）。
- `gateway/session_state.py:95` — `SessionRouter.route()`（§八 ③；会话归属，非跨 profile 消息路由）。

### 附录 A 追加（g3）

真实侧：

- `hermes_cli/profiles.py:802-815` — `profiles_to_serve(...)`（§九 ② 已引其 docstring）。
- `gateway/run_adapters.py:499` — `profile_homes = _multiplex_profile_homes(self.config)`（**适配器面**）。
- `gateway/run.py:4342` — `served = {name for name, _home in _multiplex_profile_homes(config)}`（**路由面**）。
- `gateway/run_adapters.py:886-900` — `_record_served_profiles() → write_runtime_status(served_profiles=served)`（**状态面**）。
- `hermes_cli/profiles.py:942` — 注释：clone 会剥掉源 profile 的 bot token / allowlist / 平台段。
- `gateway/session.py:789-800` — `#88532` 注释（§十 ② 已引）。
- `gateway/session.py:802-822` — `self._db_pinned = _DB_UNPINNED` / `self._db_handles: Dict[Path, Any] = {}` / `self._db_handle_cache = RecoverableHandleCache(…)` / `self._open_session_db_for_active_scope()`（只「预热」，不冻结）。
- `gateway/session_persistence.py:43-81` — `_open_session_db_for_active_scope(db_path=None)` 及其 docstring：「``db_path`` pins the store; otherwise ``_default_db_path()`` follows the context-local HERMES_HOME (resolved per call so multiplexed profiles reach their own store). Handles are cached per path; failed opens enter a bounded backoff during which callers keep using the JSONL fallback. Resolving here rather than once in ``__init__`` is the whole fix for #88532…」。
- `gateway/session_persistence.py:89-101` — `@property _db`（未 pinned → 每次 `_open_session_db_for_active_scope()`）。
- `gateway/session_persistence.py:118-138` — `_profile_home_for_key()` 及其注释：「Only hits are memoized: a profile directory can be provisioned *after* startup (enrollment bridge), and a cached miss would pin that profile's rows to the ambient store for life.」
- `gateway/run_adapters.py:895-899` — `self.pairing_stores[name] = (self.pairing_store if name == active else PairingStore(profile=name))`。
- `gateway/session.py:654` — `build_session_key(source, …, profile=None)`（**唯一构造入口**）。
- `gateway/platforms/base.py:1904` — `self._owner_profile: Optional[str] = None`。
- `gateway/run_adapters.py:1115` — `adapter.set_owner_profile(profile_name)`（**配置适配器时装上**）。
- `gateway/platforms/base.py:2306-2325` — `_session_key_profile(source)` 及其 docstring：「Profile namespace for an adapter-derived session key. Ingress … stamps ``source.profile``, so without this every bot in a multiplex … ``agent:main:`` lane. Order: ``source.profile`` → ``_owner_profile`` → store resolver; getattr-guarded (object.__new__ in tests), type-checked…」。
- `gateway/session_recovery.py:35-46` — `_resolve_profile_for_key(source)`：「None when multiplexing is off (legacy ``agent:main``), else ``source.profile`` or the active profile.」
- `gateway/session.py:625-634` — `_session_key_namespace(profile)`（§十一 ② 已引其 docstring）。
- `gateway/run_adapters.py:832-873` — `_start_secondary_profile_adapters()`（§十二 ② 已引 docstring）；`:855` `for profile_name, profile_home in profile_homes:`；`:856` `if profile_name == active: continue`（主循环负责它）；`:862` `except MultiplexConfigError: raise`；`:865` `except Exception as e: logger.error("Failed to start adapters for profile '%s': %s", profile_name, e, exc_info=True)`；`:512`→ 状态面 `self._record_served_profiles(active, profile_homes)`。另有关多路复用时的状态面清理注释：「``write_runtime_status`` re-stamps the previous writer's record in place, so a multiplexer's ``served_profiles`` would outlive it into this single-profile run and `hermes -p X ...` would keep refusing (exit 78) / reporting "served" for profiles nobody serves.」
- `gateway/run_adapters.py:1004-1100` — `_start_one_profile_adapters()` **逐平台跳过分支**：
  - 无凭据 → `logger.info("[MULTIPLEX] Profile '%s': skipping %s - no bot credential in this profile's secrets")`；
  - relay/whatsapp → `self._note_unserved_secondary_platform(...)`（共享进程级入口，次级会重试循环）；注释：「four profiles with WHATSAPP_ENABLED=true and nothing in the log is a silent dead channel.」；
  - mirror 平台 → `logger.info("[MULTIPLEX] Profile '%s': %s is served by the default profile's listener at /p/%s/ — not starting a second listener")`（api_server/webhook，`#100397`）；
  - 适配器建不出 → `logger.warning("… skipping platform '%s' - adapter creation returned None")`；
  - 重复认领 → `_refuse_duplicate_claim(...) → continue`（`:932-963`）；
  - connect 失败 → `logger.warning("✗ %s failed to connect (profile: %s)")` → `_safe_adapter_disconnect` → `_schedule_secondary_profile_startup_reconnect(profile_name, platform, adapter)` → `continue`；
  - 成功 → `profile_map[platform] = adapter; logger.info("✓ %s connected (profile: %s)")`。
- `gateway/run_startup.py:1020-1062` — `_start_prefilter_platforms()`（主 profile 侧）；注释：「Multiplex: a platform enabled in the shared config.yaml may hold its token only in a secondary profile's .env; an empty primary would queue a reconnect loop that never heals. … skip the empty primary instead of failing loudly.」（`#64674`）。
- `gateway/run_startup.py:1186-1210` — `_start_secondary_profiles()`；`except MultiplexConfigError as e: logger.error("Gateway multiplexer config error: %s", str(e))` → `self._startup_fail_fatal_config(str(e))` → 启动中止。

mini 侧（§九/§十/§十一/§十二 正文未逐条列出者）：

- `gateway/session_keys.py:52` — `bot_profile_key()`（归一：默认 profile ↔ `None`）。
- `gateway/run.py:171` — 键的命名空间取 `source.profile or self.profile`。
- `gateway/run_adapters.py:193` — `profile_feed()`（未知 profile → `False`）。

### 附录 A 追加（g4）

正文每节只保留一段代码引用；以下是从 §十三~§十六 正文挪出的 `文件:行号`（原样，未删信息）：

**§十三 Control plane**
- `tui_gateway/methods_profiles.py:21-30` —— `_profile_handler(name, code)`：未捕获异常 → `_err(rid, code, str(e))`
- `tui_gateway/methods_profiles.py:59-67` —— `_hermes_home_scope(path)` 定义（`set/reset_hermes_home_override`）
- `tui_gateway/methods_profiles.py:248/362/429/592/624/662` —— 六个 handler（方法级错误码 5061…5066 + 5067；局部错误码 4061 name required / 4063-4064 resolve/not found / 4066 unknown asset / 4067 data required / 4068 not valid base64 / 4069 asset too large / 4070 unsupported format）

**§十四 Failure modes**
- `gateway/run.py:2184-2205` —— `_own_policy_open_startup_violation(config)`（*"Return a startup-abort reason when open policy lacks allow-all opt-in."*）
- `gateway/run_adapters.py:926` —— `raise MultiplexConfigError(...)`

**§十五 Known limitations**：无（五行表与全部引用均留在正文）
**§十六 Non-goals**：无

---

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

---

## 附录 C · 上游文档出入

本章共 **6 处**，完整表格与证据在《中文精译-07c》附录 C（① `profiles_to_serve` 的 allowlist 参数不存在 ·
② `SecondaryPortBindingConfigError` 无此类 · ③ 限制表 MCP 那条已被 #67605 修掉 · ④ `runtime_status.json`
实为 `gateway_state.json` · ⑤ "二级启用占端口平台 = 启动即致命"语义错位 · ⑥ 父链是单跳不是多级）。
本文 §十二/§十四 已把 ②⑤ 讲透，§十五 讲了 ③，§五/§七 引了 ④ 的落点。

**仍然未核的**（诚实清单）：① 原文"已知限制"表除 MCP 外的其余四行（`TERMINAL_*` / 内置注册表 /
能力注册表 / 监听·锁）没有逐条去源码验证；② `build_migration_plan()` 与 `_host_supports_migration()`
的完整构造（只会走 B4/B5 的"不抄"结论，不影响本章实现）。
