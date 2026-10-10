"""多路复用开关的**裁决**（对应真实 `hermes_cli/gateway_multiplex_mode.py`，149 行版）。

真实版模块 docstring 的要点（逐字摘，去掉原文的引号围栏）：

    Boot-time verdict for an UNSET ``gateway.multiplex_profiles`` (the default is on).
    … Turning the default on must not make a default gateway double-bind a fleet that still runs
    per-profile gateways (two pollers on one bot token, port fights), so the implicit default is a
    *request*: the gateway runs the same preflight … and multiplexes only when the fold would have
    been safe. An explicit value is never second-guessed …
    The refusal is logged, never fatal: the gateway comes up standalone exactly as before the
    default flipped, and the log names the blocker …

★ 为什么需要"三态"（本章 §二）：`None` = **待裁决**（等 boot 期跑预检），`True/False` = **有人已经定了**。
  如果把 `None` 当成 `False`，"默认开"这个设计就不存在；如果把 `None` 当成 `True`，
  一次安装里两个进程会去抢同一个 bot token —— 那不是报错，是**间歇性错乱**。

★ 为什么预检崩了**不许**把网关带走（本章 §三）：预检读的是"别人还在不在跑"这类外部状态，
  它自己的失败不该等价于"这个部署配置错了"。所以异常一律转成 blocker → 按 standalone 起 + 记日志。

与真实版的**刻意差别**（都写在这里，免得以后以为是漏抄）：

1. 真实版预检有六条；mini 抄**三条**（见 `implicit_multiplex_blocker` 的注释）——
   另外三条依赖 mini 不存在的基座：没有服务管理器、没有 `gateway migrate` 命令、没有占端口平台。
2. 真实版环境变量是 `GATEWAY_MULTIPLEX_PROFILES`；mini 用自己的前缀 `MINI_MULTIPLEX_PROFILES`。
3. 真实版把裁决**就地写回** `config.multiplex_profiles`；mini 的 config 是只读 dict，
   所以裁决只落在 `GatewayRunner` 的字段与状态文件里（不写配置）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ── ① 真值词表（真实版 gateway/config.py:29-36 `_TRUTHY_STRINGS` / `_bool_token`） ──
TRUTHY_TOKENS = frozenset({"1", "true", "yes", "on"})
FALSY_TOKENS = frozenset({"0", "false", "no", "off"})

MULTIPLEX_ENV_VAR = "MINI_MULTIPLEX_PROFILES"

# 真实版逐字：SINGLE_PROFILE_REASON = "only one profile exists (nothing to multiplex)"
SINGLE_PROFILE_REASON = "only one profile exists (nothing to multiplex)"


def bool_token(value: Any) -> Optional[bool]:
    """识别的真/假词 → True/False；其余（含空串）→ None。

    ★ 返回 `None` 而不是 `False` 是关键：调用方要能区分"明确说关"和"没说话"。
    """
    token = str(value).strip().lower()
    if token in TRUTHY_TOKENS:
        return True
    if token in FALSY_TOKENS:
        return False
    return None


def env_multiplex_override() -> Optional[bool]:
    """`MINI_MULTIPLEX_PROFILES` 操作者覆盖：识别的词 → True/False；未设/空白/未识别 → None。

    真实版 docstring 逐字（`gateway/config.py:45-64`）：

        ``None`` when unset, blank, or unrecognized so the caller keeps the config.yaml value
        (env > config > default). **Blank is deliberately ``None``, not ``False``**: a
        provisioned-but-unpopulated Fly secret arrives as an empty string and must NOT shadow
        a config.yaml opt-in.

    ★ 空白 ≠ False：部署系统（容器/CI）常常"提供一个值为空的环境变量"，
      那是在占位，不是在说"关掉"。
    """
    raw = os.getenv(MULTIPLEX_ENV_VAR)
    if not (raw or "").strip():
        return None
    parsed = bool_token(raw)
    if parsed is None:
        logger.warning(
            "忽略无法识别的 %s=%r（只认 %s 或 %s）；回落到显式参数 / 默认。",
            MULTIPLEX_ENV_VAR, raw, sorted(TRUTHY_TOKENS), sorted(FALSY_TOKENS),
        )
    return parsed


# ── ② 裁决结果（真实版 `MultiplexDecision`，:46-51） ──
@dataclass(frozen=True)
class MultiplexDecision:
    """一次 boot 的裁决。

    `source` 三个取值（真实版逐字注释）：

      * `"config"`  —— 显式值（命令行 / 环境变量），**永不复核**；
      * `"default"` —— 未设置 + 预检通过 → 应用默认（开）；
      * `"guard"`   —— 未设置 + 预检拦下 → standalone，`reason` 说明是谁拦的。
    """

    enabled: bool
    source: str                     # config | default | guard
    reason: str = ""

    def describe(self) -> str:
        if self.source == "config":
            return f"显式指定：{'开' if self.enabled else '关'}（{self.reason}）"
        if self.source == "default":
            return f"未指定 → 应用默认（开）：{self.reason}"
        return f"未指定 → 预检拦下，按 standalone 启动：{self.reason}"


def _live_secondary_gateways(homes: Sequence[Path],
                             *, active_home: Path,
                             own_pid: int) -> List[Tuple[str, int]]:
    """哪些**次级** home 上还有活着的网关进程（排除活动 home —— 那就是本进程自己）。

    ★ 用 `Path.resolve()` 比较：`/var` 与 `/private/var` 这类符号链接差异会让"同一个 home"
      被算成两个（第 3 章踩过同类）。
    """
    from gateway.status import live_gateway_pid_for_home

    active_key = str(Path(active_home).resolve())
    out: List[Tuple[str, int]] = []
    for home in homes:
        if str(Path(home).resolve()) == active_key:
            continue
        pid = live_gateway_pid_for_home(Path(home))
        if pid is not None and pid != own_pid:
            out.append((Path(home).name, pid))
    return out


# ── ③ 预检：为什么这个进程**不该**多路复用（真实版 `implicit_multiplex_blocker`，:75-107） ──
def implicit_multiplex_blocker(
    *,
    served: Optional[Sequence[Path]] = None,
    active: Optional[str] = None,
    own_pid: Optional[int] = None,
    home: Optional[Path] = None,
) -> Optional[str]:
    """返回"不能按默认开启"的原因串；`None` = 可以开。

    mini 抄的三条（顺序 = 短路顺序，最便宜最能排除大类的排前面）：

    1. **本进程是非默认 profile 自己的网关** —— 只有默认 profile 的网关做多路复用。
       真实版逐字：*"this is profile '<x>'s own gateway; only the default profile's gateway
       multiplexes (hermes gateway migrate --multiplex folds the fleet onto it)"*。
       mini 没有 migrate 命令，所以文案里的补救办法换成 mini 真有的：用默认 profile 起网关，
       或显式 `--multiplex` 强制开启。
    2. **服务集合 < 2** —— 单 profile 安装没有可复用的对象，而多路复用会给它装上
       fail-closed 的密钥读取（`agent/secret_scope.py`），**白装**。
    3. **有次级 profile 自己还在跑网关** —— 那会变成"两个进程轮询同一个 bot"。
       mini 用第 7 章那套"PID + 启动时间指纹"的存活判据（`gateway.status.live_gateway_pid_for_home`）。

    下列三条**不抄**（当场写明理由，别以后以为漏了）：宿主可迁移性（mini 无服务管理器）、
    迁移预检 blocker（mini 无 `gateway migrate`、无 HTTP 监听）、重复 bot 凭据检测（mini 适配器不带 token）。
    """
    from hermes_cli.profiles import (active_profile_name, default_home,
                                     profiles_to_serve)

    # 检查 1
    active = active if active is not None else active_profile_name()
    if active != "default":
        return (f"这是 profile '{active}' 自己的网关；只有默认 profile 的网关做多路复用"
                f"（用默认 profile 起网关，或显式 --multiplex 强制开启）")

    # 检查 1b（★ mini 版扩展，真实版没有这一条）：
    #   **本进程的 home 不是本机默认家** → 只服务自己。
    #   真实版同一条道理（第 ① 条）：多路复用是"默认 profile 的网关"的职能。
    #   mini 额外必须防一件事：`MINI_HOME` 指到临时目录时（测试 / 演示 / `MINI_HOME=/tmp/x`），
    #   **HOME 锚定**的 `profiles/` 里装的是**用户真实的** profile —— 顺手服务它们
    #   就等于往用户的家写东西（实测踩过：测试进程替用户的 default + lab 建了适配器）。
    if home is not None:
        if str(Path(home).resolve()) != str(Path(default_home()).resolve()):
            return (f"本进程的 home（{home}）不是本机默认家（{default_home()}）——"
                    f"只有本机默认家的网关做多路复用（子进程/临时 home 请显式 --no-multiplex）")

    # 检查 2
    homes = list(served) if served is not None else profiles_to_serve(multiplex=True)
    if len(homes) < 2:
        return SINGLE_PROFILE_REASON

    # 检查 3
    own = os.getpid() if own_pid is None else own_pid
    siblings = _live_secondary_gateways(homes, active_home=default_home(), own_pid=own)
    if siblings:
        owned = ", ".join(f"'{name}' (pid {pid})" for name, pid in siblings)
        return (f"profile(s) {owned} 自己还在跑网关；先停掉它们，"
                f"或显式固定关闭多路复用（--no-multiplex / {MULTIPLEX_ENV_VAR}=false）")
    return None


# ── ④ 裁决入口（真实版 `resolve_multiplex_mode`，:110-123） ──
def resolve_multiplex_mode(explicit: Optional[bool] = None, *,
                           served: Optional[Sequence[Path]] = None,
                           home: Optional[Path] = None) -> MultiplexDecision:
    """给一次 boot 定下开关；**不抛异常**（异常一律降级成 standalone）。

    顺序（真实版：env > 配置 > 未设置）：
      1. 命令行显式值（`--multiplex` / `--no-multiplex`）—— 有就直接用；
      2. 环境变量覆盖 —— 有识别的词就用；
      3. 都没说话 → 跑预检：过 → `default`（开）；拦下 → `guard`（关，并记原因）。
    """
    if explicit is not None:
        return MultiplexDecision(bool(explicit), "config", "命令行显式指定")

    env_value = env_multiplex_override()
    if env_value is not None:
        return MultiplexDecision(env_value, "config", f"{MULTIPLEX_ENV_VAR} 覆盖")

    try:
        blocker = implicit_multiplex_blocker(served=served, home=home)
    except Exception as exc:      # noqa: BLE001
        # ★ 真实版逐字：a broken preflight must not take the gateway down with it
        logger.warning("多路复用预检失败，按 standalone 启动：%s", exc, exc_info=True)
        blocker = f"预检失败（{exc}）"

    if blocker:
        return MultiplexDecision(False, "guard", blocker)
    return MultiplexDecision(True, "default", "未设置且预检通过；应用默认（开）")


# ── ⑤ 记录与日志（真实版 `record_multiplex_decision` / `log_multiplex_decision`，:126-149） ──
def multiplex_status_fields(decision: MultiplexDecision) -> Dict[str, Any]:
    """状态文件里那几栏（★ 供**每一个**写状态的地方带上）。

    为什么必须做成一个共享函数：`write_runtime_status` 是**全量写**（每次新建 payload），
    所以"后来那次写"会把裁决栏抹掉 —— 实测踩过：网关关机时写 `state="stopped"`，
    状态文件里 `multiplex_*` 四栏全没了，运维查不到"它为什么没多路复用"。
    → 规则：**状态文件的每个写者都要带上这几栏**（值只从这一个函数来）。

    真实版逐字：`write_runtime_status(multiplex_standalone_reason=decision.reason
    if decision.source == "guard" else None)` —— **只有 `guard` 才写原因**：
    显式指定与"默认开"都不需要解释，而"为什么没开"必须留证据。
    """
    return {
        "multiplex": bool(decision.enabled),
        "multiplex_source": decision.source,
        "multiplex_reason": decision.reason,
        "multiplex_standalone_reason": (decision.reason
                                        if decision.source == "guard" else None),
    }


def record_multiplex_decision(home: Path, decision: MultiplexDecision) -> Path:
    """把裁决写进状态文件（`gateway_state.json`，与真实版同名）。

    ★ 实现上是**读回现有 payload → 只改这几栏 → 写回**：状态文件里还有 `state` / `pid` /
      `start_fingerprint` 等别人写的字段，不能因为记一条裁决就把它们抹掉；
      而这几栏本身在关机写 `stopped` 时也要能活下来（见 `multiplex_status_fields`）。
    """
    from gateway.status import read_runtime_status, state_file, write_runtime_status

    home = Path(home)
    previous = read_runtime_status(state_file(home)) or {}
    state = previous.get("state") or "running"
    fields: Dict[str, Any] = {k: v for k, v in previous.items()
                              if k not in ("state", "updated_at", "home")}
    fields.update(multiplex_status_fields(decision))
    return write_runtime_status(home, state, **fields)


def log_multiplex_decision(decision: MultiplexDecision) -> None:
    """按"谁定的"分级打日志（真实版 `:136-149`）。

    分级理由：`guard` + "只有一个 profile"是**正常情况**（不该吵人）；
    其它 `guard`（有人还在跑网关）是**需要人处理**的，所以 warning。
    """
    if decision.source == "guard" and decision.reason != SINGLE_PROFILE_REASON:
        logger.warning("多路复用：%s", decision.describe())
    else:
        logger.info("多路复用：%s", decision.describe())


def recorded_served_profiles(home: Path) -> List[str]:
    """活着的网关记下的"我服务了谁"（别的进程**不猜**未设置的默认值时要先看它）。

    真实版对应 `hermes_cli/gateway_multiplex_served.py::recorded_served_profiles`：
    **先读活网关的记录，再读显式开关**，绝不读"合并后的默认值" ——
    否则"其实还没多路复用"会被读成"已经在多路复用"。
    """
    from gateway.status import read_runtime_status, state_file

    payload = read_runtime_status(state_file(Path(home))) or {}
    served = payload.get("served_profiles")
    if isinstance(served, list):
        return [str(x) for x in served]
    return []


def default_gateway_multiplexes(home: Path) -> bool:
    """**别的进程**眼里"默认网关会多路复用吗"（真实版 `default_gateway_multiplexes`，:53-63）。

    判定顺序：活网关记录的 `served_profiles`（> 1 个 = 它在多路复用）→ 显式开关（这里只有环境变量）
    → `False`（**未设置时不猜**，留给网关自己裁决）。
    """
    if len(recorded_served_profiles(home)) > 1:
        return True
    env_value = env_multiplex_override()
    return bool(env_value) if env_value is not None else False
