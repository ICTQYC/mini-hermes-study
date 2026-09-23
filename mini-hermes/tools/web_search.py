"""web_search —— 联网搜索。

这是第 4 章之后**第一次加新工具**，所以它同时也是那套机制的验收题：
自注册 / AST 发现 / toolset 闸门 / `check_fn` / JSON 契约，一个都不能少。

## 加一个工具要碰几个地方？——两个文件

| 文件 | 干什么 | 不做的后果 |
|---|---|---|
| `tools/web_search.py`（本文件） | 注册自己（装饰器） | 工具不存在 |
| `toolsets.py` | 把它放进某个 toolset | **工具存在但模型看不见**（静默） |

**就这两个。** 发现机制是 AST 扫描，不用改任何名单（这正是 §2 的收益）。

## 凭据型 `check_fn`（§3 的"形态三"）

没配 API key 时，这个工具**根本不会出现在发给模型的 schema 里**。
这不是"调用时报错"，是**模型不知道它存在** —— 省下的是"模型去试错"的 token。

⚠️ 与 Hermes 的一处差异：Hermes 的凭据要走 `get_secret`（按 profile 隔离，
因为一个进程可能服务多个 profile），不能直接 `os.getenv`。
mini 是单 profile 的，所以这里读环境变量；**多 profile 之前必须改**。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List

from tools.registry import register_tool, tool_error, tool_result

DEFAULT_ENDPOINT = "https://google.serper.dev/search"
DEFAULT_TIMEOUT = 15
MAX_RESULTS = 10


def check_web_search_requirements() -> bool:
    """凭据型探测：**只问"key 配了没有"**，不发任何网络请求。

    ★ 两条纪律（都是 §3 的契约）：
      · **必须便宜** —— `check_fn` 每次构建 schema 都可能被问到，30s TTL 缓存着它。
      · **不能有副作用** —— 不能在探测里真的去调一次 API。
    """
    return bool(os.environ.get("MINI_SEARCH_API_KEY", "").strip())


@register_tool(
    "web_search",
    description=(
        "联网搜索网页。返回 JSON：{query, results:[{title, url, snippet}]}。"
        "适合查时效性信息；不要用它读某个已知网址的正文（那是 web_fetch 的事）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索词"},
            "max_results": {"type": "integer", "description": f"最多返回几条（默认 5，上限 {MAX_RESULTS}）"},
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
    key = os.environ.get("MINI_SEARCH_API_KEY", "").strip()
    if not key:
        # 正常路径到不了这里（check_fn 已经把它挡在 schema 之外了）。
        # 但**不能依赖那一点**：工具可能被别的方式直接调（历史里的旧 tool_call、
        # 测试、别的入口）—— 所以这里要自己再判一次，错误也必须是 JSON。
        return tool_error("MINI_SEARCH_API_KEY 未配置")

    endpoint = os.environ.get("MINI_SEARCH_ENDPOINT", DEFAULT_ENDPOINT)
    body = json.dumps({"q": query, "num": n}).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body,
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return tool_error(f"搜索接口返回 HTTP {exc.code}", status=exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return tool_error(f"搜索请求失败：{type(exc).__name__}: {exc}")
    except (json.JSONDecodeError, ValueError) as exc:
        return tool_error(f"搜索接口返回的不是 JSON：{exc}")

    results: List[Dict[str, Any]] = []
    for item in (payload.get("organic") or [])[:n]:
        results.append({
            "title": item.get("title"),
            "url": item.get("link"),
            "snippet": item.get("snippet"),
        })
    # ★ 出口永远是 JSON 字符串；`answer` 这类"直接用它回答"的字段也原样带上。
    return tool_result(query=query, results=results, answer=payload.get("answer"))
