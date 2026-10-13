"""AIAgent —— 整个 mini-hermes 的中枢。

对应 Hermes 的 run_agent.py（真实版本 1560 行 facade + agent/turn_*.py 一堆兄弟模块）。
这里把它压成能读懂的 300 行，但**每一个关键机制都留着**：

  1. 两个入口：chat() / run_conversation()
  2. 9 步 turn lifecycle（下面 _run_conversation_turn 里编号和文档一一对应）
  3. 内部统一用 OpenAI 消息格式
  4. 消息角色交替规则（硬约束，错了 provider 会拒绝）
  5. 可中断的 API 调用（请求跑在后台线程，主线程盯着中断事件）
  6. 工具执行：单条直连 / 多条并发（结果按原始顺序回填）
  7. agent 级工具提前拦截（todo 不走 registry）
  8. 迭代预算 IterationBudget
  9. 失败回退（429/5xx/401/403 → **按序换** fallback provider，链来自 config.json 或构造入参）
      —— 实现见 agent/fallback.py；本文件的接缝是 `_try_activate_fallback` 与
      `_post_chat_completions` 的失败分支。
 10. 辅助模型路由（压缩摘要经 agent/auxiliary.py 选 provider/model；未配置 = 主模型）

没实现的（后面章节补）：prompt 缓存标记、上下文压缩、记忆落盘、
流式输出、工具审批的持久化白名单。
"""
from __future__ import annotations

import http.client
import json
import logging
import queue
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from datetime import datetime

import model_tools
from agent import fallback as _fallback
from agent.iteration_budget import IterationBudget
from agent.system_prompt import build_system_prompt
from tools.todo import AGENT_LEVEL_TOOLS, MAX_TODO_RESULT_CHARS, TodoStore

logger = logging.getLogger(__name__)

# 触发回退的 HTTP 状态码（★ 第 8 章 A7：判据收敛到 agent/fallback.py 一处；
# 这里保留同名符号是为了不打破"从 run_agent 读它"的老读法）
FALLBACK_STATUS = _fallback.FALLBACK_STATUS
is_fallbackable_status = _fallback.is_fallbackable_status

# ==================================================================== #
# 连接超时 ≠ 读取超时
# ==================================================================== #
#
# 实测（2026-09-15，所内网络）：
#   api.deepseek.com 的 DNS 返回两个 IP，其中 123.125.246.121 从本机**完全连不通**。
#   系统默认的连接超时是 ~75s，于是"DNS 挑到坏 IP"就等于整轮对话白等 75 秒。
#   把连接超时降到 5s：create_connection 会在 5s 后放弃该地址、
#   自动落到下一个地址 —— 总耗时 3.02s（而不是 75.02s）。
#
# ⚠️ 代价不是每次调用都付：DNS 返回的顺序在一段时间内**固定**、
#    跨时间段会变。好 IP 排头时 connect 只要 0.01s，一分钱不花；
#    只有坏 IP 排头时才付满这个超时。所以调小它 = 只降低最坏情况。
#
# 为什么必须把两个超时**分开**：
#   urllib.request.urlopen(timeout=) 只有一个值，同时管连接和读取。
#   而这两件事的合理上限差两个数量级：
#     连接：好网络 20ms；坏地址可能要等到内核默认的 75s
#     读取：模型生成一整段话，几秒到几分钟
#   共用一个值只能二选一 —— 短了会掐断正常的长回答，长了坏的地址就白等。
#
# 为什么是 2s：域内 API 的握手正常在 50ms 量级，2s 留了 40 倍余量；
#   真遇到更慢的链路，create_connection 会继续试下一个地址，不会因此失败。
CONNECT_TIMEOUT = 2.0      # 每个地址最多等这么久，超了就换下一个
READ_TIMEOUT = 180.0       # 连上之后，等模型吐字可以很久


class ApiHTTPError(Exception):
    """HTTP 4xx/5xx。

    自己定义而不是用 urllib.error.HTTPError：因为我们绕过了 urlopen，
    而且回退逻辑（`_post_chat_completions`）只需要 `.code` 一个字段。
    """

    def __init__(self, status: int, body: str, url: str = ""):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.code = status
        self.body = body
        self.url = url


class _TwoTimeoutHTTPSConnection(http.client.HTTPSConnection):
    """把"连接超时"和"读取超时"分开的 HTTPS 连接。

    关键机制：`socket.create_connection` 会**按顺序尝试 getaddrinfo 返回的每个地址**，
    每个地址用我们给的短超时。所以"坏地址"从"白等 75 秒"变成"5 秒后跳过"。
    """

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        sock.settimeout(READ_TIMEOUT)        # ← 连上之后立刻换成读超时
        if self._tunnel_host:                # 走 HTTP 代理时才需要
            self.sock = sock
            self._tunnel()
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

    def __init__(self, host: str, port: int = 443, **kw: Any):
        # 默认不校验证书链之外的东西，但**要**用系统 CA —— 和 urlopen 行为一致
        super().__init__(host, port, context=ssl.create_default_context(), **kw)


# ==================================================================== #
# agent 级工具的执行表
# ==================================================================== #
#
# 这些工具在到达 model_tools.handle_function_call() **之前**就被吃掉了，
# 因为它们的"执行"需要 agent 自己的状态（`self.todo`），而注册表里的
# 工具函数签名是纯的，给不了 `self`。
#
# ⚠️ 用**表**而不是 `if name == "todo": ... elif name == "memory": ...`
#    Hermes 有一条形状规矩：不许出现按名字分派、4 个以上分支的 if/elif 阶梯
#    （见根 AGENTS.md），要写成 dict → handler。
#    加一个新 agent 级工具 = 往这张表加一行，不是往函数里加一个分支。
#
# key 必须和 tools/todo.py 的 AGENT_LEVEL_TOOLS 完全对齐 —— 有测试盯着。


def _execute_todo_list(agent: "AIAgent", args: Dict[str, Any]) -> str:
    """执行一次 todo_list 调用。

    注意返回值是 **JSON 字符串**（不是给人看的清单）。原因在 tools/todo.py 里写着：
    这个结果会被从历史里解析回来重建清单，所以它必须机器可读。
    """
    agent.todo.write(args.get("todos"), bool(args.get("merge", False)))
    return agent.todo.to_payload()


INLINE_TOOL_EXECUTORS: Dict[str, Callable[["AIAgent", Dict[str, Any]], str]] = {
    "todo_list": _execute_todo_list,
}


class AgentInterrupted(Exception):
    """用户主动打断（Ctrl-C / /stop）。"""


class AIAgent:
    def __init__(
        self,
        provider_spec: Optional[Dict[str, Any]] = None,
        *,
        api_mode: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        # ★ 回退链：`None` = **没传**（⇒ 读 config.json 的 `fallback_providers`，A9/A7）；
        #   `[]` = **显式禁用回退**（真实版同款语义）。链的条目形状见 agent/fallback.py。
        fallback_providers: Optional[List[Dict[str, Any]]] = None,
        max_turns: int = 500,
        # ---- 回调面（对应文档 Callback Surfaces 那节）----
        text_callback: Optional[Callable[[str], None]] = None,
        thinking_callback: Optional[Callable[[bool], None]] = None,
        reasoning_callback: Optional[Callable[[str], None]] = None,
        tool_progress_callback: Optional[Callable[[str, Dict[str, Any], str], None]] = None,
        step_callback: Optional[Callable[[int], None]] = None,
        approval_callback: Optional[Callable[[str], bool]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        debug: bool = False,
        # ---- 第 3 章：提示词装配的开关（对应 Hermes agent_init 里那批同名属性）----
        session_id: Optional[str] = None,
        platform: str = "cli",
        # ---- 第 6 章：会话库（逐条追加落盘）----
        #   传 None 就完全不做持久化（测试里的绝大多数 agent 就是这样）。
        session_db: Optional[Any] = None,
        ephemeral_system_prompt: Optional[str] = None,
        context_cwd: Optional[str] = None,
        skip_context_files: bool = False,
        # ★ 默认 False —— 和 Hermes 的 agent_init 对齐。
        #   注意它**不是**"要不要加载 SOUL.md"的开关（那个由
        #   `load_soul_identity or not skip_context_files` 决定），
        #   而是"**强行加载**"的开关：cron 任务需要"跳过 cwd 指令但保留人格"。
        load_soul_identity: bool = False,
        context_length: Optional[int] = None,
    ):
        spec = provider_spec or {}
        self.api_mode = api_mode or spec.get("api_mode", "chat_completions")
        self.base_url = (base_url or spec.get("base_url", "")).rstrip("/")
        self.api_key = api_key or spec.get("api_key", "")
        self.model = model or spec.get("model", "")
        self.provider = spec.get("provider", "unknown")

        # ══════════════════════════════════════════════════════════════
        # 第 8 章 A7：回退链（配置是默认来源，构造入参可覆盖）
        # ══════════════════════════════════════════════════════════════
        #   ★ 这一章之前，`fallback_providers` 是**形参 + 空列表**——全仓零调用点，
        #     而模块 docstring 却写着"失败回退"。现在它真的接线了：
        #       None → 读 config.json（A9 的 get_fallback_chain）
        #       []   → 显式禁用（真实版同款语义）
        #   链的状态（索引 / 一次性 latch / 主运行时快照）都挂在实例上 ——
        #   观察面因此是**可断言的普通属性**（真实版也是这么长的）。
        if fallback_providers is None:
            try:
                fallback_providers = _fallback.chain_from_config()
            except Exception:  # noqa: BLE001 —— 坏配置不该让 agent 起不来
                fallback_providers = []
        self.fallback_providers = list(fallback_providers or [])
        _fallback.install_chain(self, self.fallback_providers)

        # ★ 当前 client 的**构造入参**。mini 没有长命 client 对象（每次请求按这些值建连接），
        #   所以"换 client" = 换这 5 个属性 + 这份快照。单一写入口是 `_apply_runtime`，
        #   保证属性与快照**永不失同步**（真实版是 `_swap_fallback_clients` + `_transport_cache.clear()`）。
        self._client_kwargs: Dict[str, Any] = self._runtime_client_kwargs()

        self.max_turns = max_turns
        self.budget = IterationBudget(max_turns)

        self.text_callback = text_callback
        self.thinking_callback = thinking_callback
        self.reasoning_callback = reasoning_callback
        self.tool_progress_callback = tool_progress_callback
        self.step_callback = step_callback
        self.approval_callback = approval_callback
        # ★ 状态行回调（对应 Hermes 的 `_emit_status`）。
        #   它和 text_callback 是**两个不同的面**：
        #     text_callback  → "模型说的话"（进回答正文、会被存进历史）
        #     status_callback → "系统要说的话"（不进历史、只是给用户看的提示）
        #   为什么要分开：截断警告、预算耗尽的提示**都不是模型说的**，
        #   混进正文会污染历史、还会让角色交替出问题。
        self.status_callback = status_callback
        self.debug = debug               # 异常时是否直接把 traceback 打到 stderr

        # ══════════════════════════════════════════════════════════════
        # 第 3 章：系统提示词的状态
        # ══════════════════════════════════════════════════════════════
        # ★ session_start 只在**会话开始时取一次**。
        #   时间戳行靠它保持"当天字节稳定"——如果每轮 now()，缓存每轮都断。
        self.session_start = datetime.now()
        self.session_id = session_id or uuid.uuid4().hex[:12]
        # 第 6 章：会话库句柄（None = 不落盘）。真正的写入点是 _persist_message()。
        self.session_db = session_db
        self.platform = platform
        # ★ ephemeral_system_prompt 只作用于**本次 API 调用**，不写进历史。
        #   见 _inject_ephemeral_layers。
        self.ephemeral_system_prompt = ephemeral_system_prompt
        self.context_cwd = context_cwd
        self.skip_context_files = skip_context_files
        self.load_soul_identity = load_soul_identity
        # ★ 显式参数优先；没传就从 spec 拿（`resolve_provider` 已经查过表了）。
        #   两者都没有才落到 None，由压缩器用它的保守默认值兜住。
        self.context_length = context_length or spec.get("context_length")

        # ★ 系统提示词的缓存 —— **这一章的立章之本**。
        #   不缓存 = 每轮重读文件 + 每轮结果可能不同 = "字节不稳定"，
        #   而那个 bug 没有症状，只有账单。见 agent/system_prompt.py。
        self._cached_system_prompt: Optional[str] = None
        self._cached_system_prompt_key: Any = "__unset__"
        self._cached_system_prompt_static: Optional[str] = None
        # 工作区快照的 pin（按 cwd 作 key；"" 是真实值，见 _workspace_parts）
        self._frozen_workspace_snapshot: Optional[tuple] = None
        # ★ 预算告警的一次性标志（对应 Hermes 的 _iteration_budget_warning_injected）。
        #   为什么必须一次性：它不是"每轮要提醒的事"，而是"该收尾了"的一次通告。
        #   每轮重复注入 = 白占上下文 + 让请求每轮都多两行。
        self._budget_warning_injected = False

        # agent 私有状态（不落盘、不进 session）
        self.todo = TodoStore()
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self._current_task_id: Optional[str] = None   # 只为了日志里能对上是哪一轮

        # 中断信号：API 调用和工具执行都会看它
        self._interrupt = threading.Event()
        self._current_thread: Optional[threading.Thread] = None

        model_tools.discover_builtin_tools()

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #

    def chat(self, user_message: str) -> str:
        """简单接口：只要最终回答字符串。

        真实 Hermes 里这就是个薄壳 —— 把 run_conversation 的
        final_response 字段抠出来。不要在这里加逻辑。
        """
        return self.run_conversation(user_message)["final_response"]

    def run_conversation(
        self,
        user_message: str,
        system_message: Optional[str] = None,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """完整接口：返回 messages / metadata / usage。"""
        # 1. 没有 task_id 就生成一个
        task_id = task_id or f"task_{uuid.uuid4().hex[:8]}"
        self._current_task_id = task_id

        # 2. 用户消息进历史
        messages: List[Dict[str, Any]] = list(conversation_history or [])
        messages.append({"role": "user", "content": user_message})
        self._persist_message(messages[-1])

        # 2b. ★ **用量清零**：`self.usage` 是**本轮**的累加器，不是进程累计。
        #     （真实版：`agent/conversation_loop.py:1504` 每轮开头 `agent._last_turn_usage = None`
        #       + `agent/turn_usage.py:157` 写入 —— 它也只有"最后一轮"这一个槽。）
        #     ★ 为什么这行在第 8 章之后才变得关键：网关原来是**每条消息新建 agent**
        #       （对象活一轮 → 累加器天然就是本轮的），第 8 章改成**缓存 agent**之后，
        #       同一个对象活很多轮 → 不清零就会**跨轮累加**，而网关侧写库是
        #       `record_usage()`（SQL 里 `input_tokens = input_tokens + ?`，**加**）
        #       → 第二轮的用量会把第一轮再算一遍，**静默翻倍**（连跑两轮的探针抓到的：轮 2 报了 200）。
        for _k in self.usage:
            self.usage[_k] = 0

        # 3. 拼/复用系统提示词
        #    ★ build_system_prompt 内部**缓存**在 agent 上，同一会话只拼一次。
        #      这里不是"每轮重拼一个字符串"——那是第 2 章的做法，
        #      它的代价是每轮重读 SOUL.md / AGENTS.md / MEMORY.md，
        #      而且每轮结果可能不同（记忆被改了 / cwd 变了 / 跨天了）。
        #      **系统提示词必须字节级稳定，这是 prompt 缓存的前提。**
        #    ★ system_message 参数现在的语义是"调用方给的 system_message"
        #      —— 它进 **context 层**（对应 Hermes 的同名参数），不再覆盖整份提示词。
        built = build_system_prompt(self, system_message=system_message)
        self._ensure_system_prompt(messages, built)

        # 3.5 从历史重建 agent 私有状态
        #     ★ 为什么需要这一步：网关（Telegram/Discord）**每条消息都新建一个 AIAgent**，
        #     所以内存里的 todo 清单必然每次都丢。丢不要紧——它可以从历史重建。
        #     这是"状态放哪"这个问题的一个漂亮答案：放内存（快、简单），
        #     但把"可恢复"这个责任交给历史。
        self._hydrate_todo_store(messages)

        self._interrupt.clear()
        final_response = ""
        try:
            result = self._run_conversation_turn(messages, task_id)
            final_response = result["final_response"]
        except AgentInterrupted:
            final_response = "(已被用户中断)"
            result = {"interrupted": True}
        except Exception as exc:  # noqa: BLE001
            final_response = f"[agent 异常] {type(exc).__name__}: {exc}"
            result = {"error": str(exc)}
            self._log_exception(exc)

        # 3.6 ★ 第 10 章：每回合收尾后的**微压缩**。
        #     真实版在 `finalize_turn` 的 `_persist_step` 里调（`turn_finalizer.py:508`），
        #     门是 `not interrupted and not failed`。mini 的收尾点就在这里 ← run_agent
        #     没有独立的 finalize_turn，回合循环一返回即收尾。
        #     ★ 只在**正常完成**的回合跑：失败/被中断的回合不该改写已发历史。
        if (
            final_response
            and not result.get("interrupted")
            and not result.get("error")
            and not result.get("budget_exhausted")
        ):
            self._micro_compact_after_turn(messages)

        # ══════════════════════════════════════════════════════════════
        # 唯一的"汇报出口"
        # ══════════════════════════════════════════════════════════════
        # ★ 为什么必须只有这一处：原来成功路径在 loop 里调 text_callback、
        #   失败路径没人调 —— 后果是"turn 里抛异常"表现为**一个字都不打印、退出码 0**，
        #   用户看到的是"没反应"。这正是"两条路各写一遍就会漂移"的又一个实例
        #   （对照 `agent/inline_tool_executors.py` 的 docstring：Hermes 也踩过同一类坑）。
        #
        #   规矩：**成功、失败、被中断、超预算——都从这里出去。**
        if self.text_callback:
            self.text_callback(final_response)

        return {
            "final_response": final_response,
            "messages": messages,
            "task_id": task_id,
            "iterations_used": self.budget.used,
            "usage": dict(self.usage),
            **result,
        }

    # ------------------------------------------------------------------ #
    # 核心：turn loop
    # ------------------------------------------------------------------ #

    def _run_conversation_turn(self, messages: List[Dict], task_id: str) -> Dict[str, Any]:
        # 0. ★ 第 8 章 A7：**按轮次回退** —— 新一轮开始把运行时收回主 provider，并复位
        #    回退索引 + 一次性 latch（真实版 `restore_primary_runtime()`）。
        #    为什么必须复位：一次限流/抖动是**瞬时**的，上一轮的兜底 provider 不该
        #    悄悄变成这一轮的第一选择（那会让"临时兜底"变成"永久降级"，且没有症状）。
        _fallback.restore_primary_runtime(self)

        while True:
            # 每次循环 = 一次"迭代"。先要预算，要不到就收尾。
            if not self.budget.consume():
                return {
                    "final_response": f"（迭代预算 {self.max_turns} 次已用尽，任务未完成，已停止）",
                    "budget_exhausted": True,
                }

            # 4. 预检压缩：文档说超过上下文 50% 就压。这一步属于后面 compression 章节。
            self._preflight_compression(messages)

            # 5. 内部消息格式 → 该 api_mode 的线上格式
            api_messages = self._build_api_messages(messages)
            # ★ 锚点记的是**历史**（不是 api_messages）：临时层每轮都变，
            #   拿它做指纹会让锚点永远失效。存一份引用，响应回来后用它记账。
            _history_at_request = messages

            # 6. 注入临时提示层（预算告警 / ephemeral_system_prompt）—— 只改请求副本，不进历史
            api_messages = self._inject_ephemeral_layers(api_messages)

            # 7. 打 prompt 缓存标记（只有 Anthropic 需要；DeepSeek 是自动前缀缓存）
            api_messages = self._apply_prompt_caching(api_messages)

            # 7.5 ★ 校验**最终要发出去的那份**。
            #
            #     ⚠️ 我原来的顺序有问题：`_validate_alternation` 在 `_build_api_messages`
            #        里被调用，也就是跑在**注入之前**。后果是"注入自己把消息序列弄违规"
            #        这件事**永远发现不了**——校验对着中间态，发出去的却是最终态。
            #
            #     `_build_api_messages` 里那次校验**保留**，但它管的是别的事：
            #       · 早期那次 → 校验**历史本身**合法（防止历史被污染）
            #       · 这次     → 校验**最终请求**合法（含临时层）
            #     两个不同的对象，都该被校验。
            self._validate_alternation(api_messages)

            # 8. 发请求（可中断，带回退）
            if self.thinking_callback:
                self.thinking_callback(True)
            try:
                response = self._interruptible_api_call(api_messages, task_id)
                # ★ 把 provider 报出的**真实用量**记进锚点（第 5 章 §2.2.1）。
                #   指纹盖的是历史，而 prompt_tokens 是整个请求 —— 差额是系统提示词
                #   和工具 schema，它们在一段对话里稳定，所以这个近似是有意的。
                _usage = (response or {}).get("usage") or {}
                if _usage:
                    self._compressor().update_usage(_usage, _history_at_request)
            finally:
                if self.thinking_callback:
                    self.thinking_callback(False)

            # 9. 解析响应
            assistant_msg, tool_calls = self._parse_response(response)
            messages.append(assistant_msg)
            self._persist_message(assistant_msg)

            if tool_calls:
                # 有工具调用 → 执行 → 回填 → 回到第 5 步（这就是 loop 的"环"）
                self._execute_tool_calls(assistant_msg, tool_calls, messages, task_id)
                if self.step_callback:
                    self.step_callback(self.budget.used)
                continue

            # 没有工具调用 = 模型认为任务结束了 → 返回
            # ⚠️ 这里**故意不调 text_callback**：汇报统一由 run_conversation 做，
            #    否则失败路径又会漏掉（那正是我们刚修掉的"静默失败"bug）。
            return {"final_response": assistant_msg.get("content") or "", "iterations": self.budget.used}

    # ------------------------------------------------------------------ #
    # 消息层
    # ------------------------------------------------------------------ #

    def _emit_status(self, text: str) -> None:
        """给用户看的"系统要说的话"（对应 Hermes 的 ``agent._emit_status``）。

        ★ 它为什么不能走 text_callback：

        | 面 | 是什么 | 进历史吗 |
        |---|---|---|
        | ``text_callback`` | **模型说的话**（回答正文） | 进 |
        | ``status_callback`` | **系统要说的话**（截断警告、状态提示） | 不进 |

        混在一起的后果：截断警告会变成"助手说的话"被存进历史，
        下一轮模型还以为自己说过这句话 —— **污染历史 + 破坏角色交替**。
        """
        if self.status_callback is not None:
            try:
                self.status_callback(text)
            except Exception:  # noqa: BLE001 —— 提示失败不该影响 turn
                pass

    def _emit_engine_warning(self, text: str) -> None:
        """**引擎回落警告**的专用出口（第 14 章）。

        ★ 为什么不能直接复用 `_emit_status`：`_emit_status` 在
          `status_callback is None` 时**什么都不做**。而网关
          （`gateway/run_turn.py` 传 `status_callback=None`）与
          `doctor.py`（干脆不传）正是这样的入口 —— 于是"未知名回落"这条警告
          在这些入口会被**静默丢弃**，用户以为换了引擎。静默降级是本章最坏的一种。

        ★ 为什么**不**改 `_emit_status` 本身的全局行为：它还被截断警告 / 微压缩
          状态等复用，在网关入口把那些全掎到 stderr 是噪声（本该走结构化日志 /
          静默）。所以只针对"回落"这一条加兜底：

          | 情形 | 出口 |
          |---|---|
          | 有 `status_callback`（CLI 等） | 走它（保持"系统要说的话不进历史"的原语义） |
          | **没有**（网关 / doctor） | `logger.warning` + `print(..., file=sys.stderr)` |

        ⇒ "回落有**声**"在**所有**入口都成立，且默认路径（零配置）依旧安静。
        """
        if self.status_callback is not None:
            try:
                self.status_callback(text)
            except Exception:  # noqa: BLE001 —— 同 `_emit_status`：提示失败不影响 turn
                pass
            return
        # 没有 status 出口的入口（网关 / doctor）：结构化日志 + stderr 双写，
        # 保证"回落有声"不因入口不同而消失。
        # ★ 只 print 不 logger：logger 会双写（零配置时 lastResort 也落 stderr），配了 handler 的网关反倒看不见终端
        print(text, file=sys.stderr)

    def _ensure_system_prompt(self, messages: List[Dict], system_message: str) -> None:
        """系统消息永远在 index 0，且**只在这里建一次**。

        ★ 注意这里隐含了一个重要行为：**`--resume` 恢复的会话不会重建系统提示词。**
          历史里 index 0 已经有一条 system 消息（上次会话存下来的），
          所以这次构建出来的那份会被**丢掉**，继续用旧的。

          这**是对的**——继续用旧的那份，缓存前缀才能跨进程复用。
          代价是：如果 `SOUL.md` / `AGENTS.md` 在上次会话之后变了，
          恢复的会话**看不到新版本**，除非开新会话。

          Hermes 用 `_stored_prompt_matches_runtime()` 来判断"存的那份还对不对"
          （靠运行时环境块的边界当锚点，见 prompt_builder 里那两个常量）。
          mini 还没做这个校验——写进第 6 章的清单了。
        """
        if messages and messages[0].get("role") == "system":
            return
        messages.insert(0, {"role": "system", "content": system_message})

    def _build_api_messages(self, messages: List[Dict]) -> List[Dict]:
        """5. 内部格式 → 线上格式。

        内部一律 OpenAI 风格（role/content/tool_calls），所以：
          chat_completions   → 直接用
          codex_responses    → 转成 Responses API 的 input items
          anthropic_messages → 经 anthropic_adapter.py 转换
        mini 只实现了第一种；其余两种在这里给出**带指引**的报错
        （说清 mini 支持哪几种、该改哪个文件）——而不是跑到发请求才炸。

        ★ 这里的 `_validate_alternation` 校验的是**历史本身**（`messages`），
          **不是**最终请求。它防的是"历史已经被污染"。
          最终请求还要再校验一次——因为临时层是在这之后才注入的，
          而注入也可能把序列弄违规（见循环里第 7.5 步）。
        """
        self._validate_alternation(messages)
        # ★ 第 8 章 A3：协议检查收敛到 providers/api_mode.py 一处，
        #   `require_implemented_api_mode` 对未实现的模式抛**带指引**的
        #   `UnsupportedApiMode`（不再裸抛 NotImplementedError）。
        from providers.api_mode import require_implemented_api_mode

        require_implemented_api_mode(self.api_mode)
        return [dict(m) for m in messages]

    def _validate_alternation(self, messages: List[Dict]) -> None:
        """消息角色交替规则 —— 硬约束，provider 真的会因此拒绝你的请求。

          system → user → assistant → user → assistant ...
          工具调用时：assistant(tool_calls) → tool → tool → ... → assistant
          **绝不能**两个 assistant 相邻，也**绝不能**两个 user 相邻
          只有 tool 角色允许连续
        """
        prev = None
        for i, m in enumerate(messages):
            role = m.get("role")
            if role == prev and role in ("user", "assistant"):
                raise ValueError(f"消息 {i} 违反交替规则：连续两条 {role}")
            if role == "tool" and prev not in ("tool", "assistant"):
                raise ValueError(f"消息 {i}：tool 结果必须紧跟 assistant/tool，实际前一条是 {prev}")
            prev = role

    def _inject_ephemeral_layers(self, api_messages: List[Dict]) -> List[Dict]:
        """6. 临时提示层：只影响这一次请求，不写回历史。

        Hermes 用这一层塞"预算快用完了""上下文压力大"之类的一次性提示。
        关键：**不能写进 messages**，否则历史被污染、缓存失效、角色交替被打乱。

        ★ 为什么"必须在副本上"（第 3 章才想明白的）：
          `_build_api_messages` 返回的这份副本，**就是发给提供商的那份请求**。
          而提供商侧的 prompt 缓存比对的就是这份请求的前缀。
          **动它就是在动缓存**——所以临时层只能加在副本上，不能落回 messages。
        """
        # 6a. ephemeral_system_prompt：拼到"有效系统消息"的末尾。
        #     对应 Hermes `agent/turn_context.py:1152`：
        #         if agent.ephemeral_system_prompt:
        #             effective_system = (effective_system + "\n\n" + agent.ephemeral_system_prompt).strip()
        #
        #     ★ messages 本身**不动**（历史里那条 system 消息保持原样）。
        #       docstring 原话里就写着 "``messages`` stays untouched"。
        #     ★ 代价要说清楚：如果这个值**每轮都不同**，它会打破提供商侧的缓存。
        #       这是显式开关的代价，也是它名字里 "ephemeral" 的意思——
        #       想用它就要接受"这几轮没缓存"，换来"能临时改变行为"。
        if self.ephemeral_system_prompt and api_messages:
            head = api_messages[0]
            if head.get("role") == "system":
                head = dict(head)  # _build_api_messages 已经拷过一层，这里再防一手
                head["content"] = (
                    str(head.get("content", "")) + "\n\n" + self.ephemeral_system_prompt
                ).strip()
                api_messages = [head, *api_messages[1:]]

        # 6b. 预算告警 —— ★ **只改"尾巴"**，而且只在尾巴是工具结果时才改。
        #
        #     Hermes 顶层 AGENTS.md 的原文规矩：
        #
        #       never two same-role messages in a row;
        #       **never a synthetic user message injected mid-loop**
        #
        #     ⚠️ 我第一版往末尾**插了一条新的 user 行**，实测结果是
        #        `['system', 'user', 'user']` —— 被 mini **自己的** `_validate_alternation`
        #        判违规（"连续两条 user"）。而且那个校验跑在注入**之前**，所以自己发现不了。
        #
        #     Hermes 的做法（`agent/turn_iteration_prep.py:32`，逐条抄过来）：
        #
        #       · `messages[-1]["role"] != "tool"` → **直接放弃**，不找地方硬塞
        #         （源码注释：*Appended to the newest tool result; never a synthetic
        #          user/system row.*）
        #       · 那一行已被持久化过 → 也放弃（改了它，"重放历史"就和"当时真正发的
        #         请求"不一致了）
        #       · 否则把提示**追加进那条工具结果的 content 里**（改尾巴，不新增行）
        #       · **一次性标志**（`_iteration_budget_warning_injected`）——
        #         只提醒一次，不每轮唠叨
        #
        #     为什么"改尾巴"是缓存安全的（源码注释原文）：
        #       *Only the current tool-result tail is mutable; an older turn may already
        #        be cached.*  —— 前缀缓存比的是"从头开始的最长公共前缀"，**改尾巴不影响前缀**。
        remaining = self.budget.remaining
        if (
            self.max_turns
            and not self._budget_warning_injected
            and remaining <= max(3, self.max_turns // 20)
            and api_messages
            and api_messages[-1].get("role") == "tool"
        ):
            last = dict(api_messages[-1])
            last["content"] = (
                str(last.get("content", ""))
                + f"\n\n（系统提示：还剩 {remaining} 次迭代，请尽快收尾并给出结论。）"
            )
            api_messages = [*api_messages[:-1], last]
            self._budget_warning_injected = True
        return api_messages

    def _apply_prompt_caching(self, api_messages: List[Dict]) -> List[Dict]:
        """7. 只有 Anthropic 需要在历史末尾打 cache_control 断点。

        Hermes 整个工程最硬的一条规矩就是"不许破坏 prompt 缓存"：
        一次长对话每轮复用同一段缓存前缀，任何改动过去的消息、
        换工具集、重建系统提示词的行为都会让缓存失效、成本翻倍。
        """
        from agent import prompt_caching
        out, note = prompt_caching.plan(
            api_messages,
            model=getattr(self, "model", "") or "",
            provider=getattr(self, "provider", "") or "",
        )
        self._emit_status(note)
        return out

    def _parse_response(self, response: Dict[str, Any]) -> tuple[Dict[str, Any], List[Dict]]:
        if not response:
            return {"role": "assistant", "content": ""}, []

        msg = (response.get("choices") or [{}])[0].get("message") or {}
        usage = response.get("usage") or {}
        for k in self.usage:
            self.usage[k] += usage.get(k, 0) or 0

        assistant_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": msg.get("content") or "",
        }
        # 思考内容存在 assistant_msg["reasoning"]（Hermes 一致）
        reasoning = msg.get("reasoning_content")
        if reasoning:
            assistant_msg["reasoning"] = reasoning
            if self.reasoning_callback:
                self.reasoning_callback(reasoning)

        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        return assistant_msg, tool_calls

    # ------------------------------------------------------------------ #
    # 可中断的 API 调用
    # ------------------------------------------------------------------ #

    def _interruptible_api_call(self, api_messages: List[Dict], task_id: str) -> Dict[str, Any]:
        """8. 真正的 HTTP 调用跑在后台线程，主线程只盯三件事：
             - 结果回来了
             - 中断事件被置位
             - 超时

           被中断时：**后台线程被丢弃，响应直接扔掉，绝不把半截响应写进历史。**
        """
        result_q: queue.Queue = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                result_q.put(self._post_chat_completions(api_messages, task_id))
            except BaseException as exc:  # noqa: BLE001 - 异常也要交回主线程
                result_q.put(exc)

        thread = threading.Thread(target=worker, daemon=True, name="mini-api-call")
        self._current_thread = thread
        thread.start()

        try:
            while True:
                if self._interrupt.is_set():
                    raise AgentInterrupted()
                try:
                    result = result_q.get(timeout=0.1)
                    break
                except queue.Empty:
                    continue
        except KeyboardInterrupt:
            # 主线程收到 Ctrl-C：置位中断事件，让整个 agent 干净退出
            self._interrupt.set()
            raise AgentInterrupted() from None
        finally:
            self._current_thread = None

        if isinstance(result, BaseException):
            raise result
        return result

    # ------------------------------------------------------------------ #
    # 运行时（第 8 章 A7：换 provider / 换模型**只经这里**）
    # ------------------------------------------------------------------ #

    def _runtime_client_kwargs(self) -> Dict[str, Any]:
        """当前 client 的构造入参（mini 没有长命 client：每次请求按这些值建连接）。

        ★ 为什么单独留一份：回退要换的 5 个属性如果各改各的，迟早有一处漏改
          （真实版为此专门写了 `_swap_fallback_clients` + 清 `_transport_cache`）。
          这里用"单一写入口 + 派生快照"把这类失同步变成**不可能**。
        """
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "api_mode": self.api_mode,
            "model": self.model,
        }

    def _apply_runtime(self, *, provider: Optional[str] = None, model: Optional[str] = None,
                       base_url: Optional[str] = None, api_mode: Optional[str] = None,
                       api_key: Optional[str] = None,
                       context_length: Optional[int] = None) -> None:
        """**原地换**运行时（回退的唯一写入口）。

        对应真实版 `chat_completion_helpers.py:1973` 的原地替换，换的是：
        `model / provider / base_url / api_mode`（+ mini 的 client 凭据 `api_key`）。
        这里多带一项 **`context_length`**：换了 provider 就是换了模型，而窗口是
        **模型的属性** —— 不跟着换，压缩阈值就会按上家的窗口算。

        真实版还顺手清了 `_transport_cache`（连接复用会带着旧 host）。mini 每次请求
        新建连接（`_TwoTimeoutHTTPSConnection`），所以"没有需要清的缓存"是**结构上成立的**，
        不是漏做 —— 这条差异记在报告里。
        """
        if provider is not None:
            self.provider = provider
        if model is not None:
            self.model = model
        if base_url is not None:
            self.base_url = str(base_url).rstrip("/")
        if api_mode is not None:
            self.api_mode = api_mode
        if api_key is not None:
            self.api_key = api_key
        self._client_kwargs = self._runtime_client_kwargs()
        if context_length:
            self.context_length = int(context_length)
            c = getattr(self, "_compressor_obj", None)
            if c is not None:
                c.update_model(self.model, self.provider)
                c.context_length = int(context_length)

    def _candidate_for(self, runtime: Dict[str, Any]) -> Dict[str, Any]:
        """`_http_post(cand, messages)` 要的候选形状（只取它真的读的 4 个键）。"""
        return {
            "provider": runtime.get("provider", ""),
            "base_url": runtime.get("base_url", ""),
            "api_key": runtime.get("api_key", ""),
            "model": runtime.get("model", ""),
        }

    def _try_activate_fallback(self, *, reason: str = "", status: Any = None) -> bool:
        """★ **唯一的回退触发点**（A7）。换成功返回 True；链耗尽 / 无可换候选返回 False。

        接线的规矩（对应真实版三处触发点的共同收口）：
          · 只在这里换 —— 别的代码不许自己改 provider/model；
          · 换完**立刻**让调用方 `break`（见 `_post_chat_completions`）：
            不再锤刚失败的那个 provider；
          · 链耗尽时**连一句"切换中"都不报**（真实版 `_has_pending_fallback()` 的用法）——
            报一句换不了的话比不报更坏。
        """
        before_provider = self.provider
        if not _fallback.activate_fallback(self, reason=reason):
            return False
        if self.tool_progress_callback:
            self.tool_progress_callback(
                "fallback",
                {"from": before_provider, "status": status if status is not None else reason,
                 "to": self.provider},
                "start",
            )
        self._emit_status(
            f"⚠️ 主模型失败（{reason or 'unknown'}）→ 已切到备用 {self.provider}/{self.model}")
        return True

    def _post_chat_completions(self, api_messages: List[Dict], task_id: str, *,
                               runtime: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """实际发请求。两种调用形态（**刻意分开**）：

        * `runtime is None` —— **主回合请求**：用当前运行时；失败且**可回退**时按序换 provider。
          换完**原地生效**，于是这次重发自动走新 provider（这就是 A7 的"接到失败路径"）。
        * 给了 `runtime`    —— **辅助调用**（A8，压缩摘要）：一次性，用给定运行时；
          **不走回退链**（真实版：aux 是解析期建 client，没有持久 agent 的
          `_fallback_index` / `_fallback_activated` 机制）。

        ★ 判据是**二元**的：只有 `is_fallbackable_status(code)` 为真才换 provider。
          `400` / `422` 这类确定性错误**直接抛** —— 换谁都一样，换了只是把同一个错误
          再收一次（还搭上一次额外的调用）。
        """
        if runtime is not None:
            return self._http_post(self._candidate_for(runtime), api_messages)

        while True:
            try:
                return self._http_post(self._candidate_for(self._client_kwargs), api_messages)
            except ApiHTTPError as exc:
                if not is_fallbackable_status(exc.code):
                    raise                     # 400 / 422 … → 确定性错误，不回退
                # 429 / 5xx / 401 / 403 … → 按序换下一个 provider，然后用新运行时重发
                if not self._try_activate_fallback(reason=f"http_{exc.code}", status=exc.code):
                    raise                     # 链耗尽 / 全被跳过 → 把原始错误交出去

    def _http_post(self, cand: Dict[str, Any], api_messages: List[Dict]) -> Dict[str, Any]:
        """真正发 HTTP 请求。

        ⚠️ 这里**故意不用 `urllib.request.urlopen`**，而是自己搭连接 ——
        原因在 `_TwoTimeoutHTTPSConnection` 的注释里：urlopen 只有一个 timeout，
        同时管连接和读取，而这两件事的合理上限差两个数量级。
        共用一值时只能二选一：短了会掐断正常的长回答，长了坏的地址就白等 75 秒。
        """
        payload = {
            "model": cand["model"],
            "messages": api_messages,
            "tools": model_tools.get_tool_definitions(),
            "tool_choice": "auto",
            "temperature": 0.3,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cand['api_key']}",
            "Content-Length": str(len(body)),
        }

        url = f"{cand['base_url']}/chat/completions"
        parts = urllib.parse.urlsplit(url)

        last_error: Optional[Exception] = None
        for attempt in (1, 2):          # 连接类失败重试一次（可能是瞬时抖动）
            conn = _TwoTimeoutHTTPSConnection(parts.hostname, parts.port or 443)
            try:
                conn.request("POST", parts.path or "/", body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read().decode("utf-8", errors="replace")
                if resp.status >= 400:
                    raise ApiHTTPError(resp.status, raw, url)
                return json.loads(raw)
            except (OSError, http.client.HTTPException) as exc:
                # `socket.timeout` / `ConnectionResetError` / `gaierror` 都落这里
                last_error = exc
                if attempt == 2:
                    raise
            finally:
                conn.close()

        raise last_error  # pragma: no cover — 循环里必然 return 或 raise

    def interrupt(self) -> None:
        """外部（CLI 的 /stop、网关的新消息）调这个来打断当前 turn。"""
        self._interrupt.set()

    # ------------------------------------------------------------------ #
    # 工具执行
    # ------------------------------------------------------------------ #

    def _execute_tool_calls(
        self,
        assistant_message: Dict[str, Any],
        tool_calls: List[Dict],
        messages: List[Dict],
        task_id: str,
    ) -> None:
        """工具执行流程（文档 Tool Execution 那节）：

            对每个 tool_call：
              1. 解析参数
              2. 触发 pre_tool_call 钩子
              3. 危险命令？→ approval_callback 等用户确认
              4. 执行
              5. 触发 post_tool_call 钩子
              6. {"role": "tool", ...} 追加进历史

        并发策略：
          - 单条工具调用 → 主线程直连
          - 多条 → ThreadPoolExecutor 并发
          - 但只要有 **interactive** 工具（比如 clarify 要弹窗），整批退化为串行
          - **结果按原始顺序回填**（跟谁先跑完无关！顺序乱了模型会看不懂）
        """
        parsed: List[Dict[str, Any]] = []
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError as exc:
                args = {}
                parsed.append(
                    {
                        "call": call,
                        "name": name,
                        "args": args,
                        "parse_error": f"参数不是合法 JSON（{exc}）：{raw_args[:200]}",
                    }
                )
                continue
            parsed.append({"call": call, "name": name, "args": args})

        # 有 interactive 工具 → 整批串行
        force_sequential = len(parsed) <= 1 or any(
            model_tools.registry.is_interactive(p["name"]) for p in parsed
        )

        if force_sequential:
            results = [self._run_one_tool(p, task_id) for p in parsed]
        else:
            with ThreadPoolExecutor(max_workers=min(8, len(parsed))) as pool:
                results = list(
                    pool.map(
                        lambda p: self._run_one_tool(p, task_id),
                        parsed,
                    )
                )

        # 6. 按原始顺序回填
        for p, content in zip(parsed, results):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": p["call"].get("id") or p["call"].get("call_id") or "",
                    "content": content,
                }
            )
            # ★ tool_name 只进库、**不进这条消息 dict**：这份 dict 会被原样发给
            #   模型，多带字段在严格 provider 上会 400。（库里分开存（列 tool_name）
            #   是为了检索：`search_messages` 的 LIKE 兜底要扫它。）
            self._persist_message(messages[-1], tool_name=p.get("name"))

    def _run_one_tool(self, p: Dict[str, Any], task_id: str) -> str:
        name, args = p["name"], p["args"]

        if self.tool_progress_callback:
            self.tool_progress_callback(name, args, "start")

        if "parse_error" in p:
            content = f"[error] {p['parse_error']}"
        elif name in INLINE_TOOL_EXECUTORS:
            # ★ agent 级工具：在到达 handle_function_call **之前**就被吃掉了
            content = INLINE_TOOL_EXECUTORS[name](self, args)
        else:
            if model_tools.registry.looks_approval_needed(name, args):
                shown = args.get("command") or json.dumps(args, ensure_ascii=False)
                if not (self.approval_callback and self.approval_callback(shown)):
                    content = "[denied] 用户拒绝执行这条危险命令。"
                    if self.tool_progress_callback:
                        self.tool_progress_callback(name, args, "denied")
                    return content
            content = model_tools.handle_function_call(name, args, task_id=task_id)

        if self.tool_progress_callback:
            self.tool_progress_callback(name, args, "done")
        return content

    # ------------------------------------------------------------------ #
    # agent 私有状态的重建
    # ------------------------------------------------------------------ #

    def refresh_state(self, history: List[Dict]) -> None:
        """让 agent 从对话历史重建私有状态。

        为什么要单独开一个公开方法：**"什么时候重建"是一个真实的设计问题**。
        真实 Hermes 把这件事放在一轮开始时（`agent/turn_context.py`）。
        但"用户想看状态"和"要跑一轮"是两件事——
        桌面 App / TUI 的状态栏不可能为了显示任务清单先跑一轮 API。
        所以这里的规矩是：**重建可以随时被要求，但只由"知道历史"的那一方发起**。
        """
        self._hydrate_todo_store(history)

    def _hydrate_todo_store(self, history: List[Dict]) -> None:
        """从对话历史里把任务清单恢复出来。

        为什么需要它：**网关每条消息都新建一个 AIAgent**，所以内存里的清单必然每次丢。
        丢不要紧——只要每次调用的返回值都是**全量快照 + 版本号**，就能从历史重建。
        （真实 Hermes 在 `agent/turn_context.py` 每轮开始时调这个。）

        ⚠️ **安全边界**（对应真实 Hermes 的 GHSA-5g4g-6jrg-mw3g）：
        **任何"从历史恢复状态"的机制，都是一个可以被伪造输入攻击的口子。**
        所以只认"跟真实 assistant 的 `tool_call` 配对"的那条结果——
        否则有人往对话里塞一条裸的 `role: tool` 消息，就能篡改 agent 的任务清单。
        """
        found = self._latest_todo_response(history)
        if found is None:
            return
        todos, revision = found
        # 只在历史里的版本更新时才覆盖（空清单也算一次权威的"清空"）
        if revision > self.todo.revision:
            self.todo.restore(todos, revision=revision)

    @staticmethod
    def _latest_todo_response(history: List[Dict]) -> Optional[tuple]:
        """找到**最新一条合法的** todo 工具结果 → ``(todos, revision)``；没有就 None。

        单次前向扫描，同时完成两件事：
          · 收集"模型真的发起过哪些 todo 调用"（tool_call_id 集合）
          · 遇到配对上的工具结果就记下来（后面的自然覆盖前面的 → 最后拿到的就是最新的）
        这样"调用在前、结果在后"这个时序要求是**天然满足**的，不用额外检查。
        """
        valid_ids: set = set()
        latest: Optional[tuple] = None

        for msg in history:
            role = msg.get("role")
            if role == "assistant":
                for call in msg.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    if fn.get("name") in AGENT_LEVEL_TOOLS:
                        valid_ids.add(call.get("id") or call.get("call_id") or "")
            elif role == "tool":
                if msg.get("tool_call_id") in valid_ids:
                    parsed = AIAgent._parse_todo_payload(msg.get("content"))
                    if parsed is not None:
                        latest = parsed
        return latest

    @staticmethod
    def _parse_todo_payload(content: Any) -> Optional[tuple]:
        """把一条工具结果解析成 ``(todos, revision)``。解析不出来就放弃，绝不猜。"""
        if not isinstance(content, str) or len(content) > MAX_TODO_RESULT_CHARS:
            return None                      # 超大结果直接丢：可能是伪造的
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None                      # 被截断的 JSON 也走这里
        if not isinstance(data, dict) or "todos" not in data:
            return None
        try:
            revision = int(data.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        todos = data.get("todos")
        return (todos if isinstance(todos, list) else []), revision

    def _log_exception(self, exc: BaseException) -> None:
        """把异常详情写到日志文件；开了 debug 就同时打到 stderr。

        ⚠️ 为什么需要这一步：用户面前只有**一句话**（"agent 异常: ConnectionResetError"），
        而那往往不足以定位。完整 traceback 必须有个去处 —— 否则就是"报了错但没法查"。
        （对应 Hermes 的 `~/.hermes/logs/errors.log`。）
        """
        import traceback

        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        try:
            from hermes_constants import logs_dir

            path = logs_dir() / "errors.log"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n{'=' * 70}\n{time.strftime('%Y-%m-%d %H:%M:%S')}  task={self._current_task_id}\n")
                fh.write(detail)
            where = str(path)
        except Exception:  # noqa: BLE001 - 日志写不进去也不能让 agent 再崩一次
            where = "(日志写入失败)"

        if self.debug:
            print(detail, file=sys.stderr)
        else:
            print(f"（完整 traceback 已写入 {where}；加 --debug 可直接看到）", file=sys.stderr)

    def _build_default_compressor(self):
        """内置默认压缩器（`context.engine == "compressor"` 或发现失败回落时用它）。

        ★ 第 14 章：这一段**逐字保留改造前的构造参数与顺序** ——
          "零配置 = 与改造前逐字节一致"（不变量 I4）就挂在这里。
          它现在是**注**给 `select_engine()` 的工厂（不是被写死调用），
          所以"内置默认怎么构造"依然只有这一处。
        """
        from agent.context_compressor import DEFAULT_CONTEXT_LENGTH, ContextCompressor
        from hermes_cli.config import get_compression_config

        mc = get_compression_config()
        return ContextCompressor(
            context_length=int(getattr(self, "context_length", 0) or DEFAULT_CONTEXT_LENGTH),
            status_callback=self._emit_status,
            micro_compact=bool(mc["micro_compact"]),
            micro_compact_every_n_turns=int(mc["micro_compact_every_n_turns"]),
            micro_compact_defrag_threshold_tokens=int(mc["micro_compact_defrag_threshold_tokens"]),
        )

    def _compressor(self) -> "Any":
        """惰性**查表**解析一个上下文引擎（第一次用到才建）。

        ★ 第 14 章 A5：这是全文**唯一**的引擎构造点，也是本章切的接缝。
          改造前这里是写死 `ContextCompressor(...)`；现在换成
          `select_engine(context.engine, default_factory=…)`：

          | `context.engine` | 结果 |
          |---|---|
          | 没配 / `"compressor"` | 内置默认压缩器（**行为与改造前逐字节一致**） |
          | 已注册的引擎名 | 那个引擎（每次选择真读盘，拿新实例） |
          | 未知名 / 坏目录 | 回落内置 + **有声警告**（经 `_emit_status` 打到用户眼前） |

        ★ 其余调用点（`should_compress` / `compress` / `micro_compact`）**一个字没改**
          —— 它们本来就是多态调用。缓存（`_compressor_obj`）语义不变：
          一个 agent 实例内解析一次。

        ★ 第 10 章：把 `compression.micro_compact` 一族配置读进来（构造处一次性读入，
          压缩器内部不再碰磁盘 —— 保持第 5 章"纯内存"的性质）。这条现在住在
          `_build_default_compressor()` 里。
        """
        c = getattr(self, "_compressor_obj", None)
        if c is None:
            from agent.engine_registry import select_engine
            from hermes_cli.config import get_context_engine_name

            selection = select_engine(
                get_context_engine_name(),
                default_factory=self._build_default_compressor,
            )
            if selection.warning:
                # ★ 回落**必须有声** —— 静默降级是这一章最坏的一种（用户以为换了引擎）。
                #   走 `_emit_engine_warning`（不是 `_emit_status`）：后者在
                #   `status_callback is None` 的网关 / doctor 入口会**静默丢弃**警告。
                self._emit_engine_warning(selection.warning)
            c = selection.engine
            self._compressor_obj = c
        return c

    def _runtime_snapshot(self) -> Dict[str, Any]:
        """当前运行时的**只读快照**（辅助路由的 `main_runtime` 入参，A8）。

        ★ 为什么是快照而不是传 self：解析辅助运行时**不许**反过来动主运行时
          （真实版 `resolve_provider_client(main_runtime=...)` 也是只读传参）。
        """
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "api_mode": self.api_mode,
            "context_length": getattr(self, "context_length", None),
        }

    def _summary_llm_meta(self, system: str, user: str) -> tuple[Optional[str], Optional[str]]:
        """给压缩器用的辅助 LLM 调用 —— ★ 返回 `(文本, finish_reason)`。

        ★ 与 `_summary_llm` **同一套路由/请求路径**（`resolve_auxiliary_runtime(
          TASK_COMPRESSION, ...)` + `self._post_chat_completions(...)`）；区别只在于
          它**额外透出 `finish_reason`**。`_summary_llm` 就是调它、丢掉第二项。

        ★ **为什么要透出 `finish_reason`**：摘要被**截断**时（`finish_reason == "length"`）
          拿到的是一份**半截合并** —— HTTP 200、**有内容**，所以"返回空"那条判据
          **抓不住它**。调用方（微压缩 / 批量压缩）据 `finish_reason.strip() == "length"`
          ⇒ **丢弃结果、不吸收、留着重试**（真实版 `agent/micro_compaction.py:141-146`
          逐字：*A length stop means a partial merge; leave the exchange unabsorbed so a
          later pass retries.*）。不去接这条判据，半截摘要会被当成好摘要存进滚动摘要、
          此后每轮都带着缺一截的摘要跑。

        ★ `finish_reason` 直接从**原始响应**取，**不经** `_parse_response` ——
          后者会顺手记 usage；usage 记账只能算一次，别在两层里各调一次。
          这里只读 `choices[0].finish_reason`，不碰记账。
        """
        from agent.auxiliary import TASK_COMPRESSION, resolve_auxiliary_runtime

        runtime = resolve_auxiliary_runtime(
            TASK_COMPRESSION, main_runtime=self._runtime_snapshot())
        resp = self._post_chat_completions(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "compression", runtime=runtime,
        )
        text, _ = self._parse_response(resp)
        finish = ((resp.get("choices") or [{}])[0].get("finish_reason") or "") or None
        return ((text.get("content") or "").strip() or None), finish

    def _summary_llm(self, system: str, user: str) -> Optional[str]:
        """给压缩器用的辅助 LLM 调用 —— ★ **经辅助路由层（A8）** 选 provider/model。

        ★ 未配置 `auxiliary.compression` 时解析结果是 `auto` ⇒ **继承主 agent 路由**，
          与旧的"复用同一条 HTTP 路径、复用主模型"**逐字等价**（默认行为不变）。
        ★ 配了 `auxiliary.compression.provider` 才走别的 provider —— 这是 A8 接的
          **唯一一个消费者**（B3：不抄 7800 行的辅助适配器丛林）。
        ★ 辅助调用是**一次性**的：用解析出来的运行时直接发一次，**不走 agent 的回退链**
          （真实版：aux 没有持久 agent 的 `_try_activate_fallback` 机制）。
        ★ **签名与行为不变**（已有 1,156 条测试靠它）：路由逻辑搬到 `_summary_llm_meta`，
          这里只取文本。想要 `finish_reason` 的调用方改接那条 *_meta 注入点。
        """
        text, _ = self._summary_llm_meta(system, user)
        return text

    def _persist_message(self, msg: Dict[str, Any], *, tool_name: Optional[str] = None) -> None:
        """把一条消息**追加**进会话库（第 6 章）。

        ★ 为什么是"逐条追加"而不是"整份重写"：重写是 O(整个会话) 字节，
          追加是 O(1) 一行 —— 长会话里差两三个数量级。这也是 JSON 换 SQLite
          最直接的收益（旧的 `save_session()` 每轮把整个会话序列化一遍）。

        ★ 为什么 **system 不进消息表**：提示词存在 `system_prompts` 去重表里
          （会话行只存 sha256），重启时由 agent 重新拼一份当前的。
          这条差异要写明：真实版会用 `_stored_prompt_matches_runtime()` 判断
          "存的那份还对不对"，mini 还没有这道校验 → 已记进第 6 章差距清单。

        ★ 落盘失败**不吞**：它不会让回合崩（那是更坏的选择），但会经 status
          回调浮到用户眼前（"让代价可见"）。
        """
        if self.session_db is None or not isinstance(msg, dict):
            return
        role = msg.get("role")
        if role in (None, "system"):
            return
        try:
            self.session_db.append_message(
                self.session_id, role,
                content=msg.get("content"),
                tool_call_id=msg.get("tool_call_id"),
                tool_calls=msg.get("tool_calls"),
                tool_name=tool_name or msg.get("tool_name"),
                finish_reason=msg.get("finish_reason"),
                reasoning=msg.get("reasoning"),
                reasoning_content=msg.get("reasoning_content"),
            )
        except Exception as exc:                      # noqa: BLE001 —— 见 docstring 第三条
            self._emit_status(f"⚠️ 会话落盘失败：{type(exc).__name__}: {exc}")

    def _preflight_compression(self, messages: List[Dict]) -> None:
        """4. 预检压缩。

        文档：上下文超过模型窗口 50% 就压缩 —— 先把记忆落盘，
        再把中间轮次总结成一段，保留最后 N 条（compression.protect_last_n，默认 20），
        且**工具调用/结果必须成对保留，绝不拆散**。

        实现见 agent/context_compressor.py（四阶段 + 无进展守卫）。
        这里只负责三件事：判定（阈值/锚点）→ 调压缩器 → **原地**改写消息列表。
        """
        c = self._compressor()
        if not c.should_compress(messages):
            used, src = c.current_tokens(messages)
            self._emit_status(f"上下文 {used} token（{src}），未到阈值 {c.threshold_tokens}，不压缩")
            return
        before_len = len(messages)
        # ★ **原地**替换（对应原文 §3.2 的 in-place compaction）：
        #   压缩不换会话 id、不改"这段对话的身份"，只把实时消息列表重写一遍。
        before = [dict(m) for m in messages]
        messages[:] = c.compress(messages, call_llm=self._summary_llm,
                                 call_llm_meta=self._summary_llm_meta)
        # ★ 第 6 章接线：压缩 = **原地压缩的落库形式** ——
        #   老世代整体归档（active=0/compacted=1），压缩后的上下文作为新世代插入。
        #   于是"同一条消息出现在两个世代里"是**正常现象**（真实版明文警告别当重复删）。
        if self.session_db is not None and before != messages:
            try:
                self.session_db.record_compaction(
                    self.session_id, [m for m in messages if m.get("role") != "system"])
            except Exception as exc:                  # noqa: BLE001
                self._emit_status(f"⚠️ 压缩世代落库失败：{type(exc).__name__}: {exc}")
        if len(messages) == before_len:
            self._emit_status("压缩未改变消息条数（只剪了内容）")

    def _micro_compact_after_turn(self, messages: List[Dict]) -> None:
        """每回合收尾后的微压缩（第 10 章）。

        对应真实版 `agent/turn_finalizer.py:272::_micro_compact_after_turn` ——
        每回合收尾调一次，把最老的一个未吸收交换折进滚动摘要，**在落盘之前**。

        ★ 三个门（真实版同款）：
          · 编译器没开微压缩 ⇒ 直接返回（**零 LLM 调用**；默认关是本章的立章前提）；
          · 摘要调用走 A8 辅助路由（`self._summary_llm`），凭据作用域-aware；
          · 只有**真的**吸收了（`_micro_dirty`）才落库 —— **失败不落库**。
        """
        c = self._compressor()
        if not getattr(c, "micro_compact_enabled", False):
            return
        before = len(messages)
        try:
            compacted = c.micro_compact(messages, call_llm=self._summary_llm,
                                        call_llm_meta=self._summary_llm_meta)
        except Exception as exc:                      # noqa: BLE001 —— 微压缩不该把回合弄崩
            self._emit_status(f"⚠️ 微压缩失败：{type(exc).__name__}: {exc}")
            return
        if isinstance(compacted, list) and compacted:
            messages[:] = compacted
        # ★ 落库只在\"真的改了\"时发生（吸收或 defrag）。失败路径 `_micro_dirty` 为 False。
        if getattr(c, "_micro_dirty", False):
            self._persist_micro_compaction(messages)
            c._micro_dirty = False
            # ★ 文案按**当次真的走了哪条分支**标（压缩器记的 `_micro_last_action`），
            #   不按"消息条数变没变"反推 —— supersede + 相邻用户回合合并会让一次**真正的
            #   吸收**也保持条数不变（旧写法就是把这种吸收误报成 defrag）。
            if getattr(c, "_micro_last_action", "") == "defrag":
                self._emit_status("微压缩：defrag 重压了滚动摘要（条数不变）")
            else:
                self._emit_status(f"微压缩：吸收 1 个交换 · {before} → {len(messages)} 条消息")

    def _persist_micro_compaction(self, messages: List[Dict]) -> None:
        """把微压缩后的上下文**原子落库**（第 6 章 `record_compaction` 行级归档）。

        ★ 为什么不落库会坏：旧交换行仍 `active=1`，下次 `--resume` 会**同时**载入
          \"旧交换 + 新摘要\" ⇒ 摘要吸收的东西**双重加载**（真实版
          `_sync_micro_compact_to_db` 的注释逐字理由，`:343-346`）。
        ★ 与真实版的差异（清单 B1）：真实版用 rewind-style **部分**归档
          （`archive_and_compact(tail_count=len-1)`）；mini 用第 6 章既有的
          `record_compaction`（老世代整份归档 + 新世代重插）。同样原子、同样不双载 ——
          只是粒度不同（mini 的 `record_compaction` 已把 system 挡在消息表之外，故排除）。
        """
        if self.session_db is None:
            return
        try:
            self.session_db.record_compaction(
                self.session_id, [m for m in messages if m.get("role") != "system"])
        except Exception as exc:                      # noqa: BLE001
            self._emit_status(f"⚠️ 微压缩世代落库失败：{type(exc).__name__}: {exc}")
