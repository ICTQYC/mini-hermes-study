"""工具中央注册表。

对应 Hermes 的 tools/registry.py（真实版本 977 行）。第 4 章给它加了三样：

1. **`check_fn`** —— 工具自己声明"我现在能不能用"（§3）
2. **toolset 语义** —— 注册 ≠ 暴露（§4）
3. **JSON 字符串契约** —— 工具结果一律是 JSON（§1、§5）

零依赖：这个文件谁也不 import，只被 import。这是 Hermes 依赖链的第一环：
    tools/registry.py  ←  tools/*.py  ←  model_tools.py  ←  run_agent.py

## 和 Hermes 的两处有意差异

**① 注册写法：装饰器 vs 普通调用**

Hermes 是模块级的普通调用：

    registry.register(name="terminal", toolset="terminal", ...)

mini 是装饰器：

    @register_tool("terminal", toolset="terminal", ...)
    def terminal_tool(...): ...

两种都在 **import 时**注册，都会被**静态 AST 检查**发现（见 model_tools.py 里的
`_module_registers_tools`）。区别只是 AST 形状不同：
Hermes 找「模块体里的 `registry.register(...)` 语句」，
mini 找「带 `@register_tool` 装饰器的函数定义」。
装饰器版本少写一遍函数名，所以 mini 选它。

**② 没有 profile 分键**

Hermes 的 check_fn 缓存按 `hermes_home_key()` 分键，因为它一个进程服务多个 profile。
mini 是单 profile 的，所以缓存直接以「函数对象」为 key。
**这是有意的简化**，代价是：如果 mini 以后支持多 profile，
一个 profile 的探测结果会被另一个复用——那时必须补上分键。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

__all__ = [
    "ToolEntry", "register_tool", "get", "tool_names", "get_definitions",
    "is_interactive", "requires_approval", "looks_approval_needed",
    "tool_error", "tool_result", "invalidate_check_fn_cache",
    "no_cache_check_fn", "check_fn_cached", "run_check_fn_uncached",
    "check_fn_cache_info",
]


# ── 工具条目 ─────────────────────────────────────────────────────────────
class ToolEntry:
    """一个注册工具的全部元数据（对应 Hermes 的 ToolEntry）。

    用 `__slots__` 是刻意的：这是一个"数据集合"，不是"行为对象"——
    加方法的地方应该在工具自己的文件里，不该在这里。
    """

    __slots__ = ("name", "toolset", "schema", "fn", "check_fn",
                 "interactive", "requires_approval", "danger_check")

    def __init__(self, name: str, toolset: str, schema: Dict[str, Any], fn: Callable,
                 check_fn: Optional[Callable] = None, interactive: bool = False,
                 requires_approval: bool = False,
                 danger_check: Optional[Callable] = None) -> None:
        self.name = name
        self.toolset = toolset
        self.schema = schema
        self.fn = fn
        self.check_fn = check_fn
        self.interactive = interactive
        self.requires_approval = requires_approval
        self.danger_check = danger_check


_REGISTRY: Dict[str, ToolEntry] = {}
_LOCK = threading.Lock()


# ── check_fn 的 TTL 缓存（对应 Hermes 的 _check_fn_cache 家族）──────────
#
# 为什么需要缓存：check_fn 会在**每次构建 schema** 时被问到，
# 而它可能在探测外部状态（起子进程、连服务、读凭据）。
# Hermes 的注释说得很直接：探测在 ~100 个工具上要花时间，而它在启动路径上。
#
# 为什么不是"永久缓存"：用户刚 `hermes tools enable` 或刚装了依赖，
# 应该在一个回合左右生效，而不是等重启。所以 30 秒。
CHECK_FN_TTL_SECONDS = 30.0
# 抖动保护窗口。语义**不是**"失败也缓存 60 秒"，而是：
# 「最近成功过、这次失败了」→ 更可能是抖动，先当它还能用。
# 见 check_fn_cached()。
CHECK_FN_FAILURE_GRACE_SECONDS = 60.0

_cache: Dict[Callable, Tuple[float, bool]] = {}
_last_good: Dict[Callable, float] = {}
_no_cache: Set[Callable] = set()
_cache_lock = threading.Lock()


def no_cache_check_fn(fn: Callable) -> Callable:
    """标记这个 check_fn 永不缓存。

    什么时候需要：探测只是**读本地配置**（便宜），而且**必须立刻反映**——
    用户改了 config.json 就该马上生效，不该等 30 秒 TTL。
    （Hermes 里 memory_tool / kanban_tools / browser_vault 三处这么用。）
    """
    _no_cache.add(fn)
    return fn


def run_check_fn_uncached(fn: Callable) -> bool:
    """真跑一次探测。**异常一律当 False**（fail-safe）。

    fail-safe 的方向很重要：探测自己炸了的时候，选择"当作不可用"而不是"当作可用"。
    理由是——把不可用的工具发给模型，模型会去调、失败、换一个（浪费预算）；
    而少发一个工具，最坏情况是模型少一种能力，它还能用别的办法。
    """
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001 - 见 docstring：任何异常 = 不可用
        return False


def check_fn_cached(fn: Callable) -> bool:
    """`bool(fn())`，带 TTL 缓存 + **抖动保护**。

    三种情况：

    | 情况 | 返回 | 缓存吗 |
    |---|---|---|
    | 探测成功 | True | 缓存（30s TTL） |
    | 最近成功过、这次失败 | **True**（当抖动） | **不缓存** → 下次重探 |
    | 一直没成功 / 宽限期过了 | False | 缓存（30s TTL） |
    """
    now = time.monotonic()
    if fn in _no_cache:
        return run_check_fn_uncached(fn)
    with _cache_lock:
        cached = _cache.get(fn)
        if cached is not None and now - cached[0] < CHECK_FN_TTL_SECONDS:
            return cached[1]

    value = run_check_fn_uncached(fn)
    with _cache_lock:
        if value:
            _last_good[fn] = now
            _cache[fn] = (now, True)
            return True
        last = _last_good.get(fn)
        if last is not None and now - last < CHECK_FN_FAILURE_GRACE_SECONDS:
            # ★ 抖动保护：刚还能用，这次失败 → 当作瞬时故障，继续提供，
            #   但**不写缓存** —— 否则"假装还能用"会持续 30 秒。
            #   正确组合是"继续乐观 + 立刻重新检查"。
            return True
        _cache[fn] = (now, False)
        return False


def invalidate_check_fn_cache() -> None:
    """清空所有探测结果。

    对应 Hermes 的 `invalidate_check_fn_cache`：配置变了之后调它，
    让"刚启用"立刻生效而不是等 TTL 到期。
    """
    with _cache_lock:
        _cache.clear()
        _last_good.clear()


def check_fn_cache_info() -> Dict[str, int]:
    """给诊断用：缓存里现在有多少条（不进 schema，只给 /tools 显示）。"""
    with _cache_lock:
        return {"cached": len(_cache), "last_good": len(_last_good),
                "no_cache": len(_no_cache)}


# ── 注册 ────────────────────────────────────────────────────────────────
def register_tool(
    name: str,
    *,
    description: str,
    parameters: Dict[str, Any],
    toolset: str = "core",
    check_fn: Optional[Callable[[], bool]] = None,
    interactive: bool = False,
    requires_approval: bool = False,
    danger_check: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Callable:
    """把函数注册成一个模型可调用的工具（装饰器形式，import 时执行）。

    `check_fn` —— 可选。返回 True 表示"现在可用"。什么时候用：
      · 依赖的环境变量 / 凭据在不在
      · 依赖的外部程序装没装
      · 依赖的服务在不在跑
      · 一个配置开关开着没
    **不要**用它回答"这个界面/这次会话该不该给它"——那是 toolset 的事（见 toolsets.py）。
    判据：**"换个会话答案会变吗？"** 会 → toolset；不会 → check_fn。

    `interactive=True` 的工具会强制整批工具串行执行
    （Hermes 里 clarify 就是这个待遇：它要弹窗等用户，不能和别人并发）。

    `requires_approval` + `danger_check`：只有这次调用真的危险才拦住。
    注意 danger_check 只管"这次危不危险"，**不管"问谁、等多久、记不记得"**——
    那些在 tools/approval.py 里。
    """

    def decorator(fn: Callable) -> Callable:
        with _LOCK:
            if name in _REGISTRY and _REGISTRY[name].toolset != toolset:
                # 对应 Hermes 的「跨 toolset 影子」检查：同名工具来自不同 toolset
                # 一律拒绝并留日志。同 toolset 重注册是允许的（MCP 重连那种刷新）。
                import logging
                logging.getLogger(__name__).error(
                    "工具注册被拒绝：%r（toolset=%r）会盖掉已有工具（toolset=%r）。"
                    "如果是有意的，先用 deregister 摘掉旧的。",
                    name, toolset, _REGISTRY[name].toolset,
                )
                return fn
            _REGISTRY[name] = ToolEntry(
                name=name, toolset=toolset, fn=fn, check_fn=check_fn,
                interactive=interactive, requires_approval=requires_approval,
                danger_check=danger_check,
                # OpenAI function-calling schema，直接塞进 API 请求的 tools 字段
                schema={
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": parameters,
                    },
                },
            )
        return fn

    return decorator


def deregister(name: str) -> bool:
    with _LOCK:
        return _REGISTRY.pop(name, None) is not None


# ── 查询 ────────────────────────────────────────────────────────────────
def get(name: str) -> Optional[ToolEntry]:
    return _REGISTRY.get(name)


def tool_names() -> List[str]:
    """**所有注册过的**名字（不代表模型能看到它们——那要看 toolset）。"""
    return sorted(_REGISTRY)


def toolset_of(name: str) -> Optional[str]:
    entry = _REGISTRY.get(name)
    return entry.toolset if entry else None


def get_definitions(names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """构建发给模型的 schema 列表（对应 Hermes 的 `registry.get_definitions`）。

    这里是 `check_fn` **真正起作用**的地方：探测不通过的工具
    **整条 schema 都被跳过** —— 模型连"有这么个工具"都不知道。

    两层缓存：
      · `memo` 是**单次调用内**的去重（多个工具共用一个 check_fn 时只探一次）
      · `check_fn_cached` 是**进程级**的 TTL 缓存
    """
    picked = tool_names() if names is None else sorted(n for n in names if n in _REGISTRY)
    memo: Dict[Callable, bool] = {}
    out: List[Dict[str, Any]] = []
    for n in picked:
        entry = _REGISTRY[n]
        if entry.check_fn is not None and not _memo_check(entry.check_fn, memo):
            continue
        out.append(entry.schema)
    return out


def _memo_check(fn: Callable, memo: Dict[Callable, bool]) -> bool:
    """单次构建内的去重：同一个 check_fn 只探一次。"""
    if fn not in memo:
        memo[fn] = check_fn_cached(fn)
    return memo[fn]


def schema_of(name: str) -> Optional[Dict[str, Any]]:
    entry = _REGISTRY.get(name)
    return entry.schema if entry else None


# ── 审批相关的查询──
def is_interactive(name: str) -> bool:
    entry = _REGISTRY.get(name)
    return bool(entry and entry.interactive)


def requires_approval(name: str) -> bool:
    entry = _REGISTRY.get(name)
    return bool(entry and entry.requires_approval)


def looks_approval_needed(name: str, args: Dict[str, Any]) -> bool:
    """这次调用要不要弹窗问用户？

    只有注册时声明了 requires_approval 且带 danger_check 的工具才可能返回 True，
    并且最终由工具自己的 danger_check 说了算。
    """
    entry = _REGISTRY.get(name)
    if not entry or not entry.requires_approval:
        return False
    checker = entry.danger_check
    if checker is None:
        return True  # 声明了要审批但没给判断函数 → 一律问
    try:
        return bool(checker(args or {}))
    except Exception:  # noqa: BLE001 - 判断函数自己炸了，保守起见走审批
        return True


# ── 结果契约：一律 JSON 字符串 ──────────────────────────────────────────
#
# Hermes 的 tools/AGENTS.md 原文：**All handlers return a JSON string.**
#
# 为什么不是人看的文本？因为工具结果要**从历史里被重新解析**——
# todo 的状态重建就靠这个（网关每条消息新建一个 agent，状态必须能从历史读回来）。
# 给人看的文本解析不了。
#
# 第 4 章之前 mini 返回的是 "[error] xxx" 纯文本，那是错的（见 §5）。

MAX_ERROR_CHARS = 2048
_TRUNCATION_MARKER = "… [截断]"


def _bound(text: str) -> str:
    """错误文本必须有上限。

    **为什么**：错误文本会进历史，历史会随**每次**请求重发，
    所以一条超长异常的成本是**复利**的——一个 200KB 的 traceback
    会在之后每一轮都被重新发送一遍。
    （对应 Hermes 的 `_MAX_TOOL_ERROR_CHARS = 2048`。）
    """
    if len(text) <= MAX_ERROR_CHARS:
        return text
    return text[:MAX_ERROR_CHARS] + _TRUNCATION_MARKER


def tool_error(message: Any, **extra: Any) -> str:
    """`'{"error": "...", ...extra}'` —— 工具出错的统一出口。"""
    import json
    return json.dumps({"error": _bound(str(message)), **extra}, ensure_ascii=False)


def tool_result(data: Any = None, **kwargs: Any) -> str:
    """把 dict（或关键字参数）编码成 JSON 字符串。

    注意：**成功路径和失败路径都返回 JSON 字符串**。
    契约的完整含义不是"正常情况下返回 JSON"，而是"**无论拿到什么，出口都是 JSON**"。
    """
    import json
    return json.dumps(data if data is not None else kwargs, ensure_ascii=False)
