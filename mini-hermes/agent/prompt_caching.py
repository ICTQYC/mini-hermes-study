"""提示词缓存标记（第 5 章 §6）—— Anthropic 的 `cache_control` 断点。

对应 Hermes 的 `agent/prompt_caching.py`。

## 一条原则带出全部设计

**缓存的单位是「从第一个字节开始的最长公共前缀」。** 所以：

- 越靠前的内容越值钱 → 系统提示词单独占一个断点（它跨**所有**回合稳定）
- 断点必须**跟着对话末尾走** → 否则新增内容永远进不了缓存（"滚动 3 条窗口"）
- 改**中间**比改尾部贵得多 → 尾部之后没有东西可失效（第 3 章学过）

## system_and_3 策略（原文 §6.1）

Anthropic 每请求**最多 4 个** `cache_control` 断点，Hermes 这样用：

    断点 1：系统提示词                    （所有回合稳定）
    断点 2~4：倒数第 3 / 2 / 1 条非 system 消息   （滚动窗口）

## 标记放哪里（原文那张表）

| 内容类型 | 标记放哪 |
|---|---|
| 字符串内容 | 转成 `[{"type":"text","text":…,"cache_control":…}]` |
| 列表内容 | 加到**最后一个元素**的 dict 上 |
| `None` / 空 | 作为 `msg["cache_control"]` |
| 工具消息 | 作为 `msg["cache_control"]`（**仅原生 Anthropic**） |

## ⚠️ mini 的实际处境（这一节对 mini 是"教学 + 真实判断"两件事）

mini 跑的是 **DeepSeek**，而 DeepSeek 用的是**自动前缀缓存** ——
它按请求前缀自动命中，**不需要也不认识** `cache_control` 标记。

所以 mini 的 `_apply_prompt_caching` **不能**无脑插标记（那是往请求里塞未知字段，
而且会改变 payload 的字节形状 —— 而"字节稳定"恰恰是缓存的前提之一）。

正确行为是**判断 + 说明**：认识就插、不认识就说清楚为什么不动。
这也解释了原文那条"**不要添加静默切换模型或凭证的功能**"——
换模型 = 换了一把缓存钥匙 = 全量重读（§6.3 第 5 条）。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Anthropic 的硬上限：每请求最多 4 个断点
MAX_BREAKPOINTS = 4
DEFAULT_TTL = "5m"
VALID_TTLS = ("5m", "1h")

# 认得出 `cache_control` 的供应商（原生 Anthropic API / OpenRouter 的 Anthropic 路由）
CACHE_CONTROL_PROVIDERS = ("anthropic", "openrouter")

# ★ 用**自动前缀缓存**的供应商：不需要标记，插了反而是噪音。
#   （DeepSeek 就是这一类；判定它的意义是"说清楚为什么不动请求"。）
AUTO_PREFIX_CACHE_PROVIDERS = ("deepseek",)


def marker(ttl: str = DEFAULT_TTL) -> Dict[str, Any]:
    """缓存标记本体。`5m` 是默认值（Anthropic 侧写 `ephemeral`）。"""
    if ttl not in VALID_TTLS:
        raise ValueError(f"ttl 只能是 {VALID_TTLS}，收到 {ttl!r}")
    if ttl == DEFAULT_TTL:
        return {"type": "ephemeral"}
    return {"type": "ephemeral", "ttl": ttl}


def is_anthropic_like(model: str, provider: str = "") -> bool:
    """这个模型 + 路由要不要（能不能）带 `cache_control`？

    ★ 两个条件都要满足：**模型是 Claude 系**，且**路由支持这个字段**。
    只看模型名会把"Claude 走一个不支持该字段的代理"判错。
    """
    if not model:
        return False
    if "claude" not in model.lower():
        return False
    if not provider:
        return True                       # 没给路由信息时按模型名判断（宽松）
    return any(p in provider.lower() for p in CACHE_CONTROL_PROVIDERS)


def uses_automatic_prefix_cache(provider: str) -> bool:
    """这个供应商自己按前缀自动缓存（不需要标记）。"""
    return any(p in (provider or "").lower() for p in AUTO_PREFIX_CACHE_PROVIDERS)


def breakpoints(messages: Sequence[Dict[str, Any]]) -> List[int]:
    """挑出要打断点的那 4 个位置（system_and_3）。返回下标列表。

    ★ 顺序 = 优先级：系统提示词永远第一个（它最值钱），
    剩下的名额给**倒数第 3/2/1 条非 system 消息** —— 滚动窗口。
    不足 4 个就少打几个（Anthropic 允许少于 4 个）。
    """
    out: List[int] = []
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            out.append(i)
            break                              # 只给**第一个** system 打断点
    non_system = [i for i, m in enumerate(messages) if m.get("role") != "system"]
    out.extend(non_system[-3:])
    return out[:MAX_BREAKPOINTS]


def _put_marker(msg: Dict[str, Any], mark: Dict[str, Any], *, native_anthropic: bool) -> None:
    """按原文那张表把标记放到正确的位置（**就地改**，调用方负责先拷贝）。"""
    # ★ 工具消息**单独一条规则**（原文那张表的最后一行）且要先判：
    #   标记放在**消息上**、且**仅原生 Anthropic 认**。
    #   排在内容类型之后的话，一条 content 是字符串的 tool 消息会走进
    #   "转成内容块"那条分支，标记就跑到内容块里去了 —— 测试抓到的。
    if msg.get("role") == "tool":
        if native_anthropic:
            msg["cache_control"] = mark
        return

    content = msg.get("content")
    if isinstance(content, str) and content:
        msg["content"] = [{"type": "text", "text": content, "cache_control": mark}]
        return
    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = mark
        else:                                  # 元素不是 dict（理论上不该有）
            msg["cache_control"] = mark
        return
    # None / 空（工具消息已经在上面处理过了）
    msg["cache_control"] = mark


def apply_cache_control(
    messages: Sequence[Dict[str, Any]],
    *,
    ttl: str = DEFAULT_TTL,
    native_anthropic: bool = True,
) -> List[Dict[str, Any]]:
    """返回**注入了标记的新列表**（深拷贝；调用方的历史不动）。

    ⚠️ 必须深拷贝的理由（原文原话）：标记会**改写消息内容本身**
    （字符串内容被转成内容块数组）。就地改的话，历史会被永久改成
    "带 cache_control 的形状"，下一轮发给别的供应商时就多了一个未知字段。
    """
    out = copy.deepcopy(list(messages))
    mark = marker(ttl)
    for idx in breakpoints(out):
        _put_marker(out[idx], mark, native_anthropic=native_anthropic)
    return out


def plan(
    messages: Sequence[Dict[str, Any]],
    *,
    model: str,
    provider: str = "",
    ttl: str = DEFAULT_TTL,
) -> Tuple[List[Dict[str, Any]], str]:
    """总入口：返回 `(要发的消息, 一句人话说明)`。

    ★ 返回说明是刻意的（和第 3 章 `resolve_context_cwd` 返回来源同一个理由）：
    调用方/日志要能区分**三种不同的"没插标记"**：

      ① 这个模型本来就不需要（自动前缀缓存）—— 正常
      ② 这个模型不支持这个字段                —— 配置问题
      ③ 消息列表是空的                        —— 无事可做

    全都是"没插标记"，但只有 ② 需要人去改配置。只返回列表的话，
    这三种情况在日志里长得一模一样。
    """
    if not messages:
        return list(messages), "缓存：消息列表为空，跳过"

    if uses_automatic_prefix_cache(provider):
        return list(messages), (
            f"缓存：{provider} 用**自动前缀缓存**（按请求前缀自动命中），"
            f"不需要也不认识 cache_control —— 因此不改请求。"
            f"代价：换模型/换凭证会让下一轮零命中、全量重读。"
        )

    if not is_anthropic_like(model, provider):
        return list(messages), (
            f"缓存：{model or '(未命名模型)'} @ {provider or '(未知路由)'} "
            f"不支持 cache_control，跳过（0 个断点）"
        )

    native = "anthropic" in (provider or "").lower()
    out = apply_cache_control(messages, ttl=ttl, native_anthropic=native)
    idx = breakpoints(out)
    return out, (
        f"缓存：system_and_3，{len(idx)}/{MAX_BREAKPOINTS} 个断点 @ {idx}"
        f"（ttl={ttl}{'，原生 Anthropic' if native else '，经 OpenRouter'}）"
    )
