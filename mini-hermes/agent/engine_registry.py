"""引擎发现 + 注册 + 选择（第 14 章 A4/A6）。

对应真实 Hermes 的 `plugins/context_engine/__init__.py`（目录加载器）
+ `agent/agent_init.py::_select_context_engine`（按 `context.engine` 选）。

## 这一层解决什么问题

第 5 章的压缩器是**写死**在 `run_agent.py::_compressor()` 里构造的。
"换个压缩策略" = 改宿主代码。本章把那一处换成**查表**：

    context.engine  ──►  select_engine(name)  ──►  一个 ContextEngine 实例

于是"新增一种引擎 = 放一个目录"，宿主一行不改。

## 约定（目录契约 —— 唯一实现，别处不许再拼这个路径）

```text
<MINI_HOME>/engines/
    <name>/__init__.py     ← 必须**只**导出（定义）一个 ContextEngine 子类
    .git/ __pycache__/ …   ← 排除（不扫）
```

- 引擎类的 `name` 属性必须**等于**它所在目录的名字（三处一致：
  目录名 = `name` = `context.engine` 值）。不一致 ⇒ 目录被跳过并计数。
- 引擎类必须能**无参构造**：发现阶段要无参实例化一次才能读到 `name`
  （`name` 是属性，不是类属性）。宿主另按下面的"调用约定"调它。
- 名字 ``"compressor"`` 是**保留名**（内置默认引擎）：`engines/compressor/`
  会被跳过 —— 否则用户会在"以为换掉了、其实没换"的状态里运行。
- **单文件约定**：引擎类必须定义在本目录的 `__init__.py` 里（不接受从别处
  转发导入的类）—— 这样"这个目录属于哪个引擎"只有一个答案。

### 宿主调用约定（引擎实现时要接的形状）

`run_agent.py::_preflight_compression` 是**注入式**调用：

    engine.compress(messages, call_llm=…, call_llm_meta=…)
    engine.should_compress(messages)          # False 时宿主还会读
    engine.current_tokens(messages)           #   current_tokens / threshold_tokens

所以插件引擎的 `should_compress` / `compress` 必须**接受**这些（可忽略）关键字；
若 `should_compress` 可能返回 False，还要提供 `current_tokens` / `threshold_tokens`
（真实侧的 ABC 用"类属性即状态契约"表达同一件事，见清单 A2）。
ABC 的窄签名是最小义务，宿主多传的关键字是可选能力 —— 与上游
`compress()` 的 `focus_topic` / `force` / `memory_context` 同一口径。

## 三条纪律（照 mini 已有先例）

1. **零第三方依赖**：`importlib` / `pathlib` 都是 stdlib；`importlib` **只在
   真发现时**用（模块顶层不导入引擎）。
2. **每次选择真读盘**（与 `discover_skills` 同款，无进程内缓存）：选择是低频事件，
   缓存只会引入失效问题 —— "不做投机性基础设施"。
3. **宽容 + 计数**（与第 13 章技能发现同口径）：坏目录 / 导入失败 / 不满足契约
   ⇒ **跳过并计数**（带可读原因），绝不把坏引擎带进候选、绝不抛。

## 与上游的差异（为什么不抄那套插件宿主）

真实侧有通用插件系统（`register(ctx)` / `register_context_engine`）、两个发现根
（仓库 `plugins/` + `~/.hermes/plugins/`）、`deepcopy` 单例防护、线程安全契约。
mini **没有这些基座**，照抄就是没有消费者的死代码（见清单 B 批 B1/B2/B5）。
本章只留"一条发现路径 + 一个选择函数"，形状与上游一致、位置本地化。
"""
from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from agent.context_engine import ContextEngine
from hermes_constants import get_mini_home

logger = logging.getLogger(__name__)

__all__ = [
    "ENGINE_INIT_FILENAME", "EXCLUDED_ENGINE_DIRS", "DEFAULT_ENGINE_NAME",
    "EngineEntry", "SkippedEngine", "EngineScan", "EngineSelection",
    "engines_dir", "iter_engine_dirs", "read_engine",
    "discover_engines", "resolve_engine", "select_engine",
]

#: 一个引擎就是一个目录 + 这个文件（对应真实侧 `<name>/__init__.py`）。
ENGINE_INIT_FILENAME = "__init__.py"

#: 扫描时排除的目录 —— 与 `agent/skill_utils.py:79` 的 `EXCLUDED_SKILL_DIRS` 同款。
EXCLUDED_ENGINE_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv"})

#: 内置默认引擎的**保留名**（`ContextCompressor.name`）。也与
#: `hermes_cli/config.py::DEFAULT_CONTEXT_ENGINE` 同值。
DEFAULT_ENGINE_NAME = "compressor"


# ── 数据形状 ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EngineEntry:
    """一个**可用**引擎：名字 + 目录 + **已验证**的实例。

    `engine` 是发现阶段无参构造出来的实例（已经过 `name == 目录名` 的校验）。
    选择时直接用它 —— 每次 `discover_engines()` 都真读盘，所以拿到的是**新鲜的**
    实例，不是被别的调用方改过的共享单例。
    """

    name: str
    dir: Path
    engine: ContextEngine


@dataclass(frozen=True)
class SkippedEngine:
    """一个被跳过的引擎目录 + **可读的原因**（计数之外还要能解释）。"""

    dir_name: str
    reason: str


@dataclass(frozen=True)
class EngineScan:
    """一次扫描的结果：能用的（已按名字排序）+ 跳过的（已按目录名排序）。"""

    engines: List[EngineEntry] = field(default_factory=list)
    skipped: List[SkippedEngine] = field(default_factory=list)


@dataclass(frozen=True)
class EngineSelection:
    """一次选择的结果（结构化 —— 让"回落"能被调用方**读出来并打印**）。

    | 字段 | 含义 |
    |---|---|
    | `engine` | 实际用上的引擎（回落时是内置默认压缩器） |
    | `requested` | 用户请求的名字（`context.engine` 的值） |
    | `used` | 实际用上的引擎名字 |
    | `fell_back` | 是否发生了回落（`requested != used`） |
    | `warning` | **有声警告**（`None` = 无）—— 调用方**必须**打印它 |

    ★ `warning` 不是日志：它要经宿主的 `_emit_status` 打到用户眼前。
      静默回落是这一章最坏的一种 —— 用户以为换了引擎。
    """

    engine: ContextEngine
    requested: str
    used: str
    fell_back: bool
    warning: Optional[str] = None


# ── 目录 ────────────────────────────────────────────────────────────────
def engines_dir() -> Path:
    """引擎库根目录：``<MINI_HOME>/engines``（**唯一来源**）。

    ★ 与 `agent/skill_utils.py::skills_dir()` 同构：路径 = `get_mini_home()/"engines"`。
      HOME 锚定的好处逐字照抄那条注释 —— ① profile 就是 home，一套引擎天然隔离；
      ② 测试 / E2E 只靠 `MINI_HOME` 一条路重定向，不会去读用户真实的目录。
    """
    return get_mini_home() / "engines"


def iter_engine_dirs(root: Optional[Path] = None) -> List[Path]:
    """**候选**引擎目录（排序后返回；跳过隐藏 / 排除 / 非目录）。

    ★ 必须排序：`iterdir()` 的顺序由文件系统给、不保证稳定，而"选中的是哪一个"
      必须是**确定的**（I6）。排序键取目录名。
    """
    base = Path(root) if root is not None else engines_dir()
    if not base.is_dir():
        return []
    out: List[Path] = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or entry.name in EXCLUDED_ENGINE_DIRS:
            continue
        if entry.is_dir():
            out.append(entry)
    return out


# ── 读一个引擎目录 ───────────────────────────────────────────────────────
def _load_engine_class(engine_dir: Path) -> Tuple[Optional[type], str]:
    """把目录里的 `__init__.py` 载入，取**本模块定义的** ContextEngine 子类。

    返回 ``(类, "")`` 或 ``(None, 跳过原因)``。★ 导入失败**吞掉异常**并转成原因
    （宽容 + 计数，与 `read_skill` 同款）—— 一个坏引擎不该让整台机器起不来。
    """
    init_path = engine_dir / ENGINE_INIT_FILENAME
    if not init_path.is_file():
        return None, f"没有 {ENGINE_INIT_FILENAME}"
    module_name = f"mini_engine_{engine_dir.name}"
    spec = importlib.util.spec_from_file_location(module_name, init_path)
    if spec is None or spec.loader is None:
        return None, f"无法为 {ENGINE_INIT_FILENAME} 建立加载规格"
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:                          # noqa: BLE001 —— 见 docstring
        return None, f"导入失败：{type(exc).__name__}: {exc}"
    candidates: List[type] = []
    for obj in vars(module).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, ContextEngine)
            and obj is not ContextEngine
            # ★ 只认**本目录定义**的类：转发导入别的引擎不算（单文件约定）。
            and getattr(obj, "__module__", "") == module_name
        ):
            candidates.append(obj)
    if not candidates:
        return None, f"{ENGINE_INIT_FILENAME} 里没有定义 ContextEngine 子类"
    # 一个目录里定义了多个 ⇒ 取类名最小的那个（**确定序**，不靠 dict 顺序）。
    candidates.sort(key=lambda cls: cls.__name__)
    return candidates[0], ""


def read_engine(engine_dir: Path) -> Tuple[Optional[EngineEntry], str]:
    """读一个引擎目录 → ``(EngineEntry, "")`` 或 ``(None, 跳过原因)``。

    ★ **宽容 + 计数**：坏目录**跳过并计数**，永不抛、永不"带病进候选"。
    ★ 契约校验（不满足任一 ⇒ 跳过）：
      ① 不是符号链接目录（软链是逃逸路径，与技能发现的处置一致）；
      ② 目录名不是保留名 ``"compressor"``（内置默认引擎）；
      ③ `__init__.py` 能导入、且定义了 ContextEngine 子类；
      ④ 能无参实例化（漏实现抽象方法 ⇒ 这里就报 `TypeError`）；
      ⑤ 实例的 `name` 非空、且**等于目录名**。
    """
    if engine_dir.is_symlink():
        return None, "符号链接目录（不跟随：可能是逃逸路径）"
    if engine_dir.name == DEFAULT_ENGINE_NAME:
        # ★ 保留名：内置默认引擎的地址。占住它的目录一律跳过 ——
        #   否则 `select_engine("compressor")` 就有两个答案（I6 抓的正是这条）。
        return None, f"名字与内置默认引擎的保留名 {DEFAULT_ENGINE_NAME!r} 冲突"
    cls, reason = _load_engine_class(engine_dir)
    if cls is None:
        return None, reason
    try:
        engine = cls()
    except Exception as exc:                          # noqa: BLE001
        # 最常见的原因就是漏实现抽象方法（abc 在实例化时抛 `TypeError`）。
        return None, f"无法实例化（漏实现抽象方法？）：{type(exc).__name__}: {exc}"
    declared = getattr(engine, "name", None)
    if not isinstance(declared, str) or not declared.strip():
        return None, "name 为空或不是字符串"
    if declared.strip() != engine_dir.name:
        return None, f"name {declared!r} 与目录名 {engine_dir.name!r} 不一致"
    return EngineEntry(name=engine_dir.name, dir=engine_dir, engine=engine), ""


def discover_engines(root: Optional[Path] = None) -> EngineScan:
    """扫一遍引擎库 → `EngineScan`（能用的 + 跳过清单，都已排序）。

    ★ 每次调用都**真读盘**（无进程内缓存）：选择是低频事件，缓存只会引入失效问题。
    ★ 名字唯一：`name` 与目录名绑定后，两个目录本就不可能同名；这里的 `seen`
      是**纵深防御**（对应 `discover_skills` 的"首见胜出"）—— 保留它，未来若放宽
      "name == 目录名"，重名也不会让 `select_engine` 有两个答案。
    """
    engines: List[EngineEntry] = []
    skipped: List[SkippedEngine] = []
    seen: dict = {}
    for directory in iter_engine_dirs(root):
        entry, reason = read_engine(directory)
        if entry is None:
            skipped.append(SkippedEngine(dir_name=directory.name, reason=reason))
            continue
        if entry.name in seen:
            skipped.append(
                SkippedEngine(dir_name=directory.name,
                              reason=f"名字与 {seen[entry.name]} 重复（首见胜出）")
            )
            continue
        seen[entry.name] = directory.name
        engines.append(entry)
    engines.sort(key=lambda e: e.name)                 # ← 选择确定的载体
    skipped.sort(key=lambda s: s.dir_name)             # ← 跳过清单也确定序
    return EngineScan(engines=engines, skipped=skipped)


def resolve_engine(name: str, root: Optional[Path] = None) -> Optional[EngineEntry]:
    """按**名字**在已发现的引擎里找（找不到 → `None`）。

    与 `resolve_skill` 同款：**查索引**而不是拼路径
    （`engines_dir()/name/__init__.py`）—— 名字只用来做**相等比较**，
    路径永远来自扫描结果，于是"越界"在结构上不可能发生。
    """
    wanted = name.strip() if isinstance(name, str) else ""
    if not wanted:
        return None
    for entry in discover_engines(root).engines:
        if entry.name == wanted:
            return entry
    return None


# ── 选择（本章的接缝）─────────────────────────────────────────────────────
def select_engine(
    name: Optional[str] = None,
    *,
    default_factory: Callable[[], ContextEngine],
    root: Optional[Path] = None,
) -> EngineSelection:
    """按 `context.engine` 的值选一个引擎实例。**永不抛**（宽容 + 有声）。

    分流（对应真实侧 `agent_init.py:1761-1808::_select_context_engine`）：

    | 输入 | 结果 |
    |---|---|
    | 没给名 / 给了空串 / 名字是 ``"compressor"`` | 内置默认引擎（`default_factory()`），**无警告** |
    | 名字命中一个已发现的引擎 | 那个引擎（**每次真读盘**，拿新实例） |
    | 未知名 / 该目录被跳过 | 回落内置默认引擎 + **有声警告**（`warning` 非空） |

    ★ 为什么 `default_factory` 是**注入**的（而不是这里 `import ContextCompressor`）：
      "内置默认怎么构造"只有一个来源 —— `run_agent.py::_compressor()` 注入的工厂里
      **逐字保留改造前的构造参数与顺序**。这样本章不新增第二个构造形状，
      "零配置 = 与改造前逐字节一致"（I4）才守得住。注册表只负责"发现/选择"。
    ★ 为什么**回落**而不是拒绝启动：与 mini 自己的纪律一致（第 13 章技能发现的
      "宽容 + 计数"）。一个名字写错不该让 agent 起不来。但**回落必须有声** ——
      静默降级是这一章最坏的一种。
    """
    requested = name.strip() if isinstance(name, str) and name.strip() else DEFAULT_ENGINE_NAME
    if requested == DEFAULT_ENGINE_NAME:
        return EngineSelection(engine=default_factory(), requested=requested,
                               used=DEFAULT_ENGINE_NAME, fell_back=False, warning=None)

    scan = discover_engines(root)
    for entry in scan.engines:
        if entry.name == requested:
            return EngineSelection(engine=entry.engine, requested=requested,
                                   used=requested, fell_back=False, warning=None)

    available = [e.name for e in scan.engines]
    reasons = {s.dir_name: s.reason for s in scan.skipped}
    detail = f"（该目录被跳过：{reasons[requested]}）" if requested in reasons else ""
    warning = (
        f"⚠️ 未找到上下文引擎 {requested!r}，回落内置默认引擎 {DEFAULT_ENGINE_NAME!r}"
        f"{detail}；可用引擎：{available or '（无）'}。"
        f"（在 {engines_dir()}/<名字>/ 下放一个引擎目录即可注册。）"
    )
    return EngineSelection(engine=default_factory(), requested=requested,
                           used=DEFAULT_ENGINE_NAME, fell_back=True, warning=warning)
