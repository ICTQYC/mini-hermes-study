"""密钥作用域（对应 Hermes 的 `agent/secret_scope.py`）。

问题：一个进程服务多个 profile 时，`os.environ` 里装的是**启动那个 profile** 的密钥。
于是"替次级 profile 干活的代码"读 `os.environ` 就会**静默用错 profile 的凭据**。

真实版的答案有两条：

1. `set_secret_scope(dict)`：把"这一小段代码该用的密钥"放进 **contextvar**，
   而不是往 `os.environ` 里写 —— 这样子进程**永远不会继承**别的 profile 的密钥；
2. **fail closed**：只有在 `set_multiplex_active(True)` 之后，**没有作用域**的密钥读取
   才会抛 `UnscopedSecretError`。平时（单 profile）不抛，因为那时 `os.environ` 是对的。

★ 第 2 条的顺序值得注意：开关是"别人打开"的，不是"读密钥的人自己判断"的。
    读密钥的代码不需要知道自己在不在多路复用里 —— 它只管"有没有作用域"。
"""
from __future__ import annotations

import contextvars
import os
from pathlib import Path
from typing import Dict, Optional

# 当前作用域的密钥表；None = 没有作用域（读即回落到 os.environ）
_SECRET_SCOPE: contextvars.ContextVar[Optional[Dict[str, str]]] = \
    contextvars.ContextVar("mini_secret_scope", default=None)

# 进程是否处于"一个进程服务多 profile"模式（由网关 / cron / migrate 打开）
_MULTIPLEX_ACTIVE = False

# ── 全局白名单（对应真实版 `_GLOBAL_ENV_EXACT` / `_GLOBAL_ENV_PREFIXES`，:98-135） ──
# 真实版那段注释逐字：
#   "# Genuinely-global env vars: process/deployment settings, NOT profile secrets.
#    # They keep reading os.environ even in multiplex mode (routing them through the
#    # fail-closed path would wrongly crash). Keep this tight — when in doubt a
#    # value is a profile secret. Membership is exact name OR prefix."
GLOBAL_ENV_EXACT = frozenset({
    # 进程身份 / 部署（真实版对应 HERMES_HOME / HERMES_PROFILE / _HERMES_GATEWAY …）
    "MINI_HOME", "MINI_ENV_FILE", "MINI_LAUNCH_CWD", "MINI_CONTEXT_CWD",
    "MINI_DEBUG", "MINI_TERMINAL_BACKEND", "MINI_GATEWAY_ALLOW_ALL_USERS",
    # shell / 解释器（进程怎么跑起来的，与 profile 无关）
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TZ", "PWD", "SHELL", "TMPDIR",
    "VIRTUAL_ENV", "PYTHONPATH", "SSL_CERT_FILE",
})

# ★ 与真实版的一条**刻意差别**：真实版有 3 个前缀（HERMES_KANBAN_ / HERMES_TELEGRAM_ / TERMINAL_），
#   mini **一个都不要** —— 因为 mini 的凭据就住在 `MINI_*` 命名空间里（MINI_SEARCH_API_KEY、
#   MINI_TAVILY_API_KEY）。**加一个 `MINI_` 前缀 = 把凭据放行**，这是这份名单最容易犯的错。
GLOBAL_ENV_PREFIXES: tuple = ()
# ★ 同样**刻意不在名单里**的（真实版那句"拿不准就当它是 profile 密钥"）：
#   MINI_CORE_TOOLS / MINI_CONTEXT_LENGTH / MINI_EPHEMERAL_SYSTEM_PROMPT / MINI_SEARCH_ENDPOINT
#   —— 它们是**行为设置**，将来完全可能变成 per-profile 的。
# 凭据一律不进名单（真实版 :111 逐字 "# API_SERVER_KEY is a credential: NOT here." 的等价物）。


def is_global_env(name: str) -> bool:
    """精确名命中，或任一前缀命中（真实版 `_is_global_env`，:133-135）。

    `startswith(())` 在空前缀表上恒为 False，所以"只有精确名"这条路不需要分支。
    """
    return name in GLOBAL_ENV_EXACT or name.startswith(GLOBAL_ENV_PREFIXES)


class UnscopedSecretError(RuntimeError):
    """多路复用下读了没有作用域的密钥。

    ★ 这个异常存在的唯一意义：把**静默的用错凭据**变成**响亮的报错**。
    """


def set_multiplex_active(active: bool) -> None:
    global _MULTIPLEX_ACTIVE
    _MULTIPLEX_ACTIVE = bool(active)


def multiplex_active() -> bool:
    return _MULTIPLEX_ACTIVE


def set_secret_scope(secrets: Dict[str, str]):
    return _SECRET_SCOPE.set(dict(secrets))


def reset_secret_scope(token) -> None:
    _SECRET_SCOPE.reset(token)


def current_secret_scope() -> Optional[Dict[str, str]]:
    return _SECRET_SCOPE.get()


def build_profile_secret_scope(home: Path) -> Dict[str, str]:
    """读某个 profile 的 `.env`，返回一份**只读字典**（不碰 `os.environ`）。"""
    out: Dict[str, str] = {}
    env_file = Path(home) / ".env"
    if not env_file.exists():
        return out
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def get_secret(key: str, default: Optional[str] = None) -> Optional[str]:
    """读一个密钥（对应真实版 `agent/secret_scope.py:143-171` 的**四分支**）。

    顺序：**全局白名单 → 当前作用域 → 兜底**。

    - 白名单（描述**进程/部署**的键，如 `PATH` / `MINI_HOME`）→ 永远读 `os.environ`；
    - 有作用域：命中 → 返回；未命中 → 多路复用下返回 `default`（**绝不回退 os.environ**），
      单 profile 下回退 `os.environ`（真实版逐字：*"the scope must stay a ``.env`` overlay,
      not a blindfold (otherwise cron 401s)"*）；
    - 没有作用域：多路复用开 → 抛 `UnscopedSecretError`；否则读 `os.environ`。

    ★ 第 3 章那版**没有白名单**，多路复用下 `get_secret("PATH")` 会抛、
      或（有作用域但映射里没有时）拿到空串 —— 这正是白名单要挡的"读到空/误抛"。
    """
    if is_global_env(key):
        return os.environ.get(key, default)
    scope = _SECRET_SCOPE.get()
    if scope is not None:
        if key in scope:
            return scope[key]
        return default if _MULTIPLEX_ACTIVE else os.environ.get(key, default)
    if _MULTIPLEX_ACTIVE:
        raise UnscopedSecretError(
            f"多路复用模式下读取密钥 {key!r} 时没有绑定 profile 作用域 —— "
            f"这会把【启动 profile】的凭据悄悄用在别的 profile 上。"
            f"调用方必须包在 profile 作用域里（gateway.run::_profile_runtime_scope）。"
        )
    return os.environ.get(key, default)
