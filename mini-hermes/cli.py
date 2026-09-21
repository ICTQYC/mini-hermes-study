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
from typing import Any, Dict, List

import hermes_state
from hermes_cli.runtime_provider import resolve_provider
from run_agent import AIAgent

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

BANNER = f"""{BOLD}mini-hermes{RESET} {DIM}· 一个从零手写的 agent loop（第 2 章：Agent Loop）{RESET}
{DIM}命令：/exit 退出 · /new 开新会话 · /todo 看任务清单 · /tools 看已注册工具 · /budget 看迭代预算 · Ctrl-C 中断当前 turn{RESET}
"""


class MiniCLI:
    def __init__(self, provider: str = "deepseek", model: str | None = None,
                 max_turns: int = 500, resume: bool = False, quiet: bool = False,
                 debug: bool = False):
        spec = resolve_provider(provider, model)
        self.quiet = quiet
        self.session_id = hermes_state.new_session_id()
        self.history: List[Dict[str, Any]] = []
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
            debug=debug,
        )
        self._say(f"{DIM}provider={spec['provider']} model={spec['model']} api_mode={spec['api_mode']}{RESET}")

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
        hermes_state.save_session(
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
                self.agent.budget = self.agent.budget.__class__(self.agent.max_turns)
                print(f"{DIM}已开新会话 {self.session_id}{RESET}")
                continue
            if user == "/tools":
                import model_tools

                for s in model_tools.get_tool_schemas():
                    print(f"{DIM}- {s['function']['name']}: {s['function']['description'][:60]}{RESET}")
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

        print(f"{DIM}会话已存：~/.mini-hermes/sessions/{self.session_id}.json{RESET}")
