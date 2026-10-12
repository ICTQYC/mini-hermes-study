"""第 8 章 A7/A8/A9 的自测：**接线的**回退链 / 辅助模型路由 / 配置层 —— 不联网。

跑法：python3 tests/test_fallback_and_aux.py

## 测的是什么（4 组）

| 组 | 守的承诺 |
|---|---|
| 1. A7 判据（二元）+ A9 配置层读法 | 429/5xx/401/403 可回退、400/422 不可；链的合并/保序/去重/丢坏条目；`model` 一栏 **config 压 env** |
| 2. A7 状态机 | 链来自构造入参或 config；索引**一次前进一格**；链耗尽**恒 False**；原地换 5 项 + `_client_kwargs`；按轮次复位 |
| 3. A7 接线（真 agent） | 429 → 换 provider **并重发**；400 → **不回退**；耗尽 → 交原始错误；**跨轮不复用兜底**；docstring 的承诺与实现一致 |
| 4. A8 辅助路由 + 消费者 | 显式 > config > auto；`main` = 主 provider 别名；未配置 ≡ 主模型（逐字）；配了就走它；aux **不消费**回退链 |

## 隔离

`MINI_HOME` 指向临时目录（import 之前设好），并且**连 `pathlib.Path.home` 一起钉住**
—— profiles 根是 HOME 锚定的（故意不看 MINI_HOME），不钉就会写用户的真实 home
（第 7 章踩过的同一条规矩）。`env_file_candidates` 收窄成只读这个临时 `.env`，
否则会读到本机 `~/.hermes/.env` 的真密钥/真端点，结果就依赖运行环境了。

## 断言口径

只写**契约/不变量**（一次只前进一格 / 400 不回退 / 耗尽恒 False / 未配置 ≡ 主模型 /
`main` → 主 provider / 跨轮复位）。不断言模型清单长度、不断言具体行数、不做"改变检测器"。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TEST_HOME = tempfile.mkdtemp(prefix="mini_fallback_test_")
os.environ["MINI_HOME"] = _TEST_HOME

# ★ 钉住 Path.home（profile 根是 HOME 锚定的）—— 不钉会写用户真实 home。
_REAL_PATH_HOME = pathlib.Path.home
_FAKE_ROOT = Path(_TEST_HOME) / "fakehome"
(_FAKE_ROOT / ".mini-hermes").mkdir(parents=True, exist_ok=True)
pathlib.Path.home = staticmethod(lambda: _FAKE_ROOT)

import hermes_cli.config as cfg  # noqa: E402
import hermes_cli.runtime_provider as rp  # noqa: E402
import run_agent  # noqa: E402
from agent import auxiliary as aux  # noqa: E402
from agent import fallback as fb  # noqa: E402
from run_agent import AIAgent, ApiHTTPError  # noqa: E402

# 只让 mini 读这个临时 .env（否则会读到本机 ~/.hermes/.env 的真值）
_ENV_FILE = Path(_TEST_HOME) / ".env"
_ENV_FILE.write_text(
    "DEEPSEEK_API_KEY=sk-from-env-file\n"
    "OPENAI_API_KEY=sk-openai-env\n"
    "ANTHROPIC_API_KEY=sk-anthropic-env\n",
    encoding="utf-8",
)
rp.env_file_candidates = lambda: [_ENV_FILE]

_CONFIG_FILE = Path(_TEST_HOME) / "config.json"

# 这些键来自真实环境/别的文件时会让结果漂移，纳入统一管理
_MANAGED_KEYS = (
    "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "DEEPSEEK_BASE_URL", "MINI_MODEL", "MINI_INFERENCE_PROVIDER",
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


def _write_config(data) -> None:
    _CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ── 固定装置 ──────────────────────────────────────────────────────────────

# 主运行时（不调 resolve_provider：测试要的是**可控**的起点）
PRIMARY_SPEC = {
    "provider": "deepseek", "api_mode": "chat_completions",
    "base_url": "https://api.deepseek.com/v1", "api_key": "sk-primary-key",
    "model": "deepseek-chat", "context_length": 128_000,
}

# 一条自洽的回退条目：显式 base_url（不是 api.openai.com，所以协议仍是
# chat_completions）+ 自己的内联 key ⇒ 一次性证明"端点/凭据都来自它自己"。
FALLBACK_ENTRY = {
    "provider": "openai", "model": "gpt-4o-mini",
    "base_url": "http://fallback.test/v1", "api_key": "sk-fallback-key",
}

FALLBACK_ENTRY_2 = {
    "provider": "anthropic", "model": "claude-sonnet-4-5",
    "base_url": "http://fallback2.test/v1", "api_key": "sk-fallback2-key",
}


def _agent(chain=None, **kw) -> AIAgent:
    return AIAgent(dict(PRIMARY_SPEC), fallback_providers=chain, max_turns=5, **kw)


def _text_response(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {}}


def _raised(fn, *a, **kw):
    """跑一下，返回抛出的异常（没抛返回 None）。"""
    try:
        fn(*a, **kw)
        return None
    except Exception as exc:  # noqa: BLE001
        return exc


# ══════════════════════════════════════════════ 1. A7 判据 + A9 配置层读法
def test_status_criterion_is_binary() -> None:
    """★ 判据只有一条线：可回退（换 provider 可能修好）/ 不可回退（换谁都一样）。

    反向验证：把 `is_fallbackable_status` 从集合判据改成 `status >= 400`
    → 本组第 2 条（400/422 不回退）立刻变红。
    """
    for code in (401, 403, 408, 409, 429, 500, 502, 503, 504):
        check(f"HTTP {code} 可回退", fb.is_fallbackable_status(code), str(code))
    for code in (400, 404, 422, 200, 201):
        check(f"HTTP {code} 不可回退", not fb.is_fallbackable_status(code), str(code))
    check("解析不成整数 → 不回退（不猜、不报错）",
          not fb.is_fallbackable_status("boom") and not fb.is_fallbackable_status(None))
    check("判据只存一份（run_agent 的 FALLBACK_STATUS 与实现同源）",
          set(run_agent.FALLBACK_STATUS) == set(fb.FALLBACK_STATUS), str(sorted(fb.FALLBACK_STATUS)))


def test_fallback_chain_merges_keeps_order_and_dedupes() -> None:
    """★ 链的数据源：`fallback_providers`（主源、**保序**）+ legacy `fallback_model`，按三元组去重。

    "保序"是契约不是细节：链是**按序消费**的，顺序变了优先级就变了。
    """
    _clear()
    chain = cfg.get_fallback_chain({
        "fallback_providers": [
            {"provider": "openai", "model": "gpt-4o-mini", "base_url": "http://b.test/v1"},
            {"provider": "anthropic", "model": "claude-sonnet-4-5"},
            {"provider": "openai", "model": "gpt-4o-mini", "base_url": "http://b.test/v1"},   # 重复
        ],
        "fallback_model": {"provider": "deepseek", "model": "deepseek-chat"},
    })
    check("主源保序（openai → anthropic）", [e["provider"] for e in chain[:2]] == ["openai", "anthropic"],
          str(chain))
    check("legacy fallback_model 被并入（在末尾）", chain[-1]["provider"] == "deepseek", str(chain))
    check("按 (provider, model, base_url) 去重（保留第一次出现的）", len(chain) == 3, str(len(chain)))


def test_fallback_chain_drops_bad_entries_and_empty_means_disabled() -> None:
    _clear()
    check("没配置 → 空链（= 没有回退）", cfg.get_fallback_chain() == [], str(cfg.get_fallback_chain()))
    chain = cfg.get_fallback_chain({"fallback_providers": [
        {"provider": "openai"},                              # 缺 model → 丢
        {"model": "gpt-4o-mini"},                            # 缺 provider → 丢
        "not-a-dict",                                        # 形状坏 → 丢
        {"provider": "openai", "model": "gpt-4o-mini"},      # 好
    ]})
    check("缺字段 / 形状坏的条目被丢，好条目留下",
          len(chain) == 1 and chain[0]["provider"] == "openai", str(chain))
    check("坏配置不抛（一个笔误不该让 agent 起不来）",
          cfg.get_fallback_chain({"fallback_providers": {"weird": 1}}) == [])
    check("返回的条目只含干净字段（provider/model 必有）",
          all("provider" in e and "model" in e for e in chain))


def test_entry_api_key_is_its_own() -> None:
    """★ 凭据隔离的一半：每条回退**自己解析自己的** key（内联 → key_env 经 get_secret）。

    反向验证：把 `resolve_entry_api_key` 改成 `os.getenv(key_env)` 直读
    → 多 profile 下会串味（本文件测不出，但语义上就是第 9 章那条 fail-closed 的反面）。
    """
    _clear()
    check("内联 api_key 最优先",
          cfg.resolve_entry_api_key({"api_key": "sk-inline", "key_env": "OPENAI_API_KEY"}) == "sk-inline")
    rp.load_env_file()
    check("key_env 经 get_secret 读出值",
          cfg.resolve_entry_api_key({"key_env": "OPENAI_API_KEY"}) == os.environ["OPENAI_API_KEY"])
    check("api_key_env 是 key_env 的等价写法",
          cfg.resolve_entry_api_key({"api_key_env": "DEEPSEEK_API_KEY"}) == os.environ["DEEPSEEK_API_KEY"])
    check("两样都没有 → None（调用方走 provider 自己的画像，不硬造空 key）",
          cfg.resolve_entry_api_key({}) is None)


def test_config_model_beats_env() -> None:
    """★ A9 的顺序：**显式 → config → env → provider 默认**（config 压 env）。

    反向验证：把 `resolve_provider` 里 `get_configured_model()` 那一段删掉
    → 第 2 条立刻变红（config 的值不再压过 `MINI_MODEL`）。
    """
    _clear()
    os.environ["MINI_MODEL"] = "env-model"
    check("没有 config 时用 MINI_MODEL（env）",
          rp.resolve_provider("deepseek")["model"] == "env-model")
    _write_config({"model": {"provider": "deepseek", "model": "config-model"}})
    check("★ config 压 env（陈旧的 shell 变量不该盖掉用户存下的选择）",
          rp.resolve_provider("deepseek")["model"] == "config-model")
    check("显式参数最优先", rp.resolve_provider("deepseek", "explicit-model")["model"] == "explicit-model")
    check("配置层把 model.provider 也读出来了（名字层的第 2 层）",
          cfg.get_configured_provider() == "deepseek", cfg.get_configured_provider())
    _clear()
    check("都没配 → provider 画像的默认模型",
          rp.resolve_provider("deepseek")["model"] == "deepseek-chat")


# ══════════════════════════════════════════════ 2. A7 状态机（不跑 HTTP）
def test_chain_from_constructor_and_index_advances_one_per_activation() -> None:
    """★ 三个不变量一起守：链来自构造入参 / **一次只前进一格** / 耗尽**恒 False**。

    反向验证：把 `activate_fallback` 的 `index += 1` 改成 `index = len(chain)`
    → 第 2、4 条变红（第一次就跳到底、第二次没得换却还报 True）。
    """
    _clear()
    ag = _agent([dict(FALLBACK_ENTRY), dict(FALLBACK_ENTRY_2)])
    check("链来自构造入参", len(ag.fallback_providers) == 2 and fb.pending_count(ag) == 2,
          str(fb.fallback_state(ag)))
    check("还没换过：latch 未置位", fb.fallback_state(ag)["activated"] is False)

    check("第一次失败 → 换到第 1 条",
          ag._try_activate_fallback(reason="http_429", status=429) and ag.provider == "openai",
          ag.provider)
    check("索引前进**一格**（不是跳到底）", fb.fallback_state(ag)["index"] == 1,
          str(fb.fallback_state(ag)))
    check("第二次失败 → 换到第 2 条",
          ag._try_activate_fallback(reason="http_500", status=500) and ag.provider == "anthropic",
          ag.provider)
    check("索引再前进一格", fb.fallback_state(ag)["index"] == 2, str(fb.fallback_state(ag)))
    check("★ 链耗尽 → 恒返回 False（不再激活、不再锤主模型）",
          ag._try_activate_fallback(reason="http_503", status=503) is False)
    check("★ 耗尽后运行时停在最后一条（不震荡、不回主）", ag.provider == "anthropic", ag.provider)


def test_activation_swaps_runtime_in_place() -> None:
    """★ 原地换 5 项 + `_client_kwargs`（client 的构造入参）+ 窗口。"""
    _clear()
    ag = _agent([dict(FALLBACK_ENTRY)])
    primary_key = ag.api_key
    ag._try_activate_fallback(reason="http_429", status=429)
    check("provider 换了", ag.provider == "openai", ag.provider)
    check("model 换了", ag.model == "gpt-4o-mini", ag.model)
    check("base_url 换了", ag.base_url == "http://fallback.test/v1", ag.base_url)
    check("★ api_key 换成回退条目自己的（凭据隔离的另一半）",
          ag.api_key == "sk-fallback-key" and ag.api_key != primary_key, ag.api_key)
    check("api_mode 跟着新端点重判（不继承主 provider 的）", ag.api_mode == "chat_completions", ag.api_mode)
    check("★ 5 个属性与 _client_kwargs 同步（换 client = 换它）",
          ag._client_kwargs == {"provider": ag.provider, "base_url": ag.base_url,
                                "api_key": ag.api_key, "api_mode": ag.api_mode, "model": ag.model},
          str(ag._client_kwargs))
    check("窗口跟着模型走（压缩阈值靠它，否则按上家的窗口算）",
          ag.context_length == 64_000, str(ag.context_length))
    check("latch 置位 + 路由记进观察面",
          ag._fallback_activated is True and ag._provider_fallback_route == "openai/gpt-4o-mini",
          str(fb.fallback_state(ag)))


def test_restore_primary_runtime_per_round() -> None:
    """★ 按轮次复位：换回主 provider，并把索引 + latch 清掉（同一条候选下一轮还能用）。"""
    _clear()
    ag = _agent([dict(FALLBACK_ENTRY)])
    ag._try_activate_fallback(reason="http_429", status=429)
    reverted = fb.restore_primary_runtime(ag)
    check("换回主 provider", reverted and ag.provider == "deepseek", ag.provider)
    check("运行时逐字还原（url / key / 窗口）",
          ag.base_url == PRIMARY_SPEC["base_url"] and ag.api_key == PRIMARY_SPEC["api_key"]
          and ag.context_length == PRIMARY_SPEC["context_length"],
          f"{ag.base_url} / {ag.api_key} / {ag.context_length}")
    check("★ 索引与 latch 都复位", fb.fallback_state(ag)["index"] == 0
          and fb.fallback_state(ag)["activated"] is False, str(fb.fallback_state(ag)))
    check("复位后同一条候选又能用（不是被永久消费）",
          ag._try_activate_fallback(reason="http_429", status=429) and ag.provider == "openai",
          ag.provider)


def test_unresolvable_entry_is_skipped_but_consumed() -> None:
    """未注册的 provider / 缺字段 / 缺凭据 → **跳过这一条**，索引照样前进（不回头再试）。"""
    _clear()
    ag = _agent([
        {"provider": "no-such-provider", "model": "m"},   # 未注册 → 解析不出来
        {"provider": "openai"},                            # 缺 model → 解析不出来
        dict(FALLBACK_ENTRY),                              # 好的
    ])
    ok = ag._try_activate_fallback(reason="http_429", status=429)
    check("坏候选被跳过，落到第一条能用的", ok and ag.provider == "openai", ag.provider)
    check("★ 被跳过的候选**也**被消费掉（索引到 3，不回头重试）",
          fb.fallback_state(ag)["index"] == 3, str(fb.fallback_state(ag)))
    check("之后没有候补了", not fb.has_pending_fallback(ag))


# ══════════════════════════════════════════════ 3. A7 接线（真 agent + 假 _http_post）
def test_fallbackable_error_switches_provider_and_resends() -> None:
    """★ 接线的主证据：429 → 换 provider → **把这次请求重发**，而且用的是新运行时。"""
    _clear()
    ag = _agent([dict(FALLBACK_ENTRY)])
    calls = []

    def post(cand, msgs):
        calls.append(dict(cand))
        if len(calls) == 1:
            raise ApiHTTPError(429, "rate limited", "http://x")
        return _text_response("ok")

    ag._http_post = post
    resp = ag._post_chat_completions([{"role": "user", "content": "hi"}], "t")
    check("回退后拿到结果（这次请求被重发了）",
          resp["choices"][0]["message"]["content"] == "ok")
    check("恰好两次尝试（换完就重发，不反复锤主模型）", len(calls) == 2, str(len(calls)))
    check("第一次打主 provider",
          calls[0]["provider"] == "deepseek" and calls[0]["model"] == "deepseek-chat", str(calls[0]))
    check("★ 第二次已经打**回退** provider（原地换生效）",
          calls[1]["provider"] == "openai" and calls[1]["base_url"] == "http://fallback.test/v1",
          str(calls[1]))
    check("★ 第二次带的是回退条目**自己的** key",
          calls[1]["api_key"] == "sk-fallback-key", str(calls[1].get("api_key")))


def test_deterministic_error_never_falls_back() -> None:
    """★ 400 / 422：请求本身不合法 —— 换 provider 只会把同一个错误再收一次。"""
    for code in (400, 422):
        _clear()
        ag = _agent([dict(FALLBACK_ENTRY)])
        calls = []

        def post(cand, msgs):
            calls.append(dict(cand))
            raise ApiHTTPError(code, "bad request", "http://x")

        ag._http_post = post
        exc = _raised(ag._post_chat_completions, [{"role": "user", "content": "hi"}], "t")
        check(f"{code} 直接抛（确定性错误）",
              isinstance(exc, ApiHTTPError) and exc.code == code, repr(exc))
        check(f"★ {code} **不回退**：只发了一次",
              len(calls) == 1, str(len(calls)))
        check(f"{code} 之后 provider 没变、latch 仍是 False",
              ag.provider == "deepseek" and ag._fallback_activated is False, ag.provider)


def test_exhausted_chain_raises_the_original_error() -> None:
    _clear()
    ag = _agent([dict(FALLBACK_ENTRY)])
    calls = []

    def post(cand, msgs):
        calls.append(dict(cand))
        raise ApiHTTPError(503, "overloaded", "http://x")

    ag._http_post = post
    exc = _raised(ag._post_chat_completions, [{"role": "user", "content": "hi"}], "t")
    check("链耗尽 → 把错误交出去", isinstance(exc, ApiHTTPError) and exc.code == 503, repr(exc))
    check("主 + 1 条回退 = 最多两次尝试", len(calls) == 2, str(len(calls)))
    check("★ 每一格只被消费一次（没有重复打同一个 provider）",
          [c["provider"] for c in calls] == ["deepseek", "openai"], str([c["provider"] for c in calls]))
    check("耗尽之后没有候补", not fb.has_pending_fallback(ag))


def test_no_chain_means_no_fallback_and_no_false_promise() -> None:
    """★ 空链：不激活、不换、也不发"切换中"的提示（报一句换不了的话比不报更坏）。"""
    _clear()
    ag = _agent([])
    progress = []
    ag.tool_progress_callback = lambda n, a, p: progress.append((n, p))
    check("显式 [] = 禁用回退", ag.fallback_providers == [] and not fb.has_pending_fallback(ag))
    check("没链时触发返回 False", ag._try_activate_fallback(reason="http_429", status=429) is False)
    check("没链时不发 fallback 进度", not [x for x in progress if x[0] == "fallback"], str(progress))
    check("provider 没被动", ag.provider == "deepseek", ag.provider)


def test_docstring_promise_matches_implementation() -> None:
    """★ 顺手修的那处"承诺没兑现"：docstring 宣传的失败回退，现在**真的接线了**。"""
    doc = run_agent.__doc__ or ""
    check("docstring 仍然宣告「失败回退」", "失败回退" in doc)
    check("docstring 点名了实现模块（读了就能找到代码）", "agent/fallback.py" in doc)
    ag = _agent([dict(FALLBACK_ENTRY)])
    check("有链的 agent 真的会回退（承诺兑现）",
          ag._try_activate_fallback(reason="http_429", status=429) and ag.provider == "openai",
          ag.provider)


def test_config_json_drives_the_chain_into_the_agent() -> None:
    """★ A9 → A7：链**从 config.json 进来**（`fallback_providers=None` 时的默认来源）。"""
    _clear()
    _write_config({
        "model": {"provider": "deepseek", "model": "deepseek-chat"},
        "fallback_providers": [{"provider": "openai", "model": "gpt-4o-mini",
                                "base_url": "http://fallback.test/v1",
                                "key_env": "OPENAI_API_KEY"}],
    })
    ag = AIAgent(dict(PRIMARY_SPEC), max_turns=5)        # 不传 fallback_providers
    check("链从 config.json 进来（不再是永远空列表）",
          [e["provider"] for e in ag.fallback_providers] == ["openai"], str(ag.fallback_providers))

    calls = []

    def post(cand, msgs):
        calls.append(dict(cand))
        if len(calls) == 1:
            raise ApiHTTPError(429, "rate limited", "http://x")
        return _text_response("ok")

    ag._http_post = post
    ag._post_chat_completions([{"role": "user", "content": "hi"}], "t")
    check("config 来的链真的被用上（有消费者）",
          len(calls) == 2 and calls[1]["provider"] == "openai", str(calls[-1]))
    check("条目的 key_env 真的解析成了凭据（不是空跑一次）",
          calls[1]["api_key"] == os.environ["OPENAI_API_KEY"], str(calls[1].get("api_key")))
    check("显式传 [] 时不读 config（禁用优先于配置）",
          AIAgent(dict(PRIMARY_SPEC), fallback_providers=[]).fallback_providers == [])
    _clear()


def test_fallback_resets_between_turns() -> None:
    """★ 按轮次回退：上一轮的兜底 provider **不会**变成这一轮的第一选择。"""
    _clear()
    ag = _agent([dict(FALLBACK_ENTRY)])
    calls = []

    def post(cand, msgs):
        calls.append(dict(cand))
        if len(calls) == 1:
            raise ApiHTTPError(429, "rate limited", "http://x")
        return _text_response("ok")

    ag._http_post = post
    first = ag.run_conversation("hi")
    check("第 1 轮：主失败 → 换到回退并成功",
          ag.provider == "openai" and len(calls) == 2 and first["final_response"] == "ok",
          f"{ag.provider} / {len(calls)}")
    second = ag.run_conversation("再来一次")
    check("★ 第 2 轮从**主 provider** 开始（索引/latch 已在轮首复位）",
          calls[2]["provider"] == "deepseek", str(calls[2]))
    check("第 2 轮跑在主 provider 上（临时兜底没变成永久降级）", ag.provider == "deepseek", ag.provider)
    check("第 2 轮答复正常", second["final_response"] == "ok", str(second.get("final_response")))


# ══════════════════════════════════════════════ 4. A8 辅助模型路由
def test_task_provider_model_precedence() -> None:
    """★ 真实版的优先级：**显式入参 > config `auxiliary.<task>` > `"auto"`**。"""
    _clear()
    check("没配置 → (auto, \"\")", aux.resolve_task_provider_model("compression") == ("auto", ""),
          str(aux.resolve_task_provider_model("compression")))
    data = {"auxiliary": {"compression": {"provider": "anthropic", "model": "claude-sonnet-4-5"}}}
    check("config 生效",
          aux.resolve_task_provider_model("compression", config=data) == ("anthropic", "claude-sonnet-4-5"),
          str(aux.resolve_task_provider_model("compression", config=data)))
    check("★ 显式入参压过 config",
          aux.resolve_task_provider_model("compression", explicit_provider="openai",
                                          explicit_model="gpt-4o-mini", config=data)
          == ("openai", "gpt-4o-mini"))
    check("别的任务的配置不串味",
          aux.resolve_task_provider_model("title", config=data) == ("auto", ""),
          str(aux.resolve_task_provider_model("title", config=data)))


def test_main_is_an_alias_for_the_main_provider() -> None:
    """★ `main` = 别名 → 主 agent 的 provider（真实版 `auxiliary_client.py:546`）。"""
    _clear()
    check("main → 主 agent 的 provider",
          aux.normalize_aux_provider("main", main_provider="deepseek") == "deepseek")
    check("其余取值也走别名归一（claude → anthropic）",
          aux.normalize_aux_provider("claude", main_provider="deepseek") == "anthropic")
    check("主 provider 为空时 main 退化为 auto（mini 没有 custom 画像，不硬造一个）",
          aux.normalize_aux_provider("main", main_provider="") == "auto")
    check("主 provider 自己就是 auto/main 时同样退化",
          aux.normalize_aux_provider("main", main_provider="auto") == "auto")
    check("不认识的名字原样穿过（不吞输入）",
          aux.normalize_aux_provider("whatever", main_provider="deepseek") == "whatever")


def test_auto_inherits_main_runtime_exactly() -> None:
    """★ "未配置时等价于主模型"这句话的**证据**（不是注释里的承诺）。"""
    _clear()
    ag = _agent([])
    main = ag._runtime_snapshot()
    rt = aux.resolve_auxiliary_runtime("compression", main_runtime=main)
    check("provider / base_url / api_key / api_mode 全部**逐字继承**主运行时",
          (rt["provider"], rt["base_url"], rt["api_key"], rt["api_mode"])
          == (main["provider"], main["base_url"], main["api_key"], main["api_mode"]),
          str({k: rt.get(k) for k in ("provider", "base_url", "api_key", "api_mode")}))
    check("model 也是主模型", rt["model"] == main["model"], str(rt.get("model")))
    check("source 标成 aux:auto（没配置）", rt.get("source") == "aux:auto", str(rt.get("source")))


def test_configured_aux_provider_resolves_independently() -> None:
    """配了 `auxiliary.<task>.provider` 才走别的 provider —— 端点与凭据都来自**那**一家。"""
    _clear()
    ag = _agent([])
    main = ag._runtime_snapshot()
    data = {"auxiliary": {"compression": {"provider": "anthropic", "model": "claude-sonnet-4-5"}}}
    rt = aux.resolve_auxiliary_runtime("compression", main_runtime=main, config=data)
    check("provider 换成配置的那家", rt["provider"] == "anthropic", str(rt.get("provider")))
    check("端点来自那家 provider（不是主 provider 的）",
          rt["base_url"] == "https://api.anthropic.com/v1", str(rt.get("base_url")))
    check("凭据也来自那家（主 provider 的 key 不挪窝）", rt["api_key"] != main["api_key"],
          str(rt.get("api_key")))
    check("source 标成 aux:config", rt.get("source") == "aux:config", str(rt.get("source")))
    bad = aux.resolve_auxiliary_runtime(
        "compression", main_runtime=main,
        config={"auxiliary": {"compression": {"provider": "no-such-provider", "model": "m"}}})
    check("解析不出来 → 兜底用主模型（辅助调用失败不该把回合弄崩）",
          bad["provider"] == main["provider"], str(bad.get("provider")))


def test_summary_llm_routes_through_the_aux_layer() -> None:
    """★ A8 接的**唯一消费者**：压缩摘要。未配置 ≡ 主模型；配了就走它。"""
    _clear()
    ag = _agent([])
    seen = []

    def post(cand, msgs):
        seen.append(dict(cand))
        return _text_response("摘要文本")

    ag._http_post = post
    check("未配置时摘要照旧拿得到文本", ag._summary_llm("sys", "user") == "摘要文本")
    check("★ 未配置 → 打的就是主 provider / 主模型 / 主 key（默认行为一个字节没变）",
          (seen[0]["provider"], seen[0]["model"], seen[0]["api_key"])
          == ("deepseek", "deepseek-chat", "sk-primary-key"), str(seen[0]))

    _write_config({"auxiliary": {"compression": {"provider": "anthropic", "model": "claude-sonnet-4-5"}}})
    seen.clear()
    out = ag._summary_llm("sys", "user")
    check("★ 配了 auxiliary → 摘要走**那家** provider", seen[0]["provider"] == "anthropic", str(seen[0]))
    check("模型换成配置里的", seen[0]["model"] == "claude-sonnet-4-5", str(seen[0]))
    check("端点跟着那家走", seen[0]["base_url"] == "https://api.anthropic.com/v1", str(seen[0]))
    check("摘要仍然拿得到文本", out == "摘要文本", str(out))

    # 配成 "main" → 解析成主 agent 的 provider（别名语义在真消费者里生效）
    _write_config({"auxiliary": {"compression": {"provider": "main", "model": "deepseek-chat"}}})
    seen.clear()
    ag._summary_llm("sys", "user")
    check("★ 配成 main → 打的是**主 agent 的 provider**",
          seen[0]["provider"] == ag.provider and seen[0]["base_url"] == ag.base_url, str(seen[0]))

    # 配成 "auto" → 逐字继承主运行时（含主 key）
    _write_config({"auxiliary": {"compression": {"provider": "auto", "model": ""}}})
    seen.clear()
    ag._summary_llm("sys", "user")
    check("配成 auto → 与「未配置」逐字一致（含主 provider 的 key）",
          (seen[0]["provider"], seen[0]["api_key"]) == (ag.provider, ag.api_key), str(seen[0]))
    _clear()


def test_aux_call_does_not_consume_the_fallback_chain() -> None:
    """★ 辅助调用是**一次性**的：不走 agent 的回退链（真实版 aux 没有 `_try_activate_fallback`）。"""
    _clear()
    ag = _agent([dict(FALLBACK_ENTRY)])

    def post(cand, msgs):
        raise ApiHTTPError(429, "rate limited", "http://x")

    ag._http_post = post
    exc = _raised(ag._summary_llm, "sys", "user")
    check("辅助调用失败 → 直接把错误交出去（不回退、不换 provider）",
          isinstance(exc, ApiHTTPError), repr(exc))
    check("★ 回退链一格都没被消费（索引仍 0、latch 未置位）",
          fb.fallback_state(ag)["index"] == 0 and fb.fallback_state(ag)["activated"] is False,
          str(fb.fallback_state(ag)))
    check("主运行时没被动", ag.provider == "deepseek", ag.provider)


GROUPS = [
    ("1. A7 判据（二元）+ A9 配置层读法",
     "可回退 / 不可回退只有一条线；链合并保序去重丢坏条目；凭据逐条自解析；model 一栏 config 压 env",
     [test_status_criterion_is_binary,
      test_fallback_chain_merges_keeps_order_and_dedupes,
      test_fallback_chain_drops_bad_entries_and_empty_means_disabled,
      test_entry_api_key_is_its_own,
      test_config_model_beats_env]),
    ("2. A7 状态机（链 / 索引 / 一次性 latch / 原地换 / 每轮复位）",
     "一次只前进一格 / 耗尽恒 False / 换 5 项 + _client_kwargs + 窗口 / 坏候选跳过但消费",
     [test_chain_from_constructor_and_index_advances_one_per_activation,
      test_activation_swaps_runtime_in_place,
      test_restore_primary_runtime_per_round,
      test_unresolvable_entry_is_skipped_but_consumed]),
    ("3. A7 接线（真 agent + 假 _http_post）",
     "429 → 换 provider 并重发 / 400、422 不回退 / 耗尽交原始错误 / 跨轮复位 / 承诺与实现一致",
     [test_fallbackable_error_switches_provider_and_resends,
      test_deterministic_error_never_falls_back,
      test_exhausted_chain_raises_the_original_error,
      test_no_chain_means_no_fallback_and_no_false_promise,
      test_docstring_promise_matches_implementation,
      test_config_json_drives_the_chain_into_the_agent,
      test_fallback_resets_between_turns]),
    ("4. A8 辅助模型路由 + 消费者（压缩摘要）",
     "显式 > config > auto / main = 主 provider 别名 / 未配置 ≡ 主模型 / aux 不消费回退链",
     [test_task_provider_model_precedence,
      test_main_is_an_alias_for_the_main_provider,
      test_auto_inherits_main_runtime_exactly,
      test_configured_aux_provider_resolves_independently,
      test_summary_llm_routes_through_the_aux_layer,
      test_aux_call_does_not_consume_the_fallback_chain]),
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
    pathlib.Path.home = _REAL_PATH_HOME
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
