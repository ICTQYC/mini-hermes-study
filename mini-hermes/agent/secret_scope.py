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
    """读一个密钥。

    顺序：**当前作用域 → （没有作用域时）os.environ**。
    多路复用打开 + 没有作用域 → 抛 `UnscopedSecretError`（fail closed）。
    """
    scope = _SECRET_SCOPE.get()
    if scope is not None:
        return scope.get(key, default)
    if _MULTIPLEX_ACTIVE:
        raise UnscopedSecretError(
            f"多路复用模式下读取密钥 {key!r} 时没有绑定 profile 作用域 —— "
            f"这会把【启动 profile】的凭据悄悄用在别的 profile 上。"
            f"调用方必须包在 profile 作用域里（gateway.run::_profile_runtime_scope）。"
        )
    return os.environ.get(key, default)
