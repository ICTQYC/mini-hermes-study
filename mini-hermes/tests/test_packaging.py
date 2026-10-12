"""发布前自检 —— 把"别人 clone 下来能跑"这个说法变成可执行的断言。

跑法：python3 tests/test_packaging.py

检查六件事：
  1. 真的零第三方依赖（不是嘴上说说 —— 逐文件 AST 扫 import，再查模块来源）
  2. 没有写死某台机器的绝对路径（/Users/某人、/home/某人）
     + 启动脚本不硬编码解释器路径（别人机器上 /usr/local/bin/python3 不存在）
  3. 密钥发现链完整（别人没有 Hermes 也能用），且**报错信息要告诉别人怎么做**
  4. 启动脚本被**符号链接**调用时也能跑（`ln -s .../mini /usr/local/bin/mini`，
     这是"想让它像普通命令一样用"的最自然做法）
  5. 用户可见的字符串里没有写死 `~/.mini-hermes`（`MINI_HOME` 是可重定向的）
  6. ★ **命令行旗标契约**：README / 注释里宣传的 `mini --旗标` 必须真的存在，
     且每个真实参数都在 README 里出现过

第 1 条尤其重要：一旦某个未来章节偷偷引入了第三方库，
这个测试会立刻红 —— 而 README 和 requirements.txt 还在说"零依赖"，
那种不一致比一开始就有依赖糟糕得多。

第 6 条是**补一个真踩过的洞**：`toolsets.py` 的注释写着"`/tools` 命令和
`mini --list-toolsets` 会显示它（description）"、`doctor.py` 写着"或 `./mini --doctor`"，
而这两个参数**从来没实现**，`/tools` 也只打名字不打说明 ——
注释替不存在的功能做了承诺，**没有任何测试会因此变红**，于是漂了很久
（用户敲 `mini --list-toolsets` 撞到 `unrecognized arguments` 才发现）。
同一个病第 4 章出现过一次：系统提示词宣传 `search_files`，而注册表里没有它。
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 项目自己的顶层模块（和 Hermes 一一对应的那套文件名）
PROJECT_MODULES = {
    "hermes_constants",
    "model_tools",
    "run_agent",
    "cli",
    "hermes_state",            # ← 第 6 章拆成门面 + 兄弟模块（对应 Hermes 的 hermes_state_*.py）
    "hermes_state_common",
    "hermes_state_schema",
    "hermes_state_sessions",
    "hermes_state_messages",
    "hermes_state_search",
    "hermes_state_errors",     # ← 第 11 章新增（错误分类谓词 + StateDbCorruptError）
    "hermes_state_fts",        # ← 第 11 章新增（fail-open 摘触发器 / 显式 rebuild_fts）
    "hermes_state_repair",     # ← 第 11 章新增（session repair 引擎：raw-copy 备份 + 重建 + 三段验证）
    "tools",
    "agent",
    "hermes_cli",
    "gateway",                 # ← 第 7 章新增（对应 Hermes 的 gateway/：门面 + run_*.py 兄弟）
    "toolsets",          # ← 第 4 章新增（对应 Hermes 的 toolsets.py）
    "providers",               # ← 第 8 章新增（对应 Hermes 的 providers/：画像注册表 + 门面派生视图）
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


# ---------------------------------------------------------------- 4 启动器能被符号链接调用
def test_launcher_via_symlink() -> None:
    """`ln -s .../mini ~/bin/mini` 是最自然的省事做法，必须真的能用。

    踩过的坑：原来启动器写的是 `dirname "$0"`。当 mini 被**符号链接**调用时，
    `$0` 是**链接名**，dirname 拿到的是链接所在的目录（例如 /usr/local/bin），
    于是 `cd` 过去 → `python3 -m hermes_cli.main` 找不到模块 →
    报 "No module named 'hermes_cli'"，**看起来像装了坏包**，而问题在启动器。
    （macOS 自带的 readlink 不支持 -f，所以启动器里是自己循环解析的。）
    """
    launcher = PROJECT_ROOT / "mini"

    def run(path: Path) -> subprocess.CompletedProcess:
        # -h 只让 argparse 打一份 usage 就退出，不联网、不花钱
        try:
            return subprocess.run([str(path), "-h"], capture_output=True, text=True, timeout=120)
        except OSError as exc:
            # 链接没解析成功会在这里以 OSError 冒出来；包装成失败结果，
            # 让它是"✗ 一条"而不是"整个文件崩掉、后面的测试都不跑"。
            return subprocess.CompletedProcess([str(path)], 127, "", f"{type(exc).__name__}: {exc}")

    with tempfile.TemporaryDirectory() as td:
        # ★ 必须 .resolve()：macOS 上 /var 是 /private/var 的符号链接，
        #   用没解析的路径算 relpath 会少数一层目录，`..` 就多退一级 → 链接指向不存在的地方。
        #   （这个坑我自己先踩了一次：测试崩在 FileNotFoundError。）
        tmp = Path(td).resolve()

        abs_link = tmp / "mini_abs"
        abs_link.symlink_to(launcher)
        r = run(abs_link)
        check("符号链接（绝对）调用能跑起来", r.returncode == 0, (r.stderr or r.stdout)[-200:])
        check("没有 ModuleNotFoundError", "ModuleNotFoundError" not in r.stderr, r.stderr[-200:])

        # 相对链接要相对"链接所在目录"解析，不是相对 cwd
        sub = tmp / "sub"
        sub.mkdir()
        rel_link = sub / "mini_rel"
        rel_link.symlink_to(os.path.relpath(launcher, sub))
        r2 = run(rel_link)
        check("符号链接（相对）也能跑起来", r2.returncode == 0, (r2.stderr or r2.stdout)[-200:])

        # 链到链接（两层）
        two = tmp / "mini_two"
        two.symlink_to(abs_link)
        r3 = run(two)
        check("两层符号链接（链到链接）也能跑起来", r3.returncode == 0,
              (r3.stderr or r3.stdout)[-200:])

        # 真正要支持的用法：链接放进一个"假 bin 目录"，然后靠 PATH 调用
        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        (fakebin / "mini").symlink_to(launcher)
        env = dict(os.environ, PATH=f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}")
        r4 = subprocess.run(["mini", "-h"], capture_output=True, text=True, timeout=120, env=env)
        check("靠 PATH 调用（symlink 进假 bin 目录）能跑起来", r4.returncode == 0,
              (r4.stderr or r4.stdout)[-200:])


# 默认值只许在这一个地方定义（`Path.home() / ".mini-hermes"`）。
# 其余任何地方要用路径都得走 get_mini_home() / display_mini_home() / display_path()。
#
# 第 7 章加进来的第二处例外 `hermes_cli/profiles.py`，理由是**语义不同**：
#   它取的不是"当前运行时 home"，而是**所有 profile 的父目录** —— 那是
#   **HOME 锚定**的常量（`Path.home()/.mini-hermes/profiles`），**故意不看 MINI_HOME**。
#   真实版 Hermes 的 `_get_profiles_root()` 是同一条锚定，且在 AGENTS.md 里
#   明确写着 *"intentional, not a bug"*：不锚在真实 home 上，
#   `hermes -p x profile list` 就永远只能看见一个 profile。
ALLOWED_HOME_LITERALS = {"hermes_constants.py", "hermes_cli/profiles.py"}


def message_source_files() -> list[Path]:
    """会**对用户说话**的源文件：排除 tests/（那里的字符串是在断言这个模式，不是显示它）
    和 hermes_constants.py（它是默认值的唯一定义处）。"""
    out: list[Path] = []
    for p in project_files():
        rel = p.relative_to(PROJECT_ROOT)
        if "tests" in rel.parts or rel.as_posix() in ALLOWED_HOME_LITERALS:
            continue
        out.append(p)
    return out


def test_no_hardcoded_home_in_messages() -> None:
    """不许在**用户可见的字符串**里写死 `~/.mini-hermes`。

    踩过的坑：退出提示写着 `会话已存：~/.mini-hermes/sessions/{id}.json`，
    但 `MINI_HOME` 是可以被重定向的（测试 / E2E / CI）——
    于是这句提示把用户**指到一个不存在的文件上**，比不打印还坏。

    **颗粒度是这条检查的全部难点**（第一版就扫到了自己的脚手架）：
      · docstring 里写 `~/.mini-hermes` **合法** —— 那是在描述默认位置 → 排除 docstring
      · `hermes_constants.py` 里的默认值 **合法** —— 那是唯一定义处 → 排除该文件
      · `tests/` 里的字符串 **合法** —— 那是断言这个模式，不是显示它 → 排除 tests/
    剩下要抓的就是：**会打印/会传输的字符串字面量**。
    """
    offenders: list[str] = []
    for path in message_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # 标出 docstring 常量的 id（模块/类/函数体的第一条语句）
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", [])
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            if ".mini-hermes" in node.value:
                offenders.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}  {node.value[:50]!r}"
                )
    check(
        f"用户可见的字符串里没有写死 ~/.mini-hermes"
        f"（扫了 {len(message_source_files())} 个非测试源文件；该用 display_mini_home/display_path）",
        not offenders,
        "; ".join(offenders),
    )


# ------------------------------------------------- 6 命令行旗标契约
# 宣传了就必须存在。**扫描范围刻意限定在 mini-hermes/ 内部**：
# 本目录是"能单独分发"的单元（README 承诺"源码 + 启动脚本 + README 就够了"），
# 所以测试不能依赖仓库根那几篇章节 md 存不存在。
FLAG_CLAIM_RE = re.compile(r"mini(?:-hermes)?\s+(--[a-z][a-z0-9-]*)")


def _declared_options() -> list[list[str]]:
    """真实存在的命令行参数，**按 argparse 的 add_argument 分组**（一组 = 一个参数 + 它的别名）。

    用 AST 取，不用正则 —— 注释和字符串里的 `--xxx` 不是声明（这条纪律和 §1 那条一样）。
    """
    path = PROJECT_ROOT / "hermes_cli" / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    options: list[list[str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        flags = [a.value for a in node.args
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)
                 and a.value.startswith("-")]
        if flags:
            options.append(flags)
    return options


def test_advertised_flags_exist() -> None:
    """README / 注释里宣传的 `mini --旗标` 必须真的存在；真实参数也必须在 README 里出现过。"""
    declared = {f for group in _declared_options() for f in group}
    claim_files = [PROJECT_ROOT / "README.md"] + message_source_files()

    offenders: list[str] = []
    for path in claim_files:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for flag in FLAG_CLAIM_RE.findall(line):
                if flag not in declared:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{i} 宣传了 {flag}")
    check(
        f"扫了 {len(claim_files)} 个会对外宣传用法的文件；宣传的 `mini --旗标` 都真实存在"
        f"（实际有 {len(declared)} 个：{' '.join(sorted(declared))}）",
        not offenders,
        "; ".join(offenders),
    )

    # 反向：每个真实参数都得在 README 里出现至少一种写法 —— 防"加了参数忘了写文档"
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    undocumented = ["/".join(group) for group in _declared_options()
                    if not any(f in readme for f in group)]
    check("每个命令行参数都在 README 里被提到过（参数与文档同步）",
          not undocumented, "README 里没提：" + "; ".join(undocumented))

    # 只读查询类参数必须**不需要密钥**就能跑 —— 实测，而不是读代码猜。
    # 隔离手段：MINI_HOME 指到临时目录 + HOME 也指到临时目录（否则会退到 ~/.hermes/.env
    # 复用本机的 Hermes 密钥，那样"没有密钥也能跑"就没被真正验证）。
    with tempfile.TemporaryDirectory() as td:
        iso = {"MINI_HOME": td, "HOME": td, "PYTHONPATH": "", "MINI_ENV_FILE": ""}
        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        env.update(iso)
        launcher = str(PROJECT_ROOT / "mini")
        r = subprocess.run([launcher, "--list-toolsets"], capture_output=True, text=True,
                           timeout=120, env=env)
        check("`mini --list-toolsets` 不需要密钥就能跑（退出码 0）",
              r.returncode == 0, (r.stderr or r.stdout)[-200:])
        check("它列出了 toolset 名字与说明（不只是名字）",
              all(t in r.stdout for t in ("file", "search", "terminal", "todo"))
              and "读写文件" in r.stdout and "搜索文件内容" in r.stdout,
              r.stdout[:200])

        r_help = subprocess.run([launcher, "-h"], capture_output=True, text=True,
                                timeout=120, env=env)
        missing = [f for f in ("--list-toolsets", "--doctor") if f not in r_help.stdout]
        check("`-h` 里能看到 --list-toolsets 与 --doctor（它们在前面的 argv 里走的是 argparse）",
              not missing, f"-h 里没有：{missing}")


if __name__ == "__main__":
    print(f"解释器：{sys.executable}  Python {sys.version.split()[0]}")
    print(f"项目根：{PROJECT_ROOT}\n")
    test_no_third_party_deps()
    test_no_hardcoded_paths()
    test_env_discovery()
    test_launcher_via_symlink()
    test_no_hardcoded_home_in_messages()
    test_advertised_flags_exist()
    print(f"\n{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：" + "; ".join(FAIL))
    else:
        print("→ 发布就绪：源码 + 启动脚本 + README 就够了，不用打包环境。")
    raise SystemExit(1 if FAIL else 0)
