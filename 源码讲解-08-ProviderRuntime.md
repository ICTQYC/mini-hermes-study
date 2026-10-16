# 源码讲解 · 第 8 章：Provider 运行时解析（Provider Runtime）

> **一句话**：「用哪个供应商、哪个模型、哪把钥匙、发到哪个地址」这件事，Hermes **共用一份解析逻辑**，多个入口按需调用。
> —— 《中文精译-08-ProviderRuntime》§0

> **原文**：`website/docs/developer-guide/provider-runtime.md`（官方 222 行）
> **源码锚**：`~/.hermes/hermes-agent/`，commit `f5d1926110`（`version = "0.21.3"`）。
> 本文所有 `文件:行号` 都对齐这一个 commit；**没实际读到的行号一律不写**（只写文件路径）。

**口径说明**

- 每节固定四块，顺序不变：**① Hermes 怎么做的**（散文把机制讲透）→ **② 读 Hermes 的代码**（只引一段 ≤15 行，优先引注释）→ **③ mini 的实现（现状）** → **④ 与真实版的差别**。
- **mini 栏口径**：`✅ 已抄（<commit>）` / `已对齐`（早先章节已一致）/ `⬜ 没做（量级）` / `⏭ 不抄（理由）`。
- 两个落点 commit：**A1–A5 + A10 = `756b493`**；**A7–A9 = `a391919`**。
- 行数一律 `wc -l` 口径。

**规模（`wc -l`，锚 commit `f5d1926110` 实测）**

| 侧 | 文件 | 行 / 量 | 角色 |
|---|---|---|---|
| 真实 | `hermes_cli/runtime_provider.py` | **953** | 门面 `resolve_runtime_provider()` + 惰性阶梯 + `api_mode` / host 助手 |
| 真实 | `hermes_cli/runtime_provider_custom.py` · `runtime_provider_backends.py` | **518 / 251** | 具名自定义端点 / OpenRouter·Bedrock·external 等 builder（自 `runtime_provider.py` 拆出，`runtime_provider.py:1-6` 模块 docstring 说明） |
| 真实 | `hermes_cli/auth.py` | **2,231** | 供应商注册表 `PROVIDER_REGISTRY` + `resolve_provider()` |
| 真实 | `hermes_cli/model_switch.py` | **1,679** | 共享 `/model` 切换流水线 `switch_model()` |
| 真实 | `agent/auxiliary_client.py` | **7,800** | 辅助路由 `resolve_provider_client()` |
| 真实 | `providers/` · `plugins/model-providers/` | 3 文件（`README.md`/`base.py`/`__init__.py`）· **39** 个内建供应商目录 | 取用口 + 每家一份声明式插件 |
| mini | `providers/` 包 | **723 行 / 9 文件** | `base` + `__init__` + `api_mode` + `host_gate` + `aliases` + 3 内建画像 + 包标记 |
| mini | `hermes_cli/runtime_provider.py` | **441** | 两段式 + 门面（A1） |
| mini | `agent/fallback.py` · `agent/auxiliary.py` | **269 / 147** | 回退链（A7）· 辅助路由（A8） |
| mini | `hermes_cli/config.py` | **247** | 配置层（A9） |
| mini | `tests/test_provider_runtime.py` · `test_fallback_and_aux.py` | **106 条 / 6 组** · **113 条 / 4 组** | 契约测试（双解释器） |
| mini | 全套件（9 文件） | **1,063 通过 / 0 失败** | 3.13 与 3.9 一致 |

> 表里的真实侧数字是**在锚 commit `f5d1926110` 上 `wc -l` 实测**的；
> 主代理最初在派活规格里给过一组数（1,653 / 3,193 / 2,335 / 11,663），那是**记错的**，以本表为准。
> `runtime_provider.py` 另有 `_custom` / `_backends` 两个兄弟文件（facade + siblings 拆分），合计才接近那组数字。

**节 ↔ 精译对照**

| 本章节 | 《中文精译-08》对应 |
|---|---|
| 导言 · 机制总览 | §0 这一章在回答什么 · §5 为什么要有这一层 |
| §〇 差异表 | 附录 C · 本篇与上游源码的出入 |
| §一 两段式解析（名字层 vs 连接层） | §2 解析优先级 |
| §二 注册表与插件式懒注册 | §3 供应商清单 |
| §三 `api_mode` 三值与按 host 判定 | §4 解析结果长什么样 · §8 Anthropic 原生路径 · §9 OpenAI Codex 路径 |
| §四 凭据的 host 门控 | §7 三把 key 各管自己的域名 · §6 AI Gateway |
| §五 自定义端点的两条路 | §3（两条「自己接」的路）· §7 |
| §六 辅助模型的独立路由 | §10 辅助模型的独立路由 |
| §七 回退链 | §11 回退模型链 |
| §八 配置层与别名归一 | §2 解析优先级（config 压 env） |
| §九 为什么这层必须共享 | §5 为什么要有这一层 |
| §十 真问题与收据 | （精译无对应正节 —— 实现纪事） |
| §十一 差距清单 | 附录 B · 附录 C |
| 附录 A 引用总表 | （精译无对应） |
| 附录 B 三方对照矩阵 | 附录 B · 本篇提到的实现文件 |
| 附录 C 上游出入 | 附录 C · 本篇与上游源码的出入 |
| （本章无对应） | §1 Chat-completions reasoning 形状 · §12 相关文档 |

[TOC]

## 发布前维护 · HTTP 传输与用量口径

`_http_post()` 根据 URL scheme 选择 HTTP 或 HTTPS 连接和默认端口，保留查询参数；连接超时与读取超时分别设置，无效 scheme 提前拒绝。`api_calls` 统计逻辑模型请求，涵盖回退请求和摘要请求；同一逻辑请求内部的传输重试不单独加计，不能用迭代预算消费次数代替它。

这些是当前实现的行为；下文的章节提交、规模表和实测输出保留各自历史时点。新增回归在 `mini-hermes/tests/test_publish_regressions.py`，完整收据见[发布前修复记录](发布前修复记录.md)。



---

## 导言 · 机制总览

这一层要回答的问题只有一件：**「这次用哪个供应商、哪个模型、哪把钥匙、发到哪个地址」**。它不难，但难在**必须五处答案一致**。CLI、TUI、网关、cron、辅助任务这几种入口的触发时机、进程模型、有没有人坐在前面全都不一样，可"用哪个模型"这件事的答案不能各算各的 —— 否则就会出现「CLI 用 A 端点、网关用 B 端点、cron 悄悄回落到 C」那种只能靠翻日志才解释得清的局面。解法是把它抽成**一层共享运行时**：一个门面、一份 dict、五处调用。

下面七小节按「谁 / 何时 / 做什么 / 什么不能破 / 出错怎么办 / 谁调它 / 顺序」把这一层拆开；每条都落到真实源码的 `文件:行号` 与**逐字注释**（锚 `f5d1926110`）。

### 0.1 谁在管（门面与唯一入口）

门面是**一个函数**：`hermes_cli/runtime_provider.py:851` 的 `resolve_runtime_provider()`。它不只是"常用函数"，而是这一层对外的**唯一物化点** —— 所有入口拿到的 `(provider, api_mode, base_url, api_key)` 都从它这里出去，没有任何入口自己拼过这份 dict。它内部串两个更小的东西：请求名选择器 `runtime_provider.py:410` 的 `resolve_requested_provider()`，以及会检查凭据和自动探测的 `auth.py:1323 resolve_provider()`，以及统一产出函数 `hermes_cli/runtime_provider.py:249` 的 `_runtime()`。

逐字引（`runtime_provider.py:1-2`，模块 docstring）：

> Shared runtime provider resolution for CLI, gateway, cron, and helpers: the resolution ORDER
> (:func:`resolve_runtime_provider`), api_mode / base_url helpers and the pool / OAuth / explicit paths.

**要害**：门面被进程模型完全不同的入口共用，所以它必须支持按不同请求和 profile 重入；缓存或凭据池若存在，也必须有明确作用域和刷新规则。这是集中解析的收益；谁在管不是"某一个类"，而是"这一个函数"。

### 0.2 什么时候（解析在什么时机发生）

**不是启动时解析一次就锁死**。最能说明时机的是 CLI 的那句注释：凭证在**每次要用 agent 之前重新解析**，好让 key 轮换 / token 刷新不必重启进程。同理，`/model` 切换要重解析、网关每次收请求要解析、cron 起 job 要解析、子代理构造要解析 —— 每一处都在**将要用**的那一刻解析。

逐字引（`hermes_cli/cli_agent_setup_mixin.py:179-180`，`_ensure_runtime_credentials` docstring）：

> Re-resolve provider credentials before agent use so key rotation / token refresh are picked up without restarting the CLI. False on auth failure.

同样这条时机在 `/model` 侧也有（`hermes_cli/model_switch.py:1087` 逐字：`Fill api_key / base_url / api_mode / validation_headers from "resolve_runtime_provider"`）。

**要害**：解析是**按需、可重复**的，不是一次性初始化 —— 跨调用缓存必须正确分键并刷新，不能把上一请求的端点或凭据无条件复用于下一请求。

### 0.3 做什么（一次解析产出什么）

一次解析产出一份 **dict**，骨架是四栏：`provider` / `api_mode` / `base_url` / `api_key`；再加上"这份结论怎么来的"及其它按路径挂上的元数据（`source`、`requested_provider`，某些路径还有 `expires_at`、`credential_pool`）。它产出的是**连接参数**（怎么发、发哪、用哪把 key），不是"模型对象"。

逐字引（`runtime_provider.py:250`，`_runtime` docstring）：

> Build a resolved-runtime dict; `extra` carries source/requested_provider/provider-specific keys.

（同函数 `:254` 落成实际四栏：`{"provider", "api_mode", "base_url", "api_key", **extra}`。）

**要害**：把"用哪家"从"怎么连"里拆开之后，产出物就成了**纯连接参数** —— 所以它才能被 CLI、网关、cron 这些异构入口消费同一份东西。

### 0.4 不变式（这一层不能破的规矩）

三条硬规矩，破哪条都会把**对的凭据发到错的地方**：

1. **key 只发给自己的域名**。逐字引（`runtime_provider.py:319-321`，`_host_gated_env_key_candidates` docstring）：
   > Sending OPENAI/OPENROUTER/OLLAMA keys to an unrelated endpoint leaks credentials (GHSA-76xc-57q6-vm5m); match on HOST, not substring.
2. **config 压 env**。逐字引（`runtime_provider.py:412-413`，`resolve_requested_provider` docstring）：
   > Config beats the env so chat uses the endpoint the user last saved, not a stale shell/.env override.
3. **凭据不跨 profile**。逐字引（`agent/secret_scope.py:4-5`）：
   > each profile's `.env` keys **cannot** be unioned into `os.environ` (profile A's keys would leak into profile B's turns and subprocesses)

（同族的还有 `runtime_provider.py:262-263`：`a stale base_url must not leak into another provider` —— 陈旧的 base_url 不许串到别的 provider 上。）

**要害**：三条其实是同一句话的三个面 —— **跨域名、跨新旧配置、跨 profile 都不许混用凭据**。

### 0.5 出错（未知 provider / 无凭据 / 不支持的模式）

这三种情况都在发请求前处理：未知供应商或缺凭据可以抛错，无效模式也可能被忽略并记录日志，不能统称为抛异常：

- **未知 provider**：名字层直接抛 `AuthError`，带 code 与"下一步"提示。逐字引（`hermes_cli/auth.py:1353`）：
  > `raise AuthError(f"Unknown provider '{normalized}'." + tail, code="invalid_provider")`
- **无凭据**：裸 `custom` 落到整条阶梯的兜底仍没 key 时抛 `AuthError(code="missing_api_key")`（`runtime_provider.py:885-890`）；`auto` 走完整个阶梯仍无路时抛 `AuthError(code="no_provider_configured")`（`auth.py:1414`）。
- **不支持的模式**：真实版三种 `api_mode` 都有适配器，但"配了一个跟端点不搭的模式"会被**忽略并记日志**，而不是发出去炸。逐字引（`runtime_provider.py:156-157`）：
  > `logger.info("Ignoring persisted custom api_mode=codex_responses for non-OpenAI endpoint %s", base_url or "(unknown)")`

  完全无效的值则由 `_parse_api_mode`（`runtime_provider.py:131-136`）判为 `None`（回落到 host 探测）。

**要害**：错误都**在发请求之前**冒出来，且指明该改哪里 —— 不把问题推到第一次 HTTP 调用。

### 0.6 跨层（谁调它）

门面被一串**进程模型完全不同**的入口共用。模块 docstring 自己点了范围（`runtime_provider.py:1`）："for CLI, gateway, cron, and helpers"。我这次 grep 到并读到的调用点：

| 入口 | 调用点 |
|---|---|
| CLI | `hermes_cli/cli_agent_setup_mixin.py:190` |
| TUI | `tui_gateway/server.py:2177` · `tui_gateway/methods_config.py:316` |
| 网关 | `gateway/run.py:2249` · `gateway/platforms/api_server.py:316` |
| cron | `cron/scheduler.py:1522` · `cron/jobs.py:1694` |
| 子代理 / 辅助 / 插件 | `tools/delegate_tool_config.py:315` · `hermes_cli/model_switch.py:1090` · `plugins/image_gen/openrouter/__init__.py:532` |

**要害**：接入一个新入口的成本 = 一次 `from ... import resolve_runtime_provider`，**不改解析逻辑** —— 这就是整章要守住的那条架构接缝。

### 0.7 时间线（一次解析从入口到产出 dict 的顺序）

1. 入口把 `(requested / model / explicit_api_key / explicit_base_url)` 交给门面（`runtime_provider.py:851`）；
2. `resolve_requested_provider()`（`:410`）做名字层四层判定（显式 → config → env → auto），得出一个 provider 名；
3. `_raise_if_provider_disabled()` 挡掉被显式禁用的供应商；
4. `_ladder_rungs()`（`:893`）逐级 `yield`，**前一级返回空才评估下一级**；
5. 第一个非空 rung 短路胜出（各 builder 都经 `_runtime()` 造 dict）；
6. `_raise_for_credentialless_bare_custom()`（`:873`）兜底校验；
7. 返回 dict。

逐字引（`runtime_provider.py:853-854`，门面 docstring）：

> Ladder (order is behavior — each rung returns or raises, else falls to the next):

逐字引（`runtime_provider.py:894-895`，`_ladder_rungs` docstring）：

> Ladder rungs 2-8, yielded lazily so each is evaluated only when the previous one returned nothing; the last rung (OpenRouter / bare-custom fallback) always yields a runtime.

**要害**：阶梯是**惰性**的，且**最后一级必定产出 runtime** —— 所以门面要么短路返回一份完整 dict，要么带着明确错误抛出，不存在"走到一半返回半成品"。

### 验收四问

1. **五个入口是不是只调同一道门面？** 是。`grep -rn resolve_runtime_provider` 只命中同一个 `resolve_runtime_provider()`，没有入口自建解析。（§0.6）
2. **名字层与连接层是不是分开的？** 是。`resolve_provider()` 只返回一个**字符串** provider id（`auth.py:1348`），运行时 dict 只在 `_runtime()` 一处构造（`runtime_provider.py:249`）。（§0.1）
3. **每把 key 是不是只发给自己那条域名？** 是。`_host_gated_env_key_candidates()`（`runtime_provider.py:317`）按 host 精确 / 点后缀匹配，`api.openai.com.attacker.test` 这类伪装一律不命中。（§0.4）
4. **回退是不是"链耗尽恒 False + 每轮复位"？** 是。`_fallback_index` 一次前进一格、`_fallback_activated` 回退状态标记、新一轮由 `restore_primary_runtime()` 复位。（§0.4 的同族约束）

---

## §〇 差异表

只列**架构性**的差别为主，外加少数"已对齐"与"量级"行，方便一眼看清两条实现是不是同一个形状。性质四取一：`架构性` / `已对齐` / `量级` / `不抄`。

| # | 面 | 真实 Hermes | mini 现状 | 性质 |
|---|---|---|---|---|
| 1 | 解析的对外门面 | `resolve_runtime_provider()`（`hermes_cli/runtime_provider.py:851`）—— 唯一物化点 | 同名门面（`hermes_cli/runtime_provider.py:417`） | 已对齐 |
| 2 | 两段式（名字层 / 连接层） | `resolve_provider()` 返**字符串** id（`auth.py:1323`）；dict 只在 `_runtime()`（`:249`）构造 | `resolve_requested_provider`(:255) / `resolve_provider`(:344) / `resolve_runtime_provider`(:417) 三函数分离 | 已对齐 |
| 3 | 注册表 + 插件式懒注册 | `PROVIDER_REGISTRY` + `plugins/model-providers/` **39** 个目录；首次访问懒发现；用户插件覆盖内建 | `providers/` 包 9 文件；同款懒发现（`$MINI_HOME/plugins/model-providers/`）；内建 3 家 | 量级 |
| 4 | `api_mode` 判定 | host 强制表 + URL 探测 + provider 叠加；三值外另有 `bedrock_converse` / `codex_app_server` | 同判据（`providers/api_mode.py`）；三值；**只实现** `chat_completions`，另两个给带指引报错 | 架构性 |
| 5 | 凭据的 host 门控 | `_host_gated_env_key_candidates()`（`runtime_provider.py:317`）+ `base_url_host_matches` | `providers/host_gate.py`（`host_matches` / `key_candidates_for` / `host_derived_api_key`） | 已对齐 |
| 6 | 凭据读取通道 | 一律 `get_secret_str`（`agent.secret_scope.py`，scope-aware） | 同一 `agent.secret_scope.get_secret` | 已对齐 |
| 7 | 别名归一 | `_plugin_aliases()`（`auth.py:1192`）+ 几十家别名表 | `providers/aliases.py`（小表；刻意**不含** `openai→openrouter`） | 量级 |
| 8 | 自定义端点 | 具名 `providers:` + legacy `custom_providers:` + 裸 `custom` 共两路 | 别名 `ollama/vllm/llamacpp → custom`（`aliases.py:28`），但注册表里**没有** `custom` 画像 ⇒ `--provider custom` 仍报未知；只支持门面 kwarg 传 `explicit_base_url` | 量级 |
| 9 | `auto` 阶梯 | 8 级（禁用守卫 → 短路 → 具名 custom → local bypass → `resolve_provider` → 凭据池 → OAuth/free-tier/Bedrock → OpenRouter 兜底） | 四层（显式 → config → env → auto）；`auto` 落地 = 按注册顺序找第一个有 env key 的供应商 | 不抄 |
| 10 | 运行中 `/model` 切换 | `switch_model()` 四步纯函数，CLI 与网关共用同一接缝 | 无（选模型 = 启动参数 + config） | 不抄 |
| 11 | 辅助模型路由 | `agent/auxiliary_client.py` 7,800 行，多消费者（视觉 / 网页摘要 / 记忆刷写…） | `agent/auxiliary.py` 147 行，一个消费者（压缩摘要） | 量级 |
| 12 | aux 的 `main` 别名 | `"main"` → 主 agent provider；主为空/auto 时 → `"custom"`（`auxiliary_client.py:546`） | `main` → 主 provider；主为空时 → `auto`（mini 无 `custom` 画像） | 架构性 |
| 13 | 回退链机制 | 链 + `_fallback_index` + `_fallback_activated`；6 个模块 / 11 处调用点；`error_classifier` 分类 | 同状态机（`agent/fallback.py`）；**二元**判据（可回退 / 不可回退）；一处触发 | 架构性 |
| 14 | 回退凭据隔离 | 每条自解析（`hermes_cli/fallback_config.py` 的 `resolve_entry_api_key`），provider 变则换凭证池 | 每条自解析（`hermes_cli/config.py::resolve_entry_api_key`，经 `get_secret`）；无池可换 | 已对齐 |
| 15 | 回退复位 | 每轮 `restore_primary_runtime()` 复位 index / latch | 同（轮首复位） | 已对齐 |
| 16 | 配置层 | `config.yaml` + env + `fallback_config.get_fallback_chain()` | `hermes_cli/config.py`（`config.json`）；键定义即唯一事实源；config 压 env | 量级 |
| 17 | `source` 栏 | 多取值（`explicit`/`env`/`config`/`pool`/`oauth`/`portal`/`process`…），无集中枚举 | 四取值（`explicit`/`config`/`env`/`auto`） | 量级 |
| 18 | 多 profile secret-scope / 凭证池轮转 | `secret_scope` + `credential_pool` 完整 | secret-scope **已对齐**（7.2 章）；凭证池无 | 不抄 |

---

## §一 两段式解析

**① Hermes 怎么做的**

解析这件事被拆成两段，各答一个不同的问题。

**名字层**答的是「你要的是谁」——它只产出一个 provider id 字符串（`deepseek` / `anthropic` / `custom` …），判据顺序是：显式参数 → `config.yaml` 的 `model.provider` → 环境变量 → `auto`。**这一段不读凭据、不碰注册表**：没有配置任何 key，它也照样能说出「你要的是 deepseek」。顺序里唯一反直觉的一处是 **config 压过 env**：一个上一次 shell 里 `export` 过的旧环境变量，不该悄悄盖掉用户刚刚在 `hermes model` 里存下的端点。所以保存下来的选择是正常运行的唯一事实来源，env 只是「进程当下碰巧有」的东西，位次更低。

**连接层**答的是「这个名字要变成什么样的请求」——把 id 解析成 `base_url` / `api_key` / `api_mode` 三样。它**不猜名字**：只接受注册表里的 canonical id，查不到就明确报未知 provider，绝不「看着像」就放行。

**为什么必须分开**：一是职责不同——回答「用哪家」不需要任何密钥在场，而回答「发到哪、拿哪把钥匙」才需要；合并的话，任何只想看一眼「这次会用谁」的诊断/列举代码，都被迫先把凭据解析出来。二是**返回值形状**不同：名字层输出的就是一个字符串（老的调用点 `== "deepseek"`、拿它当 dict 键都照旧可用），连接层输出的是一份 dict；硬捏在一起，就会为了「多带几个字段」而破坏字符串兼容性。

两段之上再压一个**门面** `resolve_runtime_provider()`，它是五个入口（CLI / 网关 / cron / ACP / 辅助模型）**唯一**共用的出口，返回的字段集**基础四栏与按路径附加的元数据（不保证每条路径都有六栏）**：`provider / api_mode / base_url / api_key / source / requested_provider`。固定的意义是契约——入口不同，但「用哪个模型」的答案只有一个来源、一个形状。

最后是 **`source`** 这一栏：它把「这次的结果是怎么来的」（显式 / config / env / auto）**落成数据**，而不是只留在日志里。这和前面几章反复出现的同一取向一致——**把裁决理由落成数据，让「为什么用了这个端点」变成可回答的**。名字层产出的名字自带 `.source`，门面把它原样带进 dict。

**② 读 Hermes 的代码**

`hermes_cli/runtime_provider.py:410-419`（真实侧，锚 `f5d1926110`）：

```python
def resolve_requested_provider(requested: Optional[str] = None) -> str:
    """Provider request from explicit arg, then config, then ``HERMES_INFERENCE_PROVIDER``, else
    "auto". Config beats the env so chat uses the endpoint the user last saved, not a stale
    shell/.env override."""
    if requested and requested.strip():
        return requested.strip().lower()                 # ① 显式 CLI / 运行时
    cfg_provider = _get_model_config().get("provider")
    if isinstance(cfg_provider, str) and cfg_provider.strip():
        return cfg_provider.strip().lower()              # ② config.yaml model.provider
    return get_secret_str("HERMES_INFERENCE_PROVIDER", "").strip().lower() or "auto"  # ③ env，否则 auto
```

要害有两处：**返回值是一个字符串**（不是 dict）——这就是「名字段」的全部产物；以及 docstring 里那句 *Config beats the env… not a stale shell/.env override* ——「config 压 env」是**写进注释的判据**，不是实现细节。第三层读 env 用的是 `get_secret_str` 而非裸 `os.environ`，这一点在 §四 展开。

**③ mini 的实现（现状）**

**✅ 已抄（756b493）**。`mini-hermes/hermes_cli/runtime_provider.py`（441 行）就是这一道形状：

- 名字层 `resolve_requested_provider()` 在 `:255`，四层判据落在 `:268-276`（显式 `:268`、config `:270-272`、env `:273-275`、auto `:276`）。
- 它返回的 `RequestedProvider`（`:226-240`）是 **`str` 子类**，额外挂一个 `.source` —— 一个带理由的字符串，两边的兼容性都保住。
- 连接层 `resolve_provider()` 在 `:344`，产出 dict 于 `:398-409`（含 `source`，`:408`）。
- 门面 `resolve_runtime_provider()` 在 `:417`，`①名字层 → ②连接层` 合成，并把 `source` / `requested_provider` 补回 dict（`:433-441`）。

名字层核心（`:268-275`，节选到第 3 层；第 4 层 `auto` 的 `return RequestedProvider("auto", "auto")` 在 `:276`）：

```python
    if requested and str(requested).strip():
        return RequestedProvider(str(requested).strip().lower(), "explicit")   # 第 1 层
    cfg_provider = _config_model_config().get("provider")
    if isinstance(cfg_provider, str) and cfg_provider.strip():
        return RequestedProvider(cfg_provider.strip().lower(), "config")       # 第 2 层
    env_provider = os.environ.get("MINI_INFERENCE_PROVIDER", "").strip().lower()
    if env_provider:
        return RequestedProvider(env_provider, "env")                          # 第 3 层
```

顺序与真实版逐字一致：config 压 env。`MINI_INFERENCE_PROVIDER` 用 `os.environ` 直读是**刻意的**——它是行为设置不是凭据（凭据一律走 `get_secret`，见 §四）。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 段划分 | 名字层 `resolve_requested_provider()`（`runtime_provider.py:410`）+ 连接层 `resolve_provider()`（`auth.py:1323`） | 同名两函数（`runtime_provider.py:255` / `:344`） |
| 门面 | `resolve_runtime_provider()` 基础字段加路径元数据（`:851`） | 同（`:417`，另带 `model` / `context_length`） |
| `source` 取值 | 一大串，散落各 builder（pool / oauth / portal / process…） | 只有 4 值：explicit / config / env / auto |
| config 压 env | 是（docstring 明写） | 是（`:270-275`） |

---

## §二 注册表与插件式懒注册

**① Hermes 怎么做的**

这一层解决的是「供应商数据住在哪、谁来改」的问题。答案是：**每个供应商一份声明式画像（`ProviderProfile`），注册表是唯一事实源**。

一份画像描述这个供应商长什么样：身份（`name` / `api_mode` / `aliases`）、展示（`display_name` / `signup_url`）、认证与端点（`env_vars` / `base_url` / `auth_type`）、视觉与请求形状的 quirk（`supports_vision` / `fixed_temperature` / `default_headers`）……它是**声明式**的：描述「是什么」，**不**建 HTTP 客户端、**不**管凭据轮换、**不**管流式——那些留在 agent 里。解析器读这个对象拿到 `base_url` / `env_vars` / `api_mode` / `fallback_models`，于是**同一个事实不用在多处重复**。

由此得到这一章最重要的一个性质：**加一家供应商 = 加一份画像**。新供应商只要在自己的目录里调一次 `register_provider(profile)`，就被解析链认识——`resolve_provider()` 的具名路径、`auto` 环境探测可读取同一份画像；特殊 host 门控仍可能需要相应规则。**不需要改解析器**，这就是「核心是窄腰、能力在边缘」那条产品姿态的落地。

画像可以来自三处：随仓库走的内建插件、`$HERMES_HOME/plugins/model-providers/` 下的用户插件、以及 第三方提供的插件代码（本章引用的发现机制只明确扫描内建和用户目录）。发现是**懒**的——第一次 `get_provider_profile()` / `list_providers()` 才去 import 各插件，免得 import 期就碰文件系统、触发副作用。冲突时 **last-writer-wins**：后注册者覆盖先注册者，所以用户插件能顶掉同名内建画像，第三方能 monkey-patch 而不动仓库一行代码。

最后一句别记错：**`providers/` 包只是「取用口」，不是「数据库」**——它就是个很薄的小包（注册表接口 + `ProviderProfile`），真正的供应商数据住在各家自己的插件目录里。

**② 读 Hermes 的代码**

`providers/__init__.py:14-27`（真实侧）：

```python
Discovery is lazy: the first call to ``get_provider_profile()`` or
``list_providers()`` scans both locations and imports every plugin. User
plugins override bundled plugins on name collision (last-writer-wins), so
third parties can monkey-patch or replace any built-in profile without
editing the repo.

Each plugin directory contains:
  - ``__init__.py`` — calls ``register_provider(profile)`` at import
  - ``plugin.yaml`` — manifest (name, kind: model-provider, version, description)
```

这段注释一句话讲清三件事：**懒发现**（首次 lookup 才扫两处）、**last-writer-wins**（用户插件覆盖内建，第三方不改仓库就能顶）、**插件目录的形状**（`__init__.py` 里注册 + `plugin.yaml` 清单）。放在 `providers/__init__.py` 顶部而不是某个函数里，因为这是整个包的对外契约。

**③ mini 的实现（现状）**

**✅ 已抄（756b493）**。`mini-hermes/providers/` 是个 9 文件、共 723 行的包：

- **画像** `ProviderProfile` 在 `providers/base.py:49-119`——字段按身份 / 展示 / 认证端点 / quirk 分组，与真实版同名同义（`env_vars` 的「优先级有序」语义也照抄，`:64-65`）。
- **注册表 + 懒发现** 在 `providers/__init__.py`：`register_provider()` `:44`、`get_provider_profile()` `:59`（首次 lookup 触发 `:65-66`）、`list_providers()` `:77`、`_discover_providers()` `:128`（先内建再用户插件，后注册赢）。
- **数据在各家目录**：`providers/builtin/` 下三家——`deepseek.py:15`、`anthropic.py:15`、`openai_compatible.py:19`（canonical 名保持 `openai`）；用户插件目录是 `$MINI_HOME/plugins/model-providers/<name>/`。
- **旧硬编码表变派生视图**：`hermes_cli/runtime_provider.py:71-89` 的 `provider_registry()` 从 `list_providers()` 派生（数据只存一份），`__getattr__`（`:92-96`）懒构造 `PROVIDER_REGISTRY`。

派生视图（`:80-88`，节选）：

```python
for profile in list_providers():
    key_envs = profile.api_key_env_vars
    view[profile.name] = {
        "base_url": profile.base_url,
        "api_key_env": key_envs[0] if key_envs else "",
        "base_url_env": profile.base_url_env_var,
        "default_model": profile.default_model,
        "api_mode": profile.api_mode}
```

注册 3 家画像 → 任何 `register_provider()` 进来的新画像**自动出现**在这个视图里，就是「唯一事实源」的可见证据。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 画像字段 | `ProviderProfile`（`providers/base.py:38`）几十字段 + 子类钩子 | 同名 dataclass（`providers/base.py:49`），字段收窄、**无钩子** |
| 注册源 | 内建 `plugins/model-providers/`（39 家）+ 用户插件目录（未据此确认 pip 入口点机制） | `providers/builtin/`（3 家）+ `$MINI_HOME/plugins/model-providers/` |
| 懒发现 | 首次 lookup 扫两处 | 同（`providers/__init__.py:65` / `:128`） |
| last-writer-wins | 是 | 是（`:44`） |
| 数据位置 | 数据住各家插件目录，`providers/` 只是取用口 | 同 |

---

## §三 `api_mode` 三值与按 host 判定

**① Hermes 怎么做的**

`api_mode` 是「这一家用哪种 API 形状说话」，三个取值：**`chat_completions`**（OpenAI 兼容，最常见，也是默认兜底）、**`anthropic_messages`**（Anthropic 原生 Messages）、**`codex_responses`**（OpenAI Responses）。

关键在于**怎么判**：不是一张 `provider → mode` 的静态大表，而是**按 URL 探测**。原因是同一个 provider 可以有多个端点（官方 / 代理 / 数据驻留子域），协议由**端点**决定，不由供应商名字决定。所以判据是一段**优先顺序**：① host 强制表 → ② 官方 OpenAI 主机族（含 `us.` / `eu.` 驻留子域，它们需要 Responses 才能带 reasoning 做工具调用）→ ③ URL 路径后缀（`/anthropic` / `/anthropic/v1`，第三方 Anthropic 兼容网关的惯用写法；Kimi 的 `api.kimi.com/coding` 也讲 Messages）→ ④ 供应商身份叠加 → ⑤ `chat_completions`。

三值各自被选中的条件，成一张小表：

| 取值 | 判据（命中即选） |
|---|---|
| `chat_completions` | 默认兜底；provider 是 Actual 通道；注册表里绝大多数 api_key 供应商 |
| `anthropic_messages` | hostname = `api.anthropic.com`；URL 以 `/anthropic`(`/v1`) 结尾；`api.kimi.com/coding`；provider ∈ {anthropic, minimax…} |
| `codex_responses` | 官方 OpenAI 主机族；`api.x.ai` / `api.meta.ai` / `api.router.com`；provider ∈ {xai, openai-codex} |

★★ 一个**安全细节**贯穿始终：host 匹配必须是**精确 hostname 或点后缀子域**，**绝不用子串**。否则 `api.anthropic.com.attacker.test`（伪装子域）和 `proxy.test/api.anthropic.com/v1`（把域名塞进路径）都会被误认成真端点（对应上游 issue #32243）。

端点说了算的**优先级**也值得记住：一个「声明自己是 chat_completions」的供应商，把 `base_url` 指到 `api.anthropic.com` 时，**端点的强制协议更可信**——所以探测优先于声明。

mini 里还有一层：`VALID_API_MODES`（认识的协议）与 `IMPLEMENTED_API_MODES`（真跑得起来的协议）**是两回事**。选中的模式**没实现**时怎么报错，本身就是设计的一部分——见下。

**② 读 Hermes 的代码**

`hermes_cli/runtime_provider.py:88-92`（真实侧）：

```python
# Hosts that only speak one wire protocol. Mirrors host_mandated_api_mode in hermes_cli/providers.py
# so the runtime resolver stays in lockstep: api.meta.ai — prompt caching only on Responses;
# api.router.com — /v1/chat/completions is a minimal shim; api.anthropic.com — native Messages.
_HOST_MANDATED_API_MODES = {
    "api.x.ai": "codex_responses", "api.meta.ai": "codex_responses", "api.actual.inc": "chat_completions",
    "api.router.com": "codex_responses", "api.anthropic.com": "anthropic_messages",
}
```

要害：这是一张**按 hostname 强制协议**的小表，注释同时点明它**镜像**了 `providers.py::host_mandated_api_mode`——「判据只有一份，两处必须同步」。表里每个 entry 都是「这个主机只讲这一种协议」，是 §① 判据的第 ① 步。

**③ mini 的实现（现状）**

**✅ 已抄（756b493）**。`mini-hermes/providers/api_mode.py`（170 行）把整条判据搬了过来：

- 三个集合/表：`VALID_API_MODES` `:23`、`IMPLEMENTED_API_MODES` `:26`、`_HOST_MANDATED_API_MODES` `:31-37`、`_PROVIDER_API_MODES` `:40-46`。
- 判定函数三段式：`detect_api_mode()` `:108`（按 URL 探测）→ `determine_api_mode()` `:137`（host 优先 + provider 叠加 + 画像声明）→ `fallback_api_mode()` `:162`（兜底 `chat_completions`）。
- `host_mandated_api_mode()` `:86` 只做精确/点后缀匹配，注释点名了「拒伪装子域与路径塞域名」（`:89-92`）。

未实现的模式怎么报错，是这节的落点（`providers/api_mode.py:23-26` + `run_agent.py:508,510`）：

```python
VALID_API_MODES: Set[str] = {"chat_completions", "codex_responses", "anthropic_messages"}
IMPLEMENTED_API_MODES: Set[str] = {"chat_completions"}
# run_agent.py:508,510
from providers.api_mode import require_implemented_api_mode
require_implemented_api_mode(self.api_mode)
```

`require_implemented_api_mode()`（`:56`）对未实现模式抛 **`UnsupportedApiMode`**（`RuntimeError` 子类，`:49`），报错里写清「mini 认识哪几种、只实现了哪种、要动哪个文件（`run_agent.py::_build_api_messages`）」。以前这里是裸抛 `NotImplementedError`（只有一行「需要格式转换层」），现在把**指引**写进错误里——「跑到发请求才炸、炸得没头没脑」换成了「能照着修的报错」。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 三值 | chat_completions / anthropic_messages / codex_responses（另有 `bedrock_converse` / `codex_app_server`） | 三个都「认识」（`VALID_API_MODES`），只**实现** chat_completions |
| 判据 | 按 URL 探测 + host 强制表 + provider 叠加 | 同（`providers/api_mode.py`） |
| 未实现模式 | 有真适配器（`anthropic_adapter.py` / `codex_responses_adapter.py`） | 发请求**前**抛 `UnsupportedApiMode`（带指引） |
| 主机表 | `_HOST_MANDATED_API_MODES`（含 kimi `/coding`） | 同表 + kimi `/coding` + `/anthropic` 后缀 |

---

## §四 凭据的 host 门控

**① Hermes 怎么做的**

同时存在多把钥匙时（`OPENROUTER_API_KEY` / `OPENAI_API_KEY` / `OLLAMA_API_KEY`），要防一件很具体的事：**别把错的 key 发给错的端点**——那就是凭据泄漏。上游为此出过一个安全公告（**GHSA-76xc-57q6-vm5m**）。

规则是「**每把 key 只发给自己那条权威域名**」：`OPENAI_API_KEY` 只在 `openai.com` / `openai.azure.com` 命中时挂载，`OPENROUTER_API_KEY` 只在 `openrouter.ai` 命中时挂载，`OLLAMA_API_KEY` 只在 `ollama.com` 命中时挂载。判据是 **host 精确匹配或点后缀匹配**，**不是子串**——`api.openai.com.attacker.test`（伪装子域）和 `evil.com/api.openai.com/v1`（域名塞路径）都**不算命中**。

自定义端点还有第四把候选，是**从 host 反推**出来的：`api.deepseek.com` → `DEEPSEEK_API_KEY`（按简化规则取**倒数第二个域名标签**；不是公共后缀规则下的通用注册域名解析，大写，拼 `_API_KEY`）。反推同样防伪装：把 key 的名字也按 host 算，`api.deepseek.com.attacker.test` 的最后两段是 `attacker.test` → 推出的是 `ATTACKER_API_KEY`，于是真 `DEEPSEEK_API_KEY` **原地不动**。反推还显式排除 OPENAI / OPENROUTER / OLLAMA, 因为它们各走自己的 `host_matches` 门控，不给「伪装 host」抢 key 的机会。文档里那句「`OPENAI_API_KEY` 用于自定义端点并作为兜底」其实**比源码松**——源码里三把 key 都只挂自己的域名，自定义端点用的是**按域名推出来的那把**，不是「把 OpenAI 的 key 发给任意端点」。

最后一块拼图是**读 key 的通道**：凭据一律走 **scope-aware 的 `get_secret_str`**（来自 `agent.secret_scope`），**不裸读 `os.environ`**。原因与多 profile 有关——`.env` 里的 key **不能**被并进 `os.environ`（否则 profile A 的 key 会漏给 profile B）。作用域解析的语义是：能命中就命中；命中不了返回默认值，**绝不回落去读别人的 environ**；处在多路复用下且没有作用域时，直接**抛错（fail-closed）**。于是「A 的回合拿不到 B 的 key」是被结构保证的，不是靠自觉。

**② 读 Hermes 的代码**

`hermes_cli/runtime_provider.py:317-326`（真实侧）：

```python
def _host_gated_env_key_candidates(base_url: str, *, ollama: bool) -> list:
    """Env API keys gated on their authoritative hosts, then the host-derived ``<VENDOR>_API_KEY``.
    Sending OPENAI/OPENROUTER/OLLAMA keys to an unrelated endpoint leaks credentials
    (GHSA-76xc-57q6-vm5m); match on HOST, not substring. ``_host_derived_api_key`` skips OLLAMA, so
    callers that want it opt in via ``ollama``."""
    is_openai = base_url_host_matches(base_url, "openai.com") or base_url_host_matches(base_url, "openai.azure.com")
    candidates = [get_secret_str("OLLAMA_API_KEY", "").strip() if base_url_host_matches(base_url, "ollama.com") else ""] if ollama else []
    return candidates + [get_secret_str("OPENAI_API_KEY", "").strip() if is_openai else "",
                         get_secret_str("OPENROUTER_API_KEY", "").strip() if base_url_host_matches(base_url, "openrouter.ai") else "",
                         _host_derived_api_key(base_url)]
```

要害两点：docstring 里那句 *match on HOST, not substring*（判据的**口径**）+ 附上的安全理由 *leaks credentials (GHSA-76xc-57q6-vm5m)*，这是把「为什么这么写」钉在代码里的写法。第二点：每把 key 都包在 `if <host 命中> else ""` 里——**空串代表「这把 key 不该发给这个端点」**，调用方用 `next(非空)` 取第一把，于是「门控」和「优先级」用同一个候选列表就表达完了。

**③ mini 的实现（现状）**

**✅ 已抄（756b493）；凭据走 scope-aware 已对齐（7.2 章 A6）**。`mini-hermes/providers/host_gate.py`（121 行）：

- 判据：`base_url_hostname()` `:30`（取**解析后的 hostname**，不是原始字符串）、`host_matches()` `:40`（精确 / 点后缀）、`is_official_openai_host()` `:51`。
- 反推：`_vendor_label()` `:56`、`host_derived_key_name()` `:81`、`host_derived_api_key()` `:87`（排除 OPENAI / OPENROUTER / OLLAMA，`:76`）。
- 三把 key 的门控：`key_candidates_for()` `:95`（`:106-121` 逐把判 host，不匹配给空串）。
- 读 key 走作用域：`from agent.secret_scope import get_secret`（`host_gate.py:16`；连接层 `runtime_provider.py:51`）。

核心判据（`providers/host_gate.py:46-48`）：

```python
hostname = base_url_hostname(base_url)
dom = str(domain or "").strip().lower().rstrip(".")
return bool(hostname and dom) and (hostname == dom or hostname.endswith("." + dom))
```

比 `domain in base_url` 安全——后者会让 `evil.com/moonshot.ai` / `moonshot.ai.evil` 蒙混过关。消费者在连接层：`_resolve_api_key()`（`runtime_provider.py:307`）把候选拼成「显式 key → **端点没被换时才用**的声明 key（`:322`）→ host 门控候选」，那个 `if not explicit_base_url` 的守卫正是 A5 的安全语义——把已声明密钥发到被 `--base-url` 换成的陌生域名，就是泄漏。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 判据 | `base_url_host_matches` 精确 / 点后缀（`utils.py:612`） | 同（`host_gate.py:40`） |
| 三把 key 门控 | OLLAMA / OPENAI / OPENROUTER 各挂自己域名 | 同（`key_candidates_for`，`:95`） |
| host 反推 | `_host_derived_api_key` 排除 OPENAI / OPENROUTER / OLLAMA | 同（`host_derived_key_name`） |
| 凭据读取 | `get_secret_str`（scope-aware） | `get_secret`（同源 `agent.secret_scope`） |

---

## §五 自定义端点的两条路

**① Hermes 怎么做的**

一个「自己接」的端点有两种身份，对应两条进入解析链的路，**都先于通用路径**被评估。

**(a) 具名自定义端点**：用户给端点起了名字，配在 `config.yaml`。这里有两种写法并存——**新式 `providers:` 字典**（键是端点名）和 **legacy `custom_providers:` 列表**（为向后兼容继续被读）。解析时的顺序是**先查新式 `providers:`，命中就返回；查不到再回落查 legacy `custom_providers:`**。这解释了「为什么两种写法都要留着读」：老配置文件不能因为迁移就失效。具名路径还要过一道「不被内建遮蔽」的守卫，且能声明 `key_env`（按名取 key，scope-aware）、`key_cmd`（每次请求铸一个短效 token）等字段；解析结果的 `source` 标成 `custom_provider:<name>`。

**(b) 裸 `provider: custom`**（含别名 `ollama` / `vllm` / `llamacpp` 等）：当且仅当解析是 `auto` 且碰上一个**非云**的 `config.base_url` 时，走一条「本地端点绕过」——直接按 OpenAI 兼容协议解析（`_local_endpoint_bypass`）；否则最终落到 **OpenRouter 兜底**。这里要和(a)区分一件长得很像的事：**用户真的选了一个自定义端点**，还是**没配任何自定义端点时走的那条兜底**——真实版专门有判据（`_config_base_url_trustworthy_for_bare_custom`）分辨两者，兜底路径不该把裸 custom 当成「用户存下的选择」。裸 custom 无凭据时，真实版**硬失败**（`_raise_for_credentialless_bare_custom`），不悄悄降级。

两条路的共同点：端点信息（`base_url`）先于凭据被定下来，凭据候选里复用 §四 的 host 门控（`key_candidates_for`）。

**② 读 Hermes 的代码**

`hermes_cli/runtime_provider_custom.py:163-179`（真实侧）：

```python
def _get_named_custom_provider(requested_provider: str) -> Optional[Dict[str, Any]]:
    requested_norm = _normalize_custom_provider_name(requested_provider or "")
    if not requested_norm or requested_norm == "auto" or _shadowed_by_builtin(requested_norm):
        return None
    rp = _rp()
    config = rp.load_config()
    providers = config.get("providers")
    found = _match_new_style_provider(requested_norm, providers) if isinstance(providers, dict) else None
    if found:
        return found
    # （中间省去：custom_providers 为 dict 而非 list 时的 warning）
    custom_providers = rp.get_compatible_custom_providers(config)
    return _match_legacy_custom_provider(requested_norm, custom_providers) if custom_providers else None
```

要害：**新式 `providers:` 先查，命中即返回；查不到才回落 legacy `custom_providers:`**（`:169-179`）——两种配置写法都活着，且新式优先。开头的 `_shadowed_by_builtin` 守卫保证具名自定义端点**不遮蔽**同名内建供应商。

**③ mini 的实现（现状）**

- 具名自定义端点（读 `providers:` / legacy `custom_providers:`）—— **⬜ 没做（量级）**：`mini-hermes` 的 `hermes_cli/config.py`（247 行）只读 `model` / `fallback_providers` / `auxiliary` 三处，**没有** `providers:` 字典的读取（`providers.py` 也没有 `resolve_user_provider` 那样的入口）。
- 裸 custom 的**本地端点绕过** —— **⬜ 没做（量级）**：`providers/aliases.py:28` 把 `ollama/vllm/llamacpp…` 归一到 `custom`，但注册表里**没有** `custom` 画像，于是连接层报「未知 provider」，不假装支持。
- **OpenRouter 兜底** —— **⏭ 不抄（理由）**：`providers/aliases.py:9-11` 明确写「真实版把 `openai` 归一成 `openrouter`；mini 刻意不含 `openai→openrouter`」——mini 没有聚合器兜底这一层，注册表里也无 `openrouter` 画像。
- 端点覆盖的现状—— 只有两处：profile 声明的 `<PROVIDER>_BASE_URL`（仅 deepseek，`providers/builtin/deepseek.py:18`）+ 门面 `explicit_base_url` kwarg（`runtime_provider.py:371-372`）。**✅ 已抄（756b493）** 的是「连接层支持显式 / 环境端点覆盖」，不是配置层具名端点。

别名到 custom（`providers/aliases.py:28`）：

```python
"custom": ("ollama", "local", "vllm", "llamacpp", "llama.cpp", "llama-cpp"),
```

实测：`get_provider_profile("custom")` 返回 `None`，`normalize_provider("ollama")` 返回 `"custom"` —— 别名归一存在，但落地的画像不在，所以这条路径止于报错。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 具名自定义端点 | `providers:` dict 先查 → legacy `custom_providers:`（`runtime_provider_custom.py:163`） | ⬜ 没做（无 `providers:` 读取） |
| 裸 custom | 本地端点绕过 + OpenRouter 兜底（分辨「用户选的」vs「兜底」） | 本地绕过 ⬜ 没做（`custom` 未注册）；OpenRouter 兜底 ⏭ 不抄（`aliases.py:9` 刻意不含 `openai→openrouter`） |
| 端点覆盖 | `explicit base_url` / `config.base_url` / env 多来源 | 只有 profile `<PROVIDER>_BASE_URL`（deepseek）+ 门面 `--base-url` kwarg |
| 别名到 custom | `ollama/vllm/…` → `custom` | `ollama/vllm/… → custom`（`aliases.py:28`），但 `custom` 未注册 → 报未知 provider |

---

## §六 辅助模型的独立路由

**① Hermes 怎么做的**

**辅助任务**（auxiliary task）指那些"不干主对话、但也要调模型"的活：看图（视觉）、网页抓取后的摘要、**上下文压缩的摘要**、技能中心（skills hub）操作、MCP 辅助操作、记忆刷写。它们是独立的一类调用，不是主对话的一个回合。

主任务与辅助任务可以有不同的质量、延迟和成本需求。所以 Hermes 给它们各自留了一个配置块 `auxiliary.<task>.{provider, model}`，让辅助调用可以走**另一个**供应商/模型。这一步的核心不是省钱，而是两件更要紧的事：**别让一次摘要用掉主模型的预算**，也**别让免费档的窗口把摘要掐掉**。

取值的优先级是三层：**显式入参 > config `auxiliary.<task>.*` > `"auto"`**。这里有两个魔法值必须分清：

- `"auto"` —— 优先主 agent 当前路由，随后按任务和能力执行自动探测/回退链；mini 的 auto 则直接复制主运行时。缺省值是 auto，但上游不保证最终始终选中主路由（`auxiliary_client.py:4373-4395`）。
- `"main"` —— **别名**，解析成"当前主 agent 的那个 provider"。它存在的理由是让**具名自定义供应商**也能被辅助任务复用（注释原话：*"Resolve to the actual main provider so named custom providers work."*）。主 provider 为空/`auto`/`main` 时，真实版退化为 `"custom"` —— 一个**始终存在**的通用插件画像，而不是乱造一个名字。

★ **辅助任务为什么不消费主 agent 的回退链**：辅助调用是**一次性**的 —— 按需 `resolve_provider_client()` 建一个 client 就用掉，**没有**持久 agent 身上的 `_fallback_chain` / `_fallback_index` / `_fallback_activated` / 重试循环。回退机制（§七）需要一个**活着的 agent 状态**来记"这次换到第几格""这一轮是否已经换过"，辅助调用给不了这个状态，所以它**不可能**走 `_try_activate_fallback`。它改在**解析期**串行试自己的链：先 `auxiliary.<task>.fallback_chain`，再主策略 `get_fallback_chain()`，最后一级是"兜底用主 agent 模型"。这不是"没有回退"，而是**换了一个发生位置**（解析期而非回合期）。

但注意：辅助路由**不另开一套解析** —— 它与回退链共用同一个 provider 路由器（见 §九）。这一点决定了"辅助任务能用另一个 provider"不意味着"认证/端点/协议各写一遍"。

**② 读 Hermes 的代码**

`agent/auxiliary_client.py:546`（`_normalize_aux_provider` 的 `"main"` 支路）：

```python
    if normalized == "main":
        # Resolve to the actual main provider so named custom providers work.
        main_prov = (_read_main_provider() or "").strip().lower()
        if not main_prov or main_prov in {"auto", "main"}:
            return "custom"
        normalized = main_prov
    return _PROVIDER_ALIASES.get(normalized, normalized)
```

要害在这七行：`main` 不是"再选一次模型"，而是**查主 agent 是谁**；查不到时给一个始终存在的通用画像 `custom`，而不是凭空造一个名字。最后一行把归一交回别名表（§八），保证 `main` 与普通名字走同一条归一。

**③ mini 的实现（现状）**

`agent/auxiliary.py` **147 行**。mini 目前只接了**一个**消费者：上下文压缩的摘要调用（`TASK_COMPRESSION`，`auxiliary.py:48`；调用点 `run_agent._summary_llm`，`run_agent.py:1082`）。三件事各有落点：

- `resolve_task_provider_model`（`auxiliary.py:51`）落实优先级 显式 > config > auto；
- `normalize_aux_provider`（`auxiliary.py:69`）落实 `main` 别名；
- `resolve_auxiliary_runtime`（`auxiliary.py:95`）产出一份与主运行时同形状的 dict。

**未配置时逐字等同主运行时**（文档要求的那句"等价于主模型"）：解析结果 `provider == "auto"` 时直接返回主运行时的副本，`base_url` / `api_key` / `api_mode` 一个字节都不变：

```python
    if resolved_provider == AUX_PROVIDER_AUTO:
        out = dict(main)
        out["model"] = model or main.get("model", "")
        out["resolved_provider"] = resolved_provider
        out["source"] = "aux:auto"          # = 继承主路由（默认）
        return out
```

（`auxiliary.py:118-123`。测试 `test_auto_inherits_main_runtime_exactly` 逐字段盯着这条不变量。）一处**刻意差别**：主 provider 为空时 mini 退化为 `"auto"`（`auxiliary.py:89-90`），不是真实版的 `"custom"` —— mini 的注册表里**没有** custom 画像，返回 `"custom"` 只会在连接层变成"未知 provider"报错；退化到 `auto` 保留了同一语义（"解析不出主 provider 时，不乱造一个新 provider"）。消费者侧 `_summary_llm`（`run_agent.py:1094-1099`）用解析出的运行时**一次性**发一次请求，**不走回退链**。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 消费者 | 视觉 / 抓取摘要 / 压缩 / 技能中心 / MCP / 记忆刷写 | 只接"压缩摘要"一个 |
| 主 provider 为空时的 `main` | 退化为 `"custom"` | 退化为 `"auto"`（无 custom 画像） |
| 辅助任务回退 | 解析期自己的 `auxiliary.<task>.fallback_chain` + 主链 | 解析失败即兜底用主模型 |
| 适配器 | `auxiliary_client.py` 7800 行（Codex/Anthropic/Bedrock/Gemini/MoA） | 只 OpenAI 兼容一条 HTTP 路径 |

---

## §七 回退链

**① Hermes 怎么做的**

**回退链**（fallback chain）是一串 `(provider, model)` 条目：主模型出错时**按顺序**尝试。它的状态是**三件套**：`_fallback_chain`（链）、`_fallback_index`（索引）、`_fallback_activated`（回退状态标记）。

索引在评估每个候选前推进，解析失败或不可用的候选也会被消费；一次激活调用可能跳过多项。
成功切换后，如果新供应商再失败，还能继续尝试下一项。`_fallback_activated` 是状态标记，
并不作为入口守卫禁止二次切换；防止重复候选靠索引和不可用候选集合。
链耗尽时 `_fallback_chain_exhausted()` 返回 False，表示本次没有切换成功，后续处置由调用方决定。
证据见 `chat_completion_helpers.py:1905-1927`。

**激活时是"原地换"运行时**，换的是：`model` / `provider` / `requested_provider` / `base_url` / `api_mode`（外加 client 与 `api_key`）。同时还要做几件配套事：清 `_transport_cache`（连接复用会带着旧 host 的句柄）；把 per-config 的 `context_length` 覆盖清掉（否则按**上家**模型的窗口算压缩阈值）；重解析 reasoning / extra_body；更新压缩器的模型上下文；最后置 latch。

**每轮复位**：新一轮开始时 `restore_primary_runtime()` 把运行时收回主 provider，并复位索引 + latch。为什么必须复位 —— 一次限流/抖动是**瞬时**的，上一轮的兜底 provider 不该悄悄变成这一轮的**第一选择**；不复位的话，"临时兜底"会变成"永久降级"，而且没有症状（只在账单和延迟里看得出来）。真实版还留了一条限流冷却：冷却期内故意**不回主**，但索引照样复位。

**可回退判据是二元的**（不是一张原因枚举表）：

- **可回退** = 限流 / 上游故障 / 凭据失效（`429` / `5xx` / `401` / `403` 一类）—— 换个 provider **可能**修好；
- mini 将 `400` / `404` / `422` 作为不可回退状态；上游则按错误分类器判断。404 可能是端点或模型不存在，不能断言换任何供应商都一样。

★ **为什么 400 不换 provider**：`400` 是"请求格式/参数不合法"，对请求本身的确定性问题，原样换供应商通常无助于修复；但 400 也可能是某家协议或能力特有的问题，不能仅凭状态码断言另一家必然失败，还白搭一次调用；更坏的是，把确定性错误当成可回退，会**把真问题藏进重试里** —— 用户看到的是"一直在切换供应商"，而根因（消息序列违规、参数错）永远浮不出来。判据只有"换个 provider 有没有可能改变结果"这一条线。

**凭据隔离**（主/回退不会拿错 key）：每条回退条目**自己解析自己的 key**（内联 `api_key`，否则 `key_env` **经 `get_secret`**，绝不裸读 `os.getenv`）；建 client 时把它自己的 `base_url` / `api_key` 显式传下去。**provider 变了就清掉主凭据池**再挂回退池，防跨 provider 污染（否则 rate_limit/billing/auth 的恢复路径会去改**旧的**凭据、把回退的 `base_url` 覆盖回去）。

★ **一处上游文档与源码不符**（必须写清）：官方文档 §11.2 说"子代理委派继承父级供应商，但**不继承回退配置**"、"辅助任务不支持回退"。**源码不是这样**：

- **子代理默认继承父链**。`_resolve_child_fallback_chain`（`tools/delegate_tool_config.py:417`）的判据是：pinned 子代理（覆盖了 provider / 端点 / model）**不借**父链；**未 pinned 的子代理默认继承**父链（`fallback_providers` 缺省 / null 时）；只有**显式** `fallback_providers: []` 才把回退断掉。所以"不支持回退"的子代理 = **pinned 或显式 `[]` 的**，不是"所有子代理"。
- **辅助任务也有自己的回退**，只是发生位置不同 —— 它在**解析期**串行试链（见 §六①），不走 `_try_activate_fallback`，所以"没走主机制"被误读成了"没有回退"。

**② 读 Hermes 的代码**

`tools/delegate_tool_config.py:417`（`_resolve_child_fallback_chain`，这段注释逐字否掉了文档的说法）：

```python
def _resolve_child_fallback_chain(parent_agent, routing_cfg, pinned):
    """Fallback chain for a child, owned by the same config block as its route.

    Pinned children (provider, endpoint or model override) never borrow the parent chain;
    unpinned children inherit it when ``fallback_providers`` is absent/null. An explicit ``[]``
    disables fallback either way. Malformed entries are dropped by the canonical normalizer.
    """
    default = None if pinned else (getattr(parent_agent, "_fallback_chain", None) or None)
    declared = routing_cfg.get("fallback_providers") if isinstance(routing_cfg, dict) else None
    if declared is None:
        return default
    if declared == []:
        return None
```

要害：第一句注释就写明"**未 pinned 的子代理继承它**"，断链要**显式** `[]`。"子代理不支持回退"这个说法的正确版本是"**pinned 或显式 `[]` 的子代理**不支持回退"。

**③ mini 的实现（现状）**

`agent/fallback.py` **269 行**。判据二元且**只存一份**：`FALLBACK_STATUS`（`fallback.py:51`）与 `NON_FALLBACK_STATUS`（`fallback.py:57`），后者旁边逐条注明 `400` / `404` / `422` **为什么**不可回退。状态按真实版的形状**直接挂在 agent 实例**上（`_fallback_chain` / `_fallback_index` / `_fallback_activated`），观察面因此是**可断言的普通属性**：

- `install_chain`（`fallback.py:95`）建链并复位；`None` = 读 config、`[]` = 显式禁用（真实版同款语义）；
- 索引**一次前进一格**：`activate_fallback` 的 `while` 里先推进再解析 ——
  ```python
      while index < len(chain):
          entry = chain[index]
          index += 1
          agent._fallback_index = index              # ★ 先推进：失败也要消费掉这一格
          spec = resolve_entry(entry)
          if spec is None:
              continue
  ```
  （`fallback.py:245-251`。解析失败的候选**照样被消费掉**，所以复位前不可能被用第二次。）；latch 置位在 `fallback.py:255`；
- `restore_primary_runtime`（`fallback.py:183`）在**轮首**复位（`run_agent.py:378` 调用）；
- **原地换**的单一写入口是 `run_agent._apply_runtime`（`run_agent.py:706`）：换 provider / model / base_url / api_mode / api_key，同步 `_client_kwargs`，并带一项 `context_length`（换了 provider 就是换了模型，窗口是**模型的属性**）；
- **接线点只有一处**：`_post_chat_completions` 的失败分支（`run_agent.py:789-797`），判据是 `is_fallbackable_status(exc.code)` —— 真才换并**重发**，假就**直接抛**；
- **凭据隔离**：`resolve_entry_api_key`（`config.py:198`）逐条自解析自己的 key，作为 `explicit_api_key` 传给共享路由器 `resolve_provider`（`fallback.py:230`）。mini **没有**凭据池，所以"清主 pool"这一步**结构上不存在**（不是漏做）。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 触发点 | 6 个模块 / 11 处调用点（原因分类见附录 C） | 一处收口（`_post_chat_completions` 失败分支） |
| 判据 | 30+ `FailoverReason` 分类 + `should_fallback` 字段 | 二元（一个状态码集合，只存一份） |
| 连接缓存 | 换 provider 清 `_transport_cache`（连接复用带旧 host） | 每次请求新建连接，**结构上无缓存可清** |
| 凭据池 | provider 变则 `_rebind_fallback_credential_pool` | 无池；逐条自解析 key |
| 每轮复位 | `restore_primary_runtime` + 限流冷却 `_rate_limited_until` | 只有复位，**无冷却** |

---

## §八 配置层与别名归一

**① Hermes 怎么做的**

**配置**是用户在 config 里存下的意图；**解析**是运行时把它变成要 POST 的 URL/key。两者必须分开：前者是数据（一份、可去重、可校验），后者是行为（可被显式参数 / 环境变量覆盖）。配置层这一节管三个键族：

**回退链**有两个键：`fallback_providers`（**主源**，**保序**）+ legacy `fallback_model`（单个 dict 或 list）。合并后的链按 `(provider, model, base_url)` **去重**；legacy 条目在**主源之后**追加，且只在"不与前面任何条目同一条 route"时才并入。

★ 为什么"保序"是**契约**而不是实现细节：链是**按序消费**的（§七的索引一次前进一格），顺序变了**优先级就变了**。所以去重时**保留第一次出现的那条**，而不是"随便留一条"。

**校验**由 `_validate_fallback_model` 做结构检查；缺 `provider` 或 `model` 的条目在归一阶段**直接丢**（等于禁用那一条），真实版的报错文案带一句 `" — fallback will be disabled"`。为什么"丢"而不是"抛"：一条写作笔误不该让整条链失效 —— 这是配置校验的两种哲学里的**宽松**那一支。

**辅助键**：`auxiliary.<task>.provider` 缺省 `"auto"`，`auxiliary.<task>.model` 缺省 `""`（空 = 主模型）。

**别名归一**：`openai-codex` ↔ `codex`、`claude` → `anthropic`、`ollama` → `custom` 等，保证多种写法命中**同一个** canonical id —— 归一表在插件声明 + `_plugin_aliases()` 里，别处不许再拼字符串。

★ **为什么 config 必须压 env**：一个陈旧的 `export HERMES_INFERENCE_PROVIDER=…`（上一次 shell 里留下的）**不该**悄悄盖掉用户刚在 `hermes model` 里选好、存进 config 的端点。原文逐字：*"Config beats the env so chat uses the endpoint the user last saved, not a stale shell/.env override."*

**② 读 Hermes 的代码**

`hermes_cli/fallback_config.py:89`（`get_fallback_chain` 的 docstring）：

```python
def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the effective fallback chain merged across old and new config keys.

    ``fallback_providers`` remains the primary source of truth and keeps its order. Legacy
    ``fallback_model`` entries are appended afterwards unless they target the same
    provider/model/base_url route as an earlier entry. The returned list always contains fresh dict
    copies.
    """
```

要害：一段注释把四条规则一次说清 —— **主源**、**保序**、legacy **追加**、按**三元组**去重；末句 "fresh dict copies" 保证调用方**改不动**配置本体。

**③ mini 的实现（现状）**

`hermes_cli/config.py` **247 行**。**键定义集中一处**（`KEY_*` 常量），别处不许再拼字符串。回退链的读法在这里：

- `get_fallback_chain`（`config.py:170`）合并 `fallback_providers`（主源、**保序**）与 legacy `fallback_model`，按 `(provider, model, base_url)` 去重；
- `_iter_fallback_entries`（`config.py:130`）把原始配置摊成干净条目，**缺 provider/model 的直接丢**：
  ```python
          provider = str(entry.get(KEY_FALLBACK_PROVIDER) or "").strip()
          model = str(entry.get(KEY_FALLBACK_MODEL_NAME) or "").strip()
          if not provider or not model:
              continue
  ```
  （`config.py:152-155`。注释写明"丢"而不是"报错"的理由：一条笔误等于禁用那一条，不该让整条链失效。）；
- `resolve_entry_api_key`（`config.py:198`）逐条解析凭据（内联 `api_key` → `key_env` **经 `get_secret`**）；
- `get_auxiliary_config`（`config.py:222`）给 `auxiliary.<task>` 两个键补缺省值。

**别名归一**在 `providers/aliases.py`（**63 行**）。**config 压 env** 落在两处：名字层 `resolve_requested_provider`（`runtime_provider.py:255`）先 config 后 env；连接层的 model 一栏（`runtime_provider.py:390-394`）是 显式 → config → `MINI_MODEL` → 画像默认 —— 与名字层同一条顺序。坏配置**永不抛**（`config.py:96`：非 dict / 坏 JSON 按 `{}` 处理）。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 配置文件 | YAML（`config.yaml`） | JSON（`config.json`） |
| 结构校验 | `_validate_fallback_model` + issue 列表，报错带 "fallback will be disabled" | **无结构校验**，坏配置按空处理（不抛） |
| legacy 迁移 | 首次写入时迁移 `fallback_model` → `fallback_providers` | 只读兼容，**不迁移** |
| 别名表 | 插件声明 + `_plugin_aliases()` | `providers/aliases.py` 一张小表 |

---

## §九 为什么这层必须共享

**① Hermes 怎么做的**

同一份解析要服务**多个入口**：CLI（`hermes chat`）、TUI、网关（消息平台）、cron（在全新会话里跑）、ACP 编辑器会话，以及**辅助模型调用**。这几个入口的**触发时机、进程模型、有没有人坐在前面**都不一样，但"用哪个模型"的答案**必须一致** —— 不能出现"CLI 用的是 A 端点、网关用的是 B 端点、cron 又悄悄回落到 C"这种局面。所以解析逻辑被抽成**一层共享运行时**，而不是各入口各写一遍。

运行中的 `/model` 切换也照同样的思路抽成共享的 `switch_model()` 函数（`hermes_cli/model_switch.py`），内部固定四步：`route`（PATH A 显式 `--provider` / PATH B 从 model 输入）→ `_resolve_switch_credentials` → `_validate_switch` → `_build_switch_result`。**共用的接缝就是这个函数本身**：CLI 端与网关端都只是"喂种子 + 收结果"，各自只做**表面动作**（`agent.switch_model()` 原地换 + `persist_model_selection()` 落盘），**没有一家重新实现解析**。把切换逻辑集中成函数（入参是当前 provider/model/凭据 + flag，出参是结果对象）的好处是：它**不依赖任何一家的会话状态**，所以两家能喂同一份入参、拿同一份结果。

再往前一层：辅助任务与**回退链**都从**同一个** provider 路由器过（注释称它 "centralized provider router"）。好处具体是三条：认证 / `base_url` / headers / `api_mode` 只在一处映射，**不重复 provider→key 映射**；走 scope-aware 的 `get_secret`（多 profile 下不会串味）；MoA / anthropic-wire / Codex 适配只维护一处。**不共享的代价是可预见的漂移**：有的入口记得 host 门控、有的忘了，凭据就可能发错端点 —— 这类 bug 没有症状，只有一个泄漏面。

**② 读 Hermes 的代码**

`agent/auxiliary_client.py:4411`（"centralized provider router" 那段注释）：

```python
# Centralized Provider Router: resolve_provider_client() is the single entry point for building a configured
# client (auth, base URL, headers, API format) from (provider, model). Never read auth env vars ad-hoc.
```

要害：两句把"唯一入口"和"别 ad-hoc 读 auth env"写在同一处 —— 共享这件事**不是性能优化，是防泄漏的前提**；一旦允许各自读 env，§六的辅助路由与 §七的回退链就都会各拿一份可能不对的凭据。

**③ mini 的实现（现状）**

mini 的共享接缝在**连接层** `resolve_provider`（`hermes_cli/runtime_provider.py:344`）—— 生产入口都调它这一处：CLI（`cli.py:85`）、网关（`gateway/run.py:133`）、doctor（`doctor.py:76`）、**辅助路由**（`agent/auxiliary.py:130`）、**回退链**（`agent/fallback.py:230`）。回退链正是从它过的：

```python
        return resolve_provider(provider, model,
                                explicit_api_key=explicit_api_key,
                                explicit_base_url=explicit_base_url)
```

（`fallback.py:230-232`。`resolve_entry` 的注释明说："走的是**同一个** provider 路由器……回退链与主解析共用一处认证 / base_url / api_mode 映射，绝不各调一套"。）数据层也共享：`providers/` 注册表是**唯一事实源**，`hermes_cli/config.py` 是配置的**唯一读法**。

**门面 `resolve_runtime_provider`（`runtime_provider.py:417`）已建，但当前只有测试在调** —— 生产入口仍**直接**调连接层 `resolve_provider`。也就是"五入口共用一个门面"这件事 mini 是**结构在、接线未搬**：门面存在且被契约测试守着，但 CLI/网关那几处还没迁到它上面。

mini **没做**运行中 `/model` 切换：模型选择是**启动参数**（`--provider` / `--model`）+ config，**没有运行中切换的消费者** —— CLI 只在构造 `AIAgent` 时解析一次，网关没有 `/model` 斜杠命令。一个没有调用者的 `switch_model()` 纯函数在 mini 里就是一句空承诺（`run_agent.py:14` 那处"承诺了却跑不起来"的教训）。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 共享门面 | `resolve_runtime_provider()` 被五入口 + aux 共调 | 门面已建，**生产入口仍直接调连接层** |
| 运行中 `/model` | 纯函数 `switch_model()`，CLI 与网关共用接缝 | **没有**运行中切换（无消费者） |
| 共享路由器 | `resolve_provider_client()` 一处映射 auth / URL / mode | `resolve_provider()` 一处（回退与 aux 都从它过） |

---

## §十 真问题与收据

**① Hermes 怎么做的**

这一节不是新机制，而是把真实版那几个"不做就会坏"的硬理由摆出来 —— mini 实现时踩到的坑基本都能追到它们：

- **凭据池重绑**：provider 变了必须换 pool，否则 `rate_limit` / `billing` / `auth` 的恢复路径会去改**旧的**凭据、并把回退的 `base_url` **覆盖回去**（同一 provider 的 pool 则保留）。
- **连接缓存**：换 provider 要清 `_transport_cache` —— 复用连接会把**旧 host** 的句柄带过去。
- **每轮复位**：临时兜底不能变永久降级（否则只在账单和延迟里看得出来）。

**② 读 Hermes 的代码**

`agent/chat_completion_helpers.py:1770`（`_rebind_fallback_credential_pool` 的 docstring）：

```python
def _rebind_fallback_credential_pool(agent, fb_provider: str, fb_model: str) -> None:
    """Rebind the credential pool when the provider changes (else rate_limit/billing/auth recovery
    mutates the wrong credentials and overwrites the fallback's base_url). Same-provider pool: kept."""
```

要害：把"不重绑"的后果压成一句话 —— 恢复路径会**改错凭据并覆盖回退端点**。这类 bug 没有症状，只有错账，所以它值得一条**注释加一条测试**一起守。

**③ mini 的实现（现状）+ 真跑收据**

**实现时炸过 / 发现的真问题**：

1. **回退条目的读凭据顺序**：条目用 `key_env` 声明凭据时，要**先 `load_env_file()` 再 `get_secret`** —— `.env` 里的值要灌进环境后才在那儿。顺序错的后果是"第一条回退因为密钥为空被**静默跳过**"，一个只出现一次的静默降级（`fallback.py:223-226` 的注释点了这条）。
2. **`MINI_MODEL` 的 config 压 env**：A9 的 model 一栏最初没有 config 层，`export MINI_MODEL=…` 会盖掉 config 里存的模型；补上 `get_configured_model()` 才对齐顺序（反向验证抓到：删掉这段，`★ config 压 env` 立刻变红）。
3. **"承诺了却跑不起来"**：`run_agent.py:14` 的 docstring 从第 2 章起就宣传"失败回退"，而 `fallback_providers` **全仓零调用点**。接线前那句注释是**假承诺**，必须连着实现一起改（现在 docstring 与实现一致，`test_docstring_promise_matches_implementation` 盯着）。

**本章交付时的离线运行收据**：

- **全套件双解释器 1,063 通过 / 0 失败**：`/usr/local/bin/python3`（3.13.14）与 `/usr/bin/python3`（3.9.6）**逐文件一致**（9 个测试文件；其中 `tests/test_fallback_and_aux.py` **113 条 / 4 组**是本批新增，逐个运行 `tests/test_*.py` 脚本汇总（通配符一次传给 python 不会依次执行多个脚本））。
- **`source` 四条来源实跑**（临时 home，只打印布尔/来源，不打印 key）：显式 →`source=explicit`；写 config →`config`；**config 与 env 同设 → 仍是 `config`（config 压 env）**；删 config →`env`；再去掉 env →`auto`。
- **host 门控实跑**：`key_candidates_for(base_url)` 对 `https://api.openai.com/v1` / `https://openrouter.ai/api/v1` / `https://api.deepseek.com/v1` 各自**只给该域名**的候选（deepseek 端点经 host 反推拿到 `DEEPSEEK_API_KEY`）。
- **别名归一实跑**：`claude→anthropic`、`codex→openai-codex`、`deep-seek→deepseek`、`ollama→custom`、`whatever→whatever`。

**④ 与真实版的差别**

| 面 | 真实 Hermes | mini 现状 |
|---|---|---|
| 回退测试 | 三处专项（凭据隔离 / `/fallback` 命令 / 网关淘汰） | 一处（`tests/test_fallback_and_aux.py`，113 条 / 4 组） |
| 断言口径 | — | 只写**契约/不变量**（一次前进一格 / 400 不回退 / 耗尽恒 False / 未配置 ≡ 主模型） |
| 真跑口径 | CLI / 网关 / cron 各自端到端 | 临时 home + 桩 `_http_post`，**不联网** |
| 缺口 | — | 静默降级无日志面（只有注释与"跳过条目"） |

---

## §十一 差距清单

抄 / 不抄的口径：只收**决定架构形状**的条目；纯细节与规模差距**当场写理由**。两个 commit：A1–A5 + A10 = `756b493`；A7–A9 = `a391919`。

### A 批 · 已抄（10 条）

| # | 条目 | mini 落点 | 状态 |
|---|---|---|---|
| A1 | 两段式解析（名字层 vs 连接层）+ 门面产出 dict | `hermes_cli/runtime_provider.py` | ✅ 已抄（`756b493`） |
| A2 | 注册表是唯一事实源 + 插件式懒注册 | `providers/` 包 | ✅ 已抄（`756b493`） |
| A3 | `api_mode` 按 host 判定 + 未实现模式明确报错 | `providers/api_mode.py` | ✅ 已抄（`756b493`） |
| A4 | 解析结果带 `source`（显式 / config / env / auto） | 三个解析函数 | ✅ 已抄（`756b493`） |
| A5 | host 门控的 key（每把 key 只发自己那条域名） | `providers/host_gate.py` | ✅ 已抄（`756b493`） |
| A6 | 凭据一律走 `get_secret`（scope-aware） | — | 已对齐（7.2 章） |
| A7 | 回退链接线：链 + 索引 + 回退状态标记 + 原地换 + 每轮复位 | `agent/fallback.py` | ✅ 已抄（`a391919`） |
| A8 | 辅助模型路由：`auxiliary.<task>.*` + `main` 别名 | `agent/auxiliary.py` | ✅ 已抄（`a391919`） |
| A9 | 配置层：provider / model / fallback / aux 的唯一读法 | `hermes_cli/config.py` | ✅ 已抄（`a391919`） |
| A10 | 别名归一（`codex` ↔ `openai-codex` 等） | `providers/aliases.py` | ✅ 已抄（`756b493`） |

### B 批 · 不抄（8 条，每条带理由）

| # | 条目 | 状态与理由 |
|---|---|---|
| B1 | 8 级 auto 阶梯全貌（pool / OAuth / free-tier / Bedrock） | ⏭ 不抄（基座）：mini 没有那些基座，抄了没消费者（只留"按 env key 自动选"一层） |
| B2 | 30+ `FailoverReason` 分类体系 | ⏭ 不抄（基座）：只抄**二元判据**（可回退 / 不可回退） |
| B3 | aux 的 7800 行适配器（vision / web-extract / skills-hub / MCP） | ⏭ 不抄（规模）：mini 只要一个消费者（压缩摘要） |
| B4 | 全部回退触发点照抄 | ⏭ 不抄（规模）：只抄一处收口（判据本身照抄） |
| B5 | secret pool / 多端点轮转 | ⏭ 不抄（基座）：mini 没有轮转的消费者 |
| B6 | `_API_KEY_BASE_URL_RESOLVERS` 特例映射表 | ⏭ 不抄（基座）：只为 kimi / zai / copilot / lmstudio / actual 几家 |
| B7 | 各家模型目录 / 别名清单（几十家） | ⏭ 不抄（规模）：mini 只注册 3 家画像 |
| B8 | 运行中 `/model` 切换流水线（`switch_model()` 四步） | ⏭ 不抄（复杂度）：mini 的模型选择是启动参数 + config，**没有运行中切换的消费者** |

---

## 附录 A · 从正文挪过来的引用（**全部逐字，行号在锚 `f5d1926110` 上实测**）

**解析层**

- `hermes_cli/runtime_provider.py:410-419 resolve_requested_provider()` —— 名字层四层顺序，注释把"为什么 config 压 env"写成了理由而不是规则：
  > `"""`Provider request from explicit arg, then config, then ``HERMES_INFERENCE_PROVIDER``, else`
  > `"auto". Config beats the env so chat uses the endpoint the user last saved, not a stale`
  > `shell/.env override."""`  ← 逐字（含 `auto` 的双引号）
- `hermes_cli/auth.py:1323-1338 resolve_provider()` —— auto 阶梯的入口：
  > `def resolve_provider(requested=None, *, explicit_api_key=None, explicit_base_url=None, skip_free_tier=False) -> str:`
- `hermes_cli/runtime_provider.py:249-254 _runtime()` —— **为什么解析结果是一个固定形状的 dict**（门面的六栏从这里来）：
  > `"""Build a resolved-runtime dict; ``extra`` carries source/requested_provider/provider-specific keys."""`
- `hermes_cli/runtime_provider.py:139-146 _fallback_api_mode()` —— `api_mode` 的判定顺序，注释里带一条真实事故：
  > `URL detection (host-mandated wire shapes) first, then the transport the provider overlay`
  > `declares via ``providers.determine_api_mode`` (``openai-api`` pointed at us.api.openai.com`
  > `400'd on every tool call without it), then ``chat_completions``.`
- `hermes_cli/runtime_provider.py:88-99 _HOST_MANDATED_API_MODES` —— "按 host 判定协议"的真值表：
  > `# api.meta.ai — prompt caching only on Responses;`
  > `# api.router.com — /v1/chat/completions is a minimal shim; api.anthropic.com — native Messages.`

**注册表与插件**

- `providers/__init__.py:14-27`（模块 docstring）—— 懒发现 + **last-writer-wins**：
  > `Discovery is lazy: the first call to ``get_provider_profile()`` or ``list_providers()`` scans both`
  > `locations and imports every plugin. User plugins override bundled plugins on name collision`
  > `(last-writer-wins), so third parties can monkey-patch or replace any built-in profile without`
  > `editing the repo.`
- `hermes_cli/auth.py:269-273` —— `env_vars` 注册时被**拆成两半**：
  > `is_url = lambda v: v.endswith("_BASE_URL") or v.endswith("_URL")`
  > `next((v for v in pp.env_vars if is_url(v)), None) or "")`

**自定义端点**

- `hermes_cli/runtime_provider_custom.py:163-179 _get_named_custom_provider()` —— 具名自定义端点的查表顺序（先新式 `providers:` 再 legacy `custom_providers:`，且**内置优先**）：
  > `if not requested_norm or requested_norm == "auto" or _shadowed_by_builtin(requested_norm): return None`
  > `found = _match_new_style_provider(requested_norm, providers) if isinstance(providers, dict) else None`

**辅助与回退**

- `agent/auxiliary_client.py:544-548` —— `"main"` 是**别名**（解析成主 provider；注释给了理由：让具名自定义端点也能用）：
  > `if normalized == "main":`
  > `    # Resolve to the actual main provider so named custom providers work.`
- `agent/chat_completion_helpers.py:1968-1976` —— 激活回退时的**原地替换**清单 + 一条修过的真 bug（`context_length` 覆盖没清，见 #22387）：
  > `# Clear the per-config context_length override so the fallback model's own context`
  > `# window is resolved instead of the previous model's stale value.`
  > `agent.model, agent.provider, agent.requested_provider = fb_model, fb_provider, fb_provider`
- `hermes_cli/fallback_config.py:89-92 get_fallback_chain()` —— 新旧配置键的合并规则（`fallback_providers` 是主源且保序）：
  > `"""Return the effective fallback chain merged across old and new config keys.`
  > ``fallback_providers`` remains the primary source of truth and keeps its order.
- `tools/delegate_tool_config.py:417-426 _resolve_child_fallback_chain()` —— **子代理到底继不继承父链**（附录 C ① 的出处）：
  > `Pinned children (provider, endpoint or model override) never borrow the parent chain;`
  > `unpinned children inherit it when ``fallback_providers`` is absent/null. An explicit ``[]```
  > `disables fallback either way.`

---

## 附录 B · 三方对照矩阵（精译节 ↔ 真实侧 ↔ mini 落点）

| 精译节 | 真实侧（文件:函数） | mini 落点 | 状态 |
|---|---|---|---|
| 0. 这一章在回答什么 | 整层 | `providers/` + `hermes_cli/runtime_provider.py` | ✅ 已抄（`756b493`） |
| 1. reasoning 形状 | `agent/chat_completion_helpers.py` 的 reasoning 回显 | `run_agent.py` 的 `reasoning_content` 回显开关 | ✅ 已对齐（第 2 章） |
| 2. 解析优先级（四层） | `runtime_provider.py:410-419` + `auth.py:1323+` | `resolve_requested_provider()` / `resolve_provider()` | ✅ 已抄（`756b493`） |
| 3. 供应商清单 | `providers/` + **39** 个内建供应商目录 | `providers/builtin/`（3 家兜底画像） | ⬜ 没做（量级：39 → 3） |
| 4. 解析结果长什么样 | `runtime_provider.py:249-254 _runtime()` | 门面基础四栏与按路径附加的元数据（不保证每条路径都有六栏） dict | ✅ 已抄（`756b493`） |
| 5. 为什么要有这一层 | 全层 | 同上（`source` 栏就是把"为什么"落成数据） | ✅ 已抄（`756b493`） |
| 6. AI Gateway（Vercel） | `_API_KEY_BASE_URL_RESOLVERS` 等 | 无 | ⏭ 不抄（① 基座：mini 无该中转） |
| 7. 三把 key 各管自己的域名 | `runtime_provider.py:317-326` + `utils.py:612-620` | `providers/host_gate.py` | ✅ 已抄（`756b493`） |
| 8. Anthropic 原生路径 | `auth.py` 的 Claude Code 可刷新凭据优先级 | 画像支持 `anthropic_messages`；凭据链没做 | ⬜ 没做（① 基座：无 OAuth/可刷新凭据） |
| 9. OpenAI Codex 路径 | `auth.py` + `providers/` 的 codex 画像 | 只做别名归一（`openai-codex` 不注册画像） | ⏭ 不抄（① 基座，**不假装支持**） |
| 10. 辅助模型的独立路由 | `auxiliary_client.py:5099 resolve_provider_client()` / `:544` | `agent/auxiliary.py`（只接压缩摘要） | ✅ 已抄（`a391919`） |
| 11. 回退模型链 | `agent/turn_*.py` 调用点 + `chat_completion_helpers.py:1968+` | `agent/fallback.py` | ✅ 已抄（`a391919`） |
| 12. 相关文档 | — | — | — |
| 附录 A 术语表 | — | `main` = 主 provider 别名 | ✅ 已对齐 |
| 附录 B 文件对照 | `runtime_provider.py` **953** / `auth.py` **2,231** / `model_switch.py` **1,679** / `auxiliary_client.py` **7,800** | `providers/` **723** / `runtime_provider.py` **441** / `fallback.py` **269** / `auxiliary.py` **147** / `config.py` **247** | — |
| 附录 C 三处出入 | 见本附录 C | — | — |

---

## 附录 C · 三处上游文档说的与源码不符（读源码取证时发现）

正文按原文照译，这里给事实。三条都**不影响主结论** —— 文档的机制描述是对的，只是三处措辞比源码松。

**① 「子代理与辅助任务不继承回退链」—— 源码是"**未 pinned 就继承**"**

- **文档怎么说**：回退链不适用于子代理与辅助任务。
- **源码怎么算**：`tools/delegate_tool_config.py:417-426 _resolve_child_fallback_chain()` 的 docstring 逐字（附录 A 有全文）：pinned 子代理（自己覆盖了 provider / 端点 / model）**不借**父链；**未 pinned 默认继承**；显式 `[]` 断链。辅助任务不是"没有回退"，而是走**解析期**它自己那条链，不走主 agent 的运行时机制。
- **对学习者意味着什么**：判据是"**这条链归谁管**"——归同一配置块（route 的所有者）管，不是"子代理一律没有"。调 `delegation.*` 时按 pinned 与否理解。

**② 「三处触发点」—— 实测 **6 个模块 / 11 处调用点****

- **文档怎么说**：`_try_activate_fallback()` "is called from three places in the turn phases"（`turn_api_error.py` / `turn_response_check.py` / `turn_recovery.py`）。
- **源码怎么算**：这三处之外还有 `agent/turn_api_call.py:258`、`agent/turn_empty_response.py:271`、`agent/turn_truncation.py:213` 与 `:600`；`turn_api_error.py` 自己就有 `:330` 与 `:359`。上游把"至少 5 处"写成了断言：`tests/agent/test_prompt_cache_ttl_propagation.py:256` —— `assert seen >= 5, "expected the retry-loop _try_activate_fallback sites"`。
- **对学习者意味着什么**：文档那三条是**按原因归纳的三类**，不是调用点清单。要改回退行为时**按原因找判据，别按位置数调用点** —— 这正是 mini 把判据收敛到 `agent/fallback.py` 一处的理由。

**③ 「`OPENAI_API_KEY` 用于自定义端点，并作为兜底」—— 源码比这句话严格**

- **文档怎么说**：三把 key 里 `OPENAI_API_KEY` 兼作"自定义端点/兜底"。
- **源码怎么算**：`runtime_provider.py:317-326 _host_gated_env_key_candidates()` 把三把 key **都只挂自己的权威域名**，其余给空串；自定义端点走 `_host_derived_api_key()`（从 host 反推 `<VENDOR>_API_KEY`）。docstring 附安全理由：*"Sending OPENAI/OPENROUTER/OLLAMA keys to an unrelated endpoint leaks credentials (GHSA-76xc-57q6-vm5m); match on HOST, not substring."*
- **对学习者意味着什么**："兜底"= **按域名推导出来的那把 key**，不是"把 OpenAI 的 key 发给任意端点"。mini 照抄了这一条（`providers/host_gate.py`；实跑：`openrouter.ai` 只发 OPENROUTER、`api.openai.com` 只发 OPENAI、陌生 host 一把都不发）。

### 后续维护：Web 的显式 DeepSeek 连接

Web 模型设置是现有凭据解析的一个内存入口：通过 `set_secret_scope` → `get_secret` → `resolve_provider` 复用连接层，并显式固定官方端点，不修改进程环境或 `.env`。真实 Web 回合禁用自动回退，防止演示时切到另一个服务商；DeepSeek 请求显式关闭思考模式，所需可选 `thinking` 参数在 Agent 传输层装配。CLI 默认请求不增加该字段。完整用法与边界见 [Web 演示说明](WEB_DEMO.md)。

### 后续维护：可操作的章节演示

Web Provider 章以固定传输注入 400/429/503，真实运行 Agent 的回退状态机，展示请求尝试、独立 api_request_id 和实际结局，不向真实服务注入错误。 操作与验证边界见 [逐章演示指南](WEB_CHAPTER_DEMOS.md)。
