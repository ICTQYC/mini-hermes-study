"""CLI 壳 —— 只负责"怎么收输入、怎么吐输出"。

对应 Hermes 的 cli.py。这一层的职责是**界面**：
它把用户敲的字交给 AIAgent，把 AIAgent 的回调变成终端上看到的东西。

⚠️ 注意别把它理解成"零逻辑"。真实的 `cli.py` 有 4689 行、220 个函数。
准确的说法是：**壳里可以有界面逻辑，不能有 agent 行为的逻辑。**
判定问题（想场景，别去理解术语）：
  "我现在是在终端里用它。如果改成从手机上使唤它，这件事还需要吗？"
  · "把工具进度画成一行彩色文字" → 手机不要彩色也不要那行字 → 壳的事 ✓
  · "这条命令危险，得先问用户"   → 手机照样得问           → 核心的事 ✗（在 registry.danger_check）

最容易判错的例子：Ctrl-C 打断
  · "接住 Ctrl-C 这个按键"                      → 只有终端有键盘 → 壳
  · "被打断时半截响应必须丢掉、不许写进历史"       → 手机上打断也一样 → 核心
  同一个功能一半在壳一半在核心 —— 这条规则是给「行为」分类，不是给「功能」分类。

所以下面这些看起来像"决策"的东西其实是**界面逻辑**，留在壳里是对的：
读哪个键、打印什么颜色、`/exit` 怎么处理、审批提示怎么排版。

Hermes 的架构原则叫 "platform-agnostic core"：
CLI / 网关 / ACP / 桌面 App 都是不同的壳，接的是同一个 AIAgent。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import hermes_state
from agent.system_prompt import invalidate_system_prompt, prompt_preview
from hermes_cli.runtime_provider import resolve_provider
from hermes_constants import display_path
from run_agent import AIAgent

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

BANNER = f"""{BOLD}mini-hermes{RESET} {DIM}· 一个从零手写的 agent（第 4 章：Tools Runtime）{RESET}
{DIM}命令：/exit 退出 · /new 开新会话 · /prompt 看三层提示词 · /todo 看任务清单 · /tools 看已注册工具 · /budget 看迭代预算 · Ctrl-C 中断当前 turn{RESET}
"""


def session_saved_message(saved: Path | None) -> str:
    """退出时那句"会话已存"。**根据事实说话**，不宣布没发生过的事。

    ★ 这一句原来有两个毛病，根因是同一个：**把打印当成了断言。**

    ① **路径写死**成 `~/.mini-hermes/sessions/{id}.json`。
       `MINI_HOME` 是可以被重定向的（测试 / E2E / CI 都这么干），
       重定向之后这句提示会把用户**指到一个不存在的文件上**——比不打印还坏。
       修法：用 `save_session()` **实际返回的路径**（而不是自己再猜一遍），
       显示时走 `display_path()`（在 home 底下缩成 `~/...`）。

    ② **无条件说"已存"**。用户开起来直接 `/exit`（一轮都没跑）时，
       连 `sessions/` 目录都不存在，它照样说"会话已存：xxx.json"。
       修法：先看路径在不在（`exists()`），没有就说没有。

    📌 **通用教训：日志/提示里的每句话都应该是"观察到的事实"，不是"预期的结论"。**
    预期和事实一旦分叉，提示就从"帮助"变成"误导"，而且**没有任何症状**——
    程序一切正常，只是用户按你给的路径去找文件却找不到。
    """
    if saved is not None and saved.exists():
        return f"{DIM}会话已存：{display_path(saved)}{RESET}"
    return f"{DIM}本次没有对话内容，未存盘{RESET}"


class MiniCLI:
    def __init__(self, provider: str = "deepseek", model: str | None = None,
                 max_turns: int = 500, resume: bool = False, quiet: bool = False,
                 debug: bool = False, ephemeral_system_prompt: str | None = None,
                 platform: str = "cli"):
        spec = resolve_provider(provider, model)
        self.quiet = quiet
        self.session_id = hermes_state.new_session_id()
        self.history: List[Dict[str, Any]] = []
        # 上一次真正落盘的路径（由 save_session 返回）。退出时据此"观察"而不是"断言"。
        self.last_save_path: Path | None = None
        self._turn_started = 0.0

        if resume:
            saved = hermes_state.latest_session()
            if saved:
                self.session_id = saved["session_id"]
                self.history = saved.get("messages", [])
                self._say(f"{DIM}已恢复会话 {self.session_id}（{len(self.history)} 条消息）{RESET}")

        self.agent = AIAgent(
            spec,
            max_turns=max_turns,
            text_callback=self._on_text,
            thinking_callback=self._on_thinking,
            reasoning_callback=self._on_reasoning,
            tool_progress_callback=self._on_tool_progress,
            step_callback=self._on_step,
            approval_callback=self._ask_approval,
            status_callback=self._on_status,
            debug=debug,
            # ---- 第 5 章：上下文压缩 ----
            # ★ 压缩阈值 = context_length × threshold，所以窗口必须传进来。
            context_length=spec.get("context_length"),
            # ---- 第 3 章：提示词装配 ----
            session_id=self.session_id,
            platform=platform,
            # ★ 临时系统提示：只作用于本次 API 调用，不写进历史。
            #   命令行 --system 优先，其次环境变量（对应 Hermes 的 HERMES_EPHEMERAL_SYSTEM_PROMPT）。
            ephemeral_system_prompt=(
                ephemeral_system_prompt
                or os.environ.get("MINI_EPHEMERAL_SYSTEM_PROMPT")
                or None
            ),
        )
        self._say(
            f"{DIM}provider={spec['provider']} model={spec['model']} "
            f"api_mode={spec['api_mode']} 窗口={spec['context_length']:,}{RESET}"
        )

    # ---------------- 回调面（这些就是文档 Callback Surfaces 那一表） ----------------

    def _on_text(self, text: str) -> None:
        if not self.quiet:
            print(text)

    def _on_thinking(self, active: bool) -> None:
        if self.quiet:
            return
        if active:
            print(f"{DIM}· 正在思考…{RESET}", end="\r", flush=True)
        else:
            print(" " * 30, end="\r", flush=True)

    def _on_reasoning(self, reasoning: str) -> None:
        if self.quiet:
            return
        first = reasoning.strip().splitlines()[0][:90] if reasoning.strip() else ""
        print(f"{DIM}· 推理：{first}…{RESET}")

    def _on_status(self, text: str) -> None:
        """系统提示（不是模型说的话）。

        ★ 第 3 章接进来的：上下文文件被截断时，警告要**浮现到用户眼前**。
          为什么重要——"我的 AGENTS.md 好像没生效"是个非常难猜的问题，
          用户会以为是模型不听话，而不是"文件被截断了 20000 字之后的都没进去"。
          **让代价可见。** 同时它不会污染历史（它不是助手说的话）。
        """
        if self.quiet:
            print(text, file=sys.stderr)
            return
        print(f"{YELLOW}{text}{RESET}")

    def _on_tool_progress(self, name: str, args: Dict[str, Any], phase: str) -> None:
        if self.quiet:
            return
        if phase == "start":
            snippet = args.get("command") or args.get("path") or args.get("action") or ""
            snippet = str(snippet).replace("\n", " ")[:80]
            self._turn_started = time.time()
            print(f"{CYAN}→ {name}{RESET} {DIM}{snippet}{RESET}")
            self._pending_name = name
        elif phase == "done":
            dt = time.time() - getattr(self, "_turn_started", time.time())
            print(f"{DIM}  ✓ {name} 完成（{dt:.2f}s）{RESET}")
        elif phase == "denied":
            print(f"{RED}  ✗ {name} 被拒绝{RESET}")
        elif phase == "fallback":
            print(f"{YELLOW}  ! 主模型失败({args.get('from')} → HTTP {args.get('status')})，切换备用 provider{RESET}")

    def _on_step(self, iteration: int) -> None:
        if not self.quiet:
            print(f"{DIM}—— 第 {iteration} 次迭代结束，回到循环顶部 ——{RESET}")

    def _ask_approval(self, command: str) -> bool:
        """危险命令审批。对应 Hermes 的 tools/approval.py。"""
        if threading.current_thread() is not threading.main_thread():
            print(f"{RED}[审批] 危险命令在并发线程里出现，无人可问 → 拒绝：{command}{RESET}")
            return False
        print(f"{YELLOW}[审批] 模型想执行危险命令：{command}{RESET}")
        try:
            return input("       允许执行？(y/N) ").strip().lower() in ("y", "yes")
        except EOFError:
            return False

    # ---------------- REPL ----------------

    def _say(self, text: str) -> None:
        print(text, file=sys.stderr if self.quiet else sys.stdout)

    def one_shot(self, prompt: str) -> Dict[str, Any]:
        """跑一轮，返回**完整结果 dict**（不只是文本）。

        为什么要返回 dict 而不是字符串：调用方需要知道 `result.get("error")`
        ——否则"出错"和"答完了"在退出码上没区别（这正是之前那个静默失败的帮凶）。
        """
        result = self.agent.run_conversation(prompt, conversation_history=self.history)
        self.history = result["messages"]
        # ★ 记下保存器**实际返回的路径**，而不是让退出提示自己再猜一遍。
        #   原来那句提示写死了 "~/.mini-hermes/sessions/{id}.json"，
        #   于是 MINI_HOME 重定向时它会把用户指到一个不存在的文件上。
        self.last_save_path = hermes_state.save_session(
            self.session_id, self.history, {"usage": result["usage"], "iterations": result["iterations_used"]}
        )
        return result

    def repl(self) -> None:
        print(BANNER)
        while True:
            try:
                user = input(f"{BOLD}你 ›{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user:
                continue

            if user in ("/exit", "/quit"):
                break
            if user == "/new":
                self.session_id = hermes_state.new_session_id()
                self.history = []
                self.last_save_path = None      # 新会话，旧路径作废
                self.agent.budget = self.agent.budget.__class__(self.agent.max_turns)
                # ★ 新会话 = 提示词重建的时机之一（对应 Hermes 的 invalidate_system_prompt）。
                #   如果忘了这一步会怎样：新会话还在用旧会话的系统提示词，
                #   里面写着旧的 Session ID、旧的时间戳、旧的工作区快照。
                #   ——**这就是"缓存复用"过度之后的样子**：省了重建，但内容是错的。
                self.agent.session_id = self.session_id
                self.agent.session_start = datetime.now()
                invalidate_system_prompt(self.agent)
                # 预算也重置了，所以"预算告警已注入"的一次性标志要跟着清
                self.agent._budget_warning_injected = False
                print(f"{DIM}已开新会话 {self.session_id}{RESET}")
                continue
            if user == "/prompt":
                # 把三层分开显示 —— 肉眼验证"每块归属哪一层、顺序对不对"。
                print(prompt_preview(self.agent))
                continue
            if user == "/tools":
                # 第 4 章：这个命令要能回答"**为什么模型看不到我那个工具**"，
                # 所以它必须把三件事分开显示：
                #   ① 已注册 ② 被 toolset 放行 ③ check_fn 说可用
                # 只显示①会让人以为"注册了就完事"（§1 六站全景的教训：
                # 11 步里有 5 步漏掉之后是**静默**的，得按站排查）。
                import model_tools
                from tools import registry
                import toolsets

                visible = {s["function"]["name"] for s in model_tools.get_tool_definitions()}
                registered = registry.tool_names()
                allowed = toolsets.get_enabled_tool_names()

                print(f"{DIM}发给模型的工具（{len(visible)} 个）：{RESET}")
                for s in model_tools.get_tool_definitions():
                    print(f"{DIM}  ✓ {s['function']['name']}: "
                          f"{s['function']['description'][:52]}{RESET}")

                hidden = [n for n in registered if n not in visible]
                if hidden:
                    print(f"{DIM}注册了但**没发给模型**（{len(hidden)} 个）：{RESET}")
                    for n in hidden:
                        why = ("没有 toolset 放行" if n not in allowed
                               else "check_fn 说现在不可用")
                        print(f"{DIM}  ✗ {n} —— {why}{RESET}")
                print(f"{DIM}toolset：{sorted(toolsets.TOOLSETS)}　"
                      f"探测缓存：{registry.check_fn_cache_info()}{RESET}")
                # ★ 第 4 章之后的补充：先把**工具清单**按 toolset 展开（名字 + 说明 + 工具），
                #   再打那行"选中的 toolset / 探测缓存"。
                #   这里调的是 `toolsets.describe_toolsets()` —— 和 `mini --list-toolsets`
                #   **同一份实现**。原来这行只打 `sorted(TOOLSETS)`（一串名字），
                #   于是 toolsets.py 里"`/tools` 会显示 description"那句承诺**是假的**：
                #   同一件事实写两处就会漂，所以现在只有一处。
                print(f"{DIM}{toolsets.describe_toolsets()}{RESET}")
                continue
            if user == "/budget":
                print(f"{DIM}迭代预算 {self.agent.budget.used}/{self.agent.budget.max_total}{RESET}")
                continue
            if user == "/todo":
                # agent 私有状态的**直接观察口**。用它来验证"从历史重建"真的生效了：
                # 退出进程、--resume 恢复历史，再敲 /todo —— 清单应该还在。
                #
                # ⚠️ 注意这里必须先 refresh_state：agent 的内存状态只在**跑一轮**时
                #    才会自动从历史重建（真实 Hermes 在 turn_context 做）。
                #    /todo 不跑 turn，所以得自己要求它重建 ——
                #    这个坑只有在"跨进程恢复"这种场景下才露出来。
                self.agent.refresh_state(self.history)
                print(f"{DIM}任务清单（revision={self.agent.todo.revision}，内存里）{RESET}")
                print(self.agent.todo.render())
                continue

            try:
                self.one_shot(user)
            except KeyboardInterrupt:
                self.agent.interrupt()
                print(f"\n{YELLOW}(已中断){RESET}")
            print()

        p = self.last_save_path
        print(session_saved_message(p))
