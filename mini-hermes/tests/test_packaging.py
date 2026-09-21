"""发布前自检 —— 把"别人 clone 下来能跑"这个说法变成可执行的断言。

跑法：python3 tests/test_packaging.py

检查三件事：
  1. 真的零第三方依赖（不是嘴上说说 —— 逐文件 AST 扫 import，再查模块来源）
  2. 没有写死某台机器的绝对路径（/Users/某人、/home/某人）
  3. 启动脚本不硬编码解释器路径（别人机器上 /usr/local/bin/python3 不存在）

第 1 条尤其重要：一旦某个未来章节偷偷引入了第三方库，
这个测试会立刻红 —— 而 README 和 requirements.txt 还在说"零依赖"，
那种不一致比一开始就有依赖糟糕得多。
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
import sysconfig
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 项目自己的顶层模块（和 Hermes 一一对应的那套文件名）
PROJECT_MODULES = {
    "hermes_constants",
    "model_tools",
    "run_agent",
    "cli",
    "hermes_state",
    "tools",
    "agent",
    "hermes_cli",
}

STDLIB_PATH = Path(sysconfig.get_paths()["stdlib"]).resolve()

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✓' if cond else '✗'} {name}" + (f"  → {detail}" if detail and not cond else ""))


def is_stdlib(module_root: str) -> bool:
    """模块来源在标准库目录里吗？（比维护一份 stdlib 名单可靠）"""
    if module_root in sys.builtin_module_names:
        return True
    try:
        spec = importlib.util.find_spec(module_root)
    except (ImportError, ValueError):
        return False
    if spec is None or spec.origin in (None, "built-in", "frozen"):
        return True
    origin = str(Path(spec.origin).resolve())
    return origin.startswith(str(STDLIB_PATH))


def imported_roots(path: Path) -> set[str]:
    """用 AST 取出文件里所有 import 的顶层模块名。

    用 AST 而不是正则：注释里的 `import openai` 不该算，字符串里的也不算。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:      # 相对 import 不算外部依赖
                roots.add(node.module.split(".")[0])
    return roots


def project_files() -> list[Path]:
    return sorted(
        p for p in PROJECT_ROOT.rglob("*.py") if "__pycache__" not in p.parts
    )


# ---------------------------------------------------------------- 1 零第三方依赖
def test_no_third_party_deps() -> None:
    offenders: list[str] = []
    seen_roots: set[str] = set()

    for path in project_files():
        for root in imported_roots(path):
            seen_roots.add(root)
            if root in PROJECT_MODULES:
                continue
            if is_stdlib(root):
                continue
            offenders.append(f"{path.relative_to(PROJECT_ROOT)} → {root}")

    check(
        f"扫了 {len(project_files())} 个 .py 文件，零第三方依赖",
        not offenders,
        "发现第三方 import：" + "; ".join(offenders),
    )
    third = sorted(r for r in seen_roots if r not in PROJECT_MODULES and not is_stdlib(r))
    check(
        "用到的全是标准库 + 项目自己的模块",
        not third,
        f"{len(seen_roots)} 个模块根里混进了：{third}",
    )
    print(f"  （共用到 {len(seen_roots - PROJECT_MODULES)} 个标准库模块："
          f"{', '.join(sorted(seen_roots - PROJECT_MODULES))}）")


# ---------------------------------------------------------------- 2 没有写死路径
def test_no_hardcoded_paths() -> None:
    # /Users/某人 或 /home/某人 —— 写死了就别人跑不了
    HOME_RE = re.compile(r"/(?:Users|home)/[A-Za-z][A-Za-z0-9_.-]*")
    offenders: list[str] = []
    for path in project_files():
        # 跳过本文件：它自己就写着上面那条正则，会被自己抓出来。
        if path.resolve() == Path(__file__).resolve():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if HOME_RE.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{i}")
    check(
        "源码里没有写死 /Users/xxx 或 /home/xxx",
        not offenders,
        "; ".join(offenders),
    )

    # 解释器路径不能写死在启动脚本里
    launcher = PROJECT_ROOT / "mini"
    text = launcher.read_text(encoding="utf-8")
    hard_interpreter = re.search(r"exec\s+/(?:usr|opt|home)/\S+python", text)
    check(
        "启动脚本不硬编码解释器绝对路径",
        hard_interpreter is None,
        hard_interpreter.group(0) if hard_interpreter else "",
    )
    check("启动脚本自己会 cd 到项目根目录（否则 import 找不到自己的模块）", "cd " in text)


# ---------------------------------------------------------------- 3 密钥发现链
def test_env_discovery() -> None:
    from hermes_cli import runtime_provider as rp

    cands = [str(p) for p in rp.env_file_candidates()]
    check(
        "密钥文件会去 ~/.mini-hermes/.env 找（别人没有 Hermes 也能用）",
        any(".mini-hermes" in c for c in cands),
        str(cands),
    )
    check(
        "错误信息会告诉别人怎么做（不能只说'key 为空'）",
        "缺少密钥" in _resolve_error_text(),
        _resolve_error_text()[:80],
    )


def _resolve_error_text() -> str:
    import os

    from hermes_cli import runtime_provider as rp

    saved = os.environ.pop("DEEPSEEK_API_KEY", None)
    saved_cands = rp.env_file_candidates
    rp.env_file_candidates = lambda: []          # 假装一个密钥文件都没有
    try:
        rp.resolve_provider("deepseek")
        return "(居然没报错)"
    except RuntimeError as exc:
        return str(exc)
    finally:
        rp.env_file_candidates = saved_cands
        if saved is not None:
            os.environ["DEEPSEEK_API_KEY"] = saved


if __name__ == "__main__":
    print(f"解释器：{sys.executable}  Python {sys.version.split()[0]}")
    print(f"项目根：{PROJECT_ROOT}\n")
    test_no_third_party_deps()
    test_no_hardcoded_paths()
    test_env_discovery()
    print(f"\n{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：" + "; ".join(FAIL))
    else:
        print("→ 发布就绪：源码 + 启动脚本 + README 就够了，不用打包环境。")
    raise SystemExit(1 if FAIL else 0)
