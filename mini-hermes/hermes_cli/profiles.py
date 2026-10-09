"""profile 的枚举（对应 Hermes 的 `hermes_cli/profiles.py`）。

★ 一个容易搞错的点，真实版特意写在 AGENTS.md 里：

    profile 操作本身是 **HOME 锚定**的
    （`_get_profiles_root()` = `Path.home()/.hermes/profiles`）

也就是说 **`hermes -p x profile list` 要能看到所有 profile** ——
如果这个根也跟着 `-p` 走，那你永远只能看到一个 profile，
而"我想切到另一个 profile"这个需求就无解了。

mini 照抄这条锚定：profile 根**不看 MINI_HOME**，只看真实 home。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

PROFILES_DIRNAME = "profiles"

# ══════════════════════════════════════════════════════════════════════════
# 第 7 章（侧线）：profile 的"入口层" —— `mini -p <名字>` / `mini profile …`
#
# ★ 为什么这层几乎不"设计"什么：**一个 profile 就是一个 home**。
#   前面几章所有"取路径"的代码都走 `get_mini_home()`，所以"多 profile"
#   在实现上就是"让 `get_mini_home()` 返回另一个目录"。真正的难点不在这一层，
#   而在"这段代码在替谁干活"（作用域绑定）—— 那部分在 `gateway/profile_scope.py`。
# ══════════════════════════════════════════════════════════════════════════

# 新建 profile 时**实际建出来**的目录。
# 真实版是 9 个（`hermes_cli/profiles.py:32`，逐字）：
#     ["memories", "sessions", "skills", "skins", "logs", "plans", "workspace", "cron", "home"]
# mini 只建**它真的会读写的**两个 —— 建一个没有消费者的目录，
# 后果是"用户以为那个功能存在"（真实版那条判据：没有具体消费者的东西不要先建）。
#   logs/   ← hermes_constants.logs_dir()
#   hooks/  ← gateway/hooks.py 的 HookRegistry（第 7 章）
PROFILE_DIRS = ["logs", "hooks"]

# `--clone` 时**复制**的文件（真实版 `:35`，逐字）：
#     _CLONE_CONFIG_FILES = ["config.yaml", ".env", "SOUL.md"]
# mini 没有 config.yaml，配置等价物是 config.json（prompt_builder 读取）。
#
# ★ 注意这份清单里**没有 MEMORY.md**，这是设计不是遗漏：
#   克隆只带"配置与身份"，**不带"记忆"**。记忆是"这个 profile 经历过什么"，
#   把它复制过去，两个 profile 就会共享同一段私人历史 —— 那不是"起步时复制一次"，
#   那是"耦合"（真实版对 profile 耦合的态度见 AGENTS.md：independent islands on purpose）。
CLONE_FILES = [".env", "SOUL.md", "config.json"]

# 播种给新 profile 的 `.env` 头部。
# 真实版 `:74-78` 的注释逐字：*"Header seeded into a profile's empty .env so it owns a
# credentials file from day one."* —— 从第一天起它就有自己的凭据文件（哪怕先是空的）。
# 里面那句"行为设置不要写这里"也是真实版的纪律（`.env` 只放密钥）。
ENV_HEADER = (
    "# 这是本 profile 自己的凭据文件（一个 profile = home + 密钥作用域）。\n"
    "# 密钥写在这里；**行为设置不要写这里**（.env 只放密钥）。\n"
    "# 本文件由 `mini profile create` 播种 —— 创建时是空的，但它从第一天起就归你。\n"
)

# profile id 的字母表。
# ★ 这里的正则是第 7 章 §一 那条教训的另一半：**`~` 不在字母表里**
#   （`session_key_namespace()` 用 `main~` 标记"真名就叫 main 的 profile"，
#   靠的就是"`~` 不可能出现在合法 profile id 里"）。
#   所以创建时必须**真的**拒绝带 `~` 的名字 —— 否则那个"不可能"就成了假的：
#   用户可以建一个叫 `main~` 的 profile，然后在会话键上和"真名 main 的标记形态"撞车。
PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# 保留名：`default` 在命名空间里等同于"默认 profile"（`session_key_namespace()` 里
# `profile in ("", "default")` → `main`），所以它不能是一个真 profile 的名字。
RESERVED_NAMES = {"default"}


class ProfileError(RuntimeError):
    """profile 层的错误（创建/解析）。**必须说得出补救方法**。"""


def validate_profile_name(name: str) -> None:
    if not name:
        raise ProfileError("profile 名不能为空")
    if name in RESERVED_NAMES:
        raise ProfileError(
            f"{name!r} 是保留名：它在会话键里等同于【默认 profile】（namespace = main），"
            f"所以不能当一个真 profile 的名字。换一个（例如 {name}-1）。"
        )
    if not PROFILE_ID_RE.match(name):
        raise ProfileError(
            f"profile 名 {name!r} 不合法：只允许字母/数字/._-，且必须以字母或数字开头。\n"
            f"  为什么这么严：会话键的命名空间直接用它 —— "
            f"`~` 不在字母表里（默认 profile 的标记形态靠它），空格/中文会让键难以阅读与匹配。"
        )


def create_profile(name: str, *, clone: bool = False,
                   base_home: Optional[Path] = None) -> Path:
    """建一个 profile 的 home 目录（存在则报错，不覆盖）。返回它的 home。"""
    validate_profile_name(name)
    home = profile_home(name)
    if home.exists():
        raise ProfileError(
            f"profile {name!r} 已经存在：{home}\n"
            f"  要看它就 `mini -p {name} --doctor`，要列全部就 `mini profile list`。"
        )

    src = Path(base_home) if base_home is not None else default_home()
    home.mkdir(parents=True)
    for d in PROFILE_DIRS:
        (home / d).mkdir()

    env_file = home / ".env"
    if clone:
        # ★ 逐文件判断存在才拷：`--clone` 的语义是"能拷的都拷过来"，
        #   而不是"我必须有一份完整配置"（真实版同理 —— 用户可能从没写过 SOUL.md）。
        cloned: List[str] = []
        for fname in CLONE_FILES:
            source = src / fname
            if fname == ".env":
                # `.env` 特殊：目标本来就要有一份（哪怕源不存在），
                #   因为"从第一天起拥有自己的凭据文件"是这一层的承诺。
                if source.exists():
                    shutil.copy2(source, env_file)
                    cloned.append(fname)
                else:
                    env_file.write_text(ENV_HEADER, encoding="utf-8")
                continue
            if source.exists():
                shutil.copy2(source, home / fname)
                cloned.append(fname)
        if not cloned:
            # 源 profile 也没什么可拷的 → 至少给它一份凭据文件的头
            if not env_file.exists():
                env_file.write_text(ENV_HEADER, encoding="utf-8")
    else:
        env_file.write_text(ENV_HEADER, encoding="utf-8")
    return home


def resolve_profile_home(name: Optional[str]) -> Path:
    """把一个 profile 名解析成它的 home（`mini -p <名字>` 的入口）。

    ★ 三种情形都**必须是明确的**：

    | 输入 | 结果 |
    |---|---|
    | 没给 / `default` | 默认 home（老行为，一个字节都不变） |
    | 给了且存在 | 那个 profile 的 home |
    | 给了但**不存在** | **报错**（不是静默回落默认 home！） |

    第 3 行是这个函数存在的全部意义。静默回落的后果是：
    用户以为自己在用 `lab`（沙箱、可以乱来），实际在用**主 profile** ——
    而且**不报错**。这就是第 6/7 章反复看到的那类"静默失败"。
    """
    if name is None or name in ("", "default"):
        return default_home()
    home = profile_home(name)
    if not home.exists():
        existing = list_profiles()
        hint = ("、".join(existing) if existing else "（还没有任何 profile）")
        raise ProfileError(
            f"没有名为 {name!r} 的 profile（找过 {home}）。\n"
            f"  现有的：{hint}\n"
            f"  要新建：`mini profile create {name}`（加 --clone 可从默认 profile 起步）"
        )
    return home


def profile_info(home: Path) -> Dict[str, Any]:
    """给 `mini profile show` 用的一览。**只读，不建任何东西。**"""
    home = Path(home)
    db = home / "state.db"
    info: Dict[str, Any] = {
        "home": str(home),
        "exists": home.exists(),
        "env": (home / ".env").exists(),
        "state_db": db.exists(),
        "state_db_bytes": db.stat().st_size if db.exists() else 0,
        "dirs": sorted(p.name for p in home.iterdir() if p.is_dir()) if home.exists() else [],
        "files": sorted(p.name for p in home.iterdir() if p.is_file()) if home.exists() else [],
    }
    if db.exists():
        try:
            import hermes_state

            with hermes_state.SessionDB(db_path=db) as sdb:
                info["sessions"] = len(sdb.list_sessions(limit=1000))
        except Exception as exc:                      # 只读诊断不该因为库坏了就崩
            info["sessions"] = f"（读不出来：{exc!r}）"
    return info


def profiles_root() -> Path:
    """所有 profile 的父目录（**HOME 锚定，不受 MINI_HOME 影响**）。"""
    return Path.home() / ".mini-hermes" / PROFILES_DIRNAME


def list_profiles() -> List[str]:
    root = profiles_root()
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def profile_home(name: str) -> Path:
    return profiles_root() / name


def default_home() -> Path:
    return Path.home() / ".mini-hermes"


def profiles_to_serve(multiplex: bool = False) -> List[Path]:
    """要服务的 home 列表（对应真实版 `profiles_to_serve(multiplex=True)`）。

    - `multiplex=False`：只有默认 profile（**一个进程服务一个 profile**）；
    - `multiplex=True`：默认 profile **加上** `profiles/` 下每一个活着的目录。

    ★ 顺序有意义：**默认 profile 永远排第一**。真实版的规矩是
      "共享入口的平台只在默认 profile 上跑"，以及
      "在默认 profile 上 `gateway stop` 会把所有被服务的 profile 一起停掉"——
      两条都依赖这个顺序。
    """
    homes = [default_home()]
    if multiplex:
        homes.extend(profile_home(n) for n in list_profiles())
    return homes
