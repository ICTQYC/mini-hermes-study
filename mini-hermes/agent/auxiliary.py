"""辅助模型路由（第 8 章 A8）—— `auxiliary.<task>.{provider,model}` + `main` 别名。

对应 Hermes 的 `agent/auxiliary_client.py`：
  * `resolve_provider_client()`（:5099）—— 入参 `(provider, model, …)`，出参 `(client, resolved_model)`；
  * `_normalize_aux_provider()`（:546）—— `"main"` 支路；
  * `_resolve_task_provider_model()`（:5784）—— 读 `auxiliary.<task>.*` 的优先级；
  * `get_text_auxiliary_client(task)`（:5178）—— 公开入口。

## 为什么"辅助任务"要单独一条路由

主模型的挑选标准是**质量**；辅助任务（压缩摘要、起标题、视觉）要的是**便宜 + 快**。
真实版给它们各自一个 `auxiliary.<task>` 配置块。核心不是"省钱"，
而是**别让一次摘要用掉主模型的预算**，也别让免费档的窗口把摘要掐掉。

## 优先级（真实版 `_resolve_task_provider_model`）

    显式入参  >  config `auxiliary.<task>.*`  >  `"auto"`

`"auto"` = **继承主 agent 当前路由**（所以"没配置"和"配成 auto"是同一件事）。
`"main"` 是**别名** —— 解析成主 agent 的那个 provider（两者只在"主 provider 为空"时分开，
见 `normalize_aux_provider` 的注释）。

## mini 只接了一个消费者（B3：不抄 7800 行的适配器丛林）

**压缩摘要**（`run_agent._summary_llm`）。未配置时解析结果是 `auto` ⇒ 与"复用主模型"
**逐字等价**（默认行为一个字节都不变）；配了 `auxiliary.compression.provider` 才走别的。

## 与回退链的关系（真实版 §6 的结论）

两者共用**同一个** provider 路由器（这里是 `hermes_cli.runtime_provider.resolve_provider`）
—— 认证 / base_url / api_mode 只在一处映射，绝不各调一套。
但辅助调用**不走** agent 的回退链：它是一次性的按需调用，没有持久 agent 的
`_fallback_index` / `_fallback_activated` 机制。解析失败时**兜底用主 agent 的模型**
（真实版 `_try_configured_fallback_chain` → 主策略 → "兜底用主 agent 模型" 的最后一级）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from hermes_cli.config import (
    AUX_PROVIDER_AUTO,
    AUX_PROVIDER_MAIN,
    get_auxiliary_config,
)
from providers.aliases import normalize_provider

# mini 接的第一个（也是目前唯一一个）消费者：上下文压缩的摘要调用
TASK_COMPRESSION = "compression"


def resolve_task_provider_model(
    task: Optional[str],
    *,
    explicit_provider: Optional[str] = None,
    explicit_model: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """`(provider, model)`：**显式 > config `auxiliary.<task>` > `"auto"`**。

    真实版 `auxiliary_client.py:5784` 节选：`cfg_provider = str(task_config.get("provider",""))
    .strip() or None`（配了就用配的，否则回落到 `"auto"`）。
    """
    cfg = get_auxiliary_config(task, config) if task else {"provider": AUX_PROVIDER_AUTO, "model": ""}
    provider = str(explicit_provider or "").strip() or cfg.get("provider") or AUX_PROVIDER_AUTO
    model = str(explicit_model or "").strip() or cfg.get("model") or ""
    return provider, model


def normalize_aux_provider(provider: str, *, main_provider: str = "") -> str:
    """`"main"` → 主 agent 的 provider；其余走别名归一（A10）。

    真实版 `auxiliary_client.py:546`：

        if normalized == "main":
            main_prov = (_read_main_provider() or "").strip().lower()
            if not main_prov or main_prov in {"auto", "main"}:
                return "custom"
            normalized = main_prov
        return _PROVIDER_ALIASES.get(normalized, normalized)

    ★ 一处**刻意的差别**：主 provider 也为空/auto/main 时，真实版返回 `"custom"`
      （一个始终存在的通用插件画像）；mini 的注册表里**没有** custom 画像，
      返回 `"custom"` 只会在解析时报"未知 provider"。所以这里退化成 `"auto"`（= 继承主路由），
      语义上仍然满足"解析不出主 provider 时不乱造一个新 provider"。
    """
    normalized = normalize_provider(str(provider or "").strip()) or AUX_PROVIDER_AUTO
    if normalized == AUX_PROVIDER_MAIN:
        main = normalize_provider(str(main_provider or "").strip())
        if not main or main in (AUX_PROVIDER_AUTO, AUX_PROVIDER_MAIN):
            return AUX_PROVIDER_AUTO
        return main
    return normalized


def resolve_auxiliary_runtime(
    task: Optional[str],
    *,
    main_runtime: Optional[Dict[str, Any]] = None,
    explicit_provider: Optional[str] = None,
    explicit_model: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """解析出**一次辅助调用该用的运行时**（dict，形状与 `resolve_provider` 一致）。

    * `provider == "auto"`（含"没配置"）⇒ **继承主 agent 路由**：
      直接返回主运行时的副本（`base_url` / `api_key` / `api_mode` 一模一样的 —— 这就是
      "未配置时等价于主模型"这句话的**实现**，而不是注释里的承诺）。
    * 否则 ⇒ 走共享的 provider 路由器解析出独立运行时（自己那份 base_url / key / api_mode）。
    * 解析失败（未知 provider / 没配 key）⇒ **兜底用主 agent 模型**，不抛 ——
      辅助调用失败不该把整个回合弄崩（压缩器的 `generate_summary` 本来就把异常当"无摘要"）。
    """
    main = dict(main_runtime or {})
    provider, model = resolve_task_provider_model(
        task, explicit_provider=explicit_provider,
        explicit_model=explicit_model, config=config)
    resolved_provider = normalize_aux_provider(provider, main_provider=main.get("provider", ""))

    if resolved_provider == AUX_PROVIDER_AUTO:
        out = dict(main)
        out["model"] = model or main.get("model", "")
        out["resolved_provider"] = resolved_provider
        out["source"] = "aux:auto"          # = 继承主路由（默认）
        return out

    from hermes_cli.runtime_provider import resolve_provider

    # 真实版：主模型名也可作 aux 的默认 model（`_read_main_model_for_aux` 的回填）
    fallback_model = model or str(main.get("model") or "")
    try:
        spec = resolve_provider(resolved_provider, fallback_model or None)
    except Exception:                        # noqa: BLE001 —— 见 docstring 第三条
        out = dict(main)
        out["model"] = fallback_model
        out["resolved_provider"] = resolved_provider
        out["source"] = "aux:fallback-main"
        return out
    spec["resolved_provider"] = resolved_provider
    spec["source"] = "aux:config"
    return spec


__all__ = [
    "TASK_COMPRESSION",
    "resolve_task_provider_model",
    "normalize_aux_provider",
    "resolve_auxiliary_runtime",
]
