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
from pathlib import Path
from typing import Any, Dict, List, Optional

import hermes_state
from agent.system_prompt import prompt_preview
from hermes_cli.runtime_provider import resolve_provider
from hermes_constants import display_path
from run_agent import AIAgent

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

BANNER = f"""{BOLD}mini-hermes{RESET} {DIM}· 一个从零手写的 agent（逐章复刻 Hermes 官方 developer-guide）{RESET}
{DIM}命令：/exit 退出 · /new 开新会话 · /sessions 看历史会话 · /search 词 全文检索 · /prompt 看三层提示词 · /todo 看任务清单 · /tools 看已注册工具 · /budget 看迭代预算 · Ctrl-C 中断当前 turn{RESET}
"""


def session_saved_message(db_path: Path | None, message_count: int) -> str:
    """退出时那句"会话已存"。**根据事实说话**，不宣布没发生过的事。

    ★ 这一句原来有两个毛病，根因是同一个：**把打印当成了断言。**

    ① **路径写死**成 `~/.mini-hermes/sessions/{id}.json`。
       `MINI_HOME` 是可以被重定向的（测试 / E2E / CI 都这么干），
       重定向之后这句提示会把用户**指到一个不存在的文件上**——比不打印还坏。
       修法：用**实际的库路径**（`SessionDB.db_path`，来自唯一的路径来源
       `state_db_path()`），显示时走 `display_path()`（在 home 底下缩成 `~/...`）。

    ② **无条件说"已存"**。用户开起来直接 `/exit`（一轮都没跑）时，
       旧版连目录都不存在却照样说"会话已存"。
       修法：先看**事实** —— 库文件在不在 + 这个会话真有几条消息。

    📌 **通用教训：日志/提示里的每句话都应该是"观察到的事实"，不是"预期的结论"。**
    预期和事实一旦分叉，提示就从"帮助"变成"误导"，而且**没有任何症状**——
    程序一切正常，只是用户按你给的路径去找却发现什么都没有。

    （第 6 章只换了下半句的实现：现在"有没有内容"问的是库里的 `message_count`，
      不再是"那个 JSON 文件在不在"。）
    """
    if db_path is None or not Path(db_path).exists() or message_count <= 0:
        return f"{DIM}本次没有对话内容，未存盘{RESET}"
    return (f"{DIM}会话已写入 {display_path(Path(db_path))}"
            f"（{message_count} 条消息 · `mini --resume` 可继续）{RESET}")


class MiniCLI:
    def __init__(self, provider: str = "deepseek", model: str | None = None,
                 max_turns: int = 500, resume: bool = False, quiet: bool = False,
                 debug: bool = False, ephemeral_system_prompt: str | None = None,
                 platform: str = "cli"):
        spec = resolve_provider(provider, model)
        self.quiet = quiet
        # ══════════════════════════════════════════════════════════════
        # 第 6 章：会话存储从"一会话一个 JSON 文件"换成"一个 SQLite 库"
        # ══════════════════════════════════════════════════════════════
        self.db = hermes_state.SessionDB()
        self.session_id = hermes_state.new_session_id()
        self.history: List[Dict[str, Any]] = []
        self._turn_started = 0.0
        self._resumed_from: str | None = None

        if resume:
            previous = self.db.latest_session_id()
            if previous:
                self.session_id = previous
                # ★ 取的是"线上形状"的历史（role/content/tool_calls…），不是库里的原始行。
                #   `include_ancestors=True`：上下文压缩触发**会话轮转**时，
                #   历史会散在一条血缘链上（parent_session_id），只读最后一段会丢前面。
                self.history = self.db.get_messages_as_conversation(
                    previous, include_ancestors=True)
                self._resumed_from = previous
                self._say(f"{DIM}已恢复会话 {previous}"
                          f"（{len(self.history)} 条消息，来自 {display_path(self.db.db_path)}）{RESET}")
            else:
                self._say(f"{DIM}没有可恢复的会话{RESET}")

        # ★ 会话行**先建出来**（此刻还是一行"空壳"：模型已知、提示词还没有）。
        #   这不是省事，是真实版的选择 —— `_insert_session_row` 的 docstring 逐字：
        #     "the gateway creates a bare row before create_session carries the real model/prompt"
        #   后面的 upsert 只补空缺（COALESCE），所以"补"永远不会冲掉"已有的"。
        self.db.create_session(self.session_id, "cli", model=spec["model"],
                               model_config={k: spec.get(k) for k in ("provider", "api_mode")})

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
            # ---- 第 6 章：会话库（agent 在回合里**逐条追加**落盘）----
            session_db=self.db,
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

    def _on_tool_progress(
        self, name: str, args: Dict[str, Any], phase: str,
        status: Optional[str] = None, summary: Optional[str] = None,
    ) -> None:
        """工具进度。★ `done` 阶段还带 `(status, summary)`（第 4 章修）：

        以前只有名字 ⇒ 成功与失败**长得一样**（都打 `✓ 完成`），而失败原因只在模型的
        消息里。现在按 `status` 分岔打 `✓ 完成（… · 结果 N 字符）` / `✗ 失败：<原因首行>`；
        `phase="fallback"` 是**非工具事件**（主模型失败切换备用），同样在这张面上播报。
        """
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
            if status == "error":
                print(f"{RED}  ✗ {name} 失败（{dt:.2f}s）：{summary or ''}{RESET}")
            else:
                print(f"{DIM}  ✓ {name} 完成（{dt:.2f}s · {summary or ''}）{RESET}")
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
        self._check_resumed_prompt()
        # ★ 第 6 章：这里**不再整份重写**。消息已经在回合里逐条追加进库了
        #   （`AIAgent._persist_message`）—— 重写是 O(整个会话) 字节，追加是 O(1) 行。
        #   这里只补两样**会话级**的东西：用量计数 + 系统提示词的哈希。
        self._sync_session_meta(result)
        return result

    def _sync_session_meta(self, result: Dict[str, Any]) -> None:
        """把会话级的元数据补进库（用量计数 + 系统提示词哈希）。

        ★ 为什么这里用 `create_session()` 就够了 —— 它其实是 **upsert**：
          `ON CONFLICT DO UPDATE` 里只列了要"谁先写谁赢"的列（COALESCE 保留旧值），
          所以"第一轮之后才补提示词"这件事不会覆盖任何已有值。
          这正是真实版那句 docstring 说的场景：
            "the gateway creates a bare row before create_session carries the real model/prompt"
        """
        usage = result.get("usage") or {}
        self.db.record_usage(
            self.session_id,
            input_tokens=usage.get("prompt_tokens") or 0,
            output_tokens=usage.get("completion_tokens") or 0,
            api_calls=result.get("api_calls", result.get("iterations_used")) or 0,
        )
        prompt = getattr(self.agent, "_cached_system_prompt", None)
        if prompt:
            self.db.create_session(self.session_id, "cli", system_prompt=prompt)

    def _check_resumed_prompt(self) -> None:
        """恢复的会话：把"**存盘时的**系统提示词"和"现在拼出来的"比一比。

        ★ 这是第 3 章欠下的那笔账（当时写在 `_ensure_system_message` 的注释里：
          "mini 还没做这个校验——写进第 6 章的清单了"）。真实版对应
          `_stored_prompt_matches_runtime()`，用运行时环境块的边界当锚点。

        ★ 为什么要**说出来**而不是静默处理：提示词变了 = 人格 / 环境 / 工具说明
          变了（SOUL.md 改过、AGENTS.md 改过、cwd 变了…）。mini 的做法是
          **重建一份新的**（真实版是沿用存盘的并给出警告）——
          所以"它变了"这件事用户必须知道：他以为"它还记得我们上次的约定"，
          实际上人格已经换了。
        """
        if not self._resumed_from:
            return
        self._resumed_from = None            # 只查一次
        stored = self.db.get_stored_system_prompt(self.session_id)
        current = getattr(self.agent, "_cached_system_prompt", None)
        if stored and current and stored != current:
            self._on_status("注意：这个会话的**系统提示词**和存盘时不一样了"
                            f"（存盘 {len(stored):,} 字符 → 现在 {len(current):,} 字符）。"
                            "恢复出来的会话用的是**新**那一份（mini 的行为）；"
                            "对应的后果是：SOUL.md / AGENTS.md / 工作区这期间改过的话，"
                            "历史里的约定可能已经失效。")

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
                self._resumed_from = None
                # 新会话 = 新的一行（此刻还是空壳，模型等后面补）—— 与启动时同一套动作。
                self.db.create_session(self.session_id, "cli")
                self.agent.reset_session(self.session_id)
                # ★ 新会话 = 提示词重建的时机之一（对应 Hermes 的 invalidate_system_prompt）。
                #   如果忘了这一步会怎样：新会话还在用旧会话的系统提示词，
                #   里面写着旧的 Session ID、旧的时间戳、旧的工作区快照。
                #   ——**这就是"缓存复用"过度之后的样子**：省了重建，但内容是错的。
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
            if user == "/sessions":
                # 第 6 章新增：看一眼库里有什么（预览 = 第一条用户消息前 63 字符）。
                rows = self.db.list_sessions(limit=10)
                if not rows:
                    print(f"{DIM}库里还没有会话{RESET}")
                else:
                    print(f"{DIM}最近 {len(rows)} 个会话（库：{display_path(self.db.db_path)}）：{RESET}")
                    for r in rows:
                        mark = "●" if r["id"] == self.session_id else " "
                        title = r["title"] or "（无标题）"
                        print(f"{DIM} {mark} {r['id']}  {title}  消息 {r['message_count']:>3}"
                              f"  预览 {r['preview'][:40]!r}{RESET}")
                continue
            if user == "/search" or user.startswith("/search "):
                # 第 6 章新增：FTS5 全文检索（这也是"抄 FTS5"的**消费者** ——
                # 抄一个没有消费者的能力等于给自己留个假承诺）。
                query = user[len("/search"):].strip()
                if not query:
                    print(f"{DIM}用法：/search 词{RESET}")
                    continue
                hits = self.db.search_messages(query, limit=8)
                if not hits:
                    print(f"{DIM}没搜到 {query!r}{RESET}")
                else:
                    print(f"{DIM}命中 {len(hits)} 条（走的是 {hits[0]['route']} 那条路）：{RESET}")
                    for h in hits:
                        who = h["title"] or h["session_id"][-6:]
                        print(f"{DIM}  [{who}] {h['role']}: {h['snippet'][:90]}{RESET}")
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

        count = self.db.count_messages(self.session_id)
        print(session_saved_message(self.db.db_path, count))
        self.db.close()
