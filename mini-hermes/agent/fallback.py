"""回退链（第 8 章 A7）—— **一次一次消费链** + 原地换运行时 + 按轮次复位。

对应 Hermes 的：
  * `agent/chat_completion_helpers.py::_try_activate_fallback`（:1973，换属性的那 6 行）；
  * `agent/chat_completion_helpers.py::_fallback_chain_exhausted`（:1792-1800，耗尽恒 False）；
  * `agent/turn_api_error.py` / `turn_response_check.py` / `turn_recovery.py`（三处触发点）；
  * `hermes_cli/fallback_config.py`（链的合并 / 去重 / 丢弃坏条目 —— 在 `hermes_cli/config.py` 里）。

## 这个模块存在的理由

mini 的 `run_agent.py` 模块 docstring 从第 2 章起就写着"9. 失败回退（…换 fallback provider）"，
而 `fallback_providers` **全仓零调用点**（形参永远 `[]`）—— 一个**承诺了却跑不起来**的功能。
这个模块把那句承诺变成真的：链从 `config.json`（A9）或构造入参来，失败时**按序**换 provider。

## 三条必须记住的语义（都是真实版的形状）

1. **按序消费**：每次激活把 `_fallback_index += 1`，一次失败只前进一格；
   **绝不重复用同一格**。链耗尽 ⇒ `has_pending_fallback()` 为 False ⇒ 激活**恒返回 False**
   （真实版 `_fallback_chain_exhausted` 的原话：不再激活、不再锤主模型）。
2. **一次性 latch**：`_fallback_activated` 一旦置位就表示"这一轮已经换过 provider"。
   新的一轮开始由 `restore_primary_runtime()` **复位**（索引归零、latch 清掉、运行时收回主 provider）。
   ⇒ 任何一格在同一轮里**只触发一次**；链走完后再触发**什么都不做**。
3. **原地换**：激活时直接改 agent 上的运行时（provider/model/base_url/api_mode/api_key
   + `_client_kwargs`）。换完之后**重发的那次请求自动用新 provider**（mini 没有长命 client，
   client 的构造入参就是 `_client_kwargs`）。

## 凭据隔离

每条回退条目**自己解析自己的 key**（`hermes_cli/config.py::resolve_entry_api_key`：
内联 `api_key` → `key_env` **经 `get_secret`**），主 provider 的凭据**绝不复用**给回退 provider。
条目解析不出来（未知 provider / 缺 key / 端点被换到陌生域名）⇒ **跳过这一条**、索引照样前进
（真实版 `_should_skip_fallback_candidate`）。

## 判据是**二元**的（B2：不抄 30+ 项 FailoverReason 分类体系）

可回退 = 限流 / 上游故障 / 凭据失效 —— 换个 provider **可能**修好：
`{401, 403, 408, 409, 429, 500, 502, 503, 504}`。
不可回退 = 确定性错误（请求本身不合法）—— 换谁都一样：
`400` / `404` / `422` 等。判据只有这一条线，没有十几个"原因"枚举。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ══════════════════════════════════════════════════════════════════════════
# 判据：可回退 / 不可回退（**二元**）
# ══════════════════════════════════════════════════════════════════════════

# 可回退的 HTTP 状态码。与 mini 旧版 `run_agent.FALLBACK_STATUS` 同一集合（那是第 2 章
# 就写下的、从没被接线过的那张表）—— 这里把它变成真正被读的东西。
FALLBACK_STATUS = frozenset({401, 403, 408, 409, 429, 500, 502, 503, 504})

# 明确**不可回退**的典型（写出来是为了让"为什么 400 不换 provider"这句话有出处）：
#   400 —— 请求格式/参数不合法（换 provider 只会再收一次 400）；
#   404 —— 模型/路径不存在（确定性）；
#   422 —— 语义校验失败（同为确定性）。
NON_FALLBACK_STATUS = frozenset({400, 404, 422})


def is_fallbackable_status(status: Any) -> bool:
    """这个状态码**可回退**吗？解析不成整数 ⇒ False（不猜、不回退）。"""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    return code in FALLBACK_STATUS


def chain_from_config(config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """链的**默认来源**：`config.json` 的 `fallback_providers`（+ legacy `fallback_model`）。

    ★ 为什么默认放这里而不是让每个入口自己去读：第 8 章 A9 的配置层是**唯一读法**
      （合并 / 保序 / 去重 / 丢坏条目都在那里），`agent` 只消费它产出的干净列表。
    """
    from hermes_cli.config import get_fallback_chain

    return get_fallback_chain(config)


# ══════════════════════════════════════════════════════════════════════════
# 状态：链 + 索引 + latch + 主运行时快照
# ══════════════════════════════════════════════════════════════════════════
#
# ★ 状态直接挂在 **agent 实例**上（真实版就是 `agent._fallback_chain` /
#   `_fallback_index` / `_fallback_activated`），不用单独的状态对象：
#   观察面因此是**可断言的普通属性**，而不是"藏在某个对象里"。

# 换运行时时要动的那几项（`_client_kwargs` 是它们的派生快照，由 `_apply_runtime` 同步）
_RUNTIME_KEYS = ("provider", "model", "base_url", "api_mode", "api_key", "context_length")

# 主运行时的快照挂在这个属性上（None = 这一轮还没换过）
_PRIMARY_ATTR = "_fallback_primary"


def install_chain(agent: Any, chain: Optional[List[Dict[str, Any]]] = None) -> None:
    """装上回退链并复位状态（构造时调一次）。

    ★ 传 `None` = 用**默认来源**（config.json）；传 `[]` = **显式禁用回退**。
      这个区分是刻意的（真实版：`fallback_providers: []` 表示禁用，`None` 表示继承）。
    """
    agent._fallback_chain = list(chain or [])
    agent._fallback_index = 0
    agent._fallback_activated = False
    setattr(agent, _PRIMARY_ATTR, None)
    agent._provider_fallback_active = False
    agent._provider_fallback_route = ""


def _chain(agent: Any) -> List[Dict[str, Any]]:
    return list(getattr(agent, "_fallback_chain", None) or [])


def _index(agent: Any) -> int:
    try:
        return max(0, int(getattr(agent, "_fallback_index", 0) or 0))
    except (TypeError, ValueError):
        return 0


def pending_count(agent: Any) -> int:
    """链里还剩几条**没被消费**的候选（0 = 没得换了）。"""
    return max(0, len(_chain(agent)) - _index(agent))


def has_pending_fallback(agent: Any) -> bool:
    """还有没有可换的候选？空链 / 链耗尽 ⇒ False。

    ★ 这条判据的用途（真实版 `_has_pending_fallback()`）：连"trying fallback…"都不报，
      因为**报一句换不了的话比不报更坏**。
    """
    return pending_count(agent) > 0


def fallback_state(agent: Any) -> Dict[str, Any]:
    """观察面（测试 / 日志用）：链长、已消费几格、latch、当前路由。"""
    return {
        "chain_len": len(_chain(agent)),
        "index": _index(agent),
        "pending": pending_count(agent),
        "activated": bool(getattr(agent, "_fallback_activated", False)),
        "active": bool(getattr(agent, "_provider_fallback_active", False)),
        "route": str(getattr(agent, "_provider_fallback_route", "") or ""),
    }


# ══════════════════════════════════════════════════════════════════════════
# 原地换运行时
# ══════════════════════════════════════════════════════════════════════════


def snapshot_runtime(agent: Any) -> Dict[str, Any]:
    """当前运行时的快照（用来在换之前存住主 provider，好在下一轮换回来）。"""
    return {key: getattr(agent, key, None) for key in _RUNTIME_KEYS}


def apply_runtime(agent: Any, spec: Dict[str, Any]) -> None:
    """把一份运行时 spec **原地**应用到 agent 上。

    ★ 单一写入口：有 `agent._apply_runtime` 就走它（它同时同步 `_client_kwargs`、
      并让压缩器跟着换模型），没有就直接 setattr（测试里的假 agent 也能用）。
    """
    changes = {key: spec.get(key) for key in _RUNTIME_KEYS if spec.get(key) is not None}
    setter = getattr(agent, "_apply_runtime", None)
    if callable(setter):
        setter(**changes)
        return
    for key, value in changes.items():
        setattr(agent, key, value)


def reset_primary_runtime(agent: Any) -> None:
    """复位回退状态（索引归零 + 清 latch），**不动**运行时。

    与 `restore_primary_runtime` 分开是因为"复位状态"和"换回主 provider"是两件事：
    限流冷却期内故意**不回主**也能复位索引（真实版 `:1124`）。
    """
    agent._fallback_index = 0
    agent._fallback_activated = False
    agent._provider_fallback_active = False
    agent._provider_fallback_route = ""


def restore_primary_runtime(agent: Any) -> bool:
    """新一轮开始：把运行时收回主 provider，并复位索引 + latch（**按轮次回退**）。

    对应真实版 `agent/agent_runtime_helpers.py::restore_primary_runtime`（:1183-1185 复位、
    :1114-1118 恢复）。返回是否真的换回来过（观察面 / 测试要用）。
    """
    primary = getattr(agent, _PRIMARY_ATTR, None)
    active = bool(getattr(agent, "_fallback_activated", False)) or _index(agent) > 0
    reverted = False
    if isinstance(primary, dict) and primary and active:
        apply_runtime(agent, primary)
        reverted = True
    setattr(agent, _PRIMARY_ATTR, None)
    reset_primary_runtime(agent)
    return reverted


# ══════════════════════════════════════════════════════════════════════════
# 激活
# ══════════════════════════════════════════════════════════════════════════


def resolve_entry(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一条回退条目解析成运行时 spec；**解析不出来返回 None**（跳过这一条）。

    走的是**同一个 provider 路由器**（`hermes_cli.runtime_provider.resolve_provider`）——
    回退链与主解析**共用**一处认证 / base_url / api_mode 映射，绝不各调一套
    （真实版 `resolve_provider_client` 的注释原话：*the single entry point … Never read
    auth env vars ad-hoc*）。

    条目的凭据**只来自它自己**：内联 `api_key` 或 `key_env`（经 `get_secret`），
    作为 `explicit_api_key` 传下去。⇒ 主 provider 的 key 不会漏给回退 provider。
    """
    from hermes_cli.config import resolve_entry_api_key
    from hermes_cli.runtime_provider import load_env_file, resolve_provider

    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or "").strip()
    if not provider or not model:
        return None                                  # 缺字段的条目 → 跳过（A9 也丢，这里再防一手）
    # ★ 先把密钥文件灌进环境再读凭据：条目用 `key_env` 声明凭据时，`get_secret` 读的是
    #   `os.environ` 的兜底分支，而 `.env` 里的值要 `load_env_file()` 之后才在那儿。
    #   （顺序错的后果：第一条回退因为"密钥为空"被跳过 —— 一个只出现一次的静默降级。）
    load_env_file()
    explicit_base_url = str(entry.get("base_url") or "").strip() or None
    explicit_api_key = resolve_entry_api_key(entry)
    try:
        return resolve_provider(provider, model,
                                explicit_api_key=explicit_api_key,
                                explicit_base_url=explicit_base_url)
    except Exception:                                # noqa: BLE001 —— 未知 provider / 缺 key → 跳过
        return None


def activate_fallback(agent: Any, *, reason: str = "") -> bool:
    """按序换到下一条可用候选。换成功返回 True，否则 False（**链耗尽时恒 False**）。

    ★ 只前进、不回头：每次调用把索引往前推（解析失败的候选**照样被消费掉**），
      所以同一个候选在复位之前**不可能被用第二次** —— 这就是"触发过就不再触发"。
    """
    chain = _chain(agent)
    index = _index(agent)
    while index < len(chain):
        entry = chain[index]
        index += 1
        agent._fallback_index = index              # ★ 先推进：失败也要消费掉这一格
        spec = resolve_entry(entry)
        if spec is None:
            continue
        if not isinstance(getattr(agent, _PRIMARY_ATTR, None), dict):
            setattr(agent, _PRIMARY_ATTR, snapshot_runtime(agent))   # 存主运行时（只存第一次）
        apply_runtime(agent, spec)
        agent._fallback_activated = True           # ★ 一次性 latch：这一轮已经换过了
        agent._provider_fallback_active = True
        agent._provider_fallback_route = f"{spec.get('provider')}/{spec.get('model')}"
        agent._last_fallback_reason = reason
        return True
    return False                                   # 链耗尽 / 全被跳过


__all__ = [
    "FALLBACK_STATUS", "NON_FALLBACK_STATUS", "is_fallbackable_status",
    "chain_from_config",
    "install_chain", "has_pending_fallback", "pending_count", "fallback_state",
    "snapshot_runtime", "apply_runtime", "reset_primary_runtime", "restore_primary_runtime",
    "resolve_entry", "activate_fallback",
]
