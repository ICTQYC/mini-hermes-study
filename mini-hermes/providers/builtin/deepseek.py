"""DeepSeek 供应商画像。

从 `hermes_cli/runtime_provider.py` 旧的硬编码表**无损搬过来**（第 8 章 A2）：
  base_url=https://api.deepseek.com/v1 · api_key_env=DEEPSEEK_API_KEY ·
  base_url_env=DEEPSEEK_BASE_URL · default_model=deepseek-chat · api_mode=chat_completions。

真实版还有一段 DeepSeek 专属的 thinking 钩子（V4 默认开思考 → 必须显式发 ``thinking``），
mini 没有 reasoning 基座，故不搬（抄了也没消费者）。
"""
from __future__ import annotations

from providers import register_provider
from providers.base import ProviderProfile

deepseek = ProviderProfile(
    name="deepseek",
    aliases=("deep-seek", "deep_seek"),
    env_vars=("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
    display_name="DeepSeek",
    description="DeepSeek 原生 API（OpenAI 兼容协议）",
    signup_url="https://platform.deepseek.com/",
    fallback_models=("deepseek-chat",),
    base_url="https://api.deepseek.com/v1",
    default_model="deepseek-chat",
    default_aux_model="deepseek-chat",
)

register_provider(deepseek)
