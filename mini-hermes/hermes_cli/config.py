"""mini-hermes 的**配置层**（第 8 章 A9）—— provider / model / 回退链 / 辅助路由的唯一读法。

对应 Hermes 的 `hermes_cli/config.py`（键定义 + 结构校验）+ `hermes_cli/fallback_config.py`
（回退链的合并 / 去重 / 丢弃坏条目）。

## 为什么要有这一层

**配置**是用户在 `config.json` 里存下的意图；**解析**是运行时把它变成要 POST 的 URL/key。
两者必须分开：前者是数据（一份、可去重、可校验），后者是行为（可被显式参数 / 环境变量覆盖）。
第 8 章 A1 已经把这套顺序落进名字层（`resolve_requested_provider`）：

    显式参数 → config → 环境变量 → auto

★ 本层的规矩与它**一致**：**config 压 env**。理由逐字来自真实版 docstring：

    *Config beats the env so chat uses the endpoint the user last saved, not a stale
     shell/.env override.* —— 一个陈旧的 `export …` 不该盖掉用户存下的选择。

## 本层管哪几个键（**键定义是唯一事实源**，别处不许再拼字符串）

| 键 | 形状 | 缺省 | 谁读它 |
|---|---|---|---|
| `model.provider` | str | 无 → `MINI_INFERENCE_PROVIDER` → `auto` | `resolve_requested_provider`（A1 名字层） |
| `model.model` | str | 无 → `MINI_MODEL` → provider 默认模型 | `resolve_provider`（A1 连接层） |
| `fallback_providers` | list[dict] | `[]`（**没有回退**） | `get_fallback_chain()` → `agent/fallback.py`（A7） |
| `fallback_model` | dict \\| list[dict] | 无（legacy 写法，仅在 `fallback_providers` 缺失时并入） | 同上 |
| `auxiliary.<task>.provider` | str | `"auto"`（= 继承主 agent 路由） | `agent/auxiliary.py`（A8，`main` = 主 provider 别名） |
| `auxiliary.<task>.model` | str | `""`（= 主模型） | 同上 |

回退条目 `{provider, model, base_url?, api_key?/key_env?}`：**缺 provider 或 model 的条目直接丢**
（等于禁用那一条）—— 真实版 `fallback_config.py:69` 同款。凭据读法见 `resolve_entry_api_key`：
内联 `api_key` → `key_env` **经 `get_secret`**（作用域感知，绝不裸读 `os.getenv`）。

坏配置**永远不抛**：一个用户的笔误不该让 agent 起不来（按空配置处理）。这条与
`agent/prompt_builder.py::_read_config` 保持同一口径。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_mini_home

# ══════════════════════════════════════════════════════════════════════════
# 键定义（唯一事实源 —— 含默认值）
# ══════════════════════════════════════════════════════════════════════════

CONFIG_FILENAME = "config.json"

KEY_MODEL = "model"                     # {"provider": str, "model": str}
KEY_MODEL_PROVIDER = "provider"         # model.provider
KEY_MODEL_NAME = "model"                # model.model

KEY_FALLBACK_PROVIDERS = "fallback_providers"   # 回退链（主源，保序）
KEY_FALLBACK_MODEL = "fallback_model"           # legacy：单条 dict 或 list[dict]

KEY_FALLBACK_PROVIDER = "provider"
KEY_FALLBACK_MODEL_NAME = "model"
KEY_FALLBACK_BASE_URL = "base_url"
KEY_FALLBACK_API_KEY = "api_key"
KEY_FALLBACK_KEY_ENV = "key_env"
KEY_FALLBACK_KEY_ENV_ALT = "api_key_env"

KEY_AUXILIARY = "auxiliary"             # {<task>: {"provider": str, "model": str}}
KEY_AUXILIARY_PROVIDER = "provider"
KEY_AUXILIARY_MODEL = "model"

# ── compression 段（第 10 章补上 micro_compact 一族）───────────────────────────
# 与真实版 `compression.*` 键同名同默认（`hermes_cli/config_defaults.py:569-578` +
# `agent/agent_init.py:1506-1515`）。★ 这是**微压缩**（滚动逐交换摘要）的旋钮，
# 是第 5 章批量压缩的兄弟开关。
KEY_COMPRESSION = "compression"
KEY_MICRO_COMPACT = "micro_compact"
KEY_MICRO_COMPACT_EVERY_N_TURNS = "micro_compact_every_n_turns"
KEY_MICRO_COMPACT_DEFRAG_THRESHOLD_TOKENS = "micro_compact_defrag_threshold_tokens"
KEY_PROACTIVE_PRUNE_MIN_RECLAIM_TOKENS = "proactive_prune_min_reclaim_tokens"

# ── context 段（第 14 章：上下文引擎的选择键）────────────────────────────────
# 与真实版 `context.engine` 同名同默认（`hermes_cli/config_defaults.py:1206`）。
# ★ 默认 `"compressor"` 永远指向**内置**实现（上游逐字：*"compressor" = built-in
#   lossy summarization; or a plugin name…*）。用户写别的名字才会走目录发现。
KEY_CONTEXT = "context"
KEY_CONTEXT_ENGINE = "engine"

# 默认值（键不在文件里时就是它们 —— 与真实版逐字对齐）
DEFAULT_MICRO_COMPACT = False                       # 默认关：每次 pass 打破缓存前缀
DEFAULT_MICRO_COMPACT_EVERY_N_TURNS = 1             # ≥1，夹取见 get_compression_config
DEFAULT_MICRO_COMPACT_DEFRAG_THRESHOLD_TOKENS = 2000
DEFAULT_PROACTIVE_PRUNE_MIN_RECLAIM_TOKENS = 4096   # 0 = 无最小回收量闸门

# ★ 上下文引擎默认名（真实版 `config_defaults.py:1206`）：恒指向内置压缩器。
DEFAULT_CONTEXT_ENGINE = "compressor"

# ★ 辅助路由的两个魔法值（真实版 `_normalize_aux_provider` / `_resolve_task_provider_model`）：
AUX_PROVIDER_AUTO = "auto"   # 继承主 agent 当前路由（未配置时的默认）
AUX_PROVIDER_MAIN = "main"   # **别名** —— 解析成主 agent 的 provider

# 缺省值（键不在文件里时就是它们）
DEFAULT_AUX_PROVIDER = AUX_PROVIDER_AUTO
DEFAULT_AUX_MODEL = ""
DEFAULT_FALLBACK_CHAIN: List[Dict[str, Any]] = []


# ══════════════════════════════════════════════════════════════════════════
# 读文件
# ══════════════════════════════════════════════════════════════════════════


def config_path() -> Path:
    """`$MINI_HOME/config.json`（**唯一**路径来源 —— 别处不许硬编码）。"""
    return get_mini_home() / CONFIG_FILENAME


def load_config() -> Dict[str, Any]:
    """读整份 config.json。文件不在 / 坏 JSON / 不是 dict ⇒ 一律返回 `{}`（不抛）。"""
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 —— 见模块 docstring：坏配置按空配置处理
        return {}
    return data if isinstance(data, dict) else {}


def _section(config: Optional[Dict[str, Any]], key: str) -> Dict[str, Any]:
    raw = config if isinstance(config, dict) else load_config()
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


# ── model 段 ─────────────────────────────────────────────────────────────


def get_model_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`model` 段（A1 名字层读 `model.provider`；A9 补上 `model.model`）。"""
    return _section(config, KEY_MODEL)


def get_configured_provider(config: Optional[Dict[str, Any]] = None) -> str:
    """`model.provider`（空串 = 没配）。名字层的第 2 层就是它。"""
    value = get_model_config(config).get(KEY_MODEL_PROVIDER)
    return str(value).strip() if isinstance(value, str) else ""


def get_configured_model(config: Optional[Dict[str, Any]] = None) -> str:
    """`model.model`（空串 = 没配）。连接层的第 2 层是它（在环境变量之前）。"""
    value = get_model_config(config).get(KEY_MODEL_NAME)
    return str(value).strip() if isinstance(value, str) else ""


# ── 回退链（A7 的数据源）──────────────────────────────────────────────────


def _iter_fallback_entries(raw: Any, *, source: str) -> List[Dict[str, Any]]:
    """把一段原始配置摊成**干净的条目列表**；缺 provider/model 的条目丢掉。

    真实版 `hermes_cli/fallback_config.py:69` 逐字：

        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue

    ★ 为什么\"丢\"而不是\"报错\"：一条写作笔误等于\"禁用那一条\"，不该让整条链失效
      （这正是配置校验的两种哲学之一，真实版选了宽松的那个）。
    """
    items: List[Any] = []
    if isinstance(raw, dict):
        items = [raw]
    elif isinstance(raw, list):
        items = list(raw)
    out: List[Dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get(KEY_FALLBACK_PROVIDER) or "").strip()
        model = str(entry.get(KEY_FALLBACK_MODEL_NAME) or "").strip()
        if not provider or not model:
            continue
        clean: Dict[str, Any] = {KEY_FALLBACK_PROVIDER: provider, KEY_FALLBACK_MODEL_NAME: model}
        base_url = str(entry.get(KEY_FALLBACK_BASE_URL) or "").strip()
        if base_url:
            clean[KEY_FALLBACK_BASE_URL] = base_url
        api_key = str(entry.get(KEY_FALLBACK_API_KEY) or "").strip()
        if api_key:
            clean[KEY_FALLBACK_API_KEY] = api_key
        key_env = str(entry.get(KEY_FALLBACK_KEY_ENV) or entry.get(KEY_FALLBACK_KEY_ENV_ALT) or "").strip()
        if key_env:
            clean[KEY_FALLBACK_KEY_ENV] = key_env
        out.append(clean)
    return out


def get_fallback_chain(config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """回退链：合并 `fallback_providers`（主源、**保序**）与 legacy `fallback_model`，
    按 `(provider, model, base_url)` 去重。空 = **没有回退**。

    对应真实版 `hermes_cli/fallback_config.py:89-106`。

    ★ 为什么\"保序\"是契约而不是实现细节：链是**按序消费**的（A7 的 `_fallback_index`
      每次只前进一格），顺序变了优先级就变了。所以去重时**保留第一次出现的**。
    ★ 返回的条目**只含干净字段**（provider/model 必有；base_url/api_key/key_env 可选），
      调用方（`agent/fallback.py`）不需要再防御性判断。
    """
    raw = config if isinstance(config, dict) else load_config()
    entries: List[Dict[str, Any]] = []
    entries.extend(_iter_fallback_entries(raw.get(KEY_FALLBACK_PROVIDERS), source=KEY_FALLBACK_PROVIDERS))
    entries.extend(_iter_fallback_entries(raw.get(KEY_FALLBACK_MODEL), source=KEY_FALLBACK_MODEL))

    out: List[Dict[str, Any]] = []
    seen: set = set()
    for entry in entries:
        key = (entry[KEY_FALLBACK_PROVIDER], entry[KEY_FALLBACK_MODEL_NAME],
               entry.get(KEY_FALLBACK_BASE_URL, ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def resolve_entry_api_key(entry: Dict[str, Any]) -> Optional[str]:
    """一条回退条目自己的凭据：内联 `api_key` → `key_env`（**经 `get_secret`**）。

    对应真实版 `hermes_cli/fallback_config.py:23`。★ 这条是**凭据隔离**的一半：
    每条回退自己解析自己的 key，主 provider 的凭据**绝不复用**给回退 provider。
    ★ 走 `get_secret` 而不是 `os.getenv`：多 profile 下别的 profile 的 `.env`
      不会串味（第 9 章的作用域语义）。
    """
    if not isinstance(entry, dict):
        return None
    inline = str(entry.get(KEY_FALLBACK_API_KEY) or "").strip()
    if inline:
        return inline
    key_env = str(entry.get(KEY_FALLBACK_KEY_ENV) or entry.get(KEY_FALLBACK_KEY_ENV_ALT) or "").strip()
    if not key_env:
        return None
    from agent.secret_scope import get_secret

    return str(get_secret(key_env, "") or "").strip() or None


# ── auxiliary 段（A8 的数据源）────────────────────────────────────────────


def get_auxiliary_config(task: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """某个辅助任务的 `{provider, model}`（缺省 `provider="auto"`、`model=""`）。

    真实版读法（`auxiliary_client.py:5784`）是 `auxiliary.<task>.provider` /
    `auxiliary.<task>.model`，未配置时 provider 落 `"auto"`。
    ★ `"auto"` 的语义 = **继承主 agent 当前路由**；`"main"` 是它的近亲
      （**别名**，解析成主 agent 的 provider）—— 两者的区别由 `agent/auxiliary.py` 落实。
    """
    entry = {}
    section = _section(config, KEY_AUXILIARY)
    if isinstance(task, str) and task and isinstance(section.get(task), dict):
        entry = section[task]
    provider = str(entry.get(KEY_AUXILIARY_PROVIDER) or "").strip() or DEFAULT_AUX_PROVIDER
    model = str(entry.get(KEY_AUXILIARY_MODEL) or "").strip()
    return {KEY_AUXILIARY_PROVIDER: provider, KEY_AUXILIARY_MODEL: model}


# ── compression 段（第 10 章的旋钮，A9 的延续）──────────────────────────────


def parse_config_int(raw: Any, default: int) -> int:
    """严格整型转换 —— 与真实版 `agent_init.py:338 _parse_config_int` 逐条对齐。

    ★ 三条刻意的严格：
      · **拒绝 bool**：YAML 里 `true` 会被解析成 `1`，于是 `micro_compact_every_n_turns: true`
        会静默变成"每回合一次" —— 一个真值开关不该冒充一个数字。
      · **拒绝小数**：`2.5` 不是合法的"每 N 回合"。
      · 字符串尽力 `int()`（配置文件里写 `"3"` 也认），其余一律回落 default。
    """
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _config_flag(raw: Any, default: bool) -> bool:
    """`compression` 段的宽松真值解析（真实版 `_cfg_flag`）：
    `str(value).lower() in {"true","1","yes"}`；缺省 key 时给 default。"""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"true", "1", "yes"}


def get_compression_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`compression` 段里**微压缩相关的四个键**（已夹取，调用方不必再防御）。

    对应真实版 `agent_init.py:1506-1515` 的四次 `max(…, _parse_config_int(...))`：

    | 键 | 默认 | 夹取 |
    |---|---|---|
    | `micro_compact` | `False` | 真值解析 |
    | `micro_compact_every_n_turns` | `1` | `max(1, int)` |
    | `micro_compact_defrag_threshold_tokens` | `2000` | `max(1, int)` |
    | `proactive_prune_min_reclaim_tokens` | `4096` | `max(0, int)`（0 = 无闸门） |

    ★ 为什么要**在这一层**夹取而不是在消费处：配置是**数据**（一份、可校验），
      消费处（压缩器 / 微压缩）只该拿"已经合法"的数 —— 否则每加一个消费者就要重写一遍
      `max(1, …)`。这与第 8 章 A9 的口径一致。
    ★ `proactive_prune_min_reclaim_tokens` 是**对照组**：它证明"恢复量闸门"是真实版
      给**批量剪枝**的旋钮，而微压缩**没有**这个键（第 10 章原文自己点出）。
    """
    section = _section(config, KEY_COMPRESSION)
    return {
        KEY_MICRO_COMPACT: _config_flag(section.get(KEY_MICRO_COMPACT), DEFAULT_MICRO_COMPACT),
        KEY_MICRO_COMPACT_EVERY_N_TURNS: max(
            1, parse_config_int(section.get(KEY_MICRO_COMPACT_EVERY_N_TURNS), DEFAULT_MICRO_COMPACT_EVERY_N_TURNS)
        ),
        KEY_MICRO_COMPACT_DEFRAG_THRESHOLD_TOKENS: max(
            1, parse_config_int(
                section.get(KEY_MICRO_COMPACT_DEFRAG_THRESHOLD_TOKENS),
                DEFAULT_MICRO_COMPACT_DEFRAG_THRESHOLD_TOKENS,
            )
        ),
        KEY_PROACTIVE_PRUNE_MIN_RECLAIM_TOKENS: max(
            0, parse_config_int(
                section.get(KEY_PROACTIVE_PRUNE_MIN_RECLAIM_TOKENS),
                DEFAULT_PROACTIVE_PRUNE_MIN_RECLAIM_TOKENS,
            )
        ),
    }


# ── context 段（第 14 章：选择上下文引擎）────────────────────────────────────

def get_context_engine_name(config: Optional[Dict[str, Any]] = None) -> str:
    """`context.engine` 的值（缺省 / 非串 / 空串 ⇒ ``"compressor"``）。**绝不抛。**

    ★ 三段式与 `get_configured_provider` / `get_auxiliary_config` 一致：
      `_section` 拿段（坏段按空 dict 处理）→ 取值 → 非字符串/空白一律落默认。
      `load_config()` 已经是"坏配置按空配置"，所以这里连"文件不存在"都免判。
    ★ 为什么默认值**在配置层**定死成 ``"compressor"``：这是"零配置 = 与改造前
      逐字节一致"（不变量 I4）的载体 —— `run_agent._compressor()` 拿到
      ``"compressor"`` 就走内置分支，一步不多。
    """
    value = _section(config, KEY_CONTEXT).get(KEY_CONTEXT_ENGINE)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_CONTEXT_ENGINE


__all__ = [
    "CONFIG_FILENAME",
    "KEY_AUXILIARY", "KEY_FALLBACK_MODEL", "KEY_FALLBACK_PROVIDERS", "KEY_MODEL",
    "KEY_COMPRESSION", "KEY_MICRO_COMPACT", "KEY_MICRO_COMPACT_EVERY_N_TURNS",
    "KEY_MICRO_COMPACT_DEFRAG_THRESHOLD_TOKENS", "KEY_PROACTIVE_PRUNE_MIN_RECLAIM_TOKENS",
    "KEY_CONTEXT", "KEY_CONTEXT_ENGINE",
    "AUX_PROVIDER_AUTO", "AUX_PROVIDER_MAIN", "DEFAULT_AUX_PROVIDER", "DEFAULT_FALLBACK_CHAIN",
    "DEFAULT_MICRO_COMPACT", "DEFAULT_MICRO_COMPACT_EVERY_N_TURNS",
    "DEFAULT_MICRO_COMPACT_DEFRAG_THRESHOLD_TOKENS", "DEFAULT_PROACTIVE_PRUNE_MIN_RECLAIM_TOKENS",
    "DEFAULT_CONTEXT_ENGINE",
    "config_path", "load_config",
    "get_model_config", "get_configured_provider", "get_configured_model",
    "get_fallback_chain", "resolve_entry_api_key",
    "get_auxiliary_config", "get_compression_config", "get_context_engine_name", "parse_config_int",
]
