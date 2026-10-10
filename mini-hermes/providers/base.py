"""供应商画像（ProviderProfile）—— 一个供应商的全部声明式事实。

对应 Hermes 的 `providers/base.py`。

**为什么要有这个类**：真实版里\"供应商\"是一堆散落的布尔开关（要不要发 temperature、
用哪个 auth 头、默认模型是谁……）。把它们收进一个 **dataclass**，传输层读这个对象，
而不是收 20 多个参数。这就是第 8 章 A2 的核心：**注册表是唯一事实源**。

profile 是**声明式**的：它描述这个供应商长什么样，
**不**负责建 HTTP 客户端、不负责凭据轮换、不负责流式 —— 那些留在 agent 里。

★ 与真实版的一处刻意差别：真实版的默认模型来自各家模型目录（`fetch_models` / catalog）；
  mini 只在 profile 上放一个 `default_model` 字段（够用且不撒谎 —— 它就是个默认值）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

# ★ 哨兵：表示\"**整个不发** temperature 字段\"（真实版给 Kimi 用 —— 服务端自己管）。
#   为什么用对象而不是 None：None 要留给\"用调用方的默认值\"这个语义，
#   两种\"不发\"必须能区分开。见真实版 `providers/base.py:21`。
OMIT_TEMPERATURE = object()


def _is_url_env(name: str) -> bool:
    """这个环境变量名是**端点**（不是密钥）吗？

    真实版 `auth.py:269` 逐字：``is_url = lambda v: v.endswith("_BASE_URL") or v.endswith("_URL")``。
    注册时按这个把 `env_vars` 拆成\"密钥列表\"和\"base_url 变量\"两份
    —— 所以 `env_vars` 一个字段就能同时声明这两类，不用两个字段。
    """
    upper = (str(name or "")).upper()
    return upper.endswith("_BASE_URL") or upper.endswith("_URL")


def _derive_hostname(base_url: str) -> str:
    raw = (base_url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else "//" + raw)
    except ValueError:
        return ""
    return (parsed.hostname or "").lower().rstrip(".")


@dataclass
class ProviderProfile:
    """一个供应商的声明式描述（子类可覆写钩子；绝大多数直接用实例）。"""

    # ── 身份 ───────────────────────────────────────────────────────────
    name: str
    api_mode: str = "chat_completions"
    aliases: Tuple[str, ...] = ()

    # ── 展示 ───────────────────────────────────────────────────────────
    display_name: str = ""       # 例如 "DeepSeek"
    description: str = ""        # 选择器副标题
    signup_url: str = ""         # 引导用户去注册

    # ── 认证 / 端点 ────────────────────────────────────────────────────
    # ★ env_vars 是\"优先级有序\"的一份名单：真实版 `auth.py:156` 注释即 \"priority order\"。
    #   约定 = **按顺序取第一个可用者**；`_is_url_env` 把其中的端点和密钥分开。
    env_vars: Tuple[str, ...] = ()
    base_url: str = ""
    models_url: str = ""         # 显式模型端点；空则回落到 {base_url}/models
    auth_type: str = "api_key"   # api_key | oauth_device_code | oauth_external | copilot | aws_sdk | external_process
    supports_health_check: bool = True
    supports_model_listing: bool = True

    # ── 视觉 / 请求形状 quirk ──────────────────────────────────────────
    supports_vision: bool = False
    supports_vision_tool_messages: bool = True
    supports_prompt_cache_key: bool = False

    # ── 模型目录 ───────────────────────────────────────────────────────
    fallback_models: Tuple[str, ...] = ()
    hostname: str = ""           # 显式主机名；空则从 base_url 反推

    # ── 客户端 quirk ───────────────────────────────────────────────────
    default_headers: Dict[str, str] = field(default_factory=dict)

    # ── 请求 quirk ─────────────────────────────────────────────────────
    # None = 用调用方默认；OMIT_TEMPERATURE = 不发这个字段
    fixed_temperature: Any = None
    default_max_tokens: Optional[int] = None
    default_aux_model: str = ""  # 辅助任务（压缩/总结）用的便宜模型；空 = 复用主模型

    # ── mini 补充：默认模型 ────────────────────────────────────────────
    # 真实版从模型目录挑；mini 只需要一个\"没传就它\"的默认值。
    default_model: str = ""

    # ── 派生视图 ───────────────────────────────────────────────────────
    @property
    def api_key_env_vars(self) -> Tuple[str, ...]:
        """密钥类环境变量（**有序**），按真实版 `_register_plugin_provider` 的拆法：

        ``tuple(v for v in env_vars if not is_url(v)) or env_vars`` ——
        若一个 `_URL` 都没声明（老写法把所有名都当密钥），就原样返回。
        """
        keys = tuple(v for v in self.env_vars if not _is_url_env(v))
        return keys or tuple(self.env_vars)

    @property
    def base_url_env_var(self) -> str:
        """端点类环境变量名（取第一个），或 ``""``。

        对应真实版 ``next((v for v in pp.env_vars if is_url(v)), None) or ""``。
        """
        for var in self.env_vars:
            if _is_url_env(var):
                return var
        return ""

    def get_hostname(self) -> str:
        """显式 hostname，否则从 base_url 反推。"""
        return (self.hostname or _derive_hostname(self.base_url)).strip().lower()
