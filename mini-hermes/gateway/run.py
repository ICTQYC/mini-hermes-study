"""网关门面（对应 Hermes 的 `gateway/run.py`）。

装配方式照真实版：`GatewayRunner` = **三个 mixin 拼起来**，每个 mixin 管一个阶段：

    InboundMixin   入站：授权 → 命令表 → 忙时处置
    TurnMixin      一轮：绑作用域 → 取历史 → 建 agent → 出站
    ShutdownMixin  收尾：结束会话 → 摘身份文件 → 状态置 stopped

★ 这个布局的代价要一并记住：**静态分析看不见跨 mixin 的属性**
（Pyright 会报 `Cannot access attribute "authz" for class "InboundMixin"`）。
真实版（`gateway/run.py` 继承 15 个 mixin、`hermes_state.py` 继承 21 个）
也有同一个代价 —— 换来的是"一个文件一个主题"，而不是三千行的一个类。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.secret_scope import set_multiplex_active
from gateway import status as gw_status
from gateway.adapters.console import ConsoleAdapter
from gateway.agent_cache import AgentCache
from gateway.authz import GatewayAuthz
from gateway.delivery import Delivery
from gateway.hooks import HookRegistry
from gateway.run_busy import BusyMixin
from gateway.run_inbound import InboundMixin
from gateway.run_shutdown import ShutdownMixin
from gateway.run_startup import StartupMixin
from gateway.run_turn import TurnMixin
from gateway.run_watchers import SESSION_STORE_MAX_AGE_DAYS_DEFAULT, WatchersMixin
from hermes_constants import get_mini_home
from hermes_cli.profiles import profiles_to_serve, profiles_root


class GatewayRunner(InboundMixin, TurnMixin, ShutdownMixin, StartupMixin, BusyMixin, WatchersMixin):
    def __init__(self, *, home: Optional[Path] = None, provider: str = "deepseek",
                 model: Optional[str] = None, profile: Optional[str] = None,
                 multiplex: bool = False, max_turns: int = 200, debug: bool = False,
                 authz: Optional[GatewayAuthz] = None, adapters: Optional[List[Any]] = None,
                 approval_timeout: float = 60.0, echo: bool = True,
                 memory_high_mb: Optional[float] = None):
        self.home = Path(home) if home else get_mini_home()
        self.profile = profile
        self.multiplex = multiplex
        self.provider = provider
        self.model = model
        self.max_turns = max_turns
        self.debug = debug
        self.approval_timeout = approval_timeout
        self.echo = echo

        self.hooks = HookRegistry(hooks_dir=self.home / "hooks")
        self.authz = authz or GatewayAuthz.from_env("console")

        self.adapters: Dict[str, Any] = {}
        if adapters is None:
            adapters = [ConsoleAdapter(on_message=self._handle_message,
                                       on_interrupt=self._interrupt_running_agent,
                                       session_key_fn=self._key_for_source,
                                       echo=echo)]
        for a in adapters:
            a._on_message = self._handle_message
            a._on_interrupt = self._interrupt_running_agent
            a._on_overwrite = self._notify_overwrite
            a._session_key_fn = self._key_for_source
            self.adapters[a.name] = a
        self.delivery = Delivery(self.adapters)

        self._session_ids: Dict[str, str] = {}
        self._dbs: Dict[str, Any] = {}
        self._routers: Dict[str, Any] = {}          # 第 8 章：每个库一个路由状态机
        # ── 第 8 章：代理缓存 + 后台巡检 ──
        self._agent_cache = AgentCache(memory_high_mb=memory_high_mb)
        self._housekeeping_thread: Optional[threading.Thread] = None
        self._housekeeping_stop: Optional[threading.Event] = None
        self._housekeeping_log: List[Dict[str, Any]] = []
        self._last_prune_at: Optional[float] = None   # None = 从没剪过（别用 0.0：那是开机时刻）
        self.session_store_max_age_days = SESSION_STORE_MAX_AGE_DAYS_DEFAULT
        self._running_agents: Dict[str, Any] = {}
        self._events_by_key: Dict[str, Any] = {}
        self._spec: Optional[Dict[str, Any]] = None

        # 观察面（测试 / 文档实证用）
        self._turn_errors: List[str] = []
        self._interrupted: List[str] = []
        self._busy_rejections: List[tuple] = []
        self._tool_events: List[str] = []
        self._ticks: int = 0
        self._pending_approval: Optional[Dict[str, Any]] = None

        self.pid: Optional[int] = None
        self._pid = None
        self.served_homes: List[Path] = [self.home]

    # ------------------------------------------------------------------ #
    # 依赖（延迟解析，避免 import 期就把 profile 的路径钉死）
    # ------------------------------------------------------------------ #
    def _provider_spec(self) -> Dict[str, Any]:
        if self._spec is None:
            from hermes_cli.runtime_provider import resolve_provider

            self._spec = resolve_provider(self.provider, self.model)
        return self._spec

    def _db(self):
        """当前作用域下的会话库。

        ★ 为什么用 `get_mini_home()` 而不是 `self.home`：作用域绑定之后，
        `get_mini_home()` 返回的是**被绑定的那个 profile 的 home** ——
        于是"替次级 profile 干活"的代码不用自己传 home，也就**没法传错**。
        """
        return self._db_for(get_mini_home())

    def _db_for(self, home: Path):
        import hermes_state

        key = str(home)
        if key not in self._dbs:
            self._dbs[key] = hermes_state.SessionDB(db_path=Path(home) / "state.db")
        return self._dbs[key]

    # ------------------------------------------------------------------ #
    # 观察面小工具
    # ------------------------------------------------------------------ #
    def _on_tool_progress(self, name: str, args: Dict[str, Any], phase: str) -> None:
        self._tool_events.append(f"{phase}:{name}")

    def _key_for_source(self, source) -> str:
        """会话键的唯一产出点（注入给适配器用）。

        ★ 为什么是"注入函数"而不是"注入 profile 名"：适配器在**第 1 级守卫**
        就要按会话分锁，所以它必须能算键；但"命名空间是哪个 profile"是网关状态。
        注入函数让两边都只用同一个 `build_session_key`，而**没有任何一方自己拼键**。
        """
        from gateway.session_keys import build_session_key

        return build_session_key(source, profile=self.profile)

    def _interrupt_running_agent(self, session_key: str) -> None:
        agent = self._running_agents.get(session_key)
        if agent is not None:
            agent.interrupt()
            self._interrupted.append(session_key)

    def _reply_to_key(self, session_key: Optional[str], text: str) -> None:
        """按会话键回复（给钩子/审批这类"不在当前事件里"的代码用）。"""
        if not session_key:
            return
        event = self._events_by_key.get(session_key)
        if event is not None:
            self._reply(event, text, target=self._explicit_target(event))

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> int:
        """启动网关。返回退出码（0 = 正常；78 = 配置冲突，**别重启我**）。"""
        # ── 单实例检查：活的外部持有者 = 配置冲突（不是瞬时故障）──
        existing = gw_status.live_gateway_pid_for_home(self.home)
        if existing is not None and existing != os.getpid():
            gw_status.write_runtime_status(self.home, "startup_failed",
                                          pid=os.getpid(), conflict_with=existing,
                                          reason="gateway_already_running")
            self._banner(f"⛔ 这个 home 已经有网关在跑（pid={existing}）："
                         f"{self.home}\n   退出码 {gw_status.GATEWAY_FATAL_CONFIG_EXIT_CODE}"
                         f"（EX_CONFIG）—— 监督者据此**不要重启**。")
            return gw_status.GATEWAY_FATAL_CONFIG_EXIT_CODE

        self.pid = os.getpid()
        self._pid = self.pid
        gw_status.write_pid_record(self.home, self.pid)
        gw_status.write_runtime_status(self.home, "running", pid=self.pid,
                                      profile=self.profile, multiplex=self.multiplex)

        # ── 第 8 章：启动恢复（真实版 gateway/run_startup.py 的四步）──
        #    ★ 顺序要求：在"身份文件写好之后"（恢复要用 home）、"适配器连上之前"
        #      （否则入站消息会在恢复中途进来）。真实版还额外"恢复期间先排队入站"，
        #      mini 因为恢复本身不跑回合（只标状态），所以不需要那道排队。
        recovery = self._recover_sessions()

        # ── 第 8 章：后台巡检线程（真实版 gateway/run_watchers.py 的会话维护）──
        #    先等 60 秒再开工（真实版同）：刚起来这会儿正在做恢复、连适配器，别抢缓存锁。
        self._start_housekeeping()

        # ── 多路复用：一个进程服务多个 profile，密钥读取转 fail-closed ──
        set_multiplex_active(self.multiplex)
        self.served_homes = profiles_to_serve(self.multiplex)

        # ── 钩子：**在调用时**解析当前 home 的 hooks 目录 ──
        loaded = self.hooks.discover_and_load()
        self.hooks.emit("gateway:startup", home=str(self.home), pid=self.pid,
                        multiplex=self.multiplex)

        for a in self.adapters.values():
            a.connect()

        self._banner(
            f"网关已启动  pid={self.pid}  home={self.home}\n"
            f"  命令表   {', '.join(sorted(self._command_handler_table()))}\n"
            f"  授权     " + ("全放行" if self.authz.global_allow_all or self.authz.platform_allow_all
                            else f"白名单 {sorted(self.authz.platform_allowlist) or '空 → 默认拒绝'}") + "\n"
            f"  钩子     {loaded or '（无）'}\n"
            f"  多路复用 {'开' if self.multiplex else '关'}"
            + (f"（服务 {len(self.served_homes)} 个 home）" if self.multiplex else "")
            + ("\n  " + "\n  ".join(self._recovery_banner(recovery)) if recovery else "")
        )
        return 0

    def run(self) -> int:
        """跑主循环（mini 没有 service 集成，所以是前台阻塞）。"""
        code = self.start()
        if code != 0:
            return code
        try:
            self.maintenance_tick()                    # 启动即一次 tick
            for adapter in self.adapters.values():
                if hasattr(adapter, "serve_forever"):
                    adapter.serve_forever()
        except KeyboardInterrupt:
            self._banner("收到 Ctrl-C，准备退出。")
        finally:
            self.shutdown()
        return 0

    def maintenance_tick(self) -> None:
        """周期性维护（真实版有四件事；mini 只留**形状**）。

        | 真实版 | mini |
        |---|---|
        | cron 滴答 | ❌ 没有 scheduler |
        | 会话清理（不结束转写本） | ❌ |
        | 记忆落盘 | ❌ 没有记忆 provider |
        | 缓存刷新（模型列表/provider 状态） | ❌ |

        留这个函数 + 一个 `gateway:tick` 钩子事件，是因为"**两轮之间有活着的东西**"
        正是网关的存在理由之一；形状留着，将来要做的东西才有地方落。
        """
        self._ticks += 1
        self.hooks.emit("gateway:tick", n=self._ticks, home=str(self.home))

    def status_text(self) -> str:
        pid = gw_status.live_gateway_pid_for_home(self.home)
        state = gw_status.read_runtime_status(gw_status.state_file(self.home)) or {}
        rows = [
            f"home       {self.home}",
            f"活着的网关 {pid if pid else '（没有）'}",
            f"状态记录   {state.get('state', '(无)')}  pid={state.get('pid', '-')}",
            f"会话缓存   {len(self._session_ids)} 个会话键",
            f"适配器     {sorted(self.adapters)}",
        ]
        if self.multiplex:
            rows.append(f"服务 profile {[str(p) for p in profiles_to_serve(True)][1:] or '（无）'}"
                        f"（根 {profiles_root()}）")
        return "\n".join(rows)

    # ------------------------------------------------------------------ #
    def feed(self, text: str, **kw) -> str:
        """测试/演示用：把一条消息喂给默认适配器（**走同一条 handle_message**）。"""
        adapter = next(iter(self.adapters.values()))
        return adapter.feed(text, **kw)

    def _banner(self, text: str) -> None:
        if self.echo:
            print(text, flush=True)
