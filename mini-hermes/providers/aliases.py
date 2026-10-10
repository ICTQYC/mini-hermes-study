"""供应商别名归一（A10）。

对应 Hermes 的 `hermes_cli/providers.py::ALIASES` / `auth.py::_PROVIDER_ALIASES`。

**为什么必须有这一层**：用户会写 `claude` / `codex` / `deep-seek` / `ollama`……
如果每个入口各自 `if` 一遍，迟早会漏一处 —— 于是\"同一个供应商，换个写法就报未知\"。
归一到一个 **canonical id** 之后，注册表 / 自动探测 / host 门控全都能用同一个键。

★ 与真实版的一处刻意差别：真实版把 `openai` 归一成 `openrouter`（聚合器优先）。
  mini 的 `openai` 是**它自己**（api.openai.com），所以这里的表**不含** `openai→openrouter`
  —— 否则会改掉 mini 既有的 `--provider openai` 行为。
"""
from __future__ import annotations

from typing import Dict, Tuple

# ── 别名分组：canonical id → (该 id 的别名…) ───────────────────────────────
# 只收 mini 真的认识的供应商 + 真实版里最常被写错的几个。
# 真实版表（`providers.py:128-139`）有几十家；mini 按\"架构优先\"只留能落地的。
_ALIAS_GROUPS: Dict[str, Tuple[str, ...]] = {
    "deepseek": ("deep-seek", "deep_seek"),
    "anthropic": ("claude", "claude-code", "claude_oauth"),
    # ★ A10 点名的例子：codex ↔ openai-codex。真实版两者都指 Codex 订阅通道。
    "openai-codex": ("codex", "openai_codex", "chatgpt-codex"),
    "openai": ("openai-compatible", "openai_compatible", "oai"),
    "openrouter": ("open-router", "openrouter.ai"),
    # 本地服务器别名 —— 全部走通用 custom 供应商（真实版 `auth.py:1186-1187`）
    "custom": ("ollama", "local", "vllm", "llamacpp", "llama.cpp", "llama-cpp"),
}

# 反查表：alias → canonical
ALIASES: Dict[str, str] = {
    alias: canon for canon, aliases in _ALIAS_GROUPS.items() for alias in aliases
}


def normalize_provider(name: str) -> str:
    """把任意写法归一到 canonical provider id。

    顺序：**内置别名表 → 注册表里各 profile 自己声明的别名 → 原样小写**。
    第三步保证\"未知名字\"原样穿过（不吞掉用户输入、也不乱报错）——
    真实版 `normalize_provider` 逐字：``ALIASES.get(key, key)``。
    """
    key = str(name or "").strip().lower()
    if not key:
        return ""
    if key in ALIASES:
        return ALIASES[key]
    # 动态层：插件/内置 profile 声明的别名（懒导入，避免 import 环）
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(key)
        if profile is not None:
            return profile.name
    except Exception:  # noqa: BLE001 —— 注册表不可用时按\"没有别名\"处理
        pass
    return key


def canonical_names() -> Tuple[str, ...]:
    """内置分组里声明的 canonical 名字（给人看的诊断用）。"""
    return tuple(_ALIAS_GROUPS)
