"""第 14 章的自测：上下文引擎的发现 / 注册 / 选择（A1~A6）—— **不联网、零第三方依赖**。

跑法：python3 tests/test_context_engine.py

## 测的是什么（7 组 = 7 条不变量）

| 不变量 | 守的承诺 |
|---|---|
| **I1** 选中的引擎**真的被用** | 行为可观测：插件的 `compress()` 返回的标记**真的进了最终历史**（不是只看返回的名字） |
| **I2** 未知名 ⇒ 回落内置 + **有声**警告 | 回落要有声：status 里能读出**被请求的那个名字**（静默降级是最坏的一种） |
| **I3** 漏实现抽象方法 ⇒ 实例化即 `TypeError` | `abc.ABC` 免费给的契约；谁删了 `@abstractmethod` 这条就变红 |
| **I4** 默认引擎行为与改造前**逐字节一致** | 零配置 ⇒ `ContextCompressor`，关键字段一致 + 同一输入输出逐条相等 |
| **I5** 好引擎入库、坏目录进 `skipped` 并计数 | 宽容 + 计数：坏目录永不抛、永不进候选 |
| **I6** 名字唯一（保留名 + 确定性） | 目录不得占用内置保留名 `compressor`；两次发现顺序/名字完全一致 |
| **I7** 契约满足 + `name` 与目录名一致 | 默认与插件引擎都满足 ABC；插件 `name == 目录名 == config 值` 三处一致 |

## 隔离

`MINI_HOME` 在 **import 之前**指到临时目录（见 `_TEST_HOME`）——
否则测试会去读用户真实的 `~/.mini-hermes/engines/` 与 `config.json`，
结果就依赖"跑测试的这个人家里放了什么引擎"。每条用例自己造目录 + 写配置、开跑前先清空
（`reset_home()`），所以用例之间顺序无关。

## 组织方式

测试函数在文件里的**位置**与它们所属的**组**解耦（分组写在 `GROUPS` 表里）。
配一条**未注册自检**：定义了 `test_` 却没收进 `GROUPS` ⇒ 退出码 2
（"写了但从来没跑"是测试里最经典的静默失败）。与 `tests/test_skills.py` 同款。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★★ MINI_HOME 必须在 import 任何 mini 模块**之前**设好 —— 这样"引擎库 / 配置跟随
#    MINI_HOME"这件事从第一个 import 起就是真的。
_TEST_HOME = tempfile.mkdtemp(prefix="mini_ch14_engine_")
os.environ["MINI_HOME"] = _TEST_HOME

from agent import engine_registry as reg          # noqa: E402
from agent.context_compressor import (            # noqa: E402
    DEFAULT_CONTEXT_LENGTH,
    ContextCompressor,
)
from agent.context_engine import ContextEngine    # noqa: E402
from hermes_cli import config as cfg              # noqa: E402
from run_agent import AIAgent                     # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {detail}" if detail and not cond else ""))


HOME = Path(_TEST_HOME)
MARKER = "标记_插件引擎接管了压缩"

_OK_SUMMARY = "## Goal\n搭 FastAPI 项目\n\n## Progress\n### Done\n- 模块已写入\n\n## Next Steps\n- 跑测试"


# ── 夹具 ────────────────────────────────────────────────────────────────
def make_agent(**kw):
    """一个不联网的假 agent（和 tests/test_skills.py 用的是同一种）。"""
    return AIAgent(
        {
            "provider": "fake",
            "api_mode": "chat_completions",
            "base_url": "http://fake",
            "api_key": "fake",
            "model": "fake-model",
        },
        **kw,
    )


def reset_home() -> None:
    """清空引擎库 + 配置（每条用例自己造，彼此不串）。"""
    shutil.rmtree(reg.engines_dir(), ignore_errors=True)
    try:
        cfg.config_path().unlink()
    except FileNotFoundError:
        pass


def write_config(engine=None) -> None:
    """写 `config.json`。`engine=None` ⇒ **不写 context 段**（= 零配置）。"""
    data = {}
    if engine is not None:
        data["context"] = {"engine": engine}
    cfg.config_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def engine_source(name: str, marker: str = MARKER) -> str:
    """一个**可辨识**的最小引擎：`compress()` 返回一条带标记的 system 消息。"""
    return (
        "from agent.context_engine import ContextEngine\n"
        "\n"
        f"MARKER = {marker!r}\n"
        "\n"
        "class Engine(ContextEngine):\n"
        "    def __init__(self):\n"
        "        self.compress_calls = 0\n"
        "\n"
        "    @property\n"
        "    def name(self):\n"
        f"        return {name!r}\n"
        "\n"
        "    def should_compress(self, messages):\n"
        "        return True\n"
        "\n"
        "    def compress(self, messages, *, call_llm=None, call_llm_meta=None,\n"
        "                 status_callback=None):\n"
        "        self.compress_calls += 1\n"
        "        return [{\"role\": \"system\", \"content\": MARKER}] + list(messages[-1:])\n"
    )


def write_engine(dir_name: str, *, name: str = None, marker: str = MARKER,
                 source: str = None) -> Path:
    """造一个引擎目录。`name` 给了就与目录名**故意不同**（用来测契约校验）。"""
    d = reg.engines_dir() / dir_name
    d.mkdir(parents=True, exist_ok=True)
    text = source if source is not None else engine_source(dir_name if name is None else name, marker)
    (d / reg.ENGINE_INIT_FILENAME).write_text(text, encoding="utf-8")
    return d


def build_prose_history(turns: int = 10, chars: int = 3000) -> list:
    """以**散文**为主的历史（没有大块工具输出）—— 让摘要**真的发生**。"""
    msgs = [{"role": "system", "content": "You are a helpful assistant. " + "规则 " * 200},
            {"role": "user", "content": "帮我逐段分析这份合同的风险点"}]
    for i in range(turns):
        msgs.append({"role": "assistant",
                     "content": f"第 {i} 段分析：" + "风险点与依据" * (chars // 6)})
        msgs.append({"role": "user", "content": f"继续看第 {i + 1} 节，重点看第 3 条"})
    return msgs


def marker_in(msgs: list) -> bool:
    return any(MARKER in (m.get("content") or "") for m in msgs if isinstance(m, dict))


# ═══════════════════════════════ I1. 选中的引擎真的被用 ★
def test_selected_engine_really_takes_over() -> None:
    reset_home()
    write_engine("mymark")
    write_config("mymark")

    ag = make_agent()
    c = ag._compressor()
    check("选中的**不是**内置压缩器（多态真的换掉了）",
          not isinstance(c, ContextCompressor), type(c).__name__)
    check("选中引擎的 `name` == 目录名（选择按名字寻址）", getattr(c, "name", None) == "mymark",
          str(getattr(c, "name", None)))
    check("它是 ContextEngine 的实例", isinstance(c, ContextEngine), type(c).__name__)

    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"}]
    ag._preflight_compression(msgs)          # ← 走**宿主真路径**，不是直接调 compress
    check("★ 引擎的 `compress()` 真的被执行了（标记进了最终历史 → 行为可观测）",
          marker_in(msgs), str(msgs))
    check("…并且调用的就是缓存的**同一个**插件实例（`compress_calls` == 1）",
          getattr(c, "compress_calls", 0) == 1, str(getattr(c, "compress_calls", None)))


# ═══════════════════════════════ I2. 未知名 ⇒ 回落 + 有声 ★
def test_unknown_engine_falls_back_with_audible_warning() -> None:
    reset_home()
    write_config("nope")

    seen = []
    ag = make_agent(status_callback=seen.append)
    c = ag._compressor()
    check("未知名 ⇒ 回落**内置**压缩器（不拒绝启动）", isinstance(c, ContextCompressor), type(c).__name__)
    check("★ 回落是**有声**的：status 里读得到被请求的名字 `nope`",
          any("nope" in s for s in seen), str(seen))
    check("…警告是可操作的（给出可用引擎清单 / 注册提示）",
          any("可用引擎" in s for s in seen), str(seen))


def test_silent_when_engine_is_default() -> None:
    """反向对照：**名字就是默认值时不许有警告** —— 否则"有声"会退化成满屏噪声。"""
    reset_home()
    write_config("compressor")
    seen = []
    c = make_agent(status_callback=seen.append)._compressor()
    check("`context.engine == \"compressor\"` ⇒ 内置压缩器", isinstance(c, ContextCompressor))
    check("…且**不产生任何回落警告**（默认路径是安静的）", seen == [], str(seen))

    reset_home()                             # 连 context 段都没有
    seen2 = []
    c2 = make_agent(status_callback=seen2.append)._compressor()
    check("零配置（没有 context 段）同样安静", isinstance(c2, ContextCompressor) and seen2 == [], str(seen2))


# ═══════════════════════════════ I3. 漏抽象方法 ⇒ TypeError ★
def test_missing_abstract_member_raises_on_construction() -> None:
    class Bad(ContextEngine):
        pass

    ok, msg = False, ""
    try:
        Bad()
    except TypeError as exc:
        ok, msg = True, str(exc)
    except Exception as exc:                                   # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
    check("★ 漏实现抽象方法 ⇒ **实例化即** `TypeError`（abc 免费给的契约）", ok, msg)
    check("…错误里点名缺失的抽象成员（`name` / `should_compress` / `compress`）",
          any(k in msg for k in ("name", "should_compress", "compress")), msg)

    class HalfDone(ContextEngine):
        @property
        def name(self):
            return "half"

        def should_compress(self, messages):
            return True
        # ← 故意漏 `compress`

    ok2, msg2 = False, ""
    try:
        HalfDone()
    except TypeError as exc:
        ok2, msg2 = True, str(exc)
    check("只漏 `compress` 也一样报错（不是「全有或全无」）", ok2 and "compress" in msg2, msg2)

    # 强对照：补齐三个抽象成员 ⇒ 能实例化
    class Good(ContextEngine):
        @property
        def name(self):
            return "good"

        def should_compress(self, messages):
            return False

        def compress(self, messages):
            return list(messages)

    check("补齐抽象成员 ⇒ 正常实例化（这条守住「契约不是拦好引擎」）",
          isinstance(Good(), ContextEngine))


# ═══════════════════════════════ I4. 默认引擎行为逐字节一致 ★
def test_default_engine_matches_prechange_behavior() -> None:
    reset_home()                                       # 零配置
    ag = make_agent()
    c = ag._compressor()
    check("零配置 ⇒ 内置 `ContextCompressor`", isinstance(c, ContextCompressor), type(c).__name__)
    check("…它的 `name` 是 `compressor`", getattr(c, "name", None) == "compressor",
          str(getattr(c, "name", None)))

    # ★ 改造前的构造形状（写死的那几行）—— 必须与 `_compressor()` 造出来的**无差别**
    direct = ContextCompressor(
        context_length=int(getattr(ag, "context_length", 0) or DEFAULT_CONTEXT_LENGTH),
        status_callback=ag._emit_status,
    )
    keys = ["context_length", "threshold", "protect_first_n", "protect_last_n",
            "tail_mode", "max_tokens", "micro_compact_enabled",
            "micro_compact_every_n_turns", "micro_compact_defrag_threshold_tokens"]
    diffs = {k: (getattr(c, k), getattr(direct, k)) for k in keys if getattr(c, k) != getattr(direct, k)}
    check("★ 关键字段逐个一致（构造参数与顺序没漂移）", not diffs, str(diffs))
    check("`threshold_tokens` 一致（三步算术没被绕过）",
          c.threshold_tokens == direct.threshold_tokens,
          f"{c.threshold_tokens} vs {direct.threshold_tokens}")
    check("`describe()` 一致（诊断行也没变）", c.describe() == direct.describe(),
          f"{c.describe()!r} vs {direct.describe()!r}")

    # 同一输入 + 同一假 LLM ⇒ 输出**逐条相等**
    msgs1 = build_prose_history()
    msgs2 = build_prose_history()
    snapshot = json.dumps(msgs1, ensure_ascii=False, sort_keys=True)
    out1 = c.compress(msgs1, call_llm=lambda s, u: _OK_SUMMARY)
    out2 = direct.compress(msgs2, call_llm=lambda s, u: _OK_SUMMARY)
    check("★ 同一输入同一假 LLM ⇒ `compress` 输出逐条相等（零行为漂移）", out1 == out2,
          f"{len(out1)} vs {len(out2)}")
    check("…且真的压了（不是两条空走路）", len(out1) < len(msgs1) and out1,
          f"{len(msgs1)} → {len(out1)}")
    check("…传进去的列表逐字节没被改（引擎不碰 agent 状态）",
          json.dumps(msgs1, ensure_ascii=False, sort_keys=True) == snapshot)


# ═══════════════════════════════ I5. 发现容错（跳过并计数）
def test_discovery_is_tolerant_and_counts_skips() -> None:
    reset_home()
    write_engine("good")

    (reg.engines_dir() / "no_init").mkdir(parents=True)                       # 没有 __init__.py
    d = reg.engines_dir() / "syntax"
    d.mkdir()
    (d / reg.ENGINE_INIT_FILENAME).write_text("def broken(:\n    pass\n", encoding="utf-8")
    d = reg.engines_dir() / "no_subclass"
    d.mkdir()
    (d / reg.ENGINE_INIT_FILENAME).write_text("X = 1\n", encoding="utf-8")
    write_engine("mismatch", name="other")                                    # name != 目录名
    d = reg.engines_dir() / "abstract"
    d.mkdir()
    (d / reg.ENGINE_INIT_FILENAME).write_text(
        "from agent.context_engine import ContextEngine\n"
        "class Engine(ContextEngine):\n"
        "    def should_compress(self, messages):\n        return True\n"
        "    def compress(self, messages):\n        return list(messages)\n",
        encoding="utf-8")

    scan = reg.discover_engines()                    # ← 出了异常，这条用例就红了
    check("只有合规的那个进了候选", [e.name for e in scan.engines] == ["good"],
          str([e.name for e in scan.engines]))
    check("★ 五类坏目录全部被**跳过并计数**", len(scan.skipped) == 5,
          str([(s.dir_name, s.reason) for s in scan.skipped]))
    reasons = {s.dir_name: s.reason for s in scan.skipped}
    check("跳过原因是**可读的**（缺文件 / 语法错 / 没子类 / 名字不符 / 漏抽象方法）",
          "没有 __init__.py" in reasons.get("no_init", "")
          and "导入失败" in reasons.get("syntax", "")
          and "没有定义 ContextEngine 子类" in reasons.get("no_subclass", "")
          and "不一致" in reasons.get("mismatch", "")
          and "无法实例化" in reasons.get("abstract", ""),
          str(reasons))
    check("跳过清单也**确定序**（按目录名）",
          [s.dir_name for s in scan.skipped] == sorted(s.dir_name for s in scan.skipped),
          str([s.dir_name for s in scan.skipped]))
    check("坏引擎**一个都没进候选**（不会带病运行）",
          all(bad not in [e.name for e in scan.engines]
              for bad in ("no_init", "syntax", "no_subclass", "mismatch", "abstract")))

    reset_home()                                     # 引擎库都不存在时也要安静
    empty = reg.discover_engines()
    check("引擎库不存在 ⇒ 空扫描（不抛）", empty.engines == [] and empty.skipped == [],
          f"{empty.engines} / {empty.skipped}")


# ═══════════════════════════════ I6. 名字唯一（保留名 + 确定性）
def test_name_uniqueness_is_deterministic() -> None:
    reset_home()
    write_engine("alpha")
    write_engine("beta")
    write_engine(reg.DEFAULT_ENGINE_NAME)            # ★ 占用内置保留名的目录

    scan1 = reg.discover_engines()
    check("候选里每个名字只出现一次", len([e.name for e in scan1.engines]) ==
          len({e.name for e in scan1.engines}), str([e.name for e in scan1.engines]))
    check("★ 占用内置保留名 `compressor` 的目录被跳过并计数（内置名不可被顶替）",
          reg.DEFAULT_ENGINE_NAME not in [e.name for e in scan1.engines]
          and any(s.dir_name == reg.DEFAULT_ENGINE_NAME and "保留名" in s.reason
                  for s in scan1.skipped),
          str([(s.dir_name, s.reason) for s in scan1.skipped]))
    check("★ `select_engine(\"compressor\")` 永远命中**内置**实现（不被目录顶替）",
          isinstance(reg.select_engine("compressor",
                                       default_factory=lambda: ContextCompressor()).engine,
                     ContextCompressor))

    scan2 = reg.discover_engines()
    check("两次发现给出**完全一致**的顺序与名字（选择确定，不靠 FS 顺序）",
          [e.name for e in scan1.engines] == [e.name for e in scan2.engines],
          f"{[e.name for e in scan1.engines]} vs {[e.name for e in scan2.engines]}")


# ═══════════════════════════════ I7. 契约满足 + 三处名字一致
def test_contract_holds_and_name_matches_dir() -> None:
    reset_home()
    write_engine("plug")
    write_config("plug")

    eng = make_agent()._compressor()
    check("插件引擎是 `ContextEngine` 实例", isinstance(eng, ContextEngine), type(eng).__name__)
    check("`should_compress` / `compress` 可调用（契约满足）",
          callable(getattr(eng, "should_compress", None))
          and callable(getattr(eng, "compress", None)))
    check("★ `name` == 目录名 == `context.engine` 值（三处一致）",
          eng.name == "plug" == cfg.get_context_engine_name(),
          f"{eng.name!r} / {'plug'!r} / {cfg.get_context_engine_name()!r}")

    reset_home()                                     # 内置默认也要满足契约
    dflt = make_agent()._compressor()
    check("内置默认引擎同样满足契约（isinstance + 两个方法可调用）",
          isinstance(dflt, ContextEngine)
          and callable(dflt.should_compress) and callable(dflt.compress))
    check("…且 `name` 稳定、非空（选择键不是靠类名猜的）",
          isinstance(dflt.name, str) and dflt.name == reg.DEFAULT_ENGINE_NAME, repr(dflt.name))

    check("`engines_dir()` 锚定 MINI_HOME（测试重定向只有这一条路）",
          reg.engines_dir() == HOME / "engines", str(reg.engines_dir()))


# ══════════════════════════════════════════════════════════════════════════════ #
GROUPS = [
    ("I1. 选中的引擎真的被用 ★",
     "多态真的换掉 · name == 目录名 · `compress()` 的标记**进了最终历史**（行为可观测）",
     [test_selected_engine_really_takes_over]),
    ("I2. 未知名 ⇒ 回落 + 有声 ★",
     "回落内置（不拒绝启动） · status 里读得到被请求的名字与可用清单 · 默认路径**安静**",
     [test_unknown_engine_falls_back_with_audible_warning,
      test_silent_when_engine_is_default]),
    ("I3. 漏抽象方法 ⇒ 实例化即 TypeError ★",
     "全漏 / 只漏 compress 都在实例化时报错 · 补齐后正常",
     [test_missing_abstract_member_raises_on_construction]),
    ("I4. 默认引擎行为与改造前逐字节一致 ★",
     "零配置 ⇒ ContextCompressor · 关键字段相同 · threshold_tokens 相同 · 输出逐条相等 · 不改传入列表",
     [test_default_engine_matches_prechange_behavior]),
    ("I5. 发现容错（跳过并计数）",
     "缺文件 / 语法错 / 没子类 / 名字不符 / 漏抽象方法 → 跳过并计数（不抛） · 空库安静",
     [test_discovery_is_tolerant_and_counts_skips]),
    ("I6. 名字唯一（保留名 + 确定性）",
     "保留名 `compressor` 不可被目录顶替 · 名字只出现一次 · 两次发现顺序一致",
     [test_name_uniqueness_is_deterministic]),
    ("I7. 契约满足 + 三处名字一致",
     "默认与插件都满足 ABC · 插件 name == 目录名 == config 值 · engines_dir 锚 MINI_HOME",
     [test_contract_holds_and_name_matches_dir]),
]


def main() -> None:
    print(f"解释器：{sys.executable}  Python {sys.version.split()[0]}")
    print(f"MINI_HOME = {_TEST_HOME}\n")

    # ★ 未注册自检：写了 test_ 却没进 GROUPS = "写了但不会跑"（最经典的测试静默失败）
    defined = {n for n in globals() if n.startswith("test_")}
    registered = {f.__name__ for _, _, fns in GROUPS for f in fns}
    missing = defined - registered
    if missing:
        print(f"⚠️ 有测试函数没登记进 GROUPS（写了但不会跑）：{sorted(missing)}")
        raise SystemExit(2)

    for title, promise, fns in GROUPS:
        print(f"══ {title} ══ {promise}")
        for fn in fns:
            # 用例抛异常 = 一条**可诊断的红**，而不是"套件崩了"（后面的组全被吞掉）
            try:
                fn()
            except Exception as exc:                 # noqa: BLE001
                check(f"{fn.__name__} 抛异常", False, f"{type(exc).__name__}: {exc}")
        print(f"   → {len(PASS)} 累计 / {len(FAIL)} 失败\n")

    print(f"{len(PASS)} 通过 / {len(FAIL)} 失败　（{len(GROUPS)} 组）")
    if FAIL:
        print("失败项：" + "; ".join(FAIL))
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
