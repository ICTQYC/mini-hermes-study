"""网关进程的身份与存活判据（对应 Hermes 的 `gateway/status.py`）。

★ 这个文件里最重要的不是"怎么写 PID 文件"，而是**一条被 bug 教出来的规矩**：

> **"PID 存在" ≠ "那是我的网关"。**

真实版 `gateway/status.py:1112-1135` 的 docstring 逐字（第 7 章附录 C 引用过）：

    Bare PID existence is not identity -- a stale record whose PID was recycled by
    an unrelated process lent it ``served_profile`` and put phantom gateways into the
    update inventory (#109680) -- while a launch-service gateway whose ``gateway.pid``
    was unlinked is still live (#110166).

两句话各说了一个方向的错：

- **号被回收**：进程早死了，号被别的进程拿走 → 只看"号存在"会以为网关还活着
  （于是把**别人的进程**当成自己的，甚至去"更新"它）；
- **文件被删**：launchd 启动的网关把 pid 文件删了 → 只看文件会以为它死了
  （其实还活着）。所以真实版还有第二判据：`gateway_state.json` 运行时状态。

于是判据是**两件事的组合**：`PID + 启动时间指纹`（防回收），
以及**状态记录**（防文件丢失）—— 两处都指向同一个 pid 才算数。

mini 抄这套判据（用 `ps -o lstart= -p <pid>` 取启动时间做指纹）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# ★ 启动时遇到"活的外部持有者" = 配置冲突，不是瞬时故障。
#   78 == sysexits.h 的 EX_CONFIG（真实版 `gateway/restart.py:14`）。
#   **这个数字是给监督者看的接口**：systemd `RestartPreventExitStatus=78` /
#   s6 finish→125 / launchd 78→0 + `KeepAlive.SuccessfulExit=false`。
#   监督者据此决定"别再把我拉起来"（否则会无限重启循环）。
GATEWAY_FATAL_CONFIG_EXIT_CODE = 78

PID_FILENAME = "gateway.pid"
STATE_FILENAME = "gateway_state.json"


def pid_file(home: Path) -> Path:
    return Path(home) / PID_FILENAME


def state_file(home: Path) -> Path:
    return Path(home) / STATE_FILENAME


# ---------------------------------------------------------------------- #
# 进程指纹
# ---------------------------------------------------------------------- #
def process_start_fingerprint(pid: int) -> Optional[str]:
    """进程的启动时间（当指纹用）。进程不存在 → None。

    为什么用启动时间：PID 会被回收，但**同一个 PID 的启动时间不会重复**。
    这样"记下的 pid"与"现在的 pid"就直接可比了。
    macOS / Linux 的 `ps -o lstart=` 都支持（输出形如
    `Mon Oct  6 11:02:33 2026`）。
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    text = (out.stdout or "").strip()
    return text or None


def pid_alive(pid: int) -> bool:
    return process_start_fingerprint(pid) is not None


def _as_int(value: Any) -> Optional[int]:
    """把记录里的值安全地读成 int（记录是**外部输入的 JSON**，不能假定形状）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------- #
# 记录读写
# ---------------------------------------------------------------------- #
def write_pid_record(home: Path, pid: int, *, cmdline: Optional[str] = None) -> Path:
    path = pid_file(home)
    payload = {
        "pid": int(pid),
        "started_at": time.time(),
        "home": str(home),
        "start_fingerprint": process_start_fingerprint(pid),
        "cmdline": cmdline if cmdline is not None else " ".join(_current_cmdline()),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_pid_record(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _current_cmdline() -> list:
    return ["python", *sys.argv]


def looks_like_gateway_command_line(cmdline: str) -> bool:
    """这条命令行**是不是**在跑网关（真实版同名的规范匹配函数）。

    ★ 真实版的规矩（AGENTS.md 里写得很重）：
    **绝不用 argv 的子串推断进程身份**（`"serve" in cmdline`）—— 那是十几起
    "更新时杀错进程"事故的根因。要匹配就匹配**完整 token**。
    """
    try:
        tokens = cmdline.split()
    except Exception:
        return False
    if "gateway" not in tokens:
        return False
    return ("run" in tokens) or ("start" in tokens)


def get_running_pid(pid_path: Path, *, cleanup_stale: bool = False,
                    expected_home: Optional[Any] = None) -> Optional[int]:
    """从 pid 文件取出**仍然有效**的 pid，否则 None。

    有效 = 记录能读 + PID 还活着 + **启动时间指纹一致**（防回收）
         + 记录里的 home 与预期一致（防拿错 profile 的身份）。
    """
    record = read_pid_record(pid_path)
    if not record:
        if cleanup_stale:
            try:
                Path(pid_path).unlink(missing_ok=True)
            except Exception:
                pass
        return None
    pid = _as_int(record.get("pid"))
    if pid is None:
        return None
    if expected_home is not None and str(record.get("home")) != str(expected_home):
        return None
    live_fp = process_start_fingerprint(pid)
    if live_fp is None:
        if cleanup_stale:
            try:
                Path(pid_path).unlink(missing_ok=True)
            except Exception:
                pass
        return None
    recorded_fp = record.get("start_fingerprint")
    if recorded_fp and live_fp != recorded_fp:
        return None                                      # ★ 号被回收了：不是我们的进程
    return pid


# ---------------------------------------------------------------------- #
# 运行时状态（第二判据）
# ---------------------------------------------------------------------- #
def write_runtime_status(home: Path, state: str, **fields: Any) -> Path:
    payload: Dict[str, Any] = {"state": state, "updated_at": time.time(),
                               "home": str(home), **fields}
    path = state_file(home)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_runtime_status(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def runtime_status_running_pid(status: Optional[Dict[str, Any]],
                               *, expected_home: Optional[Any] = None) -> Optional[int]:
    """状态记录里记的"我还在跑"的 pid（**必须过指纹这一关**）。"""
    if not status or status.get("state") in ("stopped", "startup_failed"):
        return None
    if expected_home is not None and str(status.get("home")) != str(expected_home):
        return None
    pid = _as_int(status.get("pid"))
    if pid is None:
        return None
    live_fp = process_start_fingerprint(pid)
    if live_fp is None:
        return None
    recorded = status.get("start_fingerprint")
    if recorded and live_fp != recorded:
        return None
    return pid


def live_gateway_pid_for_home(home: Path) -> Optional[int]:
    """**唯一入口**：这个 home 的网关现在活着吗？活着就返回它的 pid。

    ★ 真实版的纪律：所有"读别的 home 的网关身份"的代码都走这一个函数 ——
    这样它们证明的是**同一件事**，而不是各自实现一套（各不相同地错）。
    """
    home = Path(home)
    pid = get_running_pid(pid_file(home), cleanup_stale=False, expected_home=home)
    if pid is not None:
        return pid
    return runtime_status_running_pid(read_runtime_status(state_file(home)),
                                      expected_home=home)


def is_global_startup_conflict(error_code: str) -> bool:
    """这个错误码属于"启动期配置冲突"吗（真实版 `gateway/restart.py:61`）。

    真实版认的是适配器 token 锁那两族（`*_lock` / `lock_conflict`）：
    **启动时**遇到活的外部持有者是配置冲突（该停放、该退出 78），
    **运行中途**遇到只是可重试的瞬时故障（持有者退出后会恢复）。
    ★ 同一个错误，时机不同，处置相反 —— 这是这一节最容易被忽略的地方。
    """
    code = (error_code or "").lower()
    return code.endswith("_lock") or "lock_conflict" in code
