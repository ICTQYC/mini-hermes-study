"""每 profile 适配器与 fail-soft 启动（对应真实版 `gateway/run_adapters.py`）。

真实版两条循环（逐字结构见《源码讲解-09》§十二）：

* `_start_secondary_profile_adapters()`（`:832-873`）：逐 profile，跳过活动那个；
  **配置错上抛**（让启动中止），**其它异常记日志 + 跳过继续**；
* `_start_one_profile_adapters()`（`:1004-1100`）：逐平台，每一种"跳过"各有专属日志
  （无凭据 / 共享入口 / mirror 平台 / 建不出适配器 / 连接失败）。

mini 的形态：平台只有 console，所以"每个 profile 一个适配器"= 每个 profile 一个
`ConsoleAdapter` 实例（`owner_profile` 标好）。次级 profile 的适配器**不由 stdin 喂**，
而是走 `@<profile> <文本>` —— 那等价于真实版的 **`bot_profile` 判别项**
（"这条消息是那个 bot 收到的"），见 `ConsoleAdapter.serve_forever`。

★ 为什么"跳过要说出来"是硬要求（真实版注释逐字）：*"four profiles with
`WHATSAPP_ENABLED=true` and nothing in the log is a silent dead channel."*
—— 一个**静默死掉的通道**比一个报错的通道危险得多：没人会发现它。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from gateway.adapters.console import ConsoleAdapter
from gateway.profile_scope import profile_runtime_scope


class AdaptersMixin:
    """把"服务哪些 profile"变成"真的建出它们的适配器"（mini 侧 = 一 profile 一个 console 适配器）。"""

    # ------------------------------------------------------------------ #
    # 名字 / home 的互相解析（★ 只此一处，别在别处再拼一遍）
    # ------------------------------------------------------------------ #
    def profile_name_for_home(self, home: Path) -> str:
        """home → profile 名（默认那本家叫 `"default"`）。"""
        from hermes_cli.profiles import default_home

        home = Path(home)
        if str(home.resolve()) == str(Path(default_home()).resolve()):
            return "default"
        return home.name

    def home_for_profile_name(self, name: Optional[str]) -> Optional[Path]:
        """profile 名 → home（只在**被服务的集合**里找；找不到返回 None，不猜）。"""
        target = (name or "default").strip() or "default"
        for home in (getattr(self, "served_homes", None) or []):
            if self.profile_name_for_home(Path(home)) == target:
                return Path(home)
        return None

    def _profile_home_for_source(self, source) -> Optional[Path]:
        """这条消息该在**哪个 home 的作用域**里处理（A5：作用域按"活动"，不按启动 profile）。

        顺序：`source.profile`（路由表命中或 bot owner 盖的章）→ 没盖章 = 活动 home。
        找不到对应 home 时**回落活动 home**（并让上层照旧处理）—— 与真实版
        "未命中路由 → 默认 profile"同向。
        """
        name = getattr(source, "profile", None)
        if not name:
            return Path(self.home)
        return self.home_for_profile_name(name) or Path(self.home)

    def servable_homes(self) -> List[Path]:
        """这次到底服务哪些 home（★ 服务集合**不许越出"本机默认家"这个边界**）。

        两层保护，缺一不可：

        ① **home 不是本机默认家 → 只服务自己**。真实版同一条道理（预检第 ① 条）：
           多路复用是"默认 profile 的网关"的职能；`-p X` 起的网关只服务 X。
        ② **只加 `profiles/` 下的具名 profile**。`profiles_root()` 是 **HOME 锚定**的，
           所以 `MINI_HOME=/tmp/x` 时（测试 / 演示）它指的是**用户真实的家** ——
           照抄真实版那种"直接 `profiles_to_serve(True)`"会替用户的家建适配器
           （实测踩过：测试进程连上了用户真实的 `default` 与 `lab`）。
        """
        from hermes_cli.profiles import default_home, profiles_to_serve

        homes: List[Path] = [Path(self.home)]
        if not getattr(self, "multiplex", False):
            return homes
        # ① **home 不是本机默认家 → 只服务自己**（`-p X` / 临时 home / 子进程）
        if str(Path(self.home).resolve()) != str(Path(default_home()).resolve()):
            return homes
        # ② 只加 `profiles/` 下的具名 profile（跳过"就是我自己"那一项）
        for other in profiles_to_serve(True)[1:]:
            if str(Path(other).resolve()) != str(Path(self.home).resolve()):
                homes.append(Path(other))
        return homes

    # ------------------------------------------------------------------ #
    # 观测面（★ 跳过必须说出来）
    # ------------------------------------------------------------------ #
    def adapters_log(self) -> List[str]:
        return list(getattr(self, "_adapters_log", []))

    def _adapter_note(self, text: str) -> None:
        self._adapters_log.append(text)
        print(f"[MULTIPLEX] {text}", flush=True)

    # ------------------------------------------------------------------ #
    # 次级 profile 的适配器（真实版 `:832-873`）
    # ------------------------------------------------------------------ #
    def _start_secondary_profile_adapters(self) -> int:
        """为**每一个非活动** profile 建连适配器（只在多路复用开时）。

        真实版 docstring 逐字：*"Bring up adapters for every non-active profile (multiplex only);
        returns connected count. Each profile connects under its own HERMES_HOME + secret scope;
        credential/listener collisions are refused here — the only point seeing every profile's
        credentials together."*

        ★ 关掉多路复用时要把"服务过谁"**清掉**（真实版同）：否则上一个多路复用器留下的
        `served_profiles` 会活到这一次单 profile 运行里，让 `gateway status` 报出
        "有人服务着"几个其实没人管的 profile。
        """
        homes = [Path(h) for h in (getattr(self, "served_homes", None) or [])]
        if not self.multiplex or len(homes) < 2:
            self._served_profiles = []
            return 0

        active_home = str(Path(self.home).resolve())
        connected = 0
        for home in homes:
            if str(home.resolve()) == active_home:
                continue                                   # 主循环负责它
            name = self.profile_name_for_home(home)
            try:
                connected += self._start_one_profile_adapters(name, home)
            except Exception as exc:                       # noqa: BLE001
                # ★ fail-soft：一个 profile 起不来**不许**拖垮别的 profile。
                #   （mini 没有 `MultiplexConfigError` 的等价触发条件 —— 那是"开放策略
                #     没开 allow-all"，mini 的授权是默认拒绝，见《源码讲解-09》§十四 B9。）
                self._adapter_note(f"✗ profile '{name}' 的适配器启动失败，跳过它：{exc!r}")

        self._served_profiles = [self.profile_name_for_home(h) for h in homes]
        self._adapter_note(f"多路复用：服务 {len(homes)} 个 home"
                           f"（次级适配器连上 {connected} 个）")
        return connected

    def _start_one_profile_adapters(self, name: str, home: Path) -> int:
        """建连**一个** profile 的适配器（mini 只有 console 一个平台）。"""
        profile_map = self._profile_adapters.setdefault(name, {})
        if "console" in profile_map:
            # 热重载/重复调用不重建：同一 token 起两个 poller = 消息被两边各收一次
            # （真实版注释：*"never a second poller on the same bot"*）
            return 0

        adapter: Optional[ConsoleAdapter] = None
        try:
            # ★ A5：**建适配器**这一步也在这个 profile 的作用域里跑
            #   （真实版 `:562 with _profile_runtime_scope(profile_home, hydrate_secrets=False)`）
            with profile_runtime_scope(home, hydrate_secrets=False):
                adapter = ConsoleAdapter(owner_profile=name, echo=False,
                                         chat_id=f"{name}-console", prompt="")
        except Exception as exc:                           # noqa: BLE001
            self._adapter_note(f"profile '{name}': 适配器创建失败 —— 跳过（{exc!r}）")
            return 0
        if adapter is None:
            self._adapter_note(f"profile '{name}': 适配器创建返回 None —— 跳过")
            return 0

        self.configure_profile_adapter(adapter, name)
        try:
            with profile_runtime_scope(home, hydrate_secrets=False):
                adapter.connect()
        except Exception as exc:                           # noqa: BLE001
            self._adapter_note(f"profile '{name}': 连接失败 —— 断开后跳过（{exc!r}）")
            try:
                adapter.disconnect()
            except Exception:                              # noqa: BLE001
                pass
            return 0

        profile_map["console"] = adapter
        self._adapter_note(f"✓ profile '{name}' 的 console 适配器已连接"
                           f"（owner_profile={name}，home={home}）")
        return 1

    def configure_profile_adapter(self, adapter: Any, name: str) -> None:
        """注入回调和键函数（与活动 profile 的适配器**走同一套**）。

        ★ 注意键函数**不需要**按 profile 定制：适配器在入站口会把 `source.profile`
          盖成自己的 owner（A8），而 `_key_for_source` 用的就是 `source.profile` ——
          于是"一个函数服务所有 bot"，不会出现两份键的算法。
        """
        adapter.set_owner_profile(name)
        adapter._on_message = self._handle_message
        adapter._on_interrupt = self._interrupt_running_agent
        adapter._on_overwrite = self._notify_overwrite
        adapter._session_key_fn = self._key_for_source

    # ------------------------------------------------------------------ #
    # `@<profile>` 入口（console 平台的"共享 bot"语法）
    # ------------------------------------------------------------------ #
    def profile_feed(self, name: str, text: str) -> bool:
        """把一行输入当成"**那个 profile 的 bot** 收到了它"喂进去。

        返回 `False` = 没有这个 profile —— 调用方**必须说出来**（不许静默丢）。
        """
        target = (name or "").strip()
        if target in ("default", "main"):
            adapter = self.adapters.get("console")
            if adapter is None:
                return False
            adapter.feed(text)
            return True
        adapter = (self._profile_adapters.get(target) or {}).get("console")
        if adapter is None:
            return False
        adapter.feed(text)
        return True

    def served_profile_names(self) -> List[str]:
        """当前"服务了谁"（裁决落盘那一栏用它）。"""
        return list(getattr(self, "_served_profiles", []) or [])
