"""api_mode（线上协议）的判定 —— 按 **host 探测 + 供应商叠加**，不是一张静态大表（A3）。

对应 Hermes 的：
  * `runtime_provider.py::_detect_api_mode_for_url`（:90-128）—— 按 URL 探测；
  * `runtime_provider.py::_fallback_api_mode`（:139-146）—— 三段式兜底；
  * `hermes_cli/providers.py::determine_api_mode`（:352-368）/ `host_mandated_api_mode`（:289-318）。

**为什么不能做成 provider→mode 的静态映射**：同一个 provider 可以有多个端点
（官方 / 代理 / 数据驻留），协议由**端点**决定。所以判据顺序是：
  ① host 强制表 → ② 官方 OpenAI 主机族 → ③ URL 路径后缀 → ④ 供应商 profile 声明 → ⑤ chat_completions。

`api_mode` 一共三个取值：`chat_completions` / `anthropic_messages` / `codex_responses`。
mini 目前只实现第一个；另外两个要给**带指引的报错**（`require_implemented_api_mode`）。
"""
from __future__ import annotations

from typing import Optional, Set

from providers.aliases import normalize_provider
from providers.host_gate import base_url_hostname, host_matches, is_official_openai_host

# 只认这几种协议（真实版 `_VALID_API_MODES` 的子集：mini 没有 bedrock / codex_app_server 基座）
VALID_API_MODES: Set[str] = {"chat_completions", "codex_responses", "anthropic_messages"}

# mini **真跑得起来**的协议（`run_agent._build_api_messages` 只有这一条路）
IMPLEMENTED_API_MODES: Set[str] = {"chat_completions"}

# 只讲一种协议的端点（真实版 `_HOST_MANDATED_API_MODES` 逐字）。
# api.x.ai / api.meta.ai / api.router.com —— 这些主机的 Responses 才是原生；
# api.anthropic.com —— 原生 Messages；api.actual.inc —— 固定 chat_completions。
_HOST_MANDATED_API_MODES = {
    "api.x.ai": "codex_responses",
    "api.meta.ai": "codex_responses",
    "api.actual.inc": "chat_completions",
    "api.router.com": "codex_responses",
    "api.anthropic.com": "anthropic_messages",
}

# `provider` 身份直接决定协议的少数几家（真实版 `determine_api_mode` 里的 provider 叠加层）。
_PROVIDER_API_MODES = {
    "anthropic": "anthropic_messages",
    "minimax": "anthropic_messages",
    "minimax-cn": "anthropic_messages",
    "xai": "codex_responses",
    "openai-codex": "codex_responses",
}


class UnsupportedApiMode(RuntimeError):
    """选中的 api_mode 在 mini 里**还没有实现**。

    ★ 存在的意义：把\"跑到发请求才炸 / 炸得没头没脑\"换成一条**能照着修的**报错。
    """


def require_implemented_api_mode(api_mode: str) -> None:
    """不是 mini 已实现的协议就**明确报错**（说清支持哪几种、该改哪里）。

    ★ 以前这里是 `NotImplementedError`（`run_agent.py` 里裸抛）—— 只有一行\"需要格式转换层\"，
      既没说 mini 支持什么、也没说该动哪个文件。现在把指引写在错误里。
    """
    mode = str(api_mode or "").strip()
    if mode in IMPLEMENTED_API_MODES:
        return
    implemented = "、".join(sorted(IMPLEMENTED_API_MODES))
    if mode in VALID_API_MODES:
        head = f"api_mode={mode!r} 在 mini 里还没有实现（mini 只实现：{implemented}）。"
    else:
        head = (f"未知 api_mode：{mode!r}（mini 认识："
                f"{'、'.join(sorted(VALID_API_MODES))}；只实现：{implemented}）。")
    raise UnsupportedApiMode(
        head + "\n"
        "  这个模式是从供应商身份 / 端点 host 判定出来的（providers/api_mode.py::detect_api_mode）。\n"
        "  要真正跑通它，请在 run_agent.py::_build_api_messages 里补一层**格式转换**\n"
        "  （对应 Hermes 的 codex_responses_adapter.py / anthropic_adapter.py）。\n"
        f"  想立刻可用：用 `--provider` 选一个走 {implemented} 的供应商（例如 deepseek）。"
    )


def is_actual_route(provider: str = "", base_url: str = "") -> bool:
    """是不是 Actual 的通道（按 provider 身份或它的主机）。真实版 `providers.py:163`。"""
    return (normalize_provider(provider or "") == "actual"
            or base_url_hostname(base_url) == "api.actual.inc")


def host_mandated_api_mode(base_url: str) -> Optional[str]:
    """这个**端点**只讲哪一种协议？没有强制就返回 None。

    ★ 只做**精确 hostname** 匹配（含点后缀），绝不用子串 ——
      否则 `api.anthropic.com.attacker.test` / `proxy.test/api.anthropic.com/v1`
      会被误认为真端点（真实版 #32243 的原话：reject lookalike subdomains and path-segment spoofing）。
    """
    if not base_url:
        return None
    url_lower = base_url.rstrip("/").lower()
    hostname = base_url_hostname(base_url)
    if hostname in _HOST_MANDATED_API_MODES:
        return _HOST_MANDATED_API_MODES[hostname]
    if hostname == "api.kimi.com" and "/coding" in url_lower:
        return "anthropic_messages"
    if hostname == "api.anthropic.com" or url_lower.endswith("/anthropic"):
        return "anthropic_messages"
    if is_official_openai_host(base_url):
        return "codex_responses"
    return None


def detect_api_mode(base_url: str, provider: str = "") -> Optional[str]:
    """**按 URL 探测**协议；没有信号返回 None（A3 的主函数）。

    判据顺序（真实版 `_detect_api_mode_for_url`）：
      ① provider 是 Actual 通道 → chat_completions；
      ② host 强制表 / 官方 OpenAI 主机族 → 对应模式；
      ③ URL 路径以 ``/anthropic``(/v1) 结尾，或 Kimi ``api.kimi.com/coding`` → anthropic_messages。

    ★ `provider` 只在\"身份即协议\"时参与，其余交给 host —— host 说了算。
    """
    if is_actual_route(provider, base_url):
        return "chat_completions"
    mandated = host_mandated_api_mode(base_url)
    if mandated:
        return mandated
    normalized = (base_url or "").strip().lower().rstrip("/")
    path = ""
    try:
        from urllib.parse import urlparse

        path = urlparse(normalized).path.rstrip("/")
    except ValueError:
        path = ""
    if path.endswith(("/anthropic", "/anthropic/v1")) or (
            base_url_hostname(base_url) == "api.kimi.com" and "/coding" in normalized):
        return "anthropic_messages"
    return None


def determine_api_mode(provider: str, base_url: str = "", model: str = "") -> str:
    """协议 = host 强制 → provider 身份 → 供应商 profile 声明 → chat_completions。

    真实版 `providers.py:352-368`（host 优先，再 provider transport）。
    """
    if is_actual_route(provider, base_url):
        return "chat_completions"
    mandated = host_mandated_api_mode(base_url)
    if mandated:
        return mandated
    canon = normalize_provider(provider)
    if canon in _PROVIDER_API_MODES:
        return _PROVIDER_API_MODES[canon]
    # 供应商 profile 自己的声明（注册表是唯一事实源）
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(canon)
        if profile is not None and profile.api_mode:
            return profile.api_mode
    except Exception:  # noqa: BLE001 —— 注册表不可用时退回默认协议
        pass
    return "chat_completions"


def fallback_api_mode(provider: str, base_url: str, model: str = "") -> str:
    """**三段式兜底**（真实版 `_fallback_api_mode`）：探测 → provider 叠加 → chat_completions。

    ★ \"探测优先\"是对的：声明说 chat_completions 的供应商，把 base_url 指到
      `api.anthropic.com` 时，**端点的强制协议更可信**。
    """
    if is_actual_route(provider, base_url):
        return "chat_completions"
    return detect_api_mode(base_url, provider) or determine_api_mode(provider, base_url, model) or "chat_completions"
