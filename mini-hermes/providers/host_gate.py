"""host 门控的凭据候选（A5）。

对应 Hermes 的 `hermes_cli/runtime_provider.py::_host_gated_env_key_candidates`（:317-326）
与 `utils.py::base_url_host_matches`（:612-620）、`_host_derived_api_key`（:300-314）。

**要防的事**（真实版注释点名的 GHSA-76xc-57q6-vm5m）：把 `OPENAI_API_KEY` /
`OPENROUTER_API_KEY` / `OLLAMA_API_KEY` 发给一个**不相干的端点** = 凭据泄漏。
所以判据不是\"URL 里含不含这个词\"，而是 **host 精确匹配或点后缀匹配**——
`api.openai.com.attacker.test` 和 `evil.com/api.openai.com/v1` 都**不许**算命中。
"""
from __future__ import annotations

from typing import List
from urllib.parse import urlparse

from agent.secret_scope import get_secret


def _parse_base_url(base_url: str):
    """把裸主机 / 带路径的 URL 都解析出来；解析不了返回 None。"""
    raw = str(base_url or "").strip()
    if not raw:
        return None
    try:
        return urlparse(raw if "://" in raw else "//" + raw)
    except ValueError:      # 畸形端口之类
        return None


def base_url_hostname(base_url: str) -> str:
    """base_url 的小写 hostname（去掉末尾点），没有 host 返回 ``""``。

    真实版 `utils.py:576` 的契约：**取的是解析后的 hostname，不是原始字符串**——
    这样 `https://api.openai.com.example/v1` 不会当成 `api.openai.com`。
    """
    parsed = _parse_base_url(base_url)
    return (parsed.hostname or "").lower().rstrip(".") if parsed else ""


def host_matches(base_url: str, domain: str) -> bool:
    """base_url 的 hostname **等于** domain，或是它的**点后缀子域**。

    真实版 `utils.py:612-620` 逐字逻辑：``hostname == domain or hostname.endswith("." + domain)``。
    比 ``domain in base_url`` 安全 —— 后者会让 ``evil.com/moonshot.ai`` / ``moonshot.ai.evil`` 蒙混过关。
    """
    hostname = base_url_hostname(base_url)
    dom = str(domain or "").strip().lower().rstrip(".")
    return bool(hostname and dom) and (hostname == dom or hostname.endswith("." + dom))


def is_official_openai_host(base_url: str) -> bool:
    """官方 OpenAI API 主机族（含 ``us.``/``eu.`` 数据驻留子域）。"""
    return host_matches(base_url, "api.openai.com")


def _vendor_label(hostname: str) -> str:
    """从 host 反推厂商标识：``api.deepseek.com`` → ``DEEPSEEK``；不可信/无厂商 → ``""``。

    真实版 `runtime_provider.py:300-314` 的规则：
      * IP / localhost / 单标签 host / 带端口的 → ``""``；
      * 去掉开头的 ``api`` / ``www``，取**倒数第二个标签**（注册域名）大写；
      * 排除 OPENAI / OPENROUTER / OLLAMA（它们各有自己的门控路径，不给\"伪装 host\"抢 key 的机会）。
    ★ \"取倒数第二个\"这一点就是防伪装的关键：``api.deepseek.com.attacker.test`` 的最后两段是
      ``attacker.test`` → 得到 ``ATTACKER``，于是 `DEEPSEEK_API_KEY` **原地不动**。
    """
    if not hostname or hostname == "localhost" or ":" in hostname:
        return ""
    labels = [lbl for lbl in hostname.split(".") if lbl]
    if labels and any(ch.isdigit() for ch in labels[-1]):
        return ""                     # 末段是 IP / 数字 TLD
    while labels and labels[0] in ("api", "www"):
        labels.pop(0)
    if len(labels) < 2:
        return ""
    sanitized = "".join(ch if ch.isalnum() else "_" for ch in labels[-2]).upper()
    if not sanitized or not sanitized[0].isalpha() or sanitized in ("OPENAI", "OPENROUTER", "OLLAMA"):
        return ""
    return sanitized


def host_derived_key_name(base_url: str) -> str:
    """从 host 反推出的环境变量名（``api.deepseek.com`` → ``DEEPSEEK_API_KEY``），或 ``""``。"""
    label = _vendor_label(base_url_hostname(base_url))
    return (label + "_API_KEY") if label else ""


def host_derived_api_key(base_url: str) -> str:
    """从 host 反推出的 ``<VENDOR>_API_KEY`` 值（没配就是 ``""``）。"""
    key_name = host_derived_key_name(base_url)
    if not key_name:
        return ""
    return str(get_secret(key_name, "") or "").strip()


def key_candidates_for(base_url: str, *, ollama: bool = False) -> List[str]:
    """该端点上**允许发送**的 API key 候选（顺序即优先级），不匹配的一律是空串。

    对应真实版 `runtime_provider.py:317-326`：
      * `OLLAMA_API_KEY` —— 只在 ``ollama.com`` 命中、且 ``ollama=True`` 时挂载；
      * `OPENAI_API_KEY` —— 只在 ``openai.com`` / ``openai.azure.com`` 命中时挂载；
      * `OPENROUTER_API_KEY` —— 只在 ``openrouter.ai`` 命中时挂载；
      * `<VENDOR>_API_KEY` —— 从 host 反推（`host_derived_api_key`）。

    返回值里**空串代表\"这把 key 不该发给这个端点\"**，调用方用 `next(非空)` 取第一把。
    """
    is_openai = host_matches(base_url, "openai.com") or host_matches(base_url, "openai.azure.com")
    candidates: List[str] = []
    if ollama:
        candidates.append(
            str(get_secret("OLLAMA_API_KEY", "") or "").strip()
            if host_matches(base_url, "ollama.com") else ""
        )
    candidates.append(
        str(get_secret("OPENAI_API_KEY", "") or "").strip() if is_openai else ""
    )
    candidates.append(
        str(get_secret("OPENROUTER_API_KEY", "") or "").strip()
        if host_matches(base_url, "openrouter.ai") else ""
    )
    candidates.append(host_derived_api_key(base_url))
    return candidates
