"""provider → (api_mode, base_url, api_key, model, source) 的解析 —— **两段式**（A1）。

对应 Hermes 的 hermes_cli/runtime_provider.py + hermes_cli/auth.py。

这一层存在的原因：模型和 provider 是**两件独立的事**。
同一个模型名可以走 OpenAI 官方、OpenRouter、本地 vLLM、国内 MaaS……
所以配置里存的是"provider 名字"，运行时才解析成真正要 POST 的 URL 和 key。

═══ 第 8 章 A1：解析分两段 ═════════════════════════════════════════════════
真实版的解析被拆成**名字段**和**运行时段**两层，五入口（CLI/TUI/gateway/cron/ACP）
只调同一道门面。mini 照抄这道形状：

    ① 名字层  resolve_requested_provider(requested)   —— \"请求的是谁\"（一个字符串）
                 显式 → config → env → auto；裁决理由落在 .source
    ② 连接层  resolve_provider(provider, model)        —— 只看名字定\"凭据/base_url/api_mode\"
                 从 providers/ 注册表拿（唯一事实源）
    ③ 门面    resolve_runtime_provider(...)            —— ①+② 合成一份 dict（含 source）

  「名字层」不碰凭据（没有 key 也能说出\"你要的是谁\"）；
  「连接层」不猜名字（只认注册表里的 canonical id）。

★ 向后兼容：老的 `resolve_provider(provider, model)` **原样保留**（仍是连接层，
  返回同样的 dict）—— `cli.py` / `gateway/run.py` / `doctor.py` 的调用点一行都不用改。

密钥只从环境变量 / .env 文件读，永不写进代码、永不打印
（Hermes 的铁律：secrets in .env, settings in config.yaml）。

═══════════════════════════════════════════════════════════════════════
给别人跑的时候要注意（这是本文件唯一"不通用"的地方）：

密钥文件的查找顺序（先找到的赢，前面的不会覆盖后面的环境变量）：
    1. 环境变量 MINI_ENV_FILE 指的路径
    2. ~/.mini-hermes/.env          ← 给别人用就写这里（$MINI_HOME/.env）
    3. ~/.hermes/.env               ← 方便本机：直接复用你已有的 Hermes 密钥

真实的 Hermes 用的是一条完整的凭证解析链（OAuth / 凭证池 / key 轮换 /
多 profile），这里只留了最朴素的版本。
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_mini_home

# ★ 第 9 章：凭据读取走**密钥作用域**（`get_secret` 提供"作用域 → 兜底"的完整四分支，
#   含"多路复用下没有作用域就抛"的 fail-closed 行为）。import 提到模块级，
#   免得每次读密钥都重新 import。
from agent.secret_scope import get_secret

# ★ 第 8 章 A9：配置层（键定义 / model / fallback 链 / auxiliary 的唯一读法）。
from hermes_cli.config import get_configured_model

# ★ 第 8 章：解析的事实源与判据都搬进了 `providers/` 包（A2/A3/A5/A10）。
from providers import get_provider_profile, list_providers
from providers.aliases import normalize_provider
from providers.api_mode import fallback_api_mode
from providers.host_gate import host_derived_key_name, key_candidates_for

# 已经打过"跳过进程级 .env 加载"日志的 home（真实版同名集合 `_SCOPED_SKIP_LOGGED`）
_SCOPED_SKIP_LOGGED: set = set()

# 这些供应商**不从环境变量自动选**（真实版 `_NO_AUTO_DETECT_PROVIDERS`）：
#   copilot 的 token 常为仓库/工具访问而存在，不该劫持推理；lmstudio 是本地服务器，
#   有 LM_API_KEY 不代表它在线。两者都需要用户显式选。
_NO_AUTO_DETECT_PROVIDERS = frozenset({"copilot", "lmstudio"})


def provider_registry() -> Dict[str, Dict[str, Any]]:
    """`PROVIDER_REGISTRY` 的**派生视图**（注册表是唯一事实源，这里只是它的一种读法）。

    ★ 第 8 章 A2 的关键：以前这是一张**硬编码**的表（数据存了两份）；现在它从
      `providers.list_providers()` 派生 —— 任何 `register_provider()` 进来的画像都自动可见。
    键名沿用旧表（base_url / api_key_env / base_url_env / default_model / api_mode），
    以免打破老消费者的读法。
    """
    view: Dict[str, Dict[str, Any]] = {}
    for profile in list_providers():
        key_envs = profile.api_key_env_vars
        view[profile.name] = {
            "base_url": profile.base_url,
            "api_key_env": key_envs[0] if key_envs else "",
            "base_url_env": profile.base_url_env_var,
            "default_model": profile.default_model,
            "api_mode": profile.api_mode,
        }
    return view


def __getattr__(name: str) -> Any:
    """PEP 562：`PROVIDER_REGISTRY` 懒构造（import 期不触发文件系统发现，也不 mkdir home）。"""
    if name == "PROVIDER_REGISTRY":
        return provider_registry()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ── 模型 → 上下文窗口（token）────────────────────────────────────────────
# ★ 这张表是**外部事实**，会过时，所以：一条一行、带来源、允许环境变量覆盖。
#
# 为什么必须知道这个数：压缩阈值 = 窗口 × 0.50。窗口填**大**了 → 永远不压缩 →
# 溢出报错；填**小**了 → 压得太勤 → 多花辅助调用的钱。**宁可填小**。
#
# 查证过的：
#   deepseek-chat  128_000  —— DeepSeek 官方 API 文档：deepseek-chat / deepseek-reasoner
#                              对应 DeepSeek-V3.2，128K 上下文。查证于 2026-09-23。
#                              ⚠️ 若该别名已指向 V4（1M），要重新查证再改这一行。
# 没查证的**不填**（不猜）：例如 gpt-4o-mini / claude-sonnet-4-5 目前落到下面的
# MODEL_CONTEXT_DEFAULT。要用它们就先查官方文档，再把数字连同来源加进来。
MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    "deepseek-chat": 128_000,
}
# 表里没有的模型用这个（保守值：压早一点，不冒溢出的风险）
MODEL_CONTEXT_DEFAULT = 64_000


def resolve_context_length(model: str) -> int:
    """模型 → 窗口大小。环境变量 MINI_CONTEXT_LENGTH 优先（用来试不同窗口）。

    环境变量的值解析不出来时**报错**而不是静默用默认值 —— 静默的话你会以为
    自己设了 8_000、实际在用 64_000，压缩行为完全对不上。"""
    raw = os.environ.get("MINI_CONTEXT_LENGTH", "").strip()
    if raw:
        try:
            val = int(raw)
        except ValueError:
            raise RuntimeError(f"MINI_CONTEXT_LENGTH 必须是整数，收到 {raw!r}") from None
        if val <= 0:
            raise RuntimeError(f"MINI_CONTEXT_LENGTH 必须为正，收到 {val}")
        return val
    return MODEL_CONTEXT_WINDOWS.get(model, MODEL_CONTEXT_DEFAULT)


def env_file_candidates() -> List[Path]:
    """密钥文件的候选路径，按优先级排序。"""
    out: List[Path] = []
    override = os.environ.get("MINI_ENV_FILE")
    if override:
        out.append(Path(override).expanduser())
    out.append(get_mini_home() / ".env")           # 自己的（推荐给别人用）
    out.append(Path.home() / ".hermes" / ".env")   # 本机便利：复用 Hermes 的
    return out


def _multiplex_dotenv_boundary_active() -> bool:
    """当前是否处在"多路复用的 profile-home 覆盖"之下（真实版 `env_loader.py:340` 的判据）。

    判据就是真实版那一行：`is_multiplex_active() and get_hermes_home_override() is not None` ——
    **两个条件都要**：

    * 只开着多路复用、但没有覆盖 ⇒ 这是**启动期**的进程级加载，**照旧**（否则默认 profile 自己的
      `.env` 都读不进来，网关起不来）；
    * 有覆盖 ⇒ 说明正"替某个 profile 干活"，此刻把它的 `.env` 灌进进程环境就是泄漏。

    ★ 日志只打一次（每个 home 一条）：import 期 / 每回合都会走到这里，不节流会刷屏
      （真实版同名集合 `_SCOPED_SKIP_LOGGED`）。
    """
    try:
        from agent.secret_scope import multiplex_active
        from hermes_constants import current_home_override
    except Exception:                        # 老代码 / 极早期 import：按"没有边界"处理
        return False
    if not multiplex_active():
        return False
    override = current_home_override()
    if not override:
        return False
    key = str(override)
    if key not in _SCOPED_SKIP_LOGGED:
        _SCOPED_SKIP_LOGGED.add(key)
        print(f"[mini] 多路复用：跳过进程级 .env 加载（home={key}）"
              f"—— 该 profile 的凭据只经作用域解析")
    return True


def load_env_file(path: Optional[Path] = None) -> List[Path]:
    """把密钥文件里的键读进 os.environ（**不覆盖已存在的环境变量**）。

    真实环境变量永远优先于文件 —— 这样 CI / 容器里能直接覆盖，不用改文件。

    返回：实际读到并生效的文件列表（好让调用方/测试能断言读了哪个）。

    ★ **多路复用边界**（对应真实版 `hermes_cli/env_loader.py:334-350`，逐字）：

        Multiplex gateway: while a routed profile-home override is active, copying that profile's
        .env into os.environ would expose its credentials to sibling turns and every spawned child.
        Unscoped startup loads keep the normal path; external sources still refresh against the
        profile mapping.

      即：**有 profile-home 覆盖在生效时，这里直接跳过进程级加载并返回 `[]`**。
      为什么必须在**加载器**这一层挡：fail-closed（`get_secret`）管的是"读"，管不了"灌" ——
      一旦别人的 .env 已经进了 `os.environ`，任何读 `os.environ` 的代码（包括子进程）都绕过了作用域。
    """
    if _multiplex_dotenv_boundary_active():
        return []
    candidates = [path] if path is not None else env_file_candidates()
    used: List[Path] = []

    for env_path in candidates:
        if not env_path.exists():
            continue
        used.append(env_path)
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            # ★ 只认**非空**值：先出现的文件里的空行**不算数**，继续往下找。
            #
            # 不这样做的后果（实测过）：在 `$MINI_HOME/.env` 里留一行
            # `DEEPSEEK_API_KEY=`（准备以后填），因为跳过判据是"**键名**是否已存在"，
            # 它会先把键设成空字符串，于是后面 `~/.hermes/.env` 里那个**真值**被当成
            # "已存在"跳过 —— 报错只说"密钥为空"，绝口不提是哪个文件干的。
            if key and val and key not in os.environ:
                os.environ[key] = val
    return used


# ══════════════════════════════════════════════════════════════════════════
# ① 名字层：resolve_requested_provider —— "请求的是谁"（A1 第 1 段 / A4 source）
# ══════════════════════════════════════════════════════════════════════════


class RequestedProvider(str):
    """名字层结果：它**是 `str`**（`== "deepseek"` / `.lower()` 都照旧可用），
    额外挂一个 `.source` 说明这个决定是**怎么来的**。

    为什么用 str 子类：真实版的 `resolve_requested_provider` 返回字符串；
    A4 又要求把"裁决理由"落成数据。一个带 `.source` 的 str 两边都满足，
    而且老的字符串用法（拼接 / 比较 / 当 dict 键）全都不受影响。
    """

    source: str

    def __new__(cls, name: str, source: str) -> "RequestedProvider":
        obj = super().__new__(cls, name)
        obj.source = source
        return obj


def _config_model_config() -> Dict[str, Any]:
    """从 `config.json` 取 `model` 段（对应真实版 `_get_model_config`）。

    ★ 第 8 章 A9 起：读法**委托给配置层**（`hermes_cli/config.py`）—— 键定义与
      "坏配置不抛"的口径只存一份，名字层/连接层不再各自解析文件。
    名字层用 `model.provider`；连接层用 `model.model`（见 `resolve_provider`）。
    """
    from hermes_cli.config import get_model_config

    return get_model_config()


def resolve_requested_provider(requested: Optional[str] = None) -> str:
    """**名字层**：显式 arg → config.yaml → env → auto（返回一个带 `.source` 的字符串）。

    真实版 `runtime_provider.py:410-419` 的 docstring 逐字解释了顺序：

        Provider request from explicit arg, then config, then ``HERMES_INFERENCE_PROVIDER``,
        else "auto". Config beats the env so chat uses the endpoint the user last saved,
        not a stale shell/.env override.

    ★ **config 压过 env**（刻意）：否则一个陈旧的 `export MINI_INFERENCE_PROVIDER=…`
      会悄悄盖掉用户在 config 里存下的选择。
    ★ 这一层**不读凭据、不碰注册表** —— 没有 key 也能说出"你要的是谁"。
    """
    if requested and str(requested).strip():
        return RequestedProvider(str(requested).strip().lower(), "explicit")   # 第 1 层
    cfg_provider = _config_model_config().get("provider")
    if isinstance(cfg_provider, str) and cfg_provider.strip():
        return RequestedProvider(cfg_provider.strip().lower(), "config")       # 第 2 层
    env_provider = os.environ.get("MINI_INFERENCE_PROVIDER", "").strip().lower()
    if env_provider:
        return RequestedProvider(env_provider, "env")                          # 第 3 层
    return RequestedProvider("auto", "auto")                                  # 第 4 层


# ══════════════════════════════════════════════════════════════════════════
# ② 连接层：resolve_provider —— 只看名字，定 (凭据 / base_url / api_mode)
# ══════════════════════════════════════════════════════════════════════════


def _resolve_auto_provider() -> str:
    """`auto` 的落地：按**注册顺序**扫，第一个有可用环境密钥的 api_key 供应商胜出。

    真实版 `auth.py:1304-1319 _env_key_auto_detected`（`_NO_AUTO_DETECT_PROVIDERS` 排除
    copilot/lmstudio）。mini 只保留这一段"按 env key 自动选"，没有 OAuth / free-tier /
    Bedrock 那些基座（那是 B 批，不抄）。
    """
    for profile in list_providers():
        if profile.auth_type != "api_key" or profile.name in _NO_AUTO_DETECT_PROVIDERS:
            continue
        for env_var in profile.api_key_env_vars:
            if str(get_secret(env_var, "") or "").strip():
                return profile.name
    raise RuntimeError(
        "没有配置任何 provider 可选（provider='auto'）。\n"
        "任选一种：\n"
        "  1) 用 --provider 显式指定（如 deepseek）\n"
        "  2) 在 config.json 写 model.provider\n"
        "  3) 设 MINI_INFERENCE_PROVIDER\n"
        "  4) 给某个供应商配好密钥（如 DEEPSEEK_API_KEY）"
    )


def _resolve_api_key(profile: Any, base_url: str, *, explicit_api_key: Optional[str],
                     explicit_base_url: Optional[str]) -> str:
    """连接层的凭据解析（真实版 `_host_gated_env_key_candidates` + 注册表密钥的合成）。

    候选顺序：
      ① 显式 CLI 凭据（`--api-key`）—— 用户当场给的，最优先；
      ② 注册表声明的密钥（按 `env_vars` **优先级顺序**）—— **只在端点没被换成别的 host 时**用；
      ③ host 门控候选（A5）—— 每把 key 只发给自己那条域名。

    第 ② 步加"端点是否被换"的守卫，正是 A5 的安全语义：把一个已声明密钥发到一个
    被 `--base-url` 换成陌生域名的端点上，就是凭据泄漏。
    """
    candidates: List[str] = []
    if explicit_api_key and explicit_api_key.strip():
        candidates.append(explicit_api_key.strip())
    if not explicit_base_url and profile.api_key_env_vars:
        for env_var in profile.api_key_env_vars:
            candidates.append(str(get_secret(env_var, "") or "").strip())
    candidates.extend(key_candidates_for(base_url))
    return next((c for c in candidates if c), "")


def _missing_key_error(profile: Any, base_url: str) -> RuntimeError:
    """密钥为空的报错 —— 必须包含\"下一步该做什么\"（别人第一次跑卡住的地方就在这）。"""
    key_envs = profile.api_key_env_vars
    var_name = key_envs[0] if key_envs else (host_derived_key_name(base_url) or "API_KEY")
    own_env = get_mini_home() / ".env"
    return RuntimeError(
        f"provider {profile.name} 缺少密钥：环境变量 {var_name} 为空。\n"
        f"任选一种：\n"
        f"  1) 写进密钥文件（推荐，不会进 shell 历史、不会出现在进程列表里）：\n"
        f"       echo '{var_name}=你的key' >> {own_env}\n"
        f"  2) 设环境变量：export {var_name}=你的key\n"
        f"已查找的密钥文件：{', '.join(str(p) for p in env_file_candidates())}"
    )


def resolve_provider(provider: str, model: Optional[str] = None, *,
                     source: Optional[str] = None,
                     explicit_api_key: Optional[str] = None,
                     explicit_base_url: Optional[str] = None) -> Dict[str, Any]:
    """**连接层**：把一个 provider 名字解析成真正要用的 API 参数（A1 第 2 段）。

    ★ 这一段的入口与返回值**与旧版逐字兼容**（`resolve_provider(provider, model)` → 同样的 dict，
      多一个 `source` 键）—— `cli.py` / `gateway/run.py` / `doctor.py` 一律不用改。
    ★ 解析不出来就明确报错，不猜。
    """
    load_env_file()

    name = normalize_provider(provider) if provider else provider
    if name == "auto":
        name = _resolve_auto_provider()
        source = source or "auto"

    profile = get_provider_profile(name)
    if profile is None:
        raise RuntimeError(
            f"未知 provider：{provider}（已注册：{', '.join(provider_registry())}）"
        )

    # ── 端点：profile 默认 → 供应商自己的 base_url_env → 显式 --base-url ──
    base_url = profile.base_url
    if profile.base_url_env_var:
        base_url = os.environ.get(profile.base_url_env_var, base_url) or base_url
    if explicit_base_url:
        base_url = explicit_base_url
    base_url = base_url.rstrip("/")
    if not base_url:
        raise RuntimeError(
            f"provider {profile.name} 没有端点：请设置 {profile.base_url_env_var or 'base_url'} 或用 --base-url 指定"
        )

    # ── 凭据：走密钥作用域（第 9 章），并做 host 门控（A5）──
    api_key = _resolve_api_key(profile, base_url,
                               explicit_api_key=explicit_api_key,
                               explicit_base_url=explicit_base_url)
    if not api_key:
        raise _missing_key_error(profile, base_url)

    # ── 模型：显式参数 → config（A9）→ `MINI_MODEL`（env）→ provider 默认模型 ──
    #   ★ 与名字层**同一条顺序**：config 压 env（陈旧的 shell 变量不该盖掉用户存下的选择）。
    #   ★ `MINI_MODEL` 是**行为设置**（不是凭据），所以直读 `os.environ` 是合规的
    #     （对照 `agent/secret_scope.py` 的名单：凭据一律走 `get_secret`）。
    model_name = (model
                  or get_configured_model()
                  or os.environ.get("MINI_MODEL", "").strip()
                  or profile.default_model
                  or (profile.fallback_models[0] if profile.fallback_models else ""))
    # ★ 第 8 章 A3：api_mode 由 host 探测 + provider 叠加决定，不是入口硬编码。
    api_mode = fallback_api_mode(profile.name, base_url, model_name)

    return {
        "provider": profile.name,
        "api_mode": api_mode,
        "base_url": base_url,
        "api_key": api_key,
        "model": model_name,
        # ★ 窗口大小随 spec 一起走 —— 压缩阈值 = 窗口 × threshold，
        #   没有它就只能吃默认值，等于"假装知道模型多大"。
        "context_length": resolve_context_length(model_name),
        # ★ A4：裁决理由（显式 / config / env / auto）落成数据。
        "source": source or "explicit",
    }


# ══════════════════════════════════════════════════════════════════════════
# ③ 门面：resolve_runtime_provider —— 五入口共用的唯一出口（A1）
# ══════════════════════════════════════════════════════════════════════════


def resolve_runtime_provider(*, requested: Optional[str] = None,
                             model: Optional[str] = None,
                             explicit_api_key: Optional[str] = None,
                             explicit_base_url: Optional[str] = None) -> Dict[str, Any]:
    """**唯一运行时门面**：名字层 → 连接层，合成一份 dict。

    真实版 `runtime_provider.py:851` 的对应物：五个入口（CLI/TUI/gateway/cron/ACP）
    都只调它，不各自解析。返回 dict 固定含
    `provider / api_mode / base_url / api_key / source / requested_provider`。

    `source` 四取值（A4）：
      * `"explicit"` —— 显式 `--provider` / 运行时参数；
      * `"config"`   —— config.json 的 `model.provider`；
      * `"env"`      —— `MINI_INFERENCE_PROVIDER`；
      * `"auto"`     —— 都没给，按环境密钥自动选。
    """
    requested_name = resolve_requested_provider(requested)          # ① 名字层
    source = getattr(requested_name, "source", "auto")
    provider = normalize_provider(str(requested_name))              # 别名归一（A10）
    runtime = resolve_provider(provider, model, source=source,      # ② 连接层
                               explicit_api_key=explicit_api_key,
                               explicit_base_url=explicit_base_url)
    runtime["source"] = source
    runtime["requested_provider"] = str(requested_name)
    return runtime
