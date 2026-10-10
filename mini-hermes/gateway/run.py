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
from gateway.run_adapters import AdaptersMixin


class GatewayRunner(AdaptersMixin, InboundMixin, TurnMixin, ShutdownMixin, StartupMixin, BusyMixin, WatchersMixin):
    def __init__(self, *, home: Optional[Path] = None, provider: str = "deepseek",
                 model: Optional[str] = None, profile: Optional[str] = None,
                 multiplex: Optional[bool] = None, max_turns: int = 200, debug: bool = False,
                 authz: Optional[GatewayAuthz] = None, adapters: Optional[List[Any]] = None,
                 approval_timeout: float = 60.0, echo: bool = True,
                 memory_high_mb: Optional[float] = None):
        self.home = Path(home) if home else get_mini_home()
        self.profile = profile
        # ★ 三态（真实版 `gateway.multiplex_profiles: Optional[bool] = None`）：
        #   None = **未设置**（等 start() 里跑预检裁决）；True/False = 有人已经定了。
        #   这里只存原始值；裁决结果落在 `self.multiplex_decision` 与 `self.multiplex`。
        self.multiplex = multiplex
        self.multiplex_decision = None
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
                                       profile_feed=self.profile_feed,
                                       echo=echo)]
        for a in adapters:
            a._on_message = self._handle_message
            a._on_interrupt = self._interrupt_running_agent
            a._on_overwrite = self._notify_overwrite
            a._session_key_fn = self._key_for_source
            self.adapters[a.name] = a
        self.delivery = Delivery(self.adapters)

        # ★ 第 9 章 A6：**次级 profile 的适配器**（每 profile 一份，默认为空）
        #   真实版 `run.py:288` 逐字注释：*"Non-default profiles' adapters by profile then
        #   platform (multiplex only). Empty in single-profile gateways."*
        self._profile_adapters: Dict[str, Dict[str, Any]] = {}
        # 适配器启动的观察面（跳过/失败**要说出来** —— 静默的死信道是最坏的形态）
        self._adapters_log: List[str] = []
        # 这次真的服务了谁（写进状态文件，A12）
        self._served_profiles: List[str] = []

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
        # ★ 第 9 章：provider spec **按 home 分键**（一份缓存发给所有 profile = 用错凭据）
        self._specs: Dict[str, Dict[str, Any]] = {}
        # 观测面：每轮的会话键 → 这一轮实际跑的 home（作用域对不对只能从这看）
        self._turn_homes: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # 依赖（延迟解析，避免 import 期就把 profile 的路径钉死）
    # ------------------------------------------------------------------ #
    def _provider_spec(self) -> Dict[str, Any]:
        """provider 解析结果（**按当前作用域的家**缓存）。

        ★ 第 9 章 A5 的一条推论：**缓存不能跨 profile 共享**。
        第 7/8 章这里缓存的是**一份** spec（`self._spec`）—— 单 profile 时没问题，
        多路复用下却是"把默认 profile 的密钥/base_url/模型发给所有人"（静默用错凭据）。
        真实版的 `runtime_provider` 也是每 profile 各解析一次（它的凭据解析链就带作用域）。
        """
        key = str(get_mini_home())
        if key not in self._specs:
            from hermes_cli.runtime_provider import resolve_provider

            self._specs[key] = resolve_provider(self.provider, self.model)
            self._spec = self._specs[key]        # 兼容旧字段（观测面）
        return self._specs[key]

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

        # ★ 第 9 章 A8：命名空间取"**这条消息属于谁**"（适配器在入站口盖的章），
        #   没有章才退回进程启动时的 profile 名。
        return build_session_key(source, profile=source.profile or self.profile)

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

        # ── 多路复用裁决（对应真实版 `hermes_cli/gateway_multiplex_mode.py`）──
        #   ★ 位置很讲究：必须**在写身份/状态文件之前** ——
        #     ① 状态文件里那一栏 `multiplex` 要写**裁决后**的值（不是 `None`）；
        #     ② `guard` 的拦截理由也要一起落盘（`multiplex_standalone_reason`），
        #        运维才能在 `gateway status` 里看到"它为什么没多路复用"。
        from gateway.multiplex_mode import (log_multiplex_decision,
                                            multiplex_status_fields,
                                            record_multiplex_decision,
                                            resolve_multiplex_mode)

        self.multiplex_decision = resolve_multiplex_mode(self.multiplex, home=self.home)
        self.multiplex = self.multiplex_decision.enabled
        # ★ 这几栏要**跟着每一次状态写**（`write_runtime_status` 是全量写，不带就被抹掉）
        self._multiplex_status_fields = multiplex_status_fields(self.multiplex_decision)
        log_multiplex_decision(self.multiplex_decision)
        # 密钥读取的 fail-closed 开关按**裁决结果**打开（真实版在 __init__ 里做同一件事）
        set_multiplex_active(self.multiplex)

        self.pid = os.getpid()
        self._pid = self.pid
        gw_status.write_pid_record(self.home, self.pid)
        gw_status.write_runtime_status(self.home, "running", pid=self.pid,
                                      profile=self.profile,
                                      # ★ `_multiplex_status_fields` 里**已经**含 `multiplex` 这一栏，
                                      #   这里不许再单独传一次（重复关键字参数会让网关起不来 —— 实测踩过）
                                      **getattr(self, "_multiplex_status_fields", {}))

        # ── 第 8 章：启动恢复（真实版 gateway/run_startup.py 的四步）──
        #    ★ 顺序要求：在"身份文件写好之后"（恢复要用 home）、"适配器连上之前"
        #      （否则入站消息会在恢复中途进来）。真实版还额外"恢复期间先排队入站"，
        #      mini 因为恢复本身不跑回合（只标状态），所以不需要那道排队。
        recovery = self._recover_sessions()

        # ── 第 8 章：后台巡检线程（真实版 gateway/run_watchers.py 的会话维护）──
        #    先等 60 秒再开工（真实版同）：刚起来这会儿正在做恢复、连适配器，别抢缓存锁。
        self._start_housekeeping()

        # ── 多路复用：服务集合 + 裁决落盘 ──
        #   fail-closed 开关已在裁决处打开；这里只做两件"读到谁、记下谁"的事。
        self.served_homes = self.servable_homes()
        record_multiplex_decision(self.home, self.multiplex_decision)

        # ── 钩子：**在调用时**解析当前 home 的 hooks 目录 ──
        loaded = self.hooks.discover_and_load()
        self.hooks.emit("gateway:startup", home=str(self.home), pid=self.pid,
                        multiplex=self.multiplex)

        # ★ A6：活动 profile 的适配器连接 —— **fail-soft**（真实版 run_adapters.py 的纪律：
        #   一个平台连不上只跳过它，不许把整个网关带走；跳过必须说出来）。
        for name, a in self.adapters.items():
            try:
                a.connect()
            except Exception as exc:          # noqa: BLE001
                self._adapter_note(f"平台 '{name}' 连接失败 —— 跳过（{exc!r}）")

        # ★ A6：**每 profile 适配器**（多路复用开时把次级 profile 的 bot 也拉起来）
        self._start_secondary_profile_adapters()

        # ★ `@<profile> 文本` 这条语法**只在多路复用开着时**可用（真实版的共享 bot 判别项）。
        #   提示语要在**裁决之后**才能定 —— 所以这里统一设置，而不是构造时传。
        note = (f"（多路复用开：`@<profile> 文本` = 这条消息是那个 profile 的 bot 收到的；"
                f"服务 {len(self.served_homes)} 个 home）" if self.multiplex else "")
        for a in list(self.adapters.values()) + [
                x for m in self._profile_adapters.values() for x in m.values()]:
            try:
                a.multiplex_note = note
            except Exception:                 # noqa: BLE001
                pass

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
