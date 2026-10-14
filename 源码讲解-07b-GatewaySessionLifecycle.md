# 源码讲解 · 第 7 章 · 扩展 7.1（旧称第 8 章）：网关会话生命周期（Gateway Session Lifecycle）

> **上游锚**：`f5d1926110`（Hermes `v0.21.3`）。本文真实侧的 `文件:行号` 对这个 commit。
> **真实源码**：`~/.hermes/hermes-agent/`（`gateway/` 包 = **160 个 `.py` / 86,785 行**）
> **对照实现**：`mini-hermes/`（全项目 **11,908 行**代码 / **5,956 行**测试）
> **本章新增**：`session_state.py` / `run_startup.py` / `run_busy.py` / `agent_cache.py` / `run_watchers.py`（5 个新文件）

> 收尾维护：缓存演示清理本次临时目录，并在退出时恢复临时替换的 Path.read_text；读取次数打印实测值。完整收据见[发布前修复记录](发布前修复记录.md)。

[TOC]

## 发布前维护 · 缓存与生命周期

每条入站消息都经过 `_open_session()` 的生命周期路由，即使已有缓存 agent，也重新检查 suspended、ended 与 resume_pending。`busy` 在 `finally` 中清除，前置异常和中断不会永久占住忙状态；`pending_flush` 只有正常完成时才解除，失败、中断和超预算仍受淘汰保护。SIGTERM 现在执行关停恢复标记与资源清理；等待超时不写干净退出标记。

这些是当前实现的行为；下文的章节提交、规模表和实测输出保留各自历史时点。新增回归在 `mini-hermes/tests/test_publish_regressions.py`，完整收据见[发布前修复记录](发布前修复记录.md)。



---

## 两份代码的路径与规模

| 关注点 | 真实 Hermes | mini-hermes |
|---|---|---|
| 会话状态机 | `gateway/session.py`（**1,284 行**）`get_or_create_session` + `_apply_route_checks` | `gateway/session_state.py`（**276 行**）`SessionRouter.route` |
| 生命周期动作 | `gateway/session_lifecycle.py`（`suspend_recently_active` `:245`、`suspend_session` `:107`） | 同 `session_state.py`（`suspend_recently_active` / `suspend`） |
| 启动恢复 | `gateway/run_startup.py` + `run_shutdown.py`（drain / 标记 / 计数） | `gateway/run_startup.py`（**99 行**）+ `run_shutdown.py` |
| 忙时处置 / 队列 | `gateway/run_busy.py`（`_promote_queued_event` `:57`、`_queue_depth` `:75`） | `gateway/run_busy.py`（**163 行**，同名方法） |
| agent 缓存 | `gateway/run.py:46`（`_AGENT_CACHE_MAX_SIZE = 128`）+ `run_agent_cache.py` + `agent_cache_pressure.py` | `gateway/agent_cache.py`（**250 行**） |
| 后台巡检 | `gateway/run_watchers.py:40-86`（`_session_housekeeping_watcher(interval=300)`） | `gateway/run_watchers.py`（**134 行**） |
| 存储层状态列 | `sessions` 表 + `sessions.json` 镜像 | `sessions` 表 **+6 列**（`updated_at`/`suspended`/`resume_pending`/`resume_reason`/`last_resume_marked_at`/`is_fresh_reset`） |
| 测试 | （本次不逐条对） | `tests/test_gateway.py` **1,327 行 / 17 组 / 214 条** |

## 导言 · 机制总览：Hermes 具体怎么做的（先读这一节）

> 这一节只讲**真实 Hermes 一侧**。读完它应该能回答："两轮之间，这个会话是怎么活下来的？"
> 每条都给 `文件:行号` 和**逐字引文**（优先引注释，因为注释是维护者写下的"为什么"）。

### 0.1 谁在管

| 角色 | 住在哪 | 管什么 |
|---|---|---|
| `SessionEntry` | `gateway/session.py:498`（字段清单见 `:531`） | **一个会话键**对应的一条记录：`session_id`、`suspended`、`resume_pending`、`resume_reason`、`is_fresh_reset`、`was_auto_reset`、`auto_reset_reason`、`reset_had_activity`、`created_at`、`updated_at`…… |
| `SessionStore` | `gateway/session.py`（`get_or_create_session`） | **唯一的决策点**：这条消息该用哪个会话 |
| `gateway/session_lifecycle.py` | `:107` `suspend_session`、`:245` `suspend_recently_active` | 状态**置位**动作（挂起 / 标待恢复） |
| `gateway/run_startup.py` + `run_shutdown.py` | `:786-795`、`:1704`、`:1889` | 进程边界的收尾与恢复 |
| `gateway/run_busy.py` | `:46-80` | 忙时消息去哪儿（单槽 / 溢出） |
| `run_agent_cache.py` + `agent_cache_pressure.py` | `:820-861` | 缓存 agent 的淘汰（谁不许淘汰） |
| `gateway/run_watchers.py` | `:40-86` | 后台巡检（每 300 秒） |

### 0.2 什么时候

```text
每条消息入站        → ① 路由决策（get_or_create_session）        ← 本章的主战场
                    ② 忙时处置（单槽 / 溢出 / 命令内联）
一轮跑完            → ③ 清 resume_pending、消费 is_fresh_reset、清自己的重启计数
网关启动（脏退出）  → ④ 恢复四步（标记 → 计数 +1 → 卡死升级 → 续跑）
网关启动（干净退出）→ ④′ 直接跳过（读掉 .clean_shutdown 就返回）
网关关闭            → ⑤ 只对**正在跑**的会话打 resume_pending；写 .clean_shutdown
每 300 秒           → ⑥ 巡检 tick（空闲清扫 / 内存压力 / 每小时剪枝）
```

### 0.3 做什么

| 机制 | 真实实现的位置 | 一句话 |
|---|---|---|
| 路由决策 | `session.py`（`_apply_route_checks`） | 五个出口：新建 / **硬擦除**换新 / **软恢复**复用 / 复用 / 显式重置换新 |
| 硬擦除 vs 软擦除 | `session.py:1099-1107` | `suspended` 赢 `resume_pending`；`is_fresh_reset=True` + 旧会话 `end_reason="session_reset"` |
| 一次性标记 | `run_turn.py:444-449` | `is_fresh_reset` 在**这一轮跑完时消费掉** |
| 崩溃恢复 | `run_startup.py` + `session_lifecycle.py:245` | `suspend_recently_active(max_age_seconds=120)` → 只标"刚活跃过的" |
| 卡死升级 | `run.py:3968-3969` | `_STUCK_LOOP_THRESHOLD = 3` + `.restart_failure_counts` |
| 两层队列 | `run_busy.py:46-80` | 单槽（覆盖） + 溢出 FIFO（不合并）；已有 pending 先消费，溢出项安排到后续槽位 |
| 回执 | `run_busy.py:55`、`:314` | `queued_event._gateway_accepted = True` |
| agent 缓存 | `run.py:46` + `run_agent_cache.py:820-861` | LRU + 内存预算；**三条不许淘汰** |
| 巡检 | `run_watchers.py:50-71` | 三件事，**一件都不结束对话** |

### 0.4 不变式（本章真正的骨架）

1. **时间永不轮转对话** —— 没有"空闲 N 小时就开新会话"这条路。能轮转的只有**显式边界**（`/new`、挂起、压缩轮转）。
2. **硬擦除永远赢过软擦除** —— 用户的明确意图不能被一次意外中断撤回。
3. **一次性的标记只用一次** —— `is_fresh_reset` 跑完就消费；`resume_pending` **读到不清**、跑完才清。
4. **没落盘的不许淘汰** —— 缓存清理的判据是 `transcript_persistence_caught_up`，不是"内存紧就先扔"。
5. **只放资源，不结束对话** —— 空闲清扫 / 内存压力 / 剪枝，三者都不写 `ended_at`。
6. **进程退出不是对话边界** —— 关闭/重启只对**正在跑**的会话打软恢复标记（`shutdown_timeout` / `restart_timeout`）。

### 0.5 出错

| 出错 | 真实处理 |
|---|---|
| 进程崩了 / 掉电 | 没有 `.clean_shutdown` → 启动时 `suspend_recently_active` 标记最近活跃的会话 |
| **反复**崩（3+ 次） | `_suspend_stuck_loop_sessions()` → 挂起 + **清空计数文件**（`counters start fresh after suspension`） |
| 会话跑完 | `_clear_restart_failure_count`（计数**不是只加不减**） |
| drain 超时 | `run_shutdown.py:1892`：`"Skipping .clean_shutdown marker — drain timed out with …"`（**超时就不写标记** → 下次启动仍走恢复） |
| 长任务卡死 | **stall 看门狗**（`run_watchers.py:77-80`，`HERMES_SESSION_STALL_TIMEOUT` 默认 300，只看 `AIAgent.get_activity_summary()`）—— 文档没写，源码有 |

### 0.6 跨层

| 层 | 关心什么 |
|---|---|
| 存储层（`hermes_state_*`） | 状态列、`end_reason`、`updated_at`（"用户活动"，不是"进程活动"）、血缘 |
| 网关（`gateway/`） | 决策、队列、缓存、巡检、进程身份 |
| agent | `run_conversation` 返回 `interrupted` / `usage`；`get_activity_summary()` 供 stall 判据 |
| CLI/TUI | `/new` `/queue` `/stop` `/sessions`；**`/stop` 只打断 + 清队列，不挂起**（见附录 C ②） |
| 监督者（systemd/s6/launchd） | 退出码 78 = `EX_CONFIG`（停放，不反复重启）、`.clean_shutdown`、`.restart_failure_counts` |

### 0.7 时间线

**一条消息（同一会话，第二轮）：**

```text
入站 → route()：
        row = 最近一条会话行
        ① 没有行 → 新建
        ② 有行但已结束 → 新建（不复活）
        ③ 挂了 → end(旧,"suspended") + 新建（is_fresh_reset）
        ④ 待恢复 → **复用**（守着标记不清）+ touch
        ⑤ 否则 → 复用 + touch
     → 缓存取 agent（session_id 不一致就先淘汰）
     → 跑 → 回填用量 → 清 resume_pending / 消费 is_fresh_reset / 清重启计数
```

**一个网关进程的一生：**

```text
启动 → 读 .clean_shutdown（有 → 跳过恢复）→ 崩溃恢复四步 → 起巡检线程（先睡 60s）
运行 → 每 300s：空闲清扫 / 压力清扫 / （每小时）剪枝
关闭 → 停巡检 → 只对正在跑的会话 mark_resume_pending → 写 .clean_shutdown → 摘身份文件
```

**验收四问**（读完这一节应该答得出）：

1. 同一个会话隔了 30 天、中间还重启过 —— 下一句话会进**哪个**会话？为什么？
2. 用户 `/stop` 之后，下一句话是同一个会话还是新会话？
3. 一个会话连着 3 次重启都在"跑"，第 4 次启动会发生什么？谁决定？
4. 内存吃紧时，哪三类 agent **不许**被淘汰？为什么"没落盘的"那一类最要紧？

---

## §〇 差异表（本章 26 条）

本表以实现提交 `a4799bb` 的交付快照为主；当前修复见“发布前维护”。现有章标签已移到统一维护提交。不是差异的写法也一样：`已对齐` = 抄平了。

| # | 真实 Hermes | mini 版 | 性质 | 值得改 |
|---|---|---|---|---|
| 1 | `SessionEntry` 在内存里，另写 `sessions.json` **镜像**（源码叫 mirror，SQLite 才是权威） | 状态存在 `sessions` 表 **6 个新列**里，不写镜像 | 等价物 | 不改：mini 没有第二个读者 |
| 2 | `get_or_create_session` 是**单飞**的（同 key 并发共享 owner 结果 + 全部阻塞 IO 在锁外） | 线程模型 + 每会话一把锁（第 7 章守卫已保证同会话串行） | 规模差距 | 不改 |
| 3 | 读不到会话行时还有三条检查（压缩 tip / 已结束 / 路由重置原因） | 未实现这些检查；CLI 与网关仍可能跨进程访问同一库 | 规模差距 | 不改 |
| 4 | 优先级 `suspended`（硬） **>** `resume_pending`（软） **>** 复用 | `route()` 同一顺序，且有测试锁死 | **已对齐** | — |
| 5 | 软标记**不许覆盖**硬擦除（`mark_resume_pending` 逐字 *"Will NOT override `suspended=True`"*） | `mark_resume_pending` 里先查 `suspended` 再写 | **已对齐** | — |
| 6 | `is_fresh_reset` 在重置时置位（`session.py:1099`，自动重置也置位） | `_create()` 里 `is_fresh_reset=bool(previous)` | **已对齐** | — |
| 7 | `is_fresh_reset` 在**这一轮跑完时消费**（`run_turn.py:446-449`） | `run_turn` 成功跑完调 `clear_fresh_reset(sid)` | **已对齐** | — |
| 8 | 另有 `was_auto_reset` / `auto_reset_reason` / `reset_had_activity` 三个字段 | 没有 | **消费者缺失**：mini 没有"重注入话题/技能"、也没有"客户端附件重发" | 不改，代价见 §七 ④ |
| 9 | **时间永不轮转**：没有空闲/每日边界重置 | 同样的不变量，另有**结构断言**（动作枚举只有三种出口）+ 30 天回拨测试 | **已对齐** | 不改 |
| 10 | 恢复扫描：只标**开着的、最近 120 秒**活跃过的（`suspend_recently_active(max_age_seconds=120)`） | 同（SQL 层显式 `ended_at IS NULL`） | **已对齐** | — |
| 11 | 恢复第 ④ 步：**主动**合造 `MessageEvent` 走 `_handle_message` 续跑 | 保留 `resume_pending`、**下一条真实消息**接着原会话（不清标记） | **基座缺失**：mini 的 `AIAgent` 要求用户消息才能起轮 | 想做要先改第 2 章交付物 |
| 12 | 卡死升级：`_STUCK_LOOP_THRESHOLD = 3` + `.restart_failure_counts` + 升级后**清空**计数 | `STUCK_LOOP_THRESHOLD = 3` + 同名计数文件 + 清空；且**权威是持久化计数**（不依赖本次标了谁） | **已对齐** | — |
| 13 | 会话跑完 → `_clear_restart_failure_count`（计数不只加不减） | 同（`clear_one_stuck_loop_count`，在成功跑完那一支里） | **已对齐** | — |
| 14 | **stall 看门狗**（`run_watchers.py:77-80`，默认 300s，只看 `get_activity_summary()`） | 没有 | **基座缺失**：mini 的 agent 没有 activity summary | 值得做，成本中等 |
| 15 | 两层队列：单槽（**单值**，后到覆盖）+ 溢出 FIFO（不合并） | `SessionQueues` 同形；mini 优先显式 FIFO，再取普通消息单槽；与上游已有 pending 优先的混合顺序不同 | **已对齐** | — |
| 16 | `_gateway_accepted = True`（`run_busy.py:55`、`:314`） | 入队那一刻 `setattr(event, "_gateway_accepted", True)` | **已对齐**（第 7 章曾判"没有队列→不抄"，本章激活） | — |
| 17 | 覆盖掉上一条排队消息时给用户回执/提示 | `_notify_overwrite` → 用户可见通知（含正确替代动作 `/queue`） | **已对齐** | — |
| 18 | agent 缓存：`_AGENT_CACHE_MAX_SIZE = 128` + TTL + 保护最近 N + `memory_high_mb` 预算；三条不许淘汰 | `AgentCache` 128 / 3600 / 8 + 固定 2048MB；三条不许淘汰同 | 保护机制对齐；mini 不缓存完整消息列表，但保留提示词、todo、压缩器等状态 | — |
| 19 | `memory_high_mb` 配置默认 **`"auto"`**（`config_defaults.py:75`，自己探测） | 固定 `2048.0` | 简化 | 可改，成本低 |
| 20 | 内存压力：按"每个条目估算大小"算要到线下多少 | 固定目标（放掉约 1/4），**返回值里明说这是估算** | 规模差距 | 不改 |
| 21 | 巡检三件事：空闲清扫 / 压力清扫 / 每小时 `prune_old_entries(max_age)`（`max_age > 0` 才做） | 三件都在；第三件 mini **没有删除路径** → 抄开关语义 + 诚实记债 | **基座缺失**（第 6 章没做删除） | 值得做，成本中等 |
| 22 | `.clean_shutdown` **只在 drain 没超时**时才写（`run_shutdown.py:1892` 反之打日志） | 发布前修复已要求退出成功且回合排空；等待超时不写 | 已补齐门槛 | — |
| 23 | PII 脱敏：`user_<12hex>`（Discord 除外） | 没有 | **消费者缺失**：mini 没有"会话上下文注入"段 | 不改 |
| 24 | `SessionSource` 22 个字段（多平台怪癖） | 6 个字段 | 规模差距（只有一个 console 适配器） | 不改 |
| 25 | 进程退出**不结束**对话；只对正在跑的会话打 `shutdown_timeout` / `restart_timeout` | 同（**修了第 7 章的两处语义错**，见 §九） | **已对齐** | — |
| 26 | 压缩/轮转之后淘汰缓存 agent（`_evict_cached_agent`） | 网关侧**没有**这条调用 | **接缝为空**：mini 的压缩在 agent 侧，且 agent 每轮现读历史 → 那个风险不存在 | 不做（但风险写进 §十 ④，含"什么时候它突然变必需"） |

---

## §一 路由状态机：这条消息该用哪个会话

**① Hermes 怎么做的**

`SessionStore.get_or_create_session()` 是**唯一**的决策点。它按**固定优先级**回答一个问题：

```text
情况 0：这个键从来没有会话        → 新建
显式重置（/new、/reset）          → 结束旧的 + 新建（is_fresh_reset）
上一次那条已经结束了              → 新建（**不复活**）
优先级 1：suspended（硬擦除）     → 结束旧的 + 新建（**永远赢**）
优先级 2：resume_pending（软恢复） → **复用同一个 session_id**
优先级 3：没有触发条件            → 复用 + touch
```

**没有第 7 条。** 没有"空闲太久就开新的"、没有"每天换一个"、没有"消息数超了就换" ——
这是本章最重要的一条不变量（§二）。

**② 读 Hermes 的代码**

`gateway/session.py`（`get_or_create_session` 的决策段）：

```python
# ── 优先级 1：suspended（硬擦除）→ 强制重置，**永远赢** ──
#   真实版把这条排在 resume_pending 前面；顺序反了 = 把用户丢弃的对话复活
```

（真实版这段的注释直接写了"优先级"三个字；`_apply_route_checks` 是它的判定实现，
返回一个 `_RouteChecks` 结构，再由 `get_or_create_session` 落成动作。）

★ 顺便记下真实版**没写进文档**的一层：`get_or_create_session` 是**单飞**的 ——
同一个 `session_key` 的并发调用会共享同一个 owner 的结果（"一段会话只能被创建一次"），
而且**全部阻塞 IO 都在锁外**。文档那张状态图只是它的**逻辑内核**。

**③ mini 的实现**

`gateway/session_state.py::SessionRouter.route()` —— 逐条对齐上面那六个出口：

```python
        # ── 情况 0：这个键从来没有会话 → 开新的 ──
        if entry is None:
            return self._create(session_key, source, reason=None, **create_kwargs)

        # ── 显式重置（/new、/reset）：唯一一种"无论如何都换"的用户意图 ──
        if force_new:
            self._end(entry.session_id, reason="session_reset")
            return self._create(session_key, source, reason="session_reset",
                                previous=entry.session_id, force_new=True, **create_kwargs)

        # ── 上一次那条已经结束了 → 开新的（结束过就是结束过，不复活）──
        if not entry.open:
            return self._create(session_key, source, reason=entry.ended_reason or "ended",
                                previous=entry.session_id, **create_kwargs)
```

调用点也换了：第 7 章的 `_open_session` 是"按键查最新会话，没有就新建"，
现在**每轮都问状态机**（`run_turn._open_session` → `router.route(...)`）：

```python
        spec = self._provider_spec()
        decision = self._router().route(
            event.session_key, source=f"gateway:{event.source.platform}",
            model=spec.get("model"),
            model_config={k: spec.get(k) for k in ("provider", "api_mode")},
        )
```

★ 为什么"每轮都问"而不是"算一次缓存起来"：**状态是别人（恢复流程、卡死升级、
`/new`）改的**。缓存一个 `session_id` 就等于在内存里复制了一份状态 —— 而状态有多个写者时，
内存副本必然会漂。mini 的 `self._session_ids` 现在只是**观察面**（`/sessions` 与测试看），
不再是权威。

**④ mini 为什么没做成这样 / 值不值得改**

- **单飞（同键并发请求共用一次创建结果）没抄**（差异表 #2）：mini 是线程模型，同一会话的并发已经被第 1 级守卫挡住
  （一把锁 + 忙时排队），所以"并发创建同一会话"这种情况在 mini 里**正常适配器入口会被守卫挡住；绕过入口或跨进程调用仍可能发生**。
  要抄就得把 `route()` 拆成"锁内快照 / 无锁检查 / 锁外 IO"三段 —— 那是 200+ 行的并发改造，
  换来的收益是"同键并发下避免重复创建"这个 mini 场景里不存在的保证。**不值得改。**
- **内存镜像没抄**（差异表 #1）：真实版写 `sessions.json` 是因为有**第二个读者**
  （别的进程/工具要看路由表）。mini 的读者只有网关自己，库就是索引。**不值得改。**
  代价写在这里：将来若加一个只读消费者（比如桌面 dashboard），得补一个镜像或让它读库。

---

## §二 显式边界：时间永不轮转

**① Hermes 怎么做的**

每次"会话换了"，都必须能指出**是谁要求的**：

| 触发 | 性质 | 谁要求 |
|---|---|---|
| `/new` `/reset` | 显式边界 | **用户** |
| `suspended` | 硬擦除 | 用户（`/stop` 那条是文档的说法，**实测不成立**，见附录 C ②）+ 卡死升级 |
| 压缩轮转 | 隐式边界 | 上下文策略（第 5 章） |
| `resume_pending` | **不是边界** | 意外中断 —— 它**不改归属**，只是留个标记 |
| 空闲时间 / 每日边界 | **不存在** | —— |

真实版有一条专门的不变量注释在 `session.py`（`SessionResetPolicy` 那类东西）里，
把"时间"从轮转的判据里排除出去。

**② 读 Hermes 的代码**

`gateway/session.py:1099-1107`（自动重置那一段，**不是**用户主动 `/new`）：

```python
                is_fresh_reset=True,
                ...
            end_reason="session_reset",
```

两条信息：① 自动重置同样打 `is_fresh_reset`（不是只有 `/new` 才算"重置后的第一条"）；
② 旧会话的结束原因是 `session_reset`，不是自定义字符串 —— 这是给**读方**用的词表
（`run.py:3971` 那条注释就在维护一份 reason 词表：
`# Reasons set by _stop_impl() on force-interrupt; "restart_interrupted" by suspend_recently_active()`）。

**③ mini 的实现**

mini 把这条不变量落成**三件东西**（光写文档不算数）：

1. `route()` 里**没有**任何时间读取 —— 它的三个出口只有 `created` / `reused` / `auto_reset`；
2. **结构断言**（防止将来有人加第四条路）：

```python
            actions = set(re.findall(r'action="([a-z_]+)"', mod))
            actions |= set(re.findall(r'else "([a-z_]+)"', mod))     # 三元表达式里的那一支
            check("状态机的出口只有 created/reused/auto_reset（没有基于时间的第四种）",
                  actions == {"created", "reused", "auto_reset"}, str(actions))
```

3. **行为断言**：把 `updated_at`/`started_at` 倒拨 30 天再重启，仍必须复用同一个会话。
   反向验证里有一个改法专门给 `route()` 加一条"空闲 24 小时就重置" —— 这条断言**干净变红**。

★ 顺带修掉一处**反面**：`shutdown()` 原来给每个会话 `touch()`（刷新活动时间）。
那等于把"进程活动"记成"用户活动"，后果是恢复扫描的 **120 秒窗口**形同虚设
（一次优雅关闭之后，网关知道的**所有**会话都变成"刚刚活跃"，下次脏启动会把它们全标成待恢复）。
现在关闭路径**不 touch**，并有断言守着：`（前提）关闭没有把 updated_at 刷新成"刚刚活动"`。

**④ mini 为什么没做成这样 / 值不值得改**

mini 一开始就**没有**时间轮转（第 7 章的 `_open_session` 只做"按键找回"），
所以这一节的改动不是"抄一个机制"，而是**把一条隐式性质变成显式不变量**：
加结构断言 + 行为断言 + 修掉一处相反的做法（shutdown 的 touch）。
这就是"能对齐就对齐"最划算的形态 —— **成本几乎为零，防止将来被改坏**。

---

## §三 硬擦除 vs 软擦除：优先级与"不许覆盖"

**① Hermes 怎么做的**

两个标志位，语义完全不同：

| 标志 | 谁置位 | 下次访问的效果 | 能不能被覆盖 |
|---|---|---|---|
| `suspended`（硬） | 卡死循环升级（文档还说 `/stop`，实测不成立） | **换新会话**，旧会话 `end_reason="suspended"` | **不能被软标记撤回** |
| `resume_pending`（软） | 崩溃恢复 / drain 超时 | **复用同一个会话**，标记留到这一轮跑完 | 不许覆盖硬擦除 |

**② 读 Hermes 的代码**

`gateway/session_lifecycle.py`（`mark_resume_pending` 的 docstring 逐字）：

```text
Will NOT override `suspended=True`
```

`session_lifecycle.py:245`：

```python
    def suspend_recently_active(self, max_age_seconds: int = 120) -> int:
```

（★ 它返回的是**被标记的条数**，不是列表 —— 真实版的调用方只用来记日志。mini 返回列表，
因为 mini 的调用方（`run_startup`）要用它去 `bump_stuck_loop_counts`。这算一处**小差异**，
理由写在 §四 ④。）

**③ mini 的实现**

```python
    def mark_resume_pending(self, session_id: str, reason: str) -> None:
        row = self.db.get_session(session_id)
        if row is None:
            return
        if int(row.get("suspended") or 0):
            return                      # ★ 硬擦除是用户的明确意图，不能被一次"意外中断"撤回
        self.db.set_route_state(session_id, resume_pending=True, resume_reason=reason)
```

优先级在 `route()` 里，两个 `if` 的**先后顺序**就是优先级：

```python
        # ── 优先级 1：suspended（硬擦除）→ 强制重置，**永远赢** ──
        if entry.suspended:
            self._end(entry.session_id, reason="suspended")
            return self._create(session_key, source, reason="suspended",
                                previous=entry.session_id, **create_kwargs)

        # ── 优先级 2：resume_pending（软恢复）→ 保留 session_id ──
        if entry.resume_pending:
            ...
            self.touch(entry.session_id)
            return decision
```

★ 软恢复那一支**不清标记**，理由逐字引在代码里：

```python
        #    ★ 注意这里**不清标记**：真实版 docstring 写得很清楚 ——
        #      "The marking is not cleared here — it survives until the next successful
        #       turn completes"。读的时候就清，会让"读了之后又崩"永远恢复不上。
```

清理在**跑完之后**（`run_turn`，和消费 `is_fresh_reset`、清重启计数同一支）：

```python
            if not result.get("interrupted"):
                self._router().clear_resume_pending(sid)
                self._router().clear_fresh_reset(sid)
                gw_status.clear_one_stuck_loop_count(self.home, key)
```

**④ mini 为什么没做成这样 / 值不值得改**

- **`was_auto_reset` / `auto_reset_reason` / `reset_had_activity` 三个字段没抄**（差异表 #8）：
  它们的消费者是"重置后重注入话题/技能"和"客户端附件重发"。mini 没有话题、没有技能注入、
  也没有附件重发这条路径 —— 抄了就是**三个没人读的布尔值**。**不值得改。**
  代价写明：将来给 mini 补"重置后重注入"时，得先补这几个字段（顺便把 `is_fresh_reset`
  的消费点从"跑完就清"改成"注入完再清"）。
- **`suspend_recently_active` 的返回值不同**（真实版返回条数、mini 返回列表）：mini 的
  `run_startup` 要用这份名单去 `bump_stuck_loop_counts`（"这次重启还活着的会话计数 +1"）。
  真实版不需要，因为它的计数在**另一条路径**上维护。这是**接口按消费者定制**，
  不是"抄漏了" —— 但确实是一处**形状差异**，记在这里。

---

## §四 重启恢复：四个动作的顺序

**① Hermes 怎么做的**

```text
网关启动
   │
   ▼  ① 有 .clean_shutdown 标记吗？── 有 ──► 跳过挂起（上次是干净退出）
   │ 没有
   ▼  ② suspend_recently_active(120s)：把**最近活跃过**的会话标成 resume_pending
   ▼  ③ _suspend_stuck_loop_sessions()：跨 3+ 次重启还活跃的 → 挂起
   ▼  ④ 对每个 resume_pending 的会话：合造 MessageEvent → 跑 _handle_message 续跑
```

★ 顺序不能换：① 决定"要不要做"，② 决定"谁需要"，③ 决定"谁已经病态"，④ 才动手。
把 ④ 放在 ③ 前面，就会先把一个**病态会话**接着跑起来，然后才挂起它 —— 白跑一轮。

**② 读 Hermes 的代码**

`gateway/run_shutdown.py:1889`（写标记）与 `:1892`（**不打标记**的那条路）：

```python
                (_hermes_home / ".clean_shutdown").touch()
```
```text
"Skipping .clean_shutdown marker — drain timed out with "
```

★ 这两行合起来说明一件事：**"干净退出"是有门槛的** —— drain 超时就不算干净
（因为那一轮可能被打断了），下次启动必须照常走恢复。

`gateway/run_shutdown.py:786-795`（关闭时对**正在跑**的会话做什么）：

```python
        reason = "restart_timeout" if self._restart_requested else "shutdown_timeout"
        ...
                await self.async_session_store.mark_resume_pending(_sk, reason)
```

`session_lifecycle.py:245-259`（② 的三条边界，逐字）：

```text
    """Mark sessions active within *max_age_seconds* as ``resume_pending`` after a crash/fast
    restart (already-pending and suspended entries are skipped). Returns the number marked.
    ...
    to avoid touching long-idle sessions. Sets ``resume_pending=True`` so the next incoming message on
```
```python
            if entry.resume_pending or entry.suspended or entry.updated_at < cutoff:
                continue
```

**③ mini 的实现**

`gateway/run_startup.py::_recover_sessions()` —— 四步，每步都写进**收据**：

```python
        # ── ① 上次是干净退出吗？读过即删（标记只说明"上一次"）──
        if gw_status.consume_clean_shutdown(self.home):
            receipt["clean_shutdown"] = True
            return receipt

        router = self._router()

        # ── ② 崩溃恢复：把最近活跃过的会话标成待恢复 ──
        marked = router.suspend_recently_active()
        receipt["resume_pending"] = marked
```

★ 真实版②的三条边界，mini 落在**两个地方**（因为 mini 是查库、真实版是遍历内存索引）：
`recently_active_session_ids()` 里排除已结束的（`AND ended_at IS NULL`），
`suspend_recently_active()` 里跳过 `suspended` / 已经 `resume_pending` 的。

调用点在 `start()` 里，位置有讲究：

```python
        #    ★ 顺序要求：在"身份文件写好之后"（恢复要用 home）、"适配器连上之前"
        #      （否则入站消息会在恢复中途进来）。
        recovery = self._recover_sessions()

        # ── 第 8 章：后台巡检线程 ──
        self._start_housekeeping()
```

启动横幅里会报一行（`_recovery_banner`），例如：

```text
  恢复     ⚠️ 1 个会话跨 3+ 次重启仍活跃 → 已挂起（下次访问换新会话）：main:console:dm:console-1
```

**④ mini 为什么没做成这样 / 值不值得改**

**第 ④ 步 mini 有意做得不一样**，原因写进了模块 docstring：

- 真实版是**主动**续跑：造一条合成的消息事件，走和用户发消息**完全一样**的那条路
  （连"恢复"都不需要单独的逻辑 —— 这是它最漂亮的地方）。
- mini 的 `AIAgent`（第 2 章交付物）**要求一条用户消息才能起轮**，没有"无人值守地接着
  上一个转写本继续"这条路径。所以 mini 的等价效果是：**保留 `resume_pending`，
  下一条真实消息进来时拿到同一个 `session_id`** —— 用户看到的是"对话还在"。
- **代价写清楚**：网关不会自己接着跑（没有用户消息就一直停着）。
  要做"主动续跑"，得先给 `AIAgent` 加一条"没有用户消息也能起轮"的入口 ——
  那是**第 2 章的地盘**，不是本章能在网关层糊出来的。**本轮不改**（改在下一层，风险更大）。

★ 真跑里这一步的效果是可观测的（§十二 收据）：重启后仍是同一个 `session_id`，
转写本里 11 条消息跨重启连续。

---

## §五 卡死循环：恢复机制必须有自己的退出条件

**① Hermes 怎么做的**

崩溃恢复有一个**致命循环**：

```text
会话在跑 → 网关崩 → 重启恢复 → 接着跑 → 又崩 → 又恢复 → ……
```

用户在"一个永远跑不完的任务"前面被反复重启。所以恢复必须能**放弃**：

```text
.__restart_failure_counts = {"<session_key>": 连续重启次数}
   每次"这次重启它还活着" → +1
   到 3 → 挂起这个会话（硬擦除）→ **清空计数文件**
   会话正常跑完一轮 → 清掉它自己那条（不是只加不减）
```

**② 读 Hermes 的代码**

`gateway/run.py:3968-3971`（**逐字**，连注释一起）：

```python
    _STUCK_LOOP_THRESHOLD = 3  # restarts while active before auto-suspend
    _STUCK_LOOP_FILE = ".restart_failure_counts"
    # Reasons set by _stop_impl() on force-interrupt; "restart_interrupted" by suspend_recently_active()
```

★ 注意文件名：**不是**文档写的 `restart_counts.json`（第 8 章精译附录 C ③ 记了这处出入）。

升级那一段的注释里有一句关键的话（mini 逐字引过）：

```text
Clear the file — counters start fresh after suspension
```

——挂起之后**从头计数**。为什么：计数存在的意义是"判断是不是卡死了"；
一旦判定完成（挂起），旧账就没有价值了，留着只会让下一次偶发重启**立刻**又触发挂起。

读计数的实现里还有一条"不预检"的纪律（`_read_json_counts`）：

```text
or None when the file is missing/unreadable (no exists() pre-check needed)
```

★ 这是"**少一次竞态**"的写法：`if exists(): read()` 之间文件可能被删掉 ——
直接读、读不到当空，省掉了存在性检查与读取之间的竞态窗口；读取本身仍需处理失败。

**③ mini 的实现**

`gateway/status.py`（标记文件的家，名字照抄真实版）：

```python
CLEAN_SHUTDOWN_MARKER = ".clean_shutdown"     # gateway/run_shutdown.py:1889 优雅关闭末尾 touch
STUCK_LOOP_FILE = ".restart_failure_counts"   # gateway/run.py:3969 逐字（不是 restart_counts.json）
```

计数文件的四个动作（读/加/清空/清一条）都在 `status.py`；读函数照抄"不预检"：

```python
def read_stuck_loop_counts(home: Path) -> Dict[str, int]:
    """读 `{会话键: 连续重启次数}`。读不出来就当空（**不做 exists 预检**，
    照真实版 `_read_json_counts` 的注释：*"or None when the file is missing/unreadable
    (no exists() pre-check needed)"*）。"""
    try:
        raw = json.loads(stuck_loop_counts_path(home).read_text(encoding="utf-8"))
    except Exception:
        return {}
```

`clear_one_stuck_loop_count` 的 docstring 把"为什么不是只加不减"写下来了：

```python
    ★ 为什么不是"只加不减"：一个会话只要成功跑完一轮，就说明它不卡了 ——
    计数该归零。不归零的话，历史上攒够 3 次就再也说不清"是最近卡的还是三天前卡的"。
```

升级判定在 `session_state.suspend_stuck_loop_sessions(counts, threshold=3)`，
**清空由调用方做**（照真实版分工：返回被挂起的键，调用方清文件）。

**④ mini 为什么没做成这样 / 值不值得改**

这一条**全抄**。有一处**实现顺序**值得一提（它是本章第 4 个真 bug，见 §十一）：

mini 最初写的是

```python
        keys = [k for k in (db.session_key_for_id(sid) for sid in marked) if k]
        if keys:                      # ← 错：没有新标记就整个跳过卡死升级
            counts = gw_status.bump_stuck_loop_counts(self.home, keys)
            suspended = router.suspend_stuck_loop_sessions(counts)
```

后果：一个会话**已经被标为待恢复** → 恢复扫描会跳过它（`resume_pending` 的那条边界）→ `marked` 为空
→ `keys` 为空 → **卡死升级永远不触发**（升级路径被自己的前置条件挡死了）。
正确写法是"计数文件是权威"：

```python
        if keys:
            gw_status.bump_stuck_loop_counts(self.home, keys)   # 先给"还活着的"加一
        counts = gw_status.read_stuck_loop_counts(self.home)     # ★ 不管有没有新标记都读文件
        suspended = router.suspend_stuck_loop_sessions(counts)
```

反向验证里专门留了一条改法（"又被'本次有没有新标记'挡住"）——干净变红。

---

## §六 两层队列：两种意图，两种结构

**① Hermes 怎么做的**

一个会话同时只能跑一轮。忙的时候又来了消息，**两种用户意图必须分开**：

| 用户干了什么 | 意图 | 结构 | 行为 |
|---|---|---|---|
| 又发了一条普通消息 | "**我改主意了**" —— 后来那条才是现在想要的 | **单槽**（`_pending_messages[key]`，**单值**） | 后到**覆盖**先到 |
| `/queue 把这件也做了` | "**这几件都要做**，一件都不能少" | **溢出 FIFO** | 尾部入队、按序处理、永不合并 |

上游先消费已取出的 `pending_event`；它存在时，溢出队列头被暂存到下一次槽位。
只有没有 pending 时才直接取溢出头。因此不能概括为“先溢出、后单槽”。
mini 则把显式 `/queue` 全放进独立 FIFO，并优先于普通消息单槽处理；这是混合事件顺序的有意差异。

**② 读 Hermes 的代码**

`gateway/run_busy.py:57-73`（**逐字**，docstring + 实现）：

```python
    def _promote_queued_event(
        self, session_key: str, adapter: Any, pending_event: Optional["MessageEvent"]
    ) -> Optional["MessageEvent"]:
        """Promote the next overflow item after the slot drained.

        ``pending_event`` None → the overflow head becomes the pending event; otherwise the head is
        staged into the slot for the NEXT recursion. Returns the (possibly updated) pending_event.
        """
        overflow = self._overflow_queue(session_key)
        if not overflow:
            return pending_event
        if pending_event is None:
            return overflow.pop(0)
        if adapter is not None and hasattr(adapter, "_pending_messages"):
            adapter._pending_messages[session_key] = overflow.pop(0)
        # else: no adapter — leave the head in place so we don't silently drop it.
        return pending_event
```

三处**逐字**的纪律：

1. `adapter._pending_messages[session_key] = overflow.pop(0)` 是**赋值** —— 单槽是**单值**，
   不是列表。（第 7 章的 mini 写成了 `list` + `append`：那等于"普通消息也 FIFO 排队"，
   于是"我改主意了"这种意图**表达不出来** —— 用户发十条，十条都会挨个跑完。）
2. `# else: no adapter — leave the head in place so we don't silently drop it.` ——
   **宁可留在队列里，也不静默丢掉**。
3. `_queue_depth` 也在同一个文件（`:75`）：深度 = 溢出条数 **+ 单槽（0 或 1）**。

回执（"接受"的那一刻）：

```python
        queued_event._gateway_accepted = True        # run_busy.py:55（另一处在 :314）
```

**③ mini 的实现**

`gateway/run_busy.py::SessionQueues`（两层，方法名对齐真实版语义）：

```python
    def keep_latest(self, event: Any) -> Optional[Any]:
        """普通消息 → 单槽（**后到覆盖先到**）。

        ★ 返回被顶掉的那条事件（没有则 None）：调用方**必须**把"你的前一条被覆盖了"
        告诉用户 —— 静默丢掉用户写的字，比拒绝更糟。
        """
        key = event.session_key
        overwritten = self._slot.get(key)
        self._slot[key] = event
        return overwritten

    def promote(self, key: str) -> Optional[Any]:
        """取下一个要跑的事件：**先溢出 FIFO，再单槽**（mini 的混合事件优先级，与上游已有 pending 优先的规则不同）。"""
        overflow = self._overflow.get(key) or []
        if overflow:
            nxt = overflow.pop(0)
            if not overflow:
                self._overflow.pop(key, None)
            return nxt
        return self._slot.pop(key, None)
```

适配器那边（第 1 级守卫）改成进单槽 + 回执 + 通知：

```python
        overwritten = self.queues.keep_latest(event)
        # ★ 回执（真实版 `run_busy.py:55` 逐字同名）：标上"网关已经接受它了"，
        #   交付层据此**不重复**回执 —— "调用 ≠ 接受"，这是接受的那一刻。
        setattr(event, "_gateway_accepted", True)
        ...
        if overwritten is not None and self._on_overwrite is not None:
            self._on_overwrite(key, overwritten)
```

★ 这里有一处**第 7 章差距被激活**的故事：第 7 章的清单里，`_gateway_accepted` 被判成
**B 批（不抄）**，理由是"mini 没有内部队列，回执没有消费者"。本章做了两层队列 →
**理由消失了** → 该补，就补了。这就是"差距清单"的正确用法：**它不是欠债单，是条件单**
（当初不抄的**前提**变了，条目就活过来）。

用户可见的覆盖通知（`BusyMixin._notify_overwrite`）：

```text
⚠️ 你刚才那条还没开始跑，就被后来的消息顶掉了：
「第二句：这条应该被顶掉」
两件都要做的话，用 `/queue <文本>` 排队（它不会被顶掉）。
```

★ 还有一件事：第 7 章把"已排队"那句回复当**死代码**删掉了（当时队列是批量 FIFO，
每条都会被跑到，回话反而多余）。现在它**不是**死代码了 —— **覆盖是有损的**，
必须说出口，并且给出正确的替代动作。

`/queue` 命令也升级了（`run_inbound._cmd_queue`）：带文本 = 显式入队（不合并），
不带 = 看队列（**两层分开显示**）：

```text
排队中 2 件：
  最新（会在忙完后跑）：第三句：这条才是最新的
  #1（已承诺，会依次跑）：顺便告诉我你的模型名
（「最新」= 单槽，会被后来的普通消息顶掉；「已承诺」= /queue 排的，按序都会跑）
```

**④ mini 为什么没做成这样 / 值不值得改**

全抄。唯一"改形"的地方：真实版把队列挂在**适配器的 `_pending_messages`**（+ 溢出在
runner 侧）；mini 把两层都收进一个 `SessionQueues` 对象（挂适配器上），
runner 通过 `_queue_depth` / `_promote_queued_event` 读它 —— **方法名与语义对齐**，
但避免了"槽在适配器、溢出在 runner"这种跨对象的两半状态。理由：mini 的线程模型里，
两半状态要两把锁；一个对象一把锁就够。

---

## §七 agent 缓存：三条"不许淘汰"

**① Hermes 怎么做的**

> ### 先说清「热着」是什么意思（这一段的初版写得不准，见下面的更正记录）
>
> **agent 对象 = 一块内存**。`agent = AIAgent(...)` 就是向系统要了一块内存，里面装着三十几个变量。
>
> | 说法 | 准确的意思 |
> |---|---|
> | 「挂着东西」 | 这块内存里**有变量**：拼好的系统提示词、todo 清单、用量计数、压缩器、中断旗子…… |
> | 「还热着」 | 这块内存**还在**（对象没被丢），所以下一轮**可以直接用**，不用重算 |
> | 「每轮新建」 | 每轮把这块内存**扔掉、再造一块**：里面的东西全部重算 |
>
> 实测（`demos/why_cache_the_agent.py`，真代码不联网）：**同一个 agent** 拼系统提示词两次 →
> 第 1 次读 3 个文件（`SOUL.md` / `MEMORY.md` / `AGENTS.md`），**第 2 次读 0 个**、直接返回上次那份
> 字符串（`p1 is p2` 为真）；**每轮新建**两个 agent 各拼一次 → **每次都要读那 3 个文件、重拼那 845 字符**。
>
> ★ **更正记录**（初版写的是"历史 + 提示词缓存的命中状态"，两处不准）：
> ① mini 的 agent **不握历史**（历史每轮从库里读），"历史"是**真实版**缓存 agent 时才在身上的
> （`_session_messages`，见 `gateway/run_turn_runner.py:1521`）；
> ② **"提示词缓存"住不住在 agent 上，要分两个东西**：
>    - agent 上挂的是**拼好的那份提示词字符串**（mini：`_cached_system_prompt`）→ 这是"热"的；
>    - **服务商那边的前缀缓存**（prompt cache）**不挂在 agent 上** —— agent 只决定"你这一轮发出去的
>      字节是不是和上一轮一样"。字节一样 → 命中；第一个不同的字节往后全部作废（第 3 章）。

所以网关**缓存** agent，而不是每轮新建：

```text
_AGENT_CACHE_MAX_SIZE = 128     缓存多少个会话的 agent
TTL                            多久没用就放掉
保护最近 N 个（LRU 保护带）      刚用过的很可能马上还要用
memory_high_mb                  进程内存过线 → 按 LRU 放到线下
```

**三条"不许淘汰"**（真实版逐字：`没落盘的不许淘汰`）：

| # | 谁 | 为什么 |
|---|---|---|
| ① | 正在跑那一轮的 | 淘汰它 = 把用户的回合抽走（`_running_agent_ids()` 快照跳过） |
| ② | 还没落盘的 | `transcript_persistence_caught_up` —— 丢了就是丢了这一轮对话 |
| ③ | 最近 N 个 | LRU 保护带 |

**② 读 Hermes 的代码**

`run_agent_cache.py:782`（配置语义里那条**最容易读错**的注释）：

```text
                # has memory_high_mb=None (pressure pass OFF) but an *absent* section means "auto" — a
```

`run_agent_cache.py:820-823`（压力通道的开关与门槛）：

```python
        if not bounds.memory_high_mb or not _cache or _lock is None:
        ...
        if rss_mb is None or rss_mb < bounds.memory_high_mb:
```

`:837` / `:848` / `:861`（三条保护与"放了多少"的日志）：

```python
                protect_recent=bounds.protect_recent,
        ...
                rss_mb, bounds.memory_high_mb, len(ordered), _mid_turn, _unflushed,
        ...
            rss_mb, bounds.memory_high_mb, evicted_count, ", ".join(key for key, _ in plan),
```

★ `_mid_turn` / `_unflushed` 这两个名字就是三条保护里的两条 —— 它们在**日志里**，
说明维护者认为"这次为什么没放它"值得事后查。

`:46`（容量上限）与容量淘汰的那条边界注释：

```python
_AGENT_CACHE_MAX_SIZE = 128
```
```text
the cache may stay over cap until the next insert       # 没人可放时宁可暂时超上限
```

**③ mini 的实现**

`gateway/agent_cache.py`：`CacheEntry(busy=…, pending_flush=…)` + 三条保护：

```python
    def _is_protected(self, entry: CacheEntry) -> bool:
        """三条"不许淘汰"。★ 注意：保护带只在**还有别人可放**时才算数 ——
        全被保护时宁可不放（`sweep_*` 因此可能一个都放不掉），也不破坏这三条。"""
        if entry.busy or entry.pending_flush:
            return True
        recent = self._lru_order()[-self.protect_recent:] if self.protect_recent > 0 else []
        return entry in recent
```

**+ 第 ④ 条：会话轮转时必须淘汰**（这不是"不许"，而是"必须"）：

```python
        ★ 还有第 ④ 条不是"不许"而是**必须**：`session_id` 变了（`/new`、挂起后的自动重置）
        → 缓存里那个 agent 挂的是**旧会话**，**必须立刻淘汰**，否则它会把新会话的消息
        写进旧会话的转写本。
```

两个淘汰点（缺一个都不行）：

```python
# ① 自动重置那条路（挂起/已结束）—— run_turn._open_session
            self._agent_cache.evict(event.session_key, reason="session_rotated")
```
```python
# ② 显式 /new 那条路 —— run_inbound._cmd_new
            self._agent_cache.evict(key, reason="session_rotated")
```

★ 而且**正确性淘汰不受三条保护拦截**（`evict` 里 `forced = reason in ("force", "session_rotated")`）。
这是实现期抓到的第 6 个真 bug：原来 `evict()` 无脑过一遍 `_is_protected()`，
于是"会话轮转了"这条**正确性要求**会被"最近 N 个"这条**资源规则**驳回 ——
缓存的 agent 继续用旧会话 id，往旧转写本里写新消息。

"没落盘"的保护在轮次两端：

```python
            self._agent_cache.mark_busy(key, True)           # 跑着的：不许淘汰
            self._agent_cache.mark_pending_flush(key, True)  # 这一轮还没跑完：不许淘汰
```
```python
            # ★ "没落盘的不许淘汰"（真实版 `transcript_persistence_caught_up`）：
            #   **跑完**才解除保护；被打断的那一轮保留标记 —— 宁可多留一个 agent，
            #   也不丢一轮对话（TTL 清扫会跳过它；下一次成功跑完就清）。
            if not result.get("interrupted"):
                self._agent_cache.mark_pending_flush(key, False)
            self._agent_cache.mark_busy(key, False)
```

**④ mini 为什么没做成这样 / 值不值得改**

- **抄的是生命周期，不是收益**（诚实项）：真实版缓存的是"历史 + 提示词缓存都还活着"的
  agent（那里有真金白银：prompt cache 命中率）。mini 的 `AIAgent` **每轮按
  `session_db` + `session_id` 自己读历史**，agent 不持有跨轮消息列表，但仍保留提示词字符串、todo、压缩器和预算等状态；缓存也省去重复装配提示词等工作。**收益小，但结构对齐**：它让"淘汰/保护/压力"这套判据
  第一次有**最小可跑的舞台**（否则本章有一半内容无处落地）。
- **`memory_high_mb` 默认值没抄**（差异表 #19）：真实版配置默认是 **`"auto"`**
  （`config_defaults.py:75`，自己探测），mini 用固定 `2048.0`。**值得改，成本低**
  （读 cgroup / 系统内存按比例算），但 mini 的部署形态是单机 CLI，固定值够用，本轮不改。
- **缓存改变了对象身上所有状态的所有权**（★ 本章第 9 个真 bug 就是这么来的）：`self.usage`
  原本只活一轮（轮内累加器 + 网关写库是**加**），缓存之后活多轮 → 不清零就会跨轮累加。
  **换生命周期 = 换所有权**，这条在实现缓存时最容易漏。
- **同一个 agent 只能属于一个 `session_id`**：这是缓存真正的危险面。mini 用
  "轮转 → 立刻淘汰"（两个调用点）+ `get_or_create` 里的 `session_id` 不一致兜底
  （三层防护）。真实版同思路（`_evict_cached_agent`）。

---

## §八 内存压力与后台巡检：只放资源，不结束对话

**① Hermes 怎么做的**

```text
_session_housekeeping_watcher(interval=300)          每 300 秒醒一次
  └── _session_housekeeping()
        ① _sweep_idle_cached_agents()               太久没用的缓存 agent → 放掉
        ② _sweep_agent_cache_under_pressure()        内存过线 → 按 LRU 放到线下
        ③ prune_old_entries(max_age)                 每小时一次；且 max_age > 0 才做
```

★ 三件事的共同点：**它们都在丢东西**（缓存、内存、老记录），
而丢的东西必须是**可从库里重建的**。

**② 读 Hermes 的代码**

`gateway/run_watchers.py:40-71`（**逐字**，含那条 #80764 注释）：

```python
    async def _session_housekeeping_watcher(self, interval: int = 300):
```
```python
    async def _session_housekeeping(self) -> None:
```
```python
            if evicted := self._sweep_idle_cached_agents():
```
```text
            # (#80764). Shed LRU transcripts once the heap is over budget; they reload from the persisted
```
```python
            self._sweep_agent_cache_under_pressure()
```
```python
                if max_age > 0 and (n := await self.async_session_store.prune_old_entries(max_age)):
```

★ `max_age > 0` 就是**开关语义**：默认 0 = **不剪**（运维没说要留多久，就别自动删）。

**③ mini 的实现**

`gateway/run_watchers.py::_session_housekeeping_tick()` —— 一个 tick 返回**收据**：

```python
        receipt: Dict[str, Any] = {
            "idle_evicted": self._agent_cache.sweep_idle(now=now),
            "pressure_evicted": [],
            "rss_mb": None,
            "pruned": None,
        }
        ...
        if rss is not None and rss > self._agent_cache.memory_high_mb:
            # ★ 内存过线：**先报告再动手**（真实版也是先记日志再淘汰 —— 事后才知道放了多少）
            receipt["pressure_evicted"] = self._agent_cache.sweep_under_pressure(rss_mb=rss)
```

线程是"先睡 60 秒再开工"（照真实版）：

```python
            # ★ 先等一段时间再开工（真实版同）：刚起来的网关正在做启动恢复、连适配器，
            #   这时候跑巡检只会跟它抢缓存锁。
```

第三件事（剪枝）**诚实记债**：

```python
        # ★ 诚实记债：mini 存储层没有删除路径（第 6 章没做）。
        note = (f"未执行：mini 存储层没有删除路径（阈值 {days} 天）"
                f" —— 见第 8 章差距清单「老记录剪枝」")
        self._turn_errors.append(f"housekeeping/prune: {note}")
```

★ 为什么用"记债"而不是"静默跳过"：静默跳过会让"开关打开了但什么都没发生"
看起来像**正常工作**。记债之后，`/status`/日志/测试都能看出"这条路还没铺完"。

**④ mini 为什么没做成这样 / 值不值得改**

- **剪枝没实现**（差异表 #21）：mini 的存储层（第 6 章）**没有删除路径** ——
  没有 `prune_old_entries`、没有 `DELETE FROM sessions/messages`、没处理 FTS 索引与血缘。
  在网关层糊一个 `DELETE` 是**危险的**（会留下 FTS 孤儿行、断掉 `parent_session_id` 链）。
  **值得做，成本中等**：要在存储层做，包含 ① 选老会话 ② 删消息（含 FTS）③ 处理血缘
  ④ 加一次 VACUUM 策略。这是第 6 章的后续，不是第 8 章的活。
- **"每个条目估算大小"没抄**（差异表 #20）：真实版按估算表算"要放多少才回到线下"，
  mini 固定放"约 1/4"，并在返回值里写明这是估算。**不值得改**（估准了也没意义：
  真正该关心的是"压力过去之后 agent 会重建"）。

---

## §九 进程边界：关掉会话 vs 关掉进程

**① Hermes 怎么做的**

**进程退出不是对话边界。** 关闭/重启时真实版只做两件事：

```text
① 对**正在跑**的会话：mark_resume_pending(reason="shutdown_timeout" / "restart_timeout")
② 写 .clean_shutdown（**只在 drain 没超时**时）
```

其余会话**什么都不做** —— 保持 open，下次启动按普通复用接着用。

**② 读 Hermes 的代码**

`gateway/run_shutdown.py:1654`、`:1704`：

```python
            self, "pre-drain mark_resume_pending"
```
```python
        await GatewayRunner._mark_running_sessions_resume_pending(self, "mark_resume_pending")
```

★ 函数名里 `_mark_running_sessions_` 就是范围声明：**只有正在跑的**。

**③ mini 的实现**

第 7 章的 mini 写的是：

```python
        for key, sid in list(self._session_ids.items()):
            self._finalize_session(sid, reason)      # ← 把**所有**会话都结束了
```

这条让"重启之后对话还在"直接失效：每次重启 = 给所有用户开新会话。
第 8 章改成（`gateway/run_shutdown.py`）：

```python
        # ★ 只标记"被打断的那一轮"（正在跑的会话）→ 软恢复
        for key in list(self._running_agents):
            sid = self._session_ids.get(key)
            if sid:
                self._router().mark_resume_pending(sid, "shutdown_timeout")
```

顺带删掉了另一处**相反**的做法（§二 说过）：原来给每个会话 `touch()`，
等于把"进程活动"记成"用户活动"。现在注释写明白了：

```python
        # ★ **不 touch**（第 8 章更正）：`updated_at` 表达的是"**用户活动**"，
        #   不是"进程活动"。原来这里给每个会话 touch 一下，后果是：
        #   任何一次优雅关闭之后，网关知道的**所有**会话都变成"刚刚活跃"——
        #   于是崩溃恢复的"最近 120 秒"窗口形同虚设（下次脏启动会把它们全标成待恢复，
        #   哪怕用户上一次说话是几小时前）。
```

★ 顺带说清"结束会话"这个动词该谁用（`_finalize_session` 的 docstring）：

| 情况 | 会不会终结会话 |
|---|---|
| 显式对话边界（`/new`） | ✅ 会 |
| 空闲时间、每日边界、内存压力 | ❌ **不会** |
| 进程退出 / 重启 | ❌ **不会**（只打"待恢复"标记） |

**④ mini 为什么没做成这样 / 值不值得改**

这条不是"抄不来"，是**第 7 章抄错了**（当时把"进程收尾"顺手写成了"结束所有会话"）。
第 8 章修掉之后多出两条断言守着：

- `★ 关闭**不结束**会话（进程退出不是对话边界）`（恢复那条用例）
- `（前提）关闭**没有**把 updated_at 刷新成"刚刚活跃"`（时间不轮转那条用例）

**`.clean_shutdown` 的门槛**（差异表 #22）mini 只抄了"退出码 0 就写"这一层；
真实版还要判"drain 有没有超时"。**值得改，成本低**（mini 的 drain 是固定等待，
加一个"是否在超时前完成"的布尔即可），但 mini 没有"drain 超时"这条独立路径，
本轮不改，代价写在这里。

---

## §十 与上下文压缩的接缝：轮转 ≠ 结束对话

**① Hermes 怎么做的**

第 5 章的会话维护（session hygiene）压完历史会做三件事，本章关心**第三件**：

```text
① 改存储（压缩后的转写本落盘）
② 只截载荷（这一轮发给模型的载荷变小，存储不变）
③ **淘汰缓存 agent**（self._evict_cached_agent(session_key)）
```

第 6 章的存储层再往前一步：如果压缩产生的是**新会话行**（轮转），
新会话行带 `parent_session_id` 指回旧会话；`active` 是消息行字段，不能把“旧会话结束”写成会话行 `active=0`。

**② 读 Hermes 的代码**

真实版 `gateway/run_turn.py`（会话维护之后那一句）：

```python
            self._evict_cached_agent(session_key)
```

★ 为什么压缩要淘汰 agent：agent 手里那本"内存里的历史"刚被压过 ——
留着它，下一轮可能拿**旧历史**再发一遍（等于压缩白做，还多花一次钱）。

**③ mini 的实现**

**mini 没有这条调用，而且这不是偷懒** —— 差异在**压缩住在哪一层**：

| | 真实 Hermes | mini |
|---|---|---|
| 压缩住在哪 | 网关侧（session hygiene）+ agent 侧都有 | **只在 agent 侧**：`agent/context_compressor.py`，由 `run_agent._preflight_compression()` 触发 |
| agent 手里的历史 | **握在内存里**（缓存的 agent 就是"历史 + 提示词缓存都活着"） | **每轮从库里读**（`session_db` + `session_id`） |
| 压缩改了库之后 | 内存那份**过期**了 → 必须淘汰 agent，否则下一轮拿旧历史再发一遍 | 下一轮读到的就是**压缩后的**历史 → **不需要淘汰** |

所以 §七 那个缓存与压缩之间真正的耦合点是：

```text
agent 是否持有跨轮的完整消息列表？
  持有 → 压缩/轮转之后**必须**淘汰它（真实版：_evict_cached_agent）
  不持有 → 不需要（mini 当前状态）
```

mini 的缓存 agent 不持有完整消息列表：`get_or_create(key, sid, factory)` 只保证
"同一个会话用同一个 agent 对象"，而历史每轮现读。**这条接缝 mini 是空的** ——
不是漏了，是**没有那个风险**。

**④ mini 为什么没做成这样 / 值不值得改**

**不做，但要把风险写在最显眼的地方**：

- 什么时候这条接缝会**突然变得必需**：给 mini 的 agent 加**跨轮内存态**
  （例如把历史缓存在 agent 上以避免每轮读库 —— 那正是"让缓存收益变大"的同一步），
  或者给网关补**真正的 hygiene 压缩段**。**这两件事任一发生**，就必须同时补三处：
  ① 压缩后淘汰 agent ② 轮转后淘汰 agent（本章已有）③ 淘汰原因写进 `evictions` 便于事后查。
- 为什么现在不做：mini 的 agent **每轮现读历史**，所以"压缩 → 缓存过期"这个 bug
  在 mini 里**正常适配器入口会被守卫挡住；绕过入口或跨进程调用仍可能发生** —— 加一句 `evict(reason="compaction")` 只会变成
  **永不触发的死代码**（没有 hygiene 段可挂）。
- **代价写明**：将来谁把历史搬进 agent 内存，谁就得回来补这一句；
  而漏掉它的症状**不报错**，只在下一轮表现为"历史回退到压缩前"（多花钱 + 上下文超长）。

---

## §十一 实现期抓到的真问题（8 个）

**这一节是本章最值钱的部分**：下面每一条都是"跑出来的"，不是"想出来的"。
按发现方式分组。

### A. 第 7 章留下的两处语义错（本章路上才暴露）

| # | 症状 | 根因 | 修法 |
|---|---|---|---|
| 1 | "重启之后对话还在"根本不成立：每次重启都给所有用户开新会话 | `shutdown()` 对**所有**已知会话 `end_session("gateway_stop")` —— 把"进程收尾"写成了"结束对话" | 只对**正在跑**的会话 `mark_resume_pending("shutdown_timeout")`；其余保持 open（§九） |
| 2 | 恢复扫描的"最近 120 秒"窗口形同虚设：一次优雅关闭后，**所有**会话都成了"刚刚活跃" | `shutdown()` 给每个会话 `touch()` —— 把"进程活动"记成了"用户活动" | 关闭路径**不 touch**；并加断言守着（§二） |

★ 这两条为什么第 7 章没发现：第 7 章的收据只验了"退出码 0 / 历史从库重建 / 用量回填"，
**没有验"下一个进程拿到的是同一个会话"** —— 那是本章才有的判据。
**教训**：收据要覆盖**跨进程**的那条性质，否则"进程边界"上的错永远不会红。

### B. 本章新写的代码里的 4 个真 bug（都是静默的）

| # | 症状 | 根因 | 修法 | 谁抓到的 |
|---|---|---|---|---|
| 3 | 已结束的会话也会被标成"待恢复" | `recently_active_session_ids()` 只按 `updated_at` 筛，**没排除 `ended_at`**（真实版靠"遍历内存索引"天然排除，mini 是查库所以必须显式写） | SQL 加 `AND ended_at IS NULL` | 测试组 15 |
| 4 | 卡死升级**永远不触发** | 升级被写成 `if keys:` 才检查 —— 而“会话已是待恢复”会让 `marked` 为空 → 升级路径被自己的前置条件挡死 | **计数文件是权威**：不管本次标了谁，都读文件再判 | 测试组 15 |
| 5 | `/new` 之后缓存 agent 不淘汰（会往旧会话写新消息） | 淘汰点只加在 `_open_session`（自动重置那条路），漏了 `/new` 这条命令路 | 两个调用点都加（§七） | 测试组 17 |
| 6 | 轮转淘汰被驳回 → 同上 | `evict()` 无脑过 `_is_protected()`，于是**正确性要求**被**资源规则**拦住 | `forced = reason in ("force", "session_rotated")` | 测试组 17 |

### C. 反向验证抓出的"测试自己写虚了"（2 处）

| # | 症状 | 问题 | 修法 |
|---|---|---|---|
| 7 | "三条不许淘汰"这几条断言全在**空集**上通过 | `/new` 之后那个键**没有缓存条目**，断言对象是 `None` | 先跑一轮把缓存填上，并加一条"（前提）缓存里有"的前置断言 |
| 8 | "容量上限会淘汰"这条断言太松（`len <= 2`） | `max_size=1` 只插了 2 条，**完全不淘汰**也能通过 | 收紧成 `len == 1`；反向验证里补一条"去掉 `_make_room`"的改法 |

★ 另外还有一处"**改法自己不行**"：给 `route()` 加的"空闲轮转"改法写成
`if (entry.updated_at or 0) and …`，而测试把 `updated_at` 倒拨成 `0` —— **哨兵值让改法失效**，
于是"没红"是假象。改成 `if time.time() - (entry.updated_at or 0) > 86400` 之后立刻干净变红。
**教训**：反向验证里"没红"要先怀疑**改法**，再怀疑测试。

### D. 本章改动的**副作用**抓到的一个 bug（缓存改变了「所有权」）

| # | 症状 | 根因 | 修法 |
|---|---|---|---|
| 9 | 网关跑第二轮之后，会话行的用量**静默翻倍** | 缓存 agent 把 `self.usage` 的所有权从「一轮」变成了「多轮」，而它**每轮不清零**；网关写库又是 `record_usage()`（SQL `input_tokens = input_tokens + ?`，**加**）→ 轮 2 把轮 1 的用量再算一遍 | `run_conversation` 开头清零（对齐真实版 `agent/conversation_loop.py:1504` 的 `_last_turn_usage = None`） |

★ 这是本章最值得记的一条**因果**：**换一个对象的生命周期，等于换掉它身上所有状态的所有权** ——
`self.usage`（轮内累加器）原本只活一轮，缓存之后活多轮；同一个对象上的 `TodoStore` 同理。

发现方式：用**真 AIAgent + canned HTTP**（`tests/test_loop_mechanisms.py` 的装置）连跑两轮，
看 `result['usage']` —— 轮 2 报出 200（真实请求只有 100）。探针 `/tmp/probe_usage_ownership.py`。

### E. 顺带清掉的一处死代码

第 7 章的 `_run_turn_for_event` 里有一行 `history = db.get_messages_as_conversation(...)`，
**从来没有人用它**（agent 自己按 `session_db` + `session_id` 读历史）—— 每轮白算一次。
删掉。（它同时是第 7 章"文档说历史从库里重建"的**误读来源**：重建历史是真的，
但不是网关那行代码干的。）

---

## §十二 测试与收据

### 数字（全部实测，非估算）

| 项 | 数 |
|---|---|
| `tests/test_gateway.py` | **1,327 行 / 17 组 / 214 条断言** |
| 全套件（7 个测试文件） | **792 通过 / 0 失败**（`/usr/local/bin/python3` 3.13.14 与 `/usr/bin/python3` 3.9.6 **一致**） |
| 本章新增断言 | 第 8 章组 15/16/17 共 **+103 条**（111 → 214） |
| 反向验证 | **14/14 干净变红**（`/tmp/rev_ch08.py`；每个改法只改一处，跑完整套件） |
| 全项目代码 | 11,919 行（`gateway/` 包 19 文件 / 2,873 行；`run_agent.py` +11 行 = 第 9 个 bug 的修法） |
| 测试代码 | 5,984 行 / 171 个测试函数 / 794 处 `check(` |

### 三种验证各验到了什么

| 验证 | 覆盖不了什么 | 本章用它验到的 |
|---|---|---|
| **单元/集成断言（792 条）** | 真模型行为、真时间 | 优先级、时间不轮转、两条队列、缓存保护、卡死阈值 |
| **反向验证（14 个改法）** | 改法本身的合理性 | "把机制改回错的 → 对应断言必须红"（否则断言是装饰） |
| **真跑（真 DeepSeek）** | 规模、并发 | 缓存命中、用量回填、覆盖通知、跨重启连续性、巡检线程 |

### 真跑收据（真 DeepSeek，临时 home，**不碰用户真实 home**）

```text
══ ① 两个回合：第一轮建 agent，第二轮必须命中缓存
   → 会话 20261009_144502_1424ff｜1.2s｜用量回填 input=1652 output=34
   缓存：{'size': 1, 'hits': 0, 'misses': 1, ...}
   → 1.4s｜用量回填 input=4959 output=182｜会话没换：True
   缓存：{'size': 1, 'hits': 1, 'misses': 1, ...}
   「缓存未命中」记录条数（应只有 1 条）：1

══ ② 两层队列
   忙时第一条普通消息 → queued｜第二条 → queued
   队列深度（普通消息只占单槽，应为 1）：1
   槽里是：'第三句：这条才是最新的'
   用户收到了覆盖通知：True
   显式排队后深度（单槽 1 + 溢出 1 = 2）：2
   跑完深度归零：True

══ ③ 巡检线程
   跑了 4 次 tick；最近一次收据：{'idle_evicted': [], 'pressure_evicted': [],
     'pruned': '跳过：session_store_max_age_days=0（默认关闭，真实版同）', 'rss_mb': 29.4}
   停掉之后不再 tick：True
   巡检有没有动过对话（会话还开着）：True

══ ④ 干净关闭 → 重启
   .clean_shutdown 写了吗：True
   关闭会不会结束会话（库里 ended_at）：None
   重启后标记被消费：True
   ★ 重启后还是同一个会话：True（20261009_144502_1424ff，**同一个 id**）
   会话行累计用量：input=21498 output=877
   转写本里攒了 11 条消息（**跨重启连续**）
```

★ 真跑里还**意外**看到一条印证 §七 的现象：被打断的那一轮（"从一数到二十"）
在下一轮里**不在上下文里**（agent 回答"我这边没有前面两句的上下文"）——
因为那一轮被中断、转写本是半截的。这是当时观察到的异常回答，不能仅凭模型回答证明数据库缺行。mini 不缓存消息列表，保留 agent 的 `pending_flush` 标记也无法恢复未写入数据库的消息；应检查消息行与中断补齐路径，当前修复见发布前维护。

### 反向验证 14 个改法（名字即断言）

```text
1  时间轮转：给 route() 加一条空闲超时重置              → 30 天不活动 + 重启 → 仍复用同一会话
2  硬软优先级反了：让 resume_pending 先于 suspended 判  → 硬(挂起) 赢过 软(待恢复)
3  读到就清：route() 里顺手清掉 resume_pending          → 读到**不清**标记
4  进程退出又去结束会话（第 7 章的老行为）              → 关闭**不结束**会话
5  恢复扫描不排除已结束的会话                           → 已结束的会话不在恢复名单里
6  卡死升级又被"本次有没有新标记"挡住                   → 启动恢复：计数越线 → 挂起
7  单槽变 FIFO（普通消息也排队不覆盖）                  → 深度 = 1（普通消息只占单槽）
8  晋升顺序反了：先单槽后溢出                           → 按序跑完
9  覆盖不通知用户（静默丢掉他写的字）                   → 被顶掉的第二条收到通知
10 排队不给回执（调用 ≠ 接受）                          → 排队事件带上了回执
11 会话轮转不淘汰缓存（agent 会写错转写本）             → `/new` 之后该键被淘汰了
12 轮转淘汰也被三条保护拦住（正确性被资源规则压住）     → `/new` 之后该键被淘汰了
13 剪枝节流初值 0.0（= 开机时刻，第一次剪枝被自己挡掉） → 开关打开 → 尝试一次并诚实记债
14 缓存容量上限不淘汰（无界增长）                       → 超过上限 → 按 LRU 放掉最旧的
```

---

## §十三 差距清单（可改 / 不做，带成本）

### 值得做（历史清单，成本为当时估计）

| # | 差距 | 成本 | 前置 |
|---|---|---|---|
| 1 | 恢复第 ④ 步"**主动**续跑"（合成消息走 `_handle_message`） | **高** | 要先给 `AIAgent` 加"没有用户消息也能起轮"的入口（第 2 章） |
| 2 | **stall 看门狗**（默认 300s，只看 `get_activity_summary()`） | 中 | 要先给 `AIAgent` 加 activity summary（第 2 章） |
| 3 | **老记录剪枝**（`prune_old_entries(max_age)`） | 中 | 要在存储层做（第 6 章）：删消息 + FTS + 血缘 + VACUUM 策略 |
| 4 | `memory_high_mb` 的 **`"auto"`** 语义（按系统内存/容器限额自算） | 低 | 无 |
| 5 | `.clean_shutdown` 的 **drain 超时门槛**（超时不写标记） | 低 | 发布前修复已实现，见顶部维护说明 |
| 6 | 回执 `_gateway_accepted` 的**消费端**（交付层据此不重复回执） | 低 | mini 的 delivery 目前只按 target 路由，没有"回执去重"这条逻辑 |

### 不做（写明代价）

| # | 差距 | 为什么不抄 | 代价 |
|---|---|---|---|
| 7 | 单飞去重 + 锁外 IO 三段式 | mini 的守卫已保证同会话串行，构造不出这个场景 | 高并发场景下会缺少这层保证 |
| 8 | `sessions.json` 镜像 | 只有一个读者，库就是索引 | 加第二个只读消费者时要补镜像 |
| 9 | `was_auto_reset` / `auto_reset_reason` / `reset_had_activity` | 没有"重注入话题/技能""附件重发"这些消费者 | 补这些功能时要先补字段与消费点 |
| 10 | 内存压力按"每个条目估算大小" | 估准了也没有决策价值 | 压力下可能放多/放少一点 |
| 11 | PII 脱敏（`user_<12hex>`） | mini 没有"会话上下文注入"段 | 接入多人共享会话时必须补 |
| 12 | `SessionSource` 的 22 个字段 / 平台怪癖 | 只有一个 console 适配器 | 接第二个平台时要扩字段与键规则 |
| 13 | 跨进程压缩锁 / 多 profile 多库绑定 | 多 profile 文件层隔离已在网关族补齐；数据库跨进程压缩租约仍未实现 | 多进程部署时再补 |
| 14 | **压缩/轮转后淘汰缓存 agent 的联动** | mini 的压缩在 agent 侧、agent 不持历史 → 现在**构造不出**那个 bug（加了就是死代码） | 谁把历史搬进 agent 内存，谁就得回来补（漏了不报错，表现为"历史回退"） |

---

## 附录 A · 术语表（本章 25 条）

| 术语 | 含义（本章用法） |
|---|---|
| **会话键**（session_key） | `agent:{命名空间}:{平台}:{聊天类型}:{聊天 id}`；状态机的**唯一键** |
| **命名空间** | 键里的 profile 段；默认 `main`，字面叫 `main` 的 profile 用 `main~` |
| **`SessionSource`** | 入站消息的归一化来源（真实版 22 字段 / mini 6 字段） |
| **`SessionEntry`** | 一个会话键的注册记录（真实版在内存 + 镜像；mini 在 `sessions` 表） |
| **路由决策**（route） | "这条消息用哪个会话"的唯一决策点；五个出口 |
| **硬擦除**（`suspended`） | 下次访问**换新会话**；**永远赢**；不能被软标记撤回 |
| **软擦除**（`resume_pending`） | **不改归属**，只留"待恢复"标记；跑到这一轮结束才清 |
| **`resume_reason`** | 为什么待恢复：`restart_interrupted` / `shutdown_timeout` / `restart_timeout` |
| **`is_fresh_reset`** | "这是重置后的第一条消息"；**一次性**，跑完就消费 |
| **显式边界** | 用户要求的轮转（`/new` `/reset`）；唯一"无论如何都换"的意图 |
| **自动重置**（auto reset） | 系统要求的轮转（挂起后新建）；也会置 `is_fresh_reset` |
| **时间永不轮转** | 没有"空闲/每日边界换会话"这条路径 —— 本章第一不变量 |
| **`.clean_shutdown`** | 优雅退出的标记；**有它才跳过恢复**；drain 超时**不写** |
| **`.restart_failure_counts`** | `{会话键: 连续重启次数}`（**不是** `restart_counts.json`） |
| **卡死循环** | 反复崩→恢复→崩的会话；3 次后挂起 + **清空计数** |
| **单槽**（slot） | 忙时普通消息的去处；**单值**，后到**覆盖**先到 |
| **溢出 FIFO** | `/queue` 显式排队；**不合并**，按序跑完 |
| **晋升**（promote） | 忙完取下一条：**先溢出、后单槽** |
| **回执**（`_gateway_accepted`） | 网关**接受**（不是"调用成功"）那一刻的标记 |
| **agent 缓存** | 按键缓存 agent（真实版真金白银：prompt cache 命中率） |
| **LRU 保护带** | 最近 N 个不许淘汰（默认 8） |
| **没落盘的不许淘汰** | `transcript_persistence_caught_up` —— 三次保护里最要紧的那条 |
| **巡检 tick** | 每 300 秒的会话维护：空闲清扫 / 压力清扫 / 每小时剪枝 |
| **内存压力清扫** | RSS 过 `memory_high_mb` → 按 LRU 放到线下（跳过受保护项） |
| **只放资源不结束对话** | 三种清扫的共同不变量：**不写 `ended_at`** |

## 附录 B · 章节位置与三笔债

```text
官方 developer-guide 侧边栏（本章的位置）
 5  agent-loop              → 第 2 章
 6  prompt-assembly         → 第 3 章
 7  context-compression     → 第 5 章
 8  micro-compaction        （跳过，406 行支线）
 9  gateway-internals       → 第 7 章
10  gateway-session-lifecycle → **第 8 章（本章）**
11  multiplexing-gateway    （第 7 章侧线做了一半：入口层）
12  completion-backlog-delivery （投递债）
13  session-storage         → 第 6 章
14  state-db-recovery       （与第 6 章配对）
15  provider-runtime
36  tools-runtime           → 第 4 章
```

**本章收掉的债**：第 7 章差距 B5（`_gateway_accepted` 回执，"没有队列 → 不抄"）
在本章**条件成立** → 补上了；第 7 章"每轮新建 agent"的差距 → 缓存补上。

**本章新挂的债**（见 §十三）：主动续跑、stall 看门狗、老记录剪枝。

**还没动的债**：跨进程压缩锁（第 5/6 章）、多 profile 多库绑定（第 7 章侧线）、
`/resume` IDOR 范围（第 6 章）。

## 附录 C · 上游出入（本章 5 处）

| # | 出入 | 证据 |
|---|---|---|
| ① | **助手误判**（不是文档错）：一度以为 `sessions.json` 已废弃（被 `channel_directory.py:320` 的 "state.db first, sessions.json fallback" 带偏）→ 核到底发现存储层**仍在写它**（`write_sessions_json` 默认开，源码称它是 **mirror**，SQLite 才权威） | `gateway/session_persistence.py` 的写入路径 |
| ② | 会话 id **不是**文档隐含的 `8hex` —— `DEFAULT_HEX_LEN = 6` | `hermes_state_ids.py:21` |
| ③ | 卡死循环计数文件的真名是 **`.restart_failure_counts`**，不是文档写的 `restart_counts.json` | `gateway/run.py:3969` |
| ④ | 文档说 `suspended` 由 **`/stop`** 设置 —— **实测不成立**：`/stop` 走的是 `_interrupt_and_clear_session`（打断 + 清队列），全仓 grep `suspend_session(` 的产品级调用点**只有 evals**；挂起实际只由卡死升级设置 | `gateway/session_lifecycle.py:107`（定义）+ 全仓调用点 |
| ⑤ | 文档**没写**的三件事：① `get_or_create_session` 是**单飞**的（同 key 并发共享 owner 结果 + 阻塞 IO 全在锁外）；② **stall 看门狗**（`run_watchers.py:77-80`，`HERMES_SESSION_STALL_TIMEOUT` 默认 300，只看 `get_activity_summary()`）；③ 计数**不是只加不减**（挂起后清空 + 会话跑完清自己那条） | `gateway/session.py` / `run_watchers.py:234` / `run.py` 注释 |

★ ①②③ 已记在《中文精译-07b》附录 C；本文补上 ④⑤（都是**实现期读源码**才发现的）。



