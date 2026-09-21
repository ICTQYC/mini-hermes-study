"""agent 级工具的状态容器：带版本号的任务清单。

对应 Hermes 的 `tools/todo_tool.py`。核心设计（每一条都是被真实问题逼出来的）：

  1. **状态只在内存里** —— 一份清单，存在 AIAgent 实例上。
     但**网关每条消息都会新建一个 AIAgent 实例**，所以内存里的东西必然每次都丢。
  2. **所以必须能从历史重建** —— 每次调用都返回**全量列表 + 版本号**，
     于是从对话历史往前翻，找到最后一条结果，就能把整份清单恢复出来。
     这就是 schema 里那句 `Always returns the full current list` 的真实原因：
     **不是为了人好读，是为了可恢复。**
  3. **单调递增的 revision** —— 判断"历史里的版本"和"内存里的版本"谁新；
     桌面 App / TUI 也靠它丢弃过期的界面更新。
  4. **按 `id` 更新，不按文本** —— 否则两个同名任务会被一起改。
  5. **有上限** —— 清单会被重新注入上下文，无上限就等于把压缩的成果抵消掉。

⚠️ 安全：从历史恢复状态 = 开了一个可被伪造输入攻击的口子。
   见 `run_agent.py` 的 `_hydrate_todo_store`——只认跟真实 assistant tool_call 配对的结果。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}

# 上限：清单会被重新注入上下文（见下），无上限就等于把压缩省下的空间又花掉
MAX_TODO_CONTENT_CHARS = 4000
MAX_TODO_ITEMS = 256
# 从历史里恢复时，单条工具结果的最大长度 —— 伪造的超大结果要在解析前就被丢掉
MAX_TODO_RESULT_CHARS = 512_000
_TRUNCATION_MARKER = "… [truncated]"

# 终端上看到的勾选符号
_STATUS_MARKERS = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]", "cancelled": "[~]"}

# ⚠️ 上下文压缩之后，清单会被**重新注入**成一条合成消息。
# 这个 header 是压缩器用来区分「合成的清单行」和「真实用户消息」的稳定锚点。
TODO_INJECTION_HEADER = "[你当前的任务清单已在上下文压缩中保留]"


class TodoStore:
    """agent 私有任务清单。列表顺序 = 优先级。条目形如
    ``{id, content, status, parent?}``，``parent`` 指向父条目 id（做子任务）。"""

    def __init__(self) -> None:
        self._items: List[Dict[str, str]] = []
        self._revision = 0

    # ---------------------------------------------------------------- 读
    def read(self) -> List[Dict[str, str]]:
        """返回条目副本，调用方改不动内部状态。"""
        return [dict(i) for i in self._items]

    @property
    def revision(self) -> int:
        return self._revision

    def snapshot(self) -> Dict[str, Any]:
        return {"todos": self.read(), "revision": self._revision}

    # ---------------------------------------------------------------- 写
    def write(self, todos: Optional[List[Dict[str, Any]]], merge: bool = False) -> List[Dict[str, str]]:
        """整表替换（默认）或按 id 合并更新；**返回写完后的全量列表**。

        只有内容真的变了才 +1 版本号 —— 否则"模型重复写了一遍一样的东西"
        会被误判成一次新更新，UI 和恢复逻辑都要跟着做无用功。
        """
        if todos is None:                      # 不传 = 只读
            return self.read()

        before = self.read()
        if merge:
            self._merge(todos)
        else:
            self._items = self._validate_all(todos)
        del self._items[MAX_TODO_ITEMS:]        # 保留优先级靠前的，历史重放不能无限长
        self._sanitize_parents()
        if self._items != before:
            self._revision += 1
        return self.read()

    def restore(self, todos: List[Dict[str, Any]], *, revision: int = 0) -> List[Dict[str, str]]:
        """用一份**可信快照**恢复状态，且**不制造新的版本号**。

        和 write() 的区别：write 是模型在改清单（要 +1 版本号），
        restore 是把历史里的既成事实搬回来（版本号照抄，否则下次恢复会判错新旧）。
        """
        self._items = self._validate_all(todos)
        self._revision = max(0, int(revision or 0))
        return self.read()

    # ---------------------------------------------------------------- 内部
    def _merge(self, todos: List[Dict[str, Any]]) -> None:
        """按 id 更新已存在的条目（只改传来的字段），没见过的追加。"""
        existing = {item["id"]: item for item in self._items}
        for raw in self._dedupe_by_id(todos):
            item_id = str(raw.get("id", "")).strip()
            if not item_id:
                continue                        # 没有 id 就没法合并
            cur = existing.get(item_id)
            if cur is None:
                self._items.append(self._validate(raw))
            else:
                if raw.get("content") is not None:
                    cur["content"] = _clip(str(raw["content"]))
                if raw.get("status"):
                    cur["status"] = self._valid_status(raw["status"])
                if raw.get("parent") is not None:
                    cur["parent"] = str(raw["parent"])

    def _validate_all(self, todos: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        return [self._validate(t) for t in self._dedupe_by_id(todos or [])]

    @staticmethod
    def _validate(raw: Dict[str, Any]) -> Dict[str, str]:
        item = {
            "id": str(raw.get("id", "")).strip(),
            "content": _clip(str(raw.get("content", "")).strip()),
            "status": TodoStore._valid_status(raw.get("status")),
        }
        if raw.get("parent"):
            item["parent"] = str(raw["parent"])
        return item

    @staticmethod
    def _valid_status(value: Any) -> str:
        s = str(value or "pending").strip()
        return s if s in VALID_STATUSES else "pending"

    @staticmethod
    def _dedupe_by_id(todos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """同一个 id 出现多次时，**后面的赢** —— 模型一次给了两份更新，以最新的为准。"""
        seen: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        anonymous: List[Dict[str, Any]] = []
        for t in todos:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id", "")).strip()
            if not tid:
                anonymous.append(t)
                continue
            if tid not in seen:
                order.append(tid)
            seen[tid] = t
        return [seen[i] for i in order] + anonymous

    def _sanitize_parents(self) -> None:
        """父条目不存在（或成环）就把 parent 去掉 —— 宁可拍平，不要渲染出孤儿。"""
        ids = {i["id"] for i in self._items if i["id"]}
        for item in self._items:
            parent = item.get("parent")
            if not parent or parent not in ids or parent == item["id"]:
                item.pop("parent", None)

    # ---------------------------------------------------------------- 渲染
    def render(self) -> str:
        """给人（和模型）看的清单。子任务缩进在父条目下面。"""
        if not self._items:
            return "(清单为空)"
        children: Dict[str, List[Dict[str, str]]] = {}
        roots: List[Dict[str, str]] = []
        for item in self._items:
            if item.get("parent"):
                children.setdefault(item["parent"], []).append(item)
            else:
                roots.append(item)

        lines: List[str] = []

        def emit(item: Dict[str, str], depth: int) -> None:
            marker = _STATUS_MARKERS.get(item["status"], "[?]")
            lines.append(f"{'  ' * depth}{marker} {item['id']}. {item['content']}")
            for child in children.get(item["id"], []):
                emit(child, depth + 1)

        for root in roots:
            emit(root, 0)
        return "\n".join(lines)

    def to_payload(self) -> str:
        """工具返回值 —— **JSON 字符串**。

        Hermes 的规矩：所有工具 handler 都返回 JSON 字符串（见 tools/AGENTS.md）。
        对 todo 来说这一条是硬需求：返回值要能被**机器解析**（从历史恢复靠它），
        不能只是一坨给人看的文本。
        """
        payload = json.dumps(self.snapshot(), ensure_ascii=False)
        if len(payload) > MAX_TODO_RESULT_CHARS:
            payload = payload[:MAX_TODO_RESULT_CHARS] + _TRUNCATION_MARKER
        return payload


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TODO_CONTENT_CHARS else text[:MAX_TODO_CONTENT_CHARS] + _TRUNCATION_MARKER


def render_for_injection(store: "TodoStore") -> str:
    """生成"压缩之后重新注入"的那条合成消息。

    对应 Hermes 的 `format_for_injection()`：清单是**少数几个能穿过上下文压缩活下来的东西**，
    因为压缩会把中间轮次总结掉——而那些中间轮次里就有"我原本要做什么"。
    header 保持字节稳定，压缩器才能认出这条是自己造的、不是用户说的。
    （真正的注入逻辑在压缩那一章实现，这里先把渲染和锚点立好。）
    """
    return f"{TODO_INJECTION_HEADER}\n{store.render()}"


# ==================================================================== #
# 工具 schema
# ==================================================================== #
#
# ⚠️ 注意下面这段 description：里面**一半是给模型的纪律**，而且代码一行都没强制。
#    模型才不管"是不是只有一个 in_progress"，`TodoStore` 也无从知道
#    "你标 completed 的时候到底做完没有"。所以纪律只能写进描述里。
#    **工具描述不是给人看的文档，是给模型的指令。**
#
TODO_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "todo_list",
        "description": (
            "维护多步任务（3 步以上）的任务清单。整个替换（默认）或按 id 合并更新（merge=true）。"
            "不传 todos 就是读取当前清单。\n"
            "纪律：\n"
            "- 列表顺序即优先级。\n"
            "- **同一时刻只允许一个 in_progress。**\n"
            "- 大阶段用 parent 拆成子任务。\n"
            "- **只有确认做完了才能标 completed，绝不凭意图标。**\n"
            "- 某步失败就标 cancelled，并新增一条修正后的条目。\n"
            "- 遇到「这 N 个都处理一遍」的任务，**把每一个都单独列成一条**，别合并成一条，"
            "否则会出现「以为全做完了其实漏了」的情况。\n"
            "每次调用都返回完整清单。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "要写入的任务条目。不传则只读取当前清单。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string", "description": "任务描述"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                            },
                            "parent": {"type": "string", "description": "父条目的 id，用来做子任务"},
                        },
                        "required": ["id", "content", "status"],
                    },
                },
                "merge": {
                    "type": "boolean",
                    "description": "true：按 id 更新已有条目、追加新条目。false（默认）：整表替换。",
                    "default": False,
                },
            },
        },
    },
}

# agent 级工具的 schema 表。key 必须和 run_agent.py 的 INLINE_TOOL_EXECUTORS 对齐
# （有一致性测试盯着，见 tests/test_loop_mechanisms.py）。
AGENT_LEVEL_TOOLS = {"todo_list": TODO_LIST_SCHEMA}
