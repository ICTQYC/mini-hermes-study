"""供应商注册表 —— **唯一事实源** + 插件式注册 + **懒发现**（A2）。

对应 Hermes 的 `providers/__init__.py`。

**为什么要\"注册表 + 懒发现\"**：
  * 供应商画像可以来自三处（内置 / `$MINI_HOME` 下的用户插件 / 未来 pip 包）——
    任何一处只要 `register_provider(profile)` 就能被认识，**不用改仓库里的代码**；
  * 发现是**懒的**：第一次 `get_provider_profile()` / `list_providers()` 才去 import 各 profile，
    免得 import 期就碰文件系统 / 触发一堆副作用。

后注册者**覆盖**先注册者（last-writer-wins）—— 所以用户插件能顶掉同名内置画像，
第三人也能 monkey-patch 而不动仓库。

用法::

    from providers import get_provider_profile
    profile = get_provider_profile("deepseek")   # ProviderProfile 或 None
    profile = get_provider_profile("deep-seek")  # 名字 + 别名都能查
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional

from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

# canonical name → ProviderProfile
_REGISTRY: Dict[str, ProviderProfile] = {}
# alias → canonical name
_ALIASES: Dict[str, str] = {}
_PROVIDER_LIST_CACHE: Optional[List[ProviderProfile]] = None
_discovered = False

# 仓库内的内置画像目录：providers/builtin/<name>.py
_BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"


def register_provider(profile: ProviderProfile) -> None:
    """按名字 + 别名注册一份画像。

    同名后注册者覆盖先注册者（`last-writer-wins`）——
    用户插件因此能顶掉内置画像，而不必改仓库代码。
    """
    global _PROVIDER_LIST_CACHE
    if profile is None or not getattr(profile, "name", ""):
        raise ValueError("register_provider 需要一份带 name 的 ProviderProfile")
    _REGISTRY[profile.name] = profile
    for alias in profile.aliases:
        _ALIASES[str(alias).strip().lower()] = profile.name
    _PROVIDER_LIST_CACHE = None


def get_provider_profile(name: str) -> Optional[ProviderProfile]:
    """按名字**或别名**查一份画像；查不到返回 None（表示\"没有专属画像，走通用路径\"）。

    ★ 首次调用触发**懒发现**。`custom:<name>` 这类具名自定义路由在没有专属画像时
      回落到通用 `custom` 画像（真实版同款：named custom routes share the generic wire policy）。
    """
    if not _discovered:
        _discover_providers()
    if not isinstance(name, str):
        return None
    key = name.strip().lower()
    canonical = _ALIASES.get(key, key)
    profile = _REGISTRY.get(canonical)
    if profile is None and key.startswith("custom:"):
        profile = _REGISTRY.get("custom")
    return profile


def list_providers() -> List[ProviderProfile]:
    """所有已注册画像（每个 canonical name 一份）。首次调用触发懒发现。"""
    global _PROVIDER_LIST_CACHE
    if not _discovered:
        _discover_providers()
    if _PROVIDER_LIST_CACHE is not None:
        return list(_PROVIDER_LIST_CACHE)
    seen = set()
    result: List[ProviderProfile] = []
    for profile in _REGISTRY.values():
        if id(profile) not in seen:
            seen.add(id(profile))
            result.append(profile)
    _PROVIDER_LIST_CACHE = result
    return list(result)


def _user_plugins_dir() -> Optional[Path]:
    """``$MINI_HOME/plugins/model-providers/``（存在才返回）。"""
    try:
        from hermes_constants import get_mini_home

        d = get_mini_home() / "plugins" / "model-providers"
        return d if d.is_dir() else None
    except Exception:  # noqa: BLE001 —— 拿不到 home 就当没有用户插件
        return None


def _import_plugin_dir(plugin_dir: Path, source: str) -> None:
    """import 一个用户插件目录，让它 import 期自我注册。"""
    init_file = plugin_dir / "__init__.py"
    if not init_file.exists():
        return
    safe_name = plugin_dir.name.replace("-", "_")
    module_name = f"_mini_user_provider_{safe_name}"   # 用户插件用唯一名，避免多 profile 互相串
    if module_name in sys.modules:
        return
    try:
        spec = importlib.util.spec_from_file_location(
            module_name, init_file, submodule_search_locations=[str(plugin_dir)]
        )
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 —— 坏插件不该拖垮整个发现
        logger.warning("加载 %s 供应商插件 %s 失败：%s", source, plugin_dir.name, exc)
        sys.modules.pop(module_name, None)


def _discover_providers() -> None:
    """把内置画像 + 用户插件都 import 一遍（各自在 module 级 `register_provider`）。

    顺序 = 优先级（后注册者赢）：
      1. 内置 `providers/builtin/<name>.py`
      2. `$MINI_HOME/plugins/model-providers/<name>/`

    ★ 只想跑一次（`_discovered` 守卫）；坏文件按 warning 跳过，不影响其余。
    """
    global _discovered
    if _discovered:
        return
    _discovered = True

    # 1. 内置画像 —— 随仓库走
    if _BUILTIN_DIR.is_dir():
        names = sorted(p.stem for p in _BUILTIN_DIR.glob("*.py") if not p.stem.startswith("_"))
        for name in names:
            try:
                importlib.import_module(f"providers.builtin.{name}")
            except ImportError as exc:
                logger.warning("导入内置供应商模块 %s 失败：%s", name, exc)

    # 2. 用户插件 —— 可覆盖同名内置（last-writer-wins）
    user_dir = _user_plugins_dir()
    if user_dir is not None:
        for child in sorted(user_dir.iterdir()):
            if child.is_dir() and not child.name.startswith(("_", ".")):
                _import_plugin_dir(child, "user")


__all__ = ["ProviderProfile", "register_provider", "get_provider_profile", "list_providers"]
