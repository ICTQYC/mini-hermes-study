"""通用 OpenAI 兼容供应商画像。

「OpenAI 兼容」是 mini 里**绝大多数**端点的形状：一个 `/v1/chat/completions`、一把 bearer key。
这里登记的是 **OpenAI 官方端点**（旧表的 `openai` 条目，无损搬过来），
它同时充当\"OpenAI 兼容协议\"的参考画像。

  base_url=https://api.openai.com/v1 · api_key_env=OPENAI_API_KEY ·
  default_model=gpt-4o-mini · api_mode=chat_completions。

真实版里有几十家这样的画像（fireworks / upstage / nebius…），mini 按\"架构优先\"只留一个参考实现。
"""
from __future__ import annotations

from providers import register_provider
from providers.base import ProviderProfile

# 名字保持 "openai"（不是 "openai_compatible"）：`--provider openai` 是既有对外行为，
# 归一只加别名、不改 canonical 名。别名那几条（openai-compatible…）让两种写法都命中同一 profile。
openai = ProviderProfile(
    name="openai",
    aliases=("openai-compatible", "openai_compatible", "oai"),
    env_vars=("OPENAI_API_KEY",),
    display_name="OpenAI",
    description="OpenAI 官方 API（OpenAI 兼容协议的参考实现）",
    signup_url="https://platform.openai.com/",
    fallback_models=("gpt-4o-mini",),
    base_url="https://api.openai.com/v1",
    default_model="gpt-4o-mini",
)

register_provider(openai)
