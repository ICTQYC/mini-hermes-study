"""钩子：生命周期事件的扩展点（对应 Hermes 的 `gateway/hooks.py`）。

真实版的形态很朴素 —— **一个目录，一个钩子一个子目录**：

    <profile home>/hooks/
    ├── 我的钩子/
    │   ├── HOOK.yaml     # 清单：名字、要听哪些事件
    │   └── handler.py    # `def handle(event, payload)` 处理函数

★ 两个设计细节值得抄：

1. **路径在调用时解析，绝不在 import 时**（真实版 docstring 原话：
   "Load the active home's hooks now"）。在一个进程服务多 profile 时，
   import 期看到的 home 永远是**启动 profile** 的。
2. 内置钩子目录 `builtin_hooks/` 出厂**是空的**（真实版只放一个空壳桩
   `_register_builtin_hooks()`）—— 它是一个**扩展点**，不是"已经有什么东西"。

mini 版把 `HOOK.yaml` 的解析写成了一个极小解析器（只认 `key: value`），
因为 mini 零第三方依赖（真实版用 PyYAML）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 事件表（真实版 `gateway/hooks.py:5-6` 的 8 个；mini 有对应时机的 6 个 + 1 个维护 tick）
HOOK_EVENTS: Tuple[str, ...] = (
    "gateway:startup",     # 网关进程启动
    "session:start",       # 一个新会话开始
    "session:end",         # 会话完成
    "session:reset",       # 用户 /new 重置
    "agent:start",         # 开始处理一条消息
    "agent:end",           # 处理完并返回
    "command",             # 任何斜杠命令被执行（真实版是通配 `command:*`）
    "gateway:tick",        # ★ mini 新增：维护 tick 的形状（真实版是 cron 滴答等四件事）
)


def _parse_manifest(path: Path) -> Dict[str, str]:
    """极小 YAML 解析：只认 `key: value`（够 HOOK.yaml 用）。"""
    out: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _load_hook_dir(hook_dir: Path) -> Optional[Tuple[str, List[str], Callable, str]]:
    """加载一个钩子目录 → `(名字, 事件列表, 处理函数, 说明)`；无效返回 None。"""
    manifest = hook_dir / "HOOK.yaml"
    handler = hook_dir / "handler.py"
    if not manifest.exists() or not handler.exists():
        return None
    meta = _parse_manifest(manifest)
    events = [e.strip() for e in meta.get("events", "").replace(";", ",").split(",") if e.strip()]
    if not events:
        return None
    spec = importlib.util.spec_from_file_location(f"mini_hook_{hook_dir.name}", handler)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, "handle", None)
    if not callable(fn):
        return None
    return meta.get("name", hook_dir.name), events, fn, meta.get("description", "")


class HookRegistry:
    """钩子的发现、加载与事件分发。"""

    def __init__(self, hooks_dir: Path):
        self.hooks_dir = Path(hooks_dir)
        self._handlers: Dict[str, List[Callable]] = {}
        self.errors: List[str] = []

    def _register_builtin_hooks(self) -> None:
        """内置钩子桩（真实版 `gateway/hooks.py:86-87` 同款：**出厂没有任何内置钩子**）。

        留这个函数是为了让"扩展点在哪"有个固定名字 —— 而不是让每个想加内置钩子的人
        都在 `discover_and_load()` 里手插一行。
        """
        return None

    def discover_and_load(self) -> List[str]:
        """注册内置桩，然后扫 `<hook home>/hooks/` 下每个有效目录。返回加载到的钩子名。"""
        self._register_builtin_hooks()
        loaded: List[str] = []
        if not self.hooks_dir.exists():
            return loaded
        for hook_dir in sorted(self.hooks_dir.iterdir()):
            if not hook_dir.is_dir():
                continue
            try:
                item = _load_hook_dir(hook_dir)
            except Exception as exc:
                self.errors.append(f"{hook_dir.name}: {exc!r}")
                continue
            if item is None:
                self.errors.append(f"{hook_dir.name}: 缺少 HOOK.yaml / handler.py，或事件为空")
                continue
            name, events, fn, _desc = item
            for event in events:
                self._handlers.setdefault(event, []).append(fn)
            loaded.append(name)
        return loaded

    def emit(self, event: str, **payload: Any) -> List[Any]:
        """派发事件。**单个钩子抛异常不影响其它钩子，也不影响网关主流程**。

        ★ 为什么必须这样：钩子是**用户代码**，装坏一个钩子不该让整个网关收不到消息。
        """
        results = []
        for fn in self._handlers.get(event, ()):
            try:
                results.append(fn(event, payload))
            except Exception as exc:
                self.errors.append(f"{event}: {exc!r}")
        return results

    def handler_count(self, event: str) -> int:
        return len(self._handlers.get(event, ()))
