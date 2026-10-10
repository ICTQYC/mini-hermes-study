"""第 8 章（provider-runtime）的自测：两段式解析 / 注册表 / api_mode / host 门控 / 别名 —— 不联网。

跑法：python3 tests/test_provider_runtime.py

## 测的是什么（6 组）

| 组 | 守的承诺 |
|---|---|
| 1. A2 注册表是唯一事实源 | 硬编码表变成**派生视图**（数据只存一份）/ 插件式注册 / 懒发现 / 别名同指一个 profile |
| 2. A1 两段式 + A4 source | 名字层（显式→config→env→auto）不碰凭据；连接层只认注册表；门面 dict 带 source |
| 3. A3 api_mode 判定 | host 表 / 官方 OpenAI 主机族 / 路径后缀；**拒伪装 host**；未实现模式给**带指引**的报错 |
| 4. A5 host 门控 | 每把 key 只发给自己那条域名；host 反推 `<VENDOR>_API_KEY`；伪装 host 抢不走真 key |
| 5. A10 别名归一 | `codex↔openai-codex` 等；大小写无关；未知名字原样穿过；别名真的走进解析链 |
| 6. 端到端 | 同一个 DeepSeek 走**显式 / config / env / auto** 四条来源，`source` 栏各不相同 |

## 隔离

`MINI_HOME` 指向临时目录（import 之前设好），并把 `env_file_candidates` 收窄成**只读这个临时 .env**
—— 否则会读到本机 `~/.hermes/.env` 里的真密钥/真端点，测试结果就依赖运行环境了
（实测：本机那个文件里就有 `DEEPSEEK_BASE_URL`，会把端点换成另一台）。

只写**契约/不变量**断言：不断言模型清单长度、不断言具体行数、不做"改变检测器"。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TEST_HOME = tempfile.mkdtemp(prefix="mini_prov_test_")
os.environ["MINI_HOME"] = _TEST_HOME

import hermes_cli.runtime_provider as rp  # noqa: E402
import providers as pkg  # noqa: E402
from providers import get_provider_profile, list_providers, register_provider  # noqa: E402
from providers.aliases import normalize_provider  # noqa: E402
from providers.api_mode import (  # noqa: E402
    IMPLEMENTED_API_MODES,
    VALID_API_MODES,
    UnsupportedApiMode,
    detect_api_mode,
    fallback_api_mode,
    require_implemented_api_mode,
)
from providers.base import ProviderProfile  # noqa: E402
from providers.host_gate import (  # noqa: E402
    host_derived_api_key,
    host_derived_key_name,
    host_matches,
    key_candidates_for,
)

_ENV_FILE = Path(_TEST_HOME) / ".env"
_ENV_FILE.write_text("DEEPSEEK_API_KEY=sk-fake-deepseek\n", encoding="utf-8")
_CONFIG_FILE = Path(_TEST_HOME) / "config.json"

# ★ 只让 mini 读这个临时 .env —— 测试不许依赖运行环境的配置。
rp.env_file_candidates = lambda: [_ENV_FILE]

# 这些键来自真实环境/别的文件时会让结果漂移，纳入统一管理
_MANAGED_KEYS = (
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY", "OPENROUTER_API_KEY", "OLLAMA_API_KEY", "MINI_INFERENCE_PROVIDER",
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {detail}" if detail and not cond else ""))


def _clear() -> None:
    for key in _MANAGED_KEYS:
        os.environ.pop(key, None)
    if _CONFIG_FILE.exists():
        _CONFIG_FILE.unlink()


def _set_config_provider(provider: str) -> None:
    _CONFIG_FILE.write_text(json.dumps({"model": {"provider": provider}}), encoding="utf-8")


def _raised(fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
        return False
    except Exception:  # noqa: BLE001
        return True


# ══════════════════════════════════════════════ 1. A2 注册表（唯一事实源 / 懒发现 / 插件式注册）
def test_lazy_discovery_runs_on_first_lookup() -> None:
    """★ **懒发现**：import 不碰注册表；第一次查才 import 各 profile。

    反向验证：把 `get_provider_profile` 里那句 `if not _discovered: _discover_providers()`
    删掉 → 本组第 2 条立刻变红（内置画像查不到）。
    """
    check("import 之后还没做发现（懒）", pkg._discovered is False, str(pkg._discovered))
    profile = get_provider_profile("deepseek")
    check("第一次查 → 触发发现并命中内置画像", profile is not None and profile.name == "deepseek",
          repr(profile))
    check("发现只做一次（幂等）", pkg._discovered is True)


def test_builtin_profiles_present() -> None:
    """内置画像至少三家：deepseek / openai / anthropic（旧硬编码表无损搬过来）。"""
    names = {p.name for p in list_providers()}
    check("deepseek / openai / anthropic 都在注册表里",
          {"deepseek", "openai", "anthropic"} <= names, str(sorted(names)))


def test_profile_fields_match_the_ported_table() -> None:
    """旧表的判据字段（端点 / 密钥 env / 端点 env / 协议）必须无损搬到 profile 上。

    只断言**契约**（这几个字段还在、还是那个语义），不锁具体 URL 之外的细节。
    """
    ds = get_provider_profile("deepseek")
    check("deepseek 的密钥 env（有序清单）", ds.api_key_env_vars == ("DEEPSEEK_API_KEY",),
          str(ds.api_key_env_vars))
    check("deepseek 的端点 env 被认出来（DEEPSEEK_BASE_URL）",
          ds.base_url_env_var == "DEEPSEEK_BASE_URL", repr(ds.base_url_env_var))
    an = get_provider_profile("anthropic")
    check("anthropic 声明了 anthropic_messages 协议",
          an.api_mode == "anthropic_messages" and an.api_mode in VALID_API_MODES, an.api_mode)
    check("每家 profile 的 api_mode 都是合法取值",
          all(p.api_mode in VALID_API_MODES for p in list_providers()))


def test_registry_view_is_derived_not_duplicated() -> None:
    """★ A2 的要害：`PROVIDER_REGISTRY` 是**派生视图** —— 数据只存一份（在 profile 上）。

    反向验证：把 `provider_registry()` 改回返回一张写死的 dict → 第 2 条变红
    （新注册的画像不会出现在视图里）。
    """
    view = rp.PROVIDER_REGISTRY
    ds = get_provider_profile("deepseek")
    check("视图的 base_url 与 profile 同源", view["deepseek"]["base_url"] == ds.base_url)
    check("视图的 api_mode 与 profile 同源", view["deepseek"]["api_mode"] == ds.api_mode)

    marker = ProviderProfile(
        name="mini_test_derived", env_vars=("MINI_TEST_DERIVED_API_KEY",),
        base_url="https://derived.example/v1", default_model="m", api_mode="chat_completions",
    )
    register_provider(marker)
    try:
        check("★ 新注册的画像自动出现在派生视图里（唯一事实源）",
              "mini_test_derived" in rp.PROVIDER_REGISTRY, str(sorted(rp.PROVIDER_REGISTRY)))
    finally:
        pkg._REGISTRY.pop("mini_test_derived", None)
        pkg._PROVIDER_LIST_CACHE = None


def test_register_provider_last_writer_wins() -> None:
    """同名后注册者覆盖先注册者 —— 用户插件因此能顶掉内置画像，不必改仓库。"""
    original = get_provider_profile("deepseek")
    replacement = ProviderProfile(name="deepseek", base_url="https://overridden.example/v1")
    register_provider(replacement)
    try:
        check("后注册者赢（同名覆盖）", get_provider_profile("deepseek") is replacement)
    finally:
        register_provider(original)     # 复原全局状态
    check("复原成功（profile 对象回到原样）",
          get_provider_profile("deepseek").base_url_env_var == "DEEPSEEK_BASE_URL")


def test_env_vars_split_contract() -> None:
    """`env_vars` 一个字段声明两类：`_BASE_URL`/`_URL` 结尾是**端点**，其余是**密钥**。"""
    mixed = ProviderProfile(name="x", env_vars=("X_API_KEY", "X_BASE_URL"))
    check("密钥清单剔掉端点变量", mixed.api_key_env_vars == ("X_API_KEY",), str(mixed.api_key_env_vars))
    check("端点变量被单独认出", mixed.base_url_env_var == "X_BASE_URL", repr(mixed.base_url_env_var))
    keyonly = ProviderProfile(name="y", env_vars=("Y_API_KEY",))
    check("没有端点变量时 base_url_env_var 为空", keyonly.base_url_env_var == "")
    check("没有端点变量时密钥清单原样", keyonly.api_key_env_vars == ("Y_API_KEY",), str(keyonly.api_key_env_vars))


def test_alias_lookup_hits_same_profile() -> None:
    """按别名查到的 profile 与按 canonical 查到的**是同一个对象**。"""
    check("deep-seek → deepseek", get_provider_profile("deep-seek").name == "deepseek")
    check("claude → anthropic",
          get_provider_profile("claude") is get_provider_profile("anthropic"))
    check("openai-compatible → openai", get_provider_profile("openai-compatible").name == "openai")


def test_list_providers_dedup() -> None:
    """别名不产生重复条目 —— 每个 canonical name 只出现一次。"""
    names = [p.name for p in list_providers()]
    check("list_providers 无重复 canonical 名", len(names) == len(set(names)), str(names))
    check("两次调用结果一致（缓存不串）", {p.name for p in list_providers()} == set(names))


# ══════════════════════════════════════════════ 2. A1 两段式 + A4 source
def test_requested_provider_explicit_is_a_name() -> None:
    """★ 名字层返回**字符串**（不是 dict）—— 且裁决理由是 explicit。"""
    name = rp.resolve_requested_provider("  DeepSeek ")
    check("显式名字被 strip + lower", name == "deepseek" and isinstance(name, str), repr(name))
    check("它是 str（老的字符串用法全都不受影响）", isinstance(name, str))
    check("source = explicit", getattr(name, "source", "") == "explicit", str(getattr(name, "source", "")))


def test_requested_provider_config_beats_env() -> None:
    """★ **config 压过 env**（刻意）：陈旧的 shell 变量不该盖掉用户存下的选择。"""
    _clear()
    _set_config_provider("deepseek")
    os.environ["MINI_INFERENCE_PROVIDER"] = "openai"
    try:
        name = rp.resolve_requested_provider()
        check("config 层的值胜出", name == "deepseek", repr(name))
        check("source = config", name.source == "config", name.source)
    finally:
        _clear()


def test_requested_provider_env_when_no_config() -> None:
    _clear()
    os.environ["MINI_INFERENCE_PROVIDER"] = "openai"
    try:
        name = rp.resolve_requested_provider()
        check("没有 config 时用环境变量", name == "openai", repr(name))
        check("source = env", name.source == "env", name.source)
    finally:
        _clear()


def test_requested_provider_auto_is_the_last_rung() -> None:
    _clear()
    name = rp.resolve_requested_provider()
    check("都没有 → auto", name == "auto", repr(name))
    check("source = auto", name.source == "auto", name.source)


def test_name_layer_needs_no_credentials() -> None:
    """★ 名字层**不碰凭据**：一个 key 都没有，也能说出"你要的是谁"。"""
    _clear()
    name = rp.resolve_requested_provider("deepseek")
    check("无任何密钥也能解析名字（不抛）", name == "deepseek")


def test_two_stages_compose() -> None:
    """名字层的输出直接喂给连接层 —— 两段是\"字符串 → dict\"的关系。"""
    _clear()
    name = rp.resolve_requested_provider("deepseek")
    spec = rp.resolve_provider(name, "deepseek-chat")
    check("名字层的输出能当连接层的输入", spec["provider"] == "deepseek", spec.get("provider"))
    check("名字层返回 str、连接层返回 dict（两段形状不同）",
          isinstance(name, str) and isinstance(spec, dict))


def test_back_compat_resolve_provider_contract() -> None:
    """★ 老调用点 `resolve_provider(provider, model)` 一行都不用改 —— dict 契约照旧。"""
    _clear()
    spec = rp.resolve_provider("deepseek", "deepseek-chat")
    for key in ("provider", "api_mode", "base_url", "api_key", "model", "context_length"):
        check(f"旧 dict 键还在：{key}", key in spec, str(sorted(spec)))
    check("model 参数生效", spec["model"] == "deepseek-chat", spec.get("model"))
    check("context_length 一路带上（压缩阈值靠它）", spec["context_length"] == 128_000,
          str(spec.get("context_length")))
    check("多了一个 source 键（A4）", spec["source"] == "explicit", str(spec.get("source")))


def test_facade_returns_documented_shape() -> None:
    """门面返回固定字段集（含 source / requested_provider）。"""
    _clear()
    runtime = rp.resolve_runtime_provider(requested="deepseek")
    for key in ("provider", "api_mode", "base_url", "api_key", "source", "requested_provider"):
        check(f"门面 dict 含 {key}", key in runtime, str(sorted(runtime)))
    check("requested_provider 记的是**请求的名字**", runtime["requested_provider"] == "deepseek")
    check("provider 是解析后的 canonical id", runtime["provider"] == "deepseek")


# ══════════════════════════════════════════════ 3. A3 api_mode 判定 + 未实现模式的指引
def test_detect_api_mode_by_host_and_path() -> None:
    """协议由**端点**决定：host 强制表 / 官方 OpenAI 主机族 / URL 路径后缀。"""
    check("api.anthropic.com → anthropic_messages",
          detect_api_mode("https://api.anthropic.com/v1") == "anthropic_messages")
    check("api.x.ai → codex_responses（host 强制表）",
          detect_api_mode("https://api.x.ai/v1") == "codex_responses")
    check("api.openai.com → codex_responses（官方 OpenAI 主机族）",
          detect_api_mode("https://api.openai.com/v1") == "codex_responses")
    check("us. 数据驻留子域也算官方主机族",
          detect_api_mode("https://us.api.openai.com/v1") == "codex_responses")
    check("URL 路径以 /anthropic 结尾 → anthropic_messages",
          detect_api_mode("https://proxy.example/anthropic") == "anthropic_messages")
    check("无信号 → None（交给 provider 叠加层）",
          detect_api_mode("https://api.deepseek.com/v1") is None)


def test_detect_api_mode_rejects_lookalikes() -> None:
    """★ 只用**精确 hostname**（含点后缀）匹配 —— 伪装 host 一律不算命中。

    反向验证：把 host 判据从 \"hostname == domain or endswith(.domain)\"
    改成 `domain in base_url`（子串）→ 前两条立刻变红。
    """
    check("api.anthropic.com.attacker.test 不算真端点",
          detect_api_mode("https://api.anthropic.com.attacker.test/v1") is None)
    check("路径里塞 api.openai.com 也不算（path 伪装）",
          detect_api_mode("https://evil.example/api.openai.com/v1") is None)
    check("evil.com/openai.com 不算官方主机",
          detect_api_mode("https://evil.com/openai.com/v1") is None)


def test_fallback_api_mode_three_step() -> None:
    """三段式兜底：探测 → provider 叠加 → chat_completions。"""
    check("deepseek 默认 chat_completions",
          fallback_api_mode("deepseek", "https://api.deepseek.com/v1") == "chat_completions")
    check("端点协议**压过** provider 声明（anthropic host）",
          fallback_api_mode("anthropic", "https://api.anthropic.com") == "anthropic_messages")
    check("无 host 信号时用 provider 声明的协议（profile 叠加层）",
          fallback_api_mode("anthropic", "http://localhost:1234/v1") == "anthropic_messages")


def test_require_implemented_gives_guidance() -> None:
    """★ 未实现 / 未知的 api_mode 给**带指引**的报错（不是裸 NotImplementedError）。

    反向验证：把 `require_implemented_api_mode` 改回 `raise NotImplementedError`
    → 第 2 条变红（异常类型不对）。
    """
    check("已实现的模式放行（chat_completions）",
          require_implemented_api_mode("chat_completions") is None)
    exc = None
    try:
        require_implemented_api_mode("anthropic_messages")
    except Exception as e:  # noqa: BLE001
        exc = e
    check("未实现模式抛 UnsupportedApiMode（不是 NotImplementedError）",
          isinstance(exc, UnsupportedApiMode) and not isinstance(exc, NotImplementedError),
          type(exc).__name__ if exc else "没抛")
    text = str(exc or "")
    check("报错说清了 mini **支持哪几种**", "chat_completions" in text, text[:60])
    check("报错点名了**该改哪个文件**", "_build_api_messages" in text or "adapter" in text, text[:80])
    unk = None
    try:
        require_implemented_api_mode("banana")
    except Exception as e:  # noqa: BLE001
        unk = e
    check("未知模式也报错且点名（未知）", isinstance(unk, UnsupportedApiMode) and "未知" in str(unk),
          str(unk)[:40])
    check("已实现的集合里只有 chat_completions", set(IMPLEMENTED_API_MODES) == {"chat_completions"})


def test_agent_build_api_messages_raises_guided_error() -> None:
    """跑真 agent：未实现协议在**构建消息**时就给带指引的报错。"""
    from run_agent import AIAgent

    ag = AIAgent({"provider": "anthropic", "api_mode": "chat_completions",
                  "base_url": "http://x", "api_key": "k", "model": "m"})
    check("chat_completions 直接产出线上消息",
          ag._build_api_messages([{"role": "user", "content": "hi"}])[0]["content"] == "hi")
    ag.api_mode = "anthropic_messages"
    raised = None
    try:
        ag._build_api_messages([{"role": "user", "content": "hi"}])
    except Exception as e:  # noqa: BLE001
        raised = e
    check("未实现协议在发请求**之前**就报错", raised is not None)
    check("★ 抛的是带指引的 UnsupportedApiMode（不再裸 NotImplementedError）",
          isinstance(raised, UnsupportedApiMode) and not isinstance(raised, NotImplementedError),
          type(raised).__name__ if raised else "没抛")


# ══════════════════════════════════════════════ 4. A5 host 门控
def test_host_matches_exact_and_suffix() -> None:
    check("子域命中（api.openai.com ⊂ openai.com）", host_matches("https://api.openai.com/v1", "openai.com"))
    check("精确命中", host_matches("https://openai.com/v1", "openai.com"))
    check("★ 伪装后缀不命中（openai.com.attacker.test）",
          not host_matches("https://api.openai.com.attacker.test/v1", "openai.com"))
    check("★ 路径里塞域名不命中（evil.com/openai.com）",
          not host_matches("https://evil.com/openai.com/v1", "openai.com"))
    check("别的域不命中", not host_matches("https://api.deepseek.com/v1", "openai.com"))


def test_key_candidates_are_host_gated() -> None:
    """★ **每把 key 只发给自己那条域名**（反向验证：把 `host_matches` 改成子串 → 第 2 条变红）。"""
    saved = {k: os.environ.get(k) for k in ("OPENAI_API_KEY", "OPENROUTER_API_KEY")}
    os.environ["OPENAI_API_KEY"] = "sk-openai"
    os.environ["OPENROUTER_API_KEY"] = "sk-openrouter"
    try:
        openai_cands = key_candidates_for("https://api.openai.com/v1")
        check("OpenAI 端点拿到 OPENAI_API_KEY", "sk-openai" in openai_cands, str(openai_cands))
        check("★ OpenAI 端点**拿不到** OPENROUTER_API_KEY",
              "sk-openrouter" not in openai_cands, str(openai_cands))
        or_cands = key_candidates_for("https://openrouter.ai/api/v1")
        check("OpenRouter 端点拿到 OPENROUTER_API_KEY", "sk-openrouter" in or_cands, str(or_cands))
        check("★ 反过来也成立（拿不到 OPENAI_API_KEY）", "sk-openai" not in or_cands, str(or_cands))
        check("无关端点两把 key 都不给",
              key_candidates_for("https://api.deepseek.com/v1")[:2] == ["", ""]
              or not any(key_candidates_for("https://api.deepseek.com/v1")[:2]),
              str(key_candidates_for("https://api.deepseek.com/v1")))
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_host_derived_key_follows_the_registrable_domain() -> None:
    """从 host 反推 `<VENDOR>_API_KEY`；伪装 host 抢不走真 key。"""
    saved = {k: os.environ.get(k) for k in ("DEEPSEEK_API_KEY", "ATTACKER_API_KEY")}
    os.environ["DEEPSEEK_API_KEY"] = "sk-deepseek"
    os.environ["ATTACKER_API_KEY"] = "sk-attacker"
    try:
        check("api.deepseek.com → DEEPSEEK_API_KEY",
              host_derived_api_key("https://api.deepseek.com/v1") == "sk-deepseek",
              repr(host_derived_api_key("https://api.deepseek.com/v1")))
        check("★ 伪装 host 推出 ATTACKER_API_KEY（不是 DEEPSEEK）",
              host_derived_api_key("https://api.deepseek.com.attacker.test/v1") == "sk-attacker",
              repr(host_derived_api_key("https://api.deepseek.com.attacker.test/v1")))
        real = key_candidates_for("https://api.deepseek.com.attacker.test/v1")
        check("★ 伪装 host 的候选里**没有**真 DeepSeek key", "sk-deepseek" not in real, str(real))
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_host_derived_never_openai_openrouter_ollama() -> None:
    """OPENAI / OPENROUTER / OLLAMA 有自己的门控路径，不给\"伪装 host\"抢 key 的机会。"""
    for url in ("https://api.openai.com/v1", "https://openrouter.ai/api/v1", "https://ollama.com/v1"):
        check(f"{url} 推不出 <VENDOR>_API_KEY（属于专用门控）",
              host_derived_key_name(url) == "", host_derived_key_name(url))


def test_foreign_base_url_never_receives_declared_key() -> None:
    """★ 把 deepseek 的声明密钥发到陌生端点 = 泄漏 —— 连接层必须拦住。"""
    _clear()
    check("声明密钥不会跟着 --base-url 跑到陌生域名",
          _raised(rp.resolve_provider, "deepseek", explicit_base_url="https://evil.example.com/v1"))
    spec = rp.resolve_provider("deepseek", "deepseek-chat", explicit_api_key="sk-explicit",
                               explicit_base_url="https://evil.example.com/v1")
    check("显式给的 key 用显式端点（用户当场交的，就用它）", spec["api_key"] == "sk-explicit",
          repr(spec.get("api_key")))
    check("显式端点也如实反映在 base_url 上",
          spec["base_url"] == "https://evil.example.com/v1", spec["base_url"])


# ══════════════════════════════════════════════ 5. A10 别名归一
def test_normalize_alias_table() -> None:
    """A10 点名的例子 + 大小写无关 + 未知名字原样穿过。"""
    check("codex → openai-codex", normalize_provider("codex") == "openai-codex")
    check("chatgpt-codex 也 → openai-codex", normalize_provider("chatgpt-codex") == "openai-codex")
    check("claude → anthropic", normalize_provider("claude") == "anthropic")
    check("Claude（大小写）也归一", normalize_provider("Claude") == "anthropic")
    check("deep-seek → deepseek", normalize_provider("deep-seek") == "deepseek")
    check("ollama → custom（本地服务器别名）", normalize_provider("ollama") == "custom")
    check("★ 未知名字原样穿过（不吞输入、不乱报错）",
          normalize_provider("whatever-xyz") == "whatever-xyz")


def test_alias_flows_through_resolution() -> None:
    """别名不是摆设：按别名也能真的解析出那个 provider。"""
    _clear()
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
    try:
        spec = rp.resolve_provider("claude", "claude-sonnet-4-5")
        check("按别名 claude 解析到 canonical id = anthropic", spec["provider"] == "anthropic",
              spec.get("provider"))
        check("别名的协议也照 profile 走（anthropic_messages）",
              spec["api_mode"] == "anthropic_messages", spec.get("api_mode"))
    finally:
        _clear()


# ══════════════════════════════════════════════ 6. 端到端：同一供应商，四条来源 source 各不相同
def test_four_sources_all_reach_deepseek() -> None:
    """★ A1/A4 收尾：同一个 DeepSeek 走 显式 / config / env / auto，`source` 栏各不相同。

    反向验证：把 `resolve_requested_provider` 里 `config` 那一段去掉
    → config 来源退化成 auto，本组第 3 条变红。
    """
    _clear()
    os.environ["DEEPSEEK_API_KEY"] = "sk-fake-deepseek"

    explicit = rp.resolve_runtime_provider(requested="deepseek")

    _set_config_provider("deepseek")
    configured = rp.resolve_runtime_provider()
    _clear()
    os.environ["DEEPSEEK_API_KEY"] = "sk-fake-deepseek"

    os.environ["MINI_INFERENCE_PROVIDER"] = "deepseek"
    from_env = rp.resolve_runtime_provider()
    os.environ.pop("MINI_INFERENCE_PROVIDER", None)

    auto = rp.resolve_runtime_provider()
    _clear()

    got = {r["source"] for r in (explicit, configured, from_env, auto)}
    check("★ 四条来源的 source 各不相同（显式/config/env/auto）",
          got == {"explicit", "config", "env", "auto"}, str(sorted(got)))
    check("四条来源都解析到同一个 provider（deepseek）",
          {r["provider"] for r in (explicit, configured, from_env, auto)} == {"deepseek"},
          str([r["provider"] for r in (explicit, configured, from_env, auto)]))
    check("config 来源真的是 config，不是 auto", configured["source"] == "config", configured["source"])
    check("auto 兜底也真的选出了 deepseek（有它的密钥）", auto["provider"] == "deepseek", auto["provider"])
    check("端点不随来源变化（source 只解释\"怎么来的\"）",
          len({r["base_url"] for r in (explicit, configured, from_env, auto)}) == 1,
          str({r["base_url"] for r in (explicit, configured, from_env, auto)}))
    check("api_mode 也不随来源变化", len({r["api_mode"] for r in (explicit, configured, from_env, auto)}) == 1)


GROUPS = [
    ("1. A2 注册表是唯一事实源（懒发现 / 派生视图 / 插件式注册）",
     "硬编码表 → 派生视图（数据只存一份）/ 别名同指一个 profile / 后注册者赢",
     [test_lazy_discovery_runs_on_first_lookup,
      test_builtin_profiles_present,
      test_profile_fields_match_the_ported_table,
      test_registry_view_is_derived_not_duplicated,
      test_register_provider_last_writer_wins,
      test_env_vars_split_contract,
      test_alias_lookup_hits_same_profile,
      test_list_providers_dedup]),
    ("2. A1 两段式 + A4 source",
     "名字层不碰凭据 / config 压 env / 门面 dict 带 source / 老的 resolve_provider 契约照旧",
     [test_requested_provider_explicit_is_a_name,
      test_requested_provider_config_beats_env,
      test_requested_provider_env_when_no_config,
      test_requested_provider_auto_is_the_last_rung,
      test_name_layer_needs_no_credentials,
      test_two_stages_compose,
      test_back_compat_resolve_provider_contract,
      test_facade_returns_documented_shape]),
    ("3. A3 api_mode 判定 + 未实现模式的指引报错",
     "host 表 / 官方 OpenAI 主机族 / 路径后缀；**拒伪装 host**；带指引报错（非 NotImplementedError）",
     [test_detect_api_mode_by_host_and_path,
      test_detect_api_mode_rejects_lookalikes,
      test_fallback_api_mode_three_step,
      test_require_implemented_gives_guidance,
      test_agent_build_api_messages_raises_guided_error]),
    ("4. A5 host 门控的 key",
     "每把 key 只发给自己那条域名 / host 反推 <VENDOR>_API_KEY / 伪装 host 抢不走真 key",
     [test_host_matches_exact_and_suffix,
      test_key_candidates_are_host_gated,
      test_host_derived_key_follows_the_registrable_domain,
      test_host_derived_never_openai_openrouter_ollama,
      test_foreign_base_url_never_receives_declared_key]),
    ("5. A10 别名归一",
     "codex↔openai-codex 等 / 大小写无关 / 未知名字原样穿过 / 别名真的走进解析链",
     [test_normalize_alias_table, test_alias_flows_through_resolution]),
    ("6. 端到端：同一供应商四条来源 source 各不相同",
     "显式 / config / env / auto 都到 deepseek，但 source 栏能看出差别",
     [test_four_sources_all_reach_deepseek]),
]


def main() -> None:
    print(f"解释器：{sys.executable}  Python {sys.version.split()[0]}")
    print(f"MINI_HOME = {_TEST_HOME}\n")

    # ★ 自检：有没有 test_ 函数定义了却没登记进 GROUPS（"写了但从来没跑"是测试里最经典的静默失败）。
    registered = {t.__name__ for _, _, tests in GROUPS for t in tests}
    defined = {name for name, obj in globals().items()
               if name.startswith("test_") and callable(obj)}
    missing = sorted(defined - registered)
    if missing:
        print(f"⚠️  有测试函数没登记进 GROUPS（写了但不会跑）：{missing}")
        raise SystemExit(2)

    for title, note, tests in GROUPS:
        print(f"\n══ {title} ══ {note}")
        before_pass, before_fail = len(PASS), len(FAIL)
        for t in tests:
            t()
        print(f"   → {len(PASS) - before_pass} 通过"
              + (f" / {len(FAIL) - before_fail} 失败" if len(FAIL) > before_fail else ""))

    print(f"\n{len(PASS)} 通过 / {len(FAIL)} 失败　（{len(GROUPS)} 组）")
    if FAIL:
        print("失败项：" + "; ".join(FAIL))
    _clear()
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
