> **上游锚**：`f5d1926110`（Hermes `v0.21.3`）。本文所有 `文件:行号` 都对这个 commit。
> **真实源码**：`$HOME/.hermes/hermes-agent/`（`gateway/` 包 = 160 个 `.py` / 86,785 行）
> **本文状态**：🧱 **施工中** —— 这里先承接从《中文精译-09》搬出的【源码要点】（按"精译只做翻译"搬出，
> 与精译 §1~§13 一一对应）。差异表 / 逐项「Hermes → 读源码 → mini → 为什么不做」/
> 差距清单 / 真问题 / 真跑收据，等第 9 章实现完成后再补。

[TOC]

---

# 源码要点：Hermes 的多路复用网关怎么实现的

> 来源：《中文精译-09》各节的 `### 源码要点` 块（2026-10-10 搬出，**内容未改**）。

## 1. Overview —— 一进程 vs 一 profile

### 源码要点
| 概念 | 对应实现 |
|---|---|
| "被服务的 profile" | `hermes_cli/profiles.py::profiles_to_serve(multiplex)`（§7） |
| 共用的一把锁 / 一个监听 / 一个状态面 | 进程级单例；状态面 `gateway_state.json`（⚠️ 原文写 `runtime_status.json`，代码里没有这个文件名 —— 精译附录 C ④） |
| 失败关闭的开关 | `agent/secret_scope.py::_MULTIPLEX_ACTIVE`（§4） |

## 2. The mode flag —— 开关与它的三道来源

### 源码要点
```text
gateway/config.py:45  _env_multiplex_profiles_override()   —— env 覆盖；空白/无法识别 → None
gateway/config.py:558 multiplex_profiles: Optional[bool] = None
gateway/config.py:703 # 注释：Nothing set stays None so the boot-time guard 能接管
hermes_cli/config_defaults.py:1998   "multiplex_profiles": True      ← 默认开
hermes_cli/gateway_multiplex_mode.py:110  def resolve_multiplex_mode(config) -> MultiplexDecision
agent/secret_scope.py:24   _MULTIPLEX_ACTIVE: bool = False   ← 模块级全局
agent/secret_scope.py:27   def set_multiplex_active(active: bool) -> None
```

**判决的三个理由**（`MultiplexDecision` 的 `reason`）：`"config"`（显式写了）/ `"guard"`
（预检发现障碍 → 关）/ `"default"`（未设置且无碍 → 开）。

**预检的作用**（原文 "keeps the gateway standalone when …"）：只要
① 还有**二级 profile 自己跑着网关**，② 存在**阻塞项**，或 ③ **这台机器迁移不了**，
就维持"一个 profile 一个进程"的老样子。预检自身崩了也**不会**拖垮网关 ——
它按"启动 standalone"处理并记日志（源码里那句：*a broken preflight must not take the
gateway down with it*）。

## 3. Scope composition —— 两个作用域是怎么套上去的

### 源码要点
```text
gateway/run.py            _profile_runtime_scope(profile_home)   ← 作用域的安装/解开
hermes_constants.py:25    set_hermes_home_override(path) -> Token
agent/secret_scope.py:80  set_secret_scope(mapping) -> Token
agent/secret_scope.py:49  _SECRET_SCOPE: ContextVar[Optional[Mapping[str, str]]]
```

📌 "contextvar + 线程池"这条为什么值得单独记：`ThreadPoolExecutor` **默认不继承**调用方的
上下文；要传就得显式 `ctx = copy_context()` 再 `executor.submit(ctx.run, fn)`。
所以"作用域漏了"这件事**不会报错**，只会表现为"某个后台任务读到了默认 profile 的值"。

## 4. Workstream A —— 上下文局部的密钥作用域

### 源码要点
```text
agent/secret_scope.py:52   class UnscopedSecretError(RuntimeError)
agent/secret_scope.py:98   _GLOBAL_ENV_EXACT = frozenset({ "HERMES_HOME", "HERMES_PROFILE",
                                 "HERMES_GATEWAY_LOCK_DIR", "API_SERVER_ENABLED", "API_SERVER_HOST",
                                 "API_SERVER_PORT", "API_SERVER_CORS_ORIGINS", … })
agent/secret_scope.py:126  _GLOBAL_ENV_PREFIXES = ( … 代理设置等前缀 … )
agent/secret_scope.py:143  def get_secret(name, default=None)   ← 白名单 → 作用域 → 兜底
agent/secret_scope.py:174  def get_secret_str(...)  # 逐字 docstring：
    """… Still raises UnscopedSecretError —— swallowing it hides a spawn-site bug."""
```

**出处**：`#77562`（加载器边界）、`#84079`（配置里的 `${VAR}` 解析）、`#88441`（作用域下的 `.env` 写入）。

## 5. The HERMES_HOME override —— 让"家"也跟着 profile 走

### 源码要点
```text
hermes_constants.py:25   def set_hermes_home_override(path) -> Token
hermes_constants.py:39   def get_hermes_home_override() -> str | None
hermes_constants.py:101  def get_hermes_home() -> Path        ← 覆盖优先于环境变量
hermes_constants.py:119  def hermes_home_key(path=None) -> str
hermes_constants.py:150  def get_process_hermes_home() -> Path
hermes_constants.py:94   "… (see issue #18594)."              ← 一次性告警正文里的出处
```

**出处**：`#18594`（一次性告警）。

## 6. Inbound routing —— 把共享 bot 收到的消息分给正确的 profile

### 源码要点
```text
gateway/profile_routing.py:56   class ProfileRoute
gateway/profile_routing.py:73   def matches(...)          ← 合取 + 最具体优先
gateway/profile_routing.py:130  def parse_profile_routes(raw) -> List[ProfileRoute]
gateway/profile_routing.py:166  def match_profile_route(...)
gateway/profile_routing.py:51   class ProfileRouteRejected(RuntimeError)   ← "拒绝"是有类型的
gateway/profile_routing.py:101  def _bot_profile_key(name)  ← 默认 profile 归一化（None）
```

## 7. Serving selected profiles —— 服务集合的唯一闸口

### 源码要点
```text
hermes_cli/profiles.py:802  def profiles_to_serve(multiplex: bool) -> List[Tuple[str, Path]]
    """``(profile_name, hermes_home)`` pairs a gateway should serve — the single chokepoint
    for "which profiles does the inbound gateway handle".

    ``multiplex=False``: exactly one entry for the *active* profile (byte-for-byte the
    historical single-profile behavior; name is ``"default"`` or the named profile's id).
    ``multiplex=True``: default plus every live named profile under ``profiles/`` (tombstoned
    profiles skipped). Pure directory read: never creates a profile dir (#94590)."""
```

> ⚠️ **上游文档出入**：原文这里写的是 `profiles_to_serve(multiplex, profile_allowlist)`，还说
> "格式错的 allowlist 会安全地回退成只有默认" —— **源码签名没有 allowlist 参数**（详见附录 C ①）。

📌 注意 docstring 里那句 **"Pure directory read: never creates a profile dir"** ——
"枚举"这件事本身**不许有副作用**（否则一个"列一下服务谁"的动作会凭空造出 profile 目录）。

**出处**：`#69377`（cron 心跳按 profile）、`#94590`（枚举不建目录）。

## 8. Per-profile persistence —— 每个 profile 自己的库

### 源码要点
```text
gateway/session.py         SessionStore 构造时不取 db handle；句柄按 home 解析并缓存
                           （结构上的抓手：`_routing_db_loaded` 这类"延迟加载"标记）
hermes_constants.py:101    get_hermes_home()          ← 句柄解析用的就是 §5 那个覆盖
```

**出处**：`#88532`（这条修复的存在证据在测试里很直白：`tests/hermes_state/test_named_profile_session_db.py`
的注释写 *"#88532 made SessionStore follow HERMES_HOME; this covers the TUI/agent …"*；
一致性用例 `tests/conformance/test_profile_write_tripwire.py` 也点了它，原文是
*"gateway sessions physically stored in the root `state.db` even …"* —— 即修复前的症状）。

## 9. Per-bot session lanes —— 每个 bot 一条会话线

### 源码要点
```text
gateway/platforms/base.py:1904  self._owner_profile: Optional[str] = None
gateway/run_adapters.py:1115    adapter.set_owner_profile(...)   ← 适配器配置时装上
gateway/platforms/base.py:2306  def _session_key_profile(self, source=None) -> Optional[str]
    逐字 docstring：
    """Profile namespace for an adapter-derived session key. Ingress … stamps ``source.profile``,
    so without this every bot in a multiplex… ``agent:main:`` lane. Order: ``source.profile`` →
    ``_owner_profile`` → store resolver; getattr-guarded (object.__new__ in tests), type-checked
    (… key)."""
```

## 10. Control plane —— 桌面插件怎么管 profile

### 源码要点
```text
tui_gateway/methods_profiles.py:248  @_profile_handler("profiles.list", 5061)
tui_gateway/methods_profiles.py:362  @method("profiles.create")
tui_gateway/methods_profiles.py:429  @_profile_handler("profiles.describe", 5063)
tui_gateway/methods_profiles.py:592  @_profile_handler("profiles.configure", 5064)
tui_gateway/methods_profiles.py:624  @_profile_handler("profiles.set_asset", 5065)
tui_gateway/methods_profiles.py:662  @_profile_handler("profiles.get_asset", 5066)
```

📌 括号里那几个数字（5061…5066）是**每个 handler 的兜底错误码**，不是行号 —— 名字叫
`_profile_handler(name, code)`，装饰器把"名字 + 编号"一起注册。

## 11. Failure modes —— 四种处置

### 源码要点
```text
gateway/run.py:1636              class MultiplexConfigError(RuntimeError)          ← 存在 ✓
gateway/run_adapters.py:1139     # "A secondary's port-binding adapter never binds:
                                 #  the default profile owns the one shared …"      ← 语义在
gateway/platforms/base.py:1905   # "Set by the runner on a secondary's port-binding adapter:
                                 #  serve via the default profile's …"
gateway/platforms/shared_ingress.py:4  # 枚举了要占端口的平台（Twilio SMS、LINE、Teams、
                                 #  BlueBubbles、Microsoft Graph …）
```

> ⚠️ **上游文档出入**：原文点名的启动致命错误 `SecondaryPortBindingConfigError` ——
> **源码里没有这个类**（机制在、名字没了，详见附录 C ②）。

**出处**：`#61276`（`/p/` 请求在无作用域时进默认 profile —— 这条只在测试里能找到：
`tests/gateway/test_api_server_multiplex_secret_scope.py` 的第一行就写着 *"Regression for #61276:
api_server agent entry under multiplex isolation."*）。

## 13. Non-goals —— 不是目标的东西

### 源码要点
这一节没有实现细节 —— 它是**边界声明**。对应的实现在别处：
授权（谁能跟这个 bot 说话）是第 7 章那套四层 `authz_mixin`。

