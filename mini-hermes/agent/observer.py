"""观察者（observer hooks）—— **只读遥测**在这里的落点（第 15 章）。

对应真实 Hermes 的两处：
  · 注册面 / 派发面 —— `hermes_cli/plugins.py`（2147 行）+ `hermes_cli/plugins_dispatch.py`（530 行）
  · 载荷安全 —— `agent/api_request_hooks.py`（201 行）

真实侧是一整套**插件宿主**（`VALID_HOOKS` 39 个钩子、超时隔离线程、插件 ledger、中间件层、
`hermes hooks` 运维 CLI…）。mini 只落**四件核心**，其余写进下面的"为什么不做"：

| mini | 是什么 | 真实侧锚 |
|---|---|---|
| A1 `ObserverRegistry` | 一张 `事件名 -> [回调]` 表 + 宽容注册契约 | `plugins.py:916-938` |
| A2 零成本快路径 | **fire-site 先问 `has()`**，没人听就连载荷都不构造 | `plugins.py:1738-1743` · `plugins_dispatch.py:445-447` |
| A3 失败放开 | 每个回调**独立** try/except，记警告后继续，绝不穿出轮次 | `plugins_dispatch.py:207-219` |
| A4 载荷安全三件套 | 键级脱敏 + JSON 化截断 + 总字符上限（两级降级） | `agent/api_request_hooks.py:52-132` |

## 事件表（7 个；★ 每个名字都有真实触发点）

| 事件 | mini 触发点（`run_agent.py`） | 载荷（mini 真有的字段） |
|---|---|---|
| `pre_llm_call` | `_run_conversation_turn` 每次模型调用**前** | `session_id task_id turn_id iteration model provider platform message_count` |
| `post_llm_call` | 该调用返回 / 失败 / 被中断**后**（`status` 区分） | 上列 + `status tool_call_count duration_ms` |
| `pre_api_request` | `_one_api_request` 每次**真发 HTTP** 前（含回退重发） | `session_id task_id turn_id api_request_id attempt api_mode model provider message_count` |
| `post_api_request` | 该 HTTP 响应回来后 | 上列 + `status usage duration_ms` |
| `api_request_error` | 该请求抛错时（回退重发也走这里） | 上列 + `error retryable status_code duration_ms` |
| `pre_tool_call` | `_run_one_tool` 工具执行前（agent 级工具 / 审批拒绝 / 参数解析失败三路都发） | `session_id task_id turn_id tool_call_id tool_name args` |
| `post_tool_call` | 工具执行后（`status ∈ ok/denied/error`） | 上列 + `status result error_type error_message duration_ms` |

载荷里每个字段都**照真实侧同名**（`agent/turn_context.py` 的 `pre_llm_call`、
`agent/turn_api_request.py` 的 `pre_api_request`、`agent/turn_response_intake.py` 的 `post_api_request`、
`agent/api_request_hooks.py:161-198` 的 `api_request_error`、`model_tools.py:691-695` 的 `post_tool_call`、
`hermes_cli/plugins.py:1817-1820` 的 `pre_tool_call`）。**mini 没有的字段一律不编**：

  · 真实侧 `pre_api_request` 带 `request=`（脱敏后的请求体）、`request_messages`、
    `system_prompt`、`approx_input_tokens`、`tool_count`、`started_at`… —— mini 不带
    `request=`：载荷一旦"每次请求都塞整份 messages"，总上限会稳定地把最有用的字段挤掉
    （截断后剩下的是被削平的字符串），且 mini 的历史本来就整份在内存里。
  · 真实侧 `post_llm_call` 只发 **一次/回合**（`agent/turn_finalizer.py:411-441` 的
    `_apply_output_hooks`，工具循环跑完才发）；mini 没有 `finalize_turn` 这个收尾层，
    所以取**每次模型调用**口径（与 `pre_llm_call` 成对）。⇒ **这是与真实侧的一处口径差异**，
    已记进第 15 章实现报告。
  · `platform` / `api_mode` / `base_url` 之类真实侧也发，mini 有对应属性就发（同名）。

## 分工（拍板 P4）：**不做会话级事件**

`session:start/end/reset` 这类**会话级**边界在 mini 由 `gateway/hooks.py`（目录式钩子，
用户写 `HOOK.yaml` + `handler.py`）承担，CLI 侧没有对应边界（`cli.py` 建会话行时不发钩子）。
本章的观察者只覆盖 **turn / API / 工具**三个 **agent 内**边界 —— 三层各有明确的所有者：

| 层 | 机制 | 谁配 | 语义 |
|---|---|---|---|
| 会话 / 进程 | `gateway/hooks.py`（目录式） | 用户（写目录） | 用户可替换的**外部**扩展点 |
| agent 内三边界 | **本模块**（注册表） | 进程内代码（CLI / 网关 / 测试） | **只读**遥测 |
| 改行为 / 阻断 | `run_agent.py` 的 `approval_callback` + 网关守卫 | 宿主 | 唯一能改控制流的入口 |

## 只读语义（A8）

  · **返回值一律忽略**：`emit()` 不看回调返回什么，也不把它交给任何人
    （真实侧 `plugins.py:138-142` 逐字：*returns ignored — plugins cannot veto or pre-answer*）。
  · 想在 mini 里**阻断/改写**一条工具调用，请走 `approval_callback`
    （`run_agent.py:984-990`）或网关守卫 —— 观察者**不是**第二套控制流。
  · 回调抛异常 ⇒ 记警告后继续（A3），**绝不**让异常穿出 agent 轮次。

## 零成本快路径（A2 · 本章★核心）

真实侧的 `invoke_hook()` **内部没有** early-return（它总会先解析超时配置再遍历回调）；
真正的零成本快路径在 **fire-site 用 `has_hook()` 短路** —— 权威注释在
`hermes_cli/kanban_db_dispatch.py:1197-1201` 逐字：

    Cost rule: every call site short-circuits on has_hook(), so when nothing
    subscribes no payload is built and the hot paths … pay one dict probe.

mini 照这个形状：**调用点先 `if self._observers.has("<事件>"):`，再构造载荷并 `emit(...)`。**
⇒ 没有观察者时，`_payload_*` **一次都不会被调用**（`tests/test_observers.py` 第 ② 组把这条
"载荷构造 0 次"变成断言；主代理的反向验证会删掉调用点那道门，让那条测试变红）。
`emit()` 内部**也**再探一次（防御性：有人边跑边清空注册表），但没有回调时它立刻返回。

★ 这里**不做**身份比较（`cb is not NoOp.cb` 那种套路）：真实侧的 `has_hook` 就是一次
`bool(self._hooks.get(hook_name))`（`plugins_dispatch.py:447`），无缓存、无身份比较、无标志位。
"A8/A9 可见性同源"也指这条：**"要不要发/要不要显示"只有一个事实源 = `has()`**，不另立标志位。

## 注册契约（A1）

  1. **未知事件名：只警告、仍然存**（不 raise、不丢弃）。警告文案带**全部**合法名 ——
     拼错能被发现，但永不因一次注册把进程炸掉。
     真实侧 `plugins.py:916-918` 逐字：*unknown names warn but are still stored*。
  2. **不去重**：同一事件登记同一回调两次 ⇒ 它被调**两次**（真实侧无条件 `append`）。
  3. **保序**：按登记顺序调用（真实侧 `plugins_dispatch.py:207` 直接顺序遍历）。
  4. `registered_events()` / `callback_count()` / `count` 是**只读**查询（供测试与工具面用）。

## 载荷安全三件套（A4 · 与 `agent/api_request_hooks.py:52-132` 逐字对齐）

  ① **敏感键**：`_SENSITIVE_HOOK_KEYS`（`api_key` / `authorization` / `proxy_authorization` /
     `cookie` / `set_cookie`）+ 后缀 `*_api_key`；判定前先 `lower().replace("-", "_")`。
     命中 ⇒ 值替换为 `"<redacted>"`（真实侧的标记逐字就是它）。
     ★ **键级之外还要一层文本级**：键级只挡"字段名就叫 api_key"的载荷，而**值里**的凭证
     （工具结果、`args` 里的一条 `ghp_…`、发到 stderr 再被观察者抓走的 `Authorization: Bearer …`）
     键名完全正常。所以**:字符串值再过一遍第 5 章的 `redact_sensitive_text`
     （`agent/context_compressor.py:139`，7 条模式）**。真实侧同样两层：键级在
     `_is_sensitive_hook_key`，文本级在 `agent/redact.py`（850+ 行）——mini 用第 5 章已有的
     最小子集。⇒ 这层是 mini **按差距清单 #6 接上的**，不是真实侧 `_hook_jsonable` 的行为。
  ② **JSON 化截断**：`max_depth=8 / max_string=8000 / max_sequence=200`（默认参逐字同值）。
     ★ 语义差异照抄不改：**dict** 溢出时 `break`（后面的键**全丢**，只留 `_truncated_items` 计数），
     **list/tuple/set** 溢出时**切片保留前 N 个**再**追加**一条 `{"_truncated_items": 剩余数}`。
  ③ **总字符上限**：默认 50000，env `MINI_OBSERVER_PAYLOAD_MAX_CHARS` 可覆盖，**下限 1000**
     （`max(1000, int(raw))`），坏值回落默认。超限走**两级降级**：
     全量 → `max_string=1000 / max_sequence=50` → 还超就整份换成
     `{"_truncated": True, "original_type": …, "preview": JSON 前缀[:limit]}`。

## 关联 id（A5）

`CallIds`（冻结 dataclass，5 字段）+ `hook_kwargs()`（**None → ""**）—— 形状照
`model_tools.py:643-653`（逐字注释 *hook/middleware wire contract*）。生成点：

  · `turn_id`：**每轮**一个（`run_conversation` 入口），同一轮内所有事件**相同**、跨轮不同。
  · `api_request_id`：**每次真发 HTTP** 一个（mini 有回退链 ⇒ 一次 LLM 调用可能多次请求）。
  · `tool_call_id`：**优先用 provider 给的**（消息里的 `tool_calls[i].id`）。
  · `session_id` / `task_id`：沿用 agent 既有的 `self.session_id` / `self._current_task_id`。

## 版本字段（A6）

每份载荷在**唯一装配出口**（`emit()`）注入 `telemetry_schema_version = "mini.observer.v1"`
—— 照 `plugins_dispatch.py:200-201` 的 `kwargs.setdefault("telemetry_schema_version", …)`
（真实值 `hermes.observer.v1`，见 `hermes_cli/middleware.py:16`）。**调用点一律不手写它。**

## 为什么不做（B 批 · 逐条对着真实侧说"为什么这里没有它"）

  · **B1 超时隔离线程**（`plugins_dispatch.py:246-321` + `_HOOK_TIMEOUT_*` 三张表 + 60s 抑制窗口）：
    mini 没有"钩子跑在工作线程里"这个基座（观察者是**同步**调用的），抄过来就是 ~75 行
    死代码 —— 没有线程就永远不会超时。
  · **B2 `pre_tool_call` 的 block/approve 指令**：那是**改行为**，mini 的审批在
    `run_agent.py` 的 `approval_callback`（一处控制流）；引第二套会立刻出现"两处都能否掉工具、
    谁先谁后"的歧义。
  · **B3 抑制窗口 / gate_key / `_hook_abandoned`**：整族依赖 B1。
  · **B4 流式观察者**（`agent/plugin_stream_hooks.py` 165 行）：mini 没有流式输出。
  · **B5 事件总线**（emit/subscribe、递归深度 cap、pending budget）：不是 observer hooks 的核心能力。
  · **B6 系统提示词 section 注册**：另一个特性（prompt 字节注入），与派发无耦合。
  · **B7 `hermes hooks` 运维 CLI**（`hermes_cli/hooks.py` 335 行）：config-owned 的 shell hooks
    运维面 + 测试夹具，不属 observer 通道（它复用的只是 `VALID_HOOKS` 当白名单）。
  · **B8 注册 ledger / 可撤销句柄**（`PluginRegistration`）：mini 不卸载插件 ⇒ 句柄无收益。
  · **B9 参数深拷贝 / 线程安全**：观察者是只读的，载荷在装配时已 JSON 化（不可变副本）。
  · **B10 `VALID_HOOKS` 全 39 名**：只落本章真接线的 7 个 —— 表里没有触发点的名字
    等于给读者一个"能用"的假承诺。
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import suppress
from dataclasses import asdict, dataclass, is_dataclass
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

# ★ 文本级脱敏复用第 5 章那一份（`agent/context_compressor.py:139`）。
#   模块级 import 是安全的：`agent.context_compressor` 不 import `run_agent`
#   （已实测 `import agent.context_compressor` 后 `'run_agent' not in sys.modules`），无环。
from agent.context_compressor import redact_sensitive_text

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════════
# 事件名表 —— mini 的 VALID_HOOKS（真实侧 39 个，这里只有**真接线**的 7 个）
# ════════════════════════════════════════════════════════════════════════════
OBSERVER_EVENTS: Tuple[str, ...] = (
    "pre_llm_call",
    "post_llm_call",
    "pre_api_request",
    "post_api_request",
    "api_request_error",
    "pre_tool_call",
    "post_tool_call",
)

#: 注册校验用的集合（真实侧 `VALID_HOOKS`，`hermes_cli/plugins.py:108-199`）。
VALID_EVENTS = frozenset(OBSERVER_EVENTS)

TELEMETRY_SCHEMA_VERSION = "mini.observer.v1"
TELEMETRY_SCHEMA_VERSION_KEY = "telemetry_schema_version"

# ════════════════════════════════════════════════════════════════════════════
# 载荷安全阈值 —— 逐字对齐 `agent/api_request_hooks.py`
# ════════════════════════════════════════════════════════════════════════════
#: 敏感**键**（`api_request_hooks.py:15` 逐字）。
_SENSITIVE_HOOK_KEYS = {"api_key", "authorization", "proxy_authorization", "cookie", "set_cookie"}

#: 命中后替换成什么（真实侧 `:95` 用的就是这个字面量）。
REDACTED_MARKER = "<redacted>"

JSONABLE_MAX_DEPTH = 8        # `:68` 默认参
JSONABLE_MAX_STRING = 8000    # `:68` 默认参
JSONABLE_MAX_SEQUENCE = 200   # `:69` 默认参

#: mini 自己的 env 名（同名会与真实 Hermes 的 `HERMES_PLUGIN_PAYLOAD_MAX_CHARS` 打架）。
PAYLOAD_MAX_CHARS_ENV = "MINI_OBSERVER_PAYLOAD_MAX_CHARS"
PAYLOAD_MAX_CHARS_DEFAULT = 50000   # `:56`
PAYLOAD_MAX_CHARS_FLOOR = 1000      # `:55` 的 `max(1000, …)`


def payload_max_chars() -> int:
    """载荷总字符上限：env 覆盖（下限 1000），坏值回落默认。

    逐字对齐 `agent/api_request_hooks.py:51-57`：`max(1000, int(raw))`，
    `TypeError` / `ValueError` 一律回落到 50000。
    """
    raw = os.getenv(PAYLOAD_MAX_CHARS_ENV, str(PAYLOAD_MAX_CHARS_DEFAULT))
    try:
        return max(PAYLOAD_MAX_CHARS_FLOOR, int(raw))
    except (TypeError, ValueError):
        return PAYLOAD_MAX_CHARS_DEFAULT


def is_sensitive_key(key: Any) -> bool:
    """这个**键名**是不是凭证类？（`api_request_hooks.py:59-64` 逐字）

    先 `lower().replace("-", "_")` 再判 —— 于是 `API-Key` / `Api_Key` 都命中；
    除了固定键集，还有后缀规则 `*_api_key`（`openai_api_key` / `deepseek_api_key`）。
    """
    if not isinstance(key, str):
        return False
    lowered = key.lower().replace("-", "_")
    return lowered in _SENSITIVE_HOOK_KEYS or lowered.endswith("_api_key")


def hook_jsonable(
    value: Any,
    *,
    depth: int = 0,
    max_depth: int = JSONABLE_MAX_DEPTH,
    max_string: int = JSONABLE_MAX_STRING,
    max_sequence: int = JSONABLE_MAX_SEQUENCE,
) -> Any:
    """把任意值转成"能 JSON 化、且不会爆炸"的形状（`api_request_hooks.py:66-114`）。

    逐条对齐，**包括两处容易抄错的地方**：

      · dict 溢出 `break`（**后面的键全丢**，只留一条 `_truncated_items = 总数 - 上限`）；
      · list/tuple/set 溢出**保留前 N 个**，再**追加**一条 `{"_truncated_items": 剩余数}`。
        ⇒ 这两个容器的"溢出语义"**不同**，照抄（真实侧就是不同）。

    ★ mini 的**唯一**加法：字符串值先过 `redact_sensitive_text`（文本级脱敏，键级之外的那层）。
      代价是这条正则跑在**截断之前**的整串上（O(原长)）；换来的是"值里的凭证也不出门"。
    """
    if depth > max_depth:
        return f"<{type(value).__name__} depth limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        value = redact_sensitive_text(value)
        if len(value) > max_string:
            return value[:max_string] + f"...[truncated {len(value) - max_string} chars]"
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"

    def recurse(item: Any) -> Any:
        return hook_jsonable(
            item,
            depth=depth + 1,
            max_depth=max_depth,
            max_string=max_string,
            max_sequence=max_sequence,
        )

    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= max_sequence:
                out["_truncated_items"] = len(value) - max_sequence
                break
            str_key = str(key)
            out[str_key] = REDACTED_MARKER if is_sensitive_key(str_key) else recurse(item)
        return out
    if isinstance(value, (list, tuple, set)):
        seq = list(value)
        out_list = [recurse(item) for item in seq[:max_sequence]]
        if len(seq) > max_sequence:
            out_list.append({"_truncated_items": len(seq) - max_sequence})
        return out_list
    # SDK / 模型对象归一化（mini 没有 SDK，但保留这四条：调用方塞进来的自定义对象
    # 走 `__dict__` 总比 `str(obj)` 那种"一串无信息的 repr"有用）。
    with suppress(Exception):
        if hasattr(value, "model_dump"):
            return recurse(value.model_dump())
    with suppress(Exception):
        if is_dataclass(value):
            return recurse(asdict(value))
    if isinstance(value, SimpleNamespace):
        return recurse(vars(value))
    if hasattr(value, "__dict__"):
        with suppress(Exception):
            return recurse({k: v for k, v in vars(value).items() if not str(k).startswith("_")})
    return str(value)[:max_string]


def sanitize_payload(value: Any) -> Any:
    """JSON 化 + 总上限：**全量 → 降级 caps → 截断预览**（`api_request_hooks.py:117-132`）。

    三级（真实侧同一套）：
      1. 默认 caps（depth 8 / string 8000 / sequence 200）下 `json.dumps` 后长度 ≤ 上限 ⇒ 原样返回；
      2. 否则把 caps 收到 `max_string=1000 / max_sequence=50` 再来一次，够小 ⇒ 返回**降级版**；
      3. 还超 ⇒ 整份换成 `{"_truncated": True, "original_type": …, "preview": 前缀[:limit]}`。

    ★ 超限的处置是**降级 + 预览**，不是丢弃：观察者拿到的永远是一份能读的东西
      （哪怕只剩前缀），而不是"看起来没数据"的空载荷。
    """
    limit = payload_max_chars()
    encoded = ""
    for caps in ({}, {"max_string": 1000, "max_sequence": 50}):
        payload = hook_jsonable(value, **caps)
        try:
            encoded = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:                       # noqa: BLE001 —— 编码不了就给前缀，别抛
            return str(payload)[:limit]
        if len(encoded) <= limit:
            return payload
    return {"_truncated": True, "original_type": type(value).__name__, "preview": encoded[:limit]}


# ════════════════════════════════════════════════════════════════════════════
# 关联 id —— 线协议形状（`model_tools.py:643-653` 同形）
# ════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class CallIds:
    """一次调用（工具 / API 请求）的身份字段，跟着载荷线协议走。"""

    task_id: Optional[str] = None
    session_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    turn_id: Optional[str] = None
    api_request_id: Optional[str] = None

    def hook_kwargs(self) -> Dict[str, str]:
        """Same fields with None -> "" (hook/middleware wire contract).

        ★ 为什么"空"是 `""` 而不是 `None`：观察者拿到的永远是可以直接拼字符串/写库的值；
          "没有这个 id"与"id 是空串"在**只读遥测**里是同一件事，不必让每个消费者各写一次
          `or ""`（真实侧逐字注释同上）。
        """
        return {k: v or "" for k, v in asdict(self).items()}


# ════════════════════════════════════════════════════════════════════════════
# 注册表
# ════════════════════════════════════════════════════════════════════════════
class ObserverRegistry:
    """`事件名 -> [回调]` 表（真实侧 = `PluginManager._hooks`，`plugins.py:1152`）。

    接口只有三个动词：`add`（登记）· `has`（**唯一**的"有没有人听"）· `emit`（派发）。
    """

    def __init__(self) -> None:
        #: 保序、允许重复（**不去重**）—— 用 list 而不是 set 就是为了这两条。
        self._callbacks: Dict[str, List[Callable[..., Any]]] = {}

    # ------------------------------------------------------------------ 登记
    def add(self, event: str, callback: Callable[..., Any]) -> None:
        """登记一个回调（**未知事件名只警告、仍然存**；不去重；保序）。

        真实侧 `_track_callback`（`plugins.py:927-938`）除了 append 还会发一个
        "可撤销句柄"（ledger）；mini 不卸载插件 ⇒ 这里没有句柄（见模块 docstring 的 B8）。
        """
        if event not in VALID_EVENTS:
            logger.warning(
                "registered unknown observer event '%s' (valid: %s)",
                event,
                ", ".join(sorted(VALID_EVENTS)),
            )
        self._callbacks.setdefault(event, []).append(callback)

    # ------------------------------------------------------------------ 查询
    def has(self, event: str) -> bool:
        """**有没有人听这个事件** —— 与真实侧 `has_hook()` 同形、同语义。

        实现刻意是"一次 dict 探针"（`plugins_dispatch.py:447` 逐字
        `return bool(self._hooks.get(hook_name))`）：无缓存、无身份比较、无标志位。
        fire-site 的那道门的正确性因此只依赖这一处事实源。
        """
        return bool(self._callbacks.get(event))

    def has_observers(self, event: str) -> bool:
        """`has()` 的别名 —— 同一个实现，**不是**第二套判定。

        留这个名字是为了让"可见性同源"（A8/A9）在调用点读起来更像文档：
        任何"要不要发 / 要不要显示"的判断都必须走它。
        """
        return self.has(event)

    def registered_events(self) -> Tuple[str, ...]:
        """登记过回调的事件名（**注册顺序**，每个只出现一次）—— 只读查询。"""
        return tuple(self._callbacks)

    def callbacks_for(self, event: str) -> Tuple[Callable[..., Any], ...]:
        """该事件回调的**稳定快照**（真实侧 `iter_hook_callbacks`，`plugins_dispatch.py:449-451`）。

        快照的意义：回调里边登记/摘除也不会把当前这轮遍历改掉。
        """
        return tuple(self._callbacks.get(event, ()))

    def callback_count(self, event: str) -> int:
        """该事件登记了几个回调（不去重 ⇒ 同一回调登记两次这里就是 2）。"""
        return len(self._callbacks.get(event, ()))

    @property
    def count(self) -> int:
        """全部事件上登记的回调总数。"""
        return sum(len(v) for v in self._callbacks.values())

    # ------------------------------------------------------------------ 派发
    def emit(self, event: str, **fields: Any) -> None:
        """把载荷派发给该事件的全部回调 —— **只读、失败放开、返回值忽略**。

        三件事按顺序发生（这就是"载荷装配的唯一出口"）：

          1. **防御性再探一次**：没人听就**立刻返回**（fire-site 已经问过一遍；
             这里是防"边跑边被清空"的那道兜底，也让直接调用 `emit` 的路径零成本）。
          2. `setdefault(telemetry_schema_version, …)`（A6）+ `sanitize_payload`（A4 三件套）。
             载荷所以**永远是 JSON-safe 的副本**，回调拿不到原始对象（也就改不动 agent 状态）。
          3. 逐个回调：独立 `try/except Exception`，记警告后**继续下一个**（A3）；返回值**丢弃**。

        ★ 这里不吃 `BaseException`：`KeyboardInterrupt` / `SystemExit` 不该被观察者吞掉
          （真实侧 `plugins_dispatch.py:218` 也只用 `except Exception`）。
        """
        callbacks = self._callbacks.get(event)
        if not callbacks:
            return
        try:
            payload = dict(fields)
            payload.setdefault(TELEMETRY_SCHEMA_VERSION_KEY, TELEMETRY_SCHEMA_VERSION)
            payload = sanitize_payload(payload)
        except Exception as exc:                     # noqa: BLE001 —— 载荷都装配不出来就别发了
            logger.warning("observer payload for '%s' could not be assembled: %s: %s",
                           event, type(exc).__name__, exc)
            return
        for callback in tuple(callbacks):
            try:
                # `event` 先塞、载荷后覆盖：mini 不做"按回调签名筛 kwargs"（B7），
                # 一个回调可以同时听多个事件，所以它需要知道自己是**哪个**事件被叫起来的。
                callback(**{"event": event, **payload})
            except Exception as exc:                 # noqa: BLE001 —— ★ 失败放开（A3）
                logger.warning(
                    "observer callback for '%s' failed: %s: %s", event, type(exc).__name__, exc)


#: 进程级默认注册表（真实侧 = `PluginManager._hooks` 那张全局表：插件在 agent 之前注册，
#: fire-site 只探它）。`AIAgent(observer=...)` 可以换一份进来（测试隔离 / 多租户）。
OBSERVERS = ObserverRegistry()


def has_observers(event: str, registry: Optional[ObserverRegistry] = None) -> bool:
    """模块级门面：`has_observers("post_tool_call")`（真实侧 `has_hook()`，`plugins.py:1738-1743`）。

    没有传 registry 就问进程级 `OBSERVERS`。给"agent 之外"的调用点用（网关 / doctor / 工具面），
    免得它们绕回头去 import `run_agent` 里某个私有属性 —— 判定源仍然只有 `ObserverRegistry.has`。
    """
    return (registry or OBSERVERS).has(event)


def emit(event: str, **fields: Any) -> None:
    """模块级门面：往进程级 `OBSERVERS` 发一份载荷（形状与 `ObserverRegistry.emit` 一致）。"""
    OBSERVERS.emit(event, **fields)
