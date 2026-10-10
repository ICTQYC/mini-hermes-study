"""Anthropic 供应商画像。

从旧硬编码表无损搬过来，**保留 `api_mode="anthropic_messages"`** ——
这个字段不是装饰：它让解析链如实说出\"这个端点讲 native Messages，不是 chat/completions\"，
于是 `run_agent` 会给出\"mini 还没实现该协议\"的**带指引**报错，而不是把请求发出去才炸。

  base_url=https://api.anthropic.com/v1 · api_key_env=ANTHROPIC_API_KEY ·
  default_model=claude-sonnet-4-5 · api_mode=anthropic_messages。
"""
from __future__ import annotations

from providers import register_provider
from providers.base import ProviderProfile

anthropic = ProviderProfile(
    name="anthropic",
    aliases=("claude", "claude-code", "claude_oauth"),
    api_mode="anthropic_messages",
    env_vars=("ANTHROPIC_API_KEY",),
    display_name="Anthropic",
    description="Anthropic 原生 Messages API（mini 尚未实现该协议）",
    signup_url="https://console.anthropic.com/",
    base_url="https://api.anthropic.com/v1",
    default_model="claude-sonnet-4-5",
)

register_provider(anthropic)
