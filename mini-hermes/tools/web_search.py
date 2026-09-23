"""web_search —— 联网搜索（供应商可换）。

这是第 4 章之后**第一次加新工具**，所以它同时也是那套机制的验收题：
自注册 / AST 发现 / toolset 闸门 / `check_fn` / JSON 契约，一个都不能少。

## 加一个工具要碰几个地方？——两个文件

| 文件 | 干什么 | 不做的后果 |
|---|---|---|
| `tools/web_search.py`（本文件） | 注册自己（装饰器） | 工具不存在 |
| `toolsets.py` | 把它放进某个 toolset | **工具存在但模型看不见**（静默） |

**就这两个。** 发现机制是 AST 扫描，不用改任何名单（这正是 §2 的收益）。

## 凭据型 `check_fn`（§3 的"形态三"）

没配任何搜索 key 时，这个工具**根本不会出现在发给模型的 schema 里**。
这不是"调用时报错"，是**模型不知道它存在** —— 省下的是"模型去试错"的 token。

## 两个供应商，按"哪个 key 在"自动选

| 供应商 | key 环境变量 | 免费额度 | 为什么它值得留 |
|---|---|---|---|
| **Tavily** | `TAVILY_API_KEY` | 1,000 次/月 | ★ **和 Hermes 自己的 `web_search` 同名** → 一个 key 两处用（`tools/web_tools.py:118` 的自动探测会认它） |
| **Serper** | `MINI_SEARCH_API_KEY` | 2,500 次/月 | 额度最大；Google SERP 原样结果 |

优先级 = 表里的顺序。现在 **Serper 在前**：它的免费额度更大（2,500/月），
而且 Hermes 不占用它 —— Tavily 那 1,000 次/月是 Hermes 和 mini **共享**的，
留给 Hermes 独占更划算。

⚠️ **厂商形状不许长进提示词里**：`description` 描述的是**我们自己的**结果形状
（`{query, results:[{title,url,snippet}]}`），换供应商只动下面的表，模型看到的契约不变。

⚠️ 与 Hermes 的一处差异：Hermes 的凭据要走 `get_secret`（按 profile 隔离，
因为一个进程可能服务多个 profile），不能直接 `os.getenv`。
mini 是单 profile 的，所以这里读环境变量；**多 profile 之前必须改**。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from tools.registry import register_tool, tool_error, tool_result

DEFAULT_TIMEOUT = 15
MAX_RESULTS = 10


# ══════════════════════════════════════════════════════════════════════════════
# 供应商适配：每家只差三件事 —— 端点 / 请求怎么造 / 结果怎么读。
# 排在前面的是**优先用的**（key 在就选它）。
# ══════════════════════════════════════════════════════════════════════════════

def _tavily_build(key: str, query: str, n: int) -> urllib.request.Request:
    """Tavily：认证走 `Authorization: Bearer` 头；key **不进请求体**（少一处泄漏面）。"""
    body = json.dumps({
        "query": query,
        "max_results": n,
        # ★ 显式写 basic：省 credit。不写时若开了 auto_parameters，
        #   Tavily 可能悄悄升到 advanced —— 那是 **2 credits/次**，免费额度直接减半。
        "search_depth": "basic",
        # 让它顺手给一段"直接可用的答案"（模型读这个比读 5 条 snippet 快）
        "include_answer": True,
    }).encode("utf-8")
    return urllib.request.Request(
        "https://api.tavily.com/search", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )


def _tavily_parse(payload: Dict[str, Any], n: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    results = [
        {"title": it.get("title"), "url": it.get("url"), "snippet": it.get("content")}
        for it in (payload.get("results") or [])[:n]
    ]
    return results, payload.get("answer")


def _serper_build(key: str, query: str, n: int) -> urllib.request.Request:
    """Serper：认证走 `X-API-KEY` 头；端点和结果字段都能被环境变量覆盖。"""
    endpoint = os.environ.get("MINI_SEARCH_ENDPOINT", "https://google.serper.dev/search")
    body = json.dumps({"q": query, "num": n}).encode("utf-8")
    return urllib.request.Request(
        endpoint, data=body,
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
    )


def _serper_parse(payload: Dict[str, Any], n: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    results = [
        {"title": it.get("title"), "url": it.get("link"), "snippet": it.get("snippet")}
        for it in (payload.get("organic") or [])[:n]
    ]
    return results, payload.get("answer")


# 单一事实来源：key 名字、端点、两个适配函数都在这张表里。
# **新增一家只加一行**，不用碰 check_fn / handler / schema。
PROVIDERS: Tuple[Dict[str, Any], ...] = (
    {
        "name": "serper",
        "key_env": "MINI_SEARCH_API_KEY",
        "build": _serper_build,
        "parse": _serper_parse,
    },
    {
        "name": "tavily",
        "key_env": "TAVILY_API_KEY",
        "build": _tavily_build,
        "parse": _tavily_parse,
    },
)


def active_provider() -> Optional[Dict[str, Any]]:
    """哪个供应商可用 —— **只看 key 在不在，不出网**。"""
    for spec in PROVIDERS:
        if os.environ.get(spec["key_env"], "").strip():
            return spec
    return None


def check_web_search_requirements() -> bool:
    """凭据型探测：**只问"key 配了没有"**，不发任何网络请求。

    ★ 两条纪律（都是 §3 的契约）：
      · **必须便宜** —— `check_fn` 每次构建 schema 都可能被问到，30s TTL 缓存着它。
      · **不能有副作用** —— 不能在探测里真的去调一次 API。
    （"是不是配了 key"和"这个 key 还能用吗"是两件事：后者要出网，不属于探测。）
    """
    return active_provider() is not None


@register_tool(
    "web_search",
    description=(
        "联网搜索网页。返回 JSON：{query, results:[{title, url, snippet}], answer, provider}。"
        "适合查时效性信息；不要用它读某个已知网址的正文（那是 web_fetch 的事）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索词"},
            "max_results": {
                "type": "integer",
                "description": f"最多返回几条（默认 5，上限 {MAX_RESULTS}）",
            },
        },
        "required": ["query"],
    },
    toolset="search",          # ← 放进既有 toolset（§4 的暴露闸门）
    check_fn=check_web_search_requirements,
)
def web_search_tool(query: str, max_results: int = 5) -> str:
    if not query or not query.strip():
        return tool_error("query 不能为空")

    n = max(1, min(int(max_results or 5), MAX_RESULTS))
    spec = active_provider()
    if spec is None:
        # 正常路径到不了这里（check_fn 已经把它挡在 schema 之外了）。
        # 但**不能依赖那一点**：工具可能被别的方式直接调（历史里的旧 tool_call、
        # 测试、别的入口）—— 所以这里要自己再判一次，错误也必须是 JSON。
        names = " 或 ".join(s["key_env"] for s in PROVIDERS)
        return tool_error(f"没有可用的搜索 key（{names} 至少配一个）")

    key = os.environ[spec["key_env"]].strip()
    req = spec["build"](key, query, n)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # 401/403 最常见的原因就是 key 不对/过期 —— 顺手说清是谁的 key。
        return tool_error(
            f"搜索接口返回 HTTP {exc.code}（provider={spec['name']}，用 {spec['key_env']}）",
            status=exc.code,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return tool_error(f"搜索请求失败：{type(exc).__name__}: {exc}")
    except (json.JSONDecodeError, ValueError) as exc:
        return tool_error(f"搜索接口返回的不是 JSON：{exc}")

    if not isinstance(payload, dict):
        return tool_error(f"搜索接口返回的不是对象：{type(payload).__name__}")

    try:
        results, answer = spec["parse"](payload, n)
    except Exception as exc:                                  # 形状变了也要是 JSON
        return tool_error(f"搜索接口返回的结构不认识：{type(exc).__name__}: {exc}")

    # ★ 出口永远是 JSON 字符串。带上 provider：模型/你都能看出"这次是谁答的"。
    return tool_result(query=query, results=results, answer=answer, provider=spec["name"])
