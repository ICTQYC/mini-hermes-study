"""profile 作用域绑定（对应 Hermes `gateway/run.py::_profile_runtime_scope` 及其同族）。

**要解决的问题**：一个进程服务多个 profile 时，`os.environ`、模块级全局量、
import 期算出来的常量里，装的**永远是"启动那个 profile"**的值。
所以每一个"替次级 profile 干活"的动作都必须**显式绑定**。

一个 profile 是什么？一句话：**home + 密钥作用域 + 终端作用域，三者一体**
（真实版 `gateway/AGENTS.md` § Profile scope）。绑定要三件一起绑，
只绑其中一件就是那个著名的 bug 类：

> *an unbound read is a silent default-profile leak, never an error*
> （没绑定的读**不会报错**，只会悄悄用默认 profile 的值）

mini 绑两件（home + 密钥），第三件（终端作用域）mini 的 terminal 工具没有对应机制，
记在差距清单里。
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Dict, Iterator, Optional

from agent.secret_scope import (build_profile_secret_scope, reset_secret_scope,
                                set_secret_scope)
from hermes_constants import (reset_mini_home_override, set_mini_home_override)

# 需要从子进程环境里**摘掉**的键：profile 相关的路径与凭据
_ENV_STRIP_PREFIXES = ("MINI_", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def load_profile_secret_scope(home: Path) -> Dict[str, str]:
    return build_profile_secret_scope(Path(home))


@contextlib.contextmanager
def profile_runtime_scope(profile_home: Path, *,
                          hydrate_secrets: bool = True) -> Iterator[None]:
    """把一个回合（或一次 eviction、一次 tick）绑到某个 profile 上。

    ★ 为什么用 contextvar 而不是改 `os.environ`（真实版 docstring 原话）：
    "``set_hermes_home_override`` is a contextvar (reaches the agent worker via
    ``copy_context()``); ``set_secret_scope`` makes the profile ``.env`` the
    credential source **without mutating os.environ**, so subprocesses never
    inherit cross-profile secrets."
    """
    home_token = set_mini_home_override(str(Path(profile_home)))
    secrets = load_profile_secret_scope(profile_home) if hydrate_secrets else {}
    secret_token = set_secret_scope(secrets)
    try:
        yield
    finally:
        # ★ 复位顺序与绑定顺序相反（后绑的先解），保证异常路径也不会留残留作用域
        reset_secret_scope(secret_token)
        reset_mini_home_override(home_token)


def served_profile_child_env(profile_home: Path, base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """给子进程准备的环境（对应真实版 `tools/environments/local.py::served_profile_child_env`）。

    ★ 为什么不能直接 `os.environ.copy()`：那个副本里装的是**启动 profile** 的
    home 与密钥，子进程（比如 `hermes -p X worker`）会继承它们 ——
    于是"替 B 干活"的子进程拿着 A 的凭据跑。真实版对这种写法有一条明文禁令。
    """
    env = dict(base_env if base_env is not None else _os_environ())
    for key in list(env):
        if key.startswith(_ENV_STRIP_PREFIXES) or key.endswith("_API_KEY"):
            env.pop(key, None)
    env["MINI_HOME"] = str(Path(profile_home))
    return env


def _os_environ() -> Dict[str, str]:
    import os
    return dict(os.environ)
