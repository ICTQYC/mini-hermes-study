"""消息行的读写（对应 Hermes 的 `hermes_state_messages.py`）。

★ 抄的第一个决策：**一个签名同时定义三个契约**。
  `append_message` 的形参名 = 表列名 = 消息 dict 的键名，于是

      row = {k: v for k, v in locals().items() if k in _MESSAGE_COLUMNS}

  就完成了"把一次调用变成一行插入"。真实版的原话（`hermes_state_messages.py:291`）：

      msg = dict(locals())  # every keyword above is a message-dict field of the same name

★ 抄的第二个决策：**列清单从 `SCHEMA_SQL` 推出来**（不是手抄一份）。
  加了列忘了改这里 = 类型错误；从 SCHEMA_SQL 推 = 不可能不一致。

★ 抄的第三个决策：**归档行（active / compacted）**。
  真实版就一行 SQL（`hermes_state_messages.py:44`）：

      UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ? AND active = 1

  压缩发生一次 = 老世代整体退休（active=0）、保留的上下文作为**新行**插入（active=1）。
  于是"同一条消息同时出现在两个世代里"是**正常现象**，不是重复数据。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence

from hermes_state_common import SCHEMA_SQL
from hermes_state_schema import parse_schema_columns

# 列清单的唯一事实来源 = SCHEMA_SQL（见模块 docstring 第二条）
_MESSAGE_COLUMNS = tuple(parse_schema_columns(SCHEMA_SQL)["messages"])

# 读回"该发给模型的形状"时要还原的字段。
#
# ★ `tool_name` **不在**这里 —— 它只用于检索（LIKE 兜底要扫这一列），
#   不属于消息 dict 的形状。这条区别踩过一次：一开始把 tool_name 塞进了
#   工具结果的消息 dict，而那份 dict 会被**原样发给模型**（严格 provider 会 400）。
_WIRE_FIELDS = ("role", "content", "tool_calls", "tool_call_id")

# mini 的内存历史里会带 `reasoning`（`_parse_response` 存进去的思考文本），
# 所以恢复时要保持**同一个形状** —— 否则"恢复后的会话"和"没退出的会话"发出去的
# 请求不一样（同一份历史两种字节，缓存与行为都会分叉）。
_PASSTHROUGH_FIELDS = ("reasoning",)


class SessionMessagesMixin:
    """给 `SessionDB` 用的消息部分。"""

    _conn: sqlite3.Connection

    # ── 写 ─────────────────────────────────────────────────────────────
    def append_message(self, session_id: str, role: str, content: Optional[str] = None,
                       tool_call_id: Optional[str] = None, tool_calls: Any = None,
                       tool_name: Optional[str] = None, token_count: Optional[int] = None,
                       finish_reason: Optional[str] = None, reasoning: Optional[str] = None,
                       reasoning_content: Optional[str] = None,
                       timestamp: Optional[float] = None,
                       active: int = 1, compacted: int = 0) -> int:
        """追加一条消息，返回行 id，并顺带把会话的计数加上去。

        `tool_calls` 传列表/字典会被序列化成 JSON 字符串（列类型是 TEXT）。
        """
        row = {k: v for k, v in locals().items() if k in _MESSAGE_COLUMNS}
        row["session_id"] = session_id
        row["timestamp"] = float(timestamp if timestamp is not None else time.time())
        if isinstance(row.get("tool_calls"), (list, dict)):
            row["tool_calls"] = json.dumps(row["tool_calls"], ensure_ascii=False)
        n_calls = len(tool_calls) if isinstance(tool_calls, (list, tuple)) else 0

        def _do(conn: sqlite3.Connection) -> int:
            cols = list(row)
            cur = conn.execute(
                f"INSERT INTO messages ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                [row[c] for c in cols],
            )
            msg_id = int(cur.lastrowid or 0)
            conn.execute(
                "UPDATE sessions SET message_count = message_count + 1,"
                " tool_call_count = tool_call_count + ? WHERE id = ?",
                (n_calls, session_id),
            )
            return msg_id

        return self._execute_write(_do)

    def archive_active_rows(self, session_id: str) -> int:
        """把该会话**当前活着的行**整体归档，返回归档条数。

        这就是原地压缩的"退休"动作（真实版 `hermes_state_messages.py:44` 那行 SQL）。
        """
        def _do(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "UPDATE messages SET active = 0, compacted = 1 "
                "WHERE session_id = ? AND active = 1",
                (session_id,),
            )
            return int(cur.rowcount or 0)

        return self._execute_write(_do)

    def record_compaction(self, session_id: str, compressed: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        """**原地压缩落库**：老世代归档 → 压缩后的上下文作为新世代插入。

        这是 `active` / `compacted` 两列的**消费者**（不然它们只是摆设）。
        返回 `{"archived": n, "written": m}` 给调用方观察。

        ⚠️ 结果就是："受保护的消息"会在**两个世代里各出现一次**，
           内容与时间戳完全相同 —— 这是设计，别当重复数据删（真实版明文警告）。
        """
        reported: Dict[str, int] = {}

        def _do(conn: sqlite3.Connection) -> Dict[str, int]:
            cur = conn.execute(
                "UPDATE messages SET active = 0, compacted = 1 "
                "WHERE session_id = ? AND active = 1",
                (session_id,),
            )
            archived = int(cur.rowcount or 0)
            written = 0
            for msg in compressed:
                row = {k: v for k, v in msg.items() if k in _MESSAGE_COLUMNS}
                row["session_id"] = session_id
                row.setdefault("role", "assistant")
                row["timestamp"] = float(row.get("timestamp") or time.time())
                row["active"] = 1
                row["compacted"] = 0
                if isinstance(row.get("tool_calls"), (list, dict)):
                    row["tool_calls"] = json.dumps(row["tool_calls"], ensure_ascii=False)
                cols = list(row)
                conn.execute(
                    f"INSERT INTO messages ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' * len(cols))})",
                    [row[c] for c in cols],
                )
                # 注意：这里**不**加 message_count —— 那是"用户看得见的轮次"计数，
                # 压缩只是把内部表示换了个世代。
                written += 1
            reported["archived"], reported["written"] = archived, written
            return dict(reported)

        return self._execute_write(_do)

    # ── 读 ─────────────────────────────────────────────────────────────
    def get_messages(self, session_id: str, *, include_inactive: bool = False,
                     include_compacted: bool = False) -> List[Dict[str, Any]]:
        """原始行（**带全部元数据**，`tool_calls` 保持 JSON 字符串）。

        ★ 和 `get_messages_as_conversation` 是两种用途，别混：
          - 这个 = "库里长什么样"（排查、导出、看世代）
          - 那个 = "要发给模型的样子"（角色 + 内容）
        """
        return [dict(r) for r in self._fetch_rows([session_id], include_inactive, include_compacted)]

    def get_messages_as_conversation(self, session_id: str, *,
                                     include_ancestors: bool = False,
                                     include_inactive: bool = False,
                                     include_compacted: bool = False,
                                     include_reasoning: bool = False) -> List[Dict[str, Any]]:
        # include_reasoning=True 时额外带回 `reasoning_content`（第 2 章那个"出网前
        # 复制一份"的字段）。默认 False：mini 的内存历史用的是 `reasoning`。
        """读成"能喂回模型/历史"的形状。

        ★ 抄的核心约束（真实版 `hermes_state_messages.py:951` docstring 逐字）：
          `the model-fed restore must not regrow what compaction summarized away`
          —— 喂给模型的那份**默认只看活行**（`active = 1`），
             已被压缩掉的归档世代不许"复活"回来。

        `include_ancestors=True` 时会**按血缘把祖先会话的消息一起取回来**
        （压缩轮转会让历史散在一条链上）。
        """
        ids = self.session_lineage(session_id) if include_ancestors else [session_id]
        rows = self._fetch_rows(ids, include_inactive, include_compacted)
        out: List[Dict[str, Any]] = []
        fields = list(_WIRE_FIELDS) + list(_PASSTHROUGH_FIELDS)
        if include_reasoning:
            fields.append("reasoning_content")
        for row in rows:
            # ⚠️ sqlite3.Row **没有** .get()（只有 `row["键"]` 与 `.keys()`）——
            #    这里老实用 keys() 判断，别照 dict 的习惯写。
            raw = dict(row)
            msg = {k: raw[k] for k in fields if raw.get(k) is not None}
            if isinstance(msg.get("tool_calls"), str):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except json.JSONDecodeError:
                    pass
            out.append(msg)
        return out

    # ── 内部 ───────────────────────────────────────────────────────────
    def _fetch_rows(self, session_ids: Sequence[str], include_inactive: bool,
                    include_compacted: bool) -> List[sqlite3.Row]:
        """取行。三个档位的语义（对应真实版 `_DISPLAY_ACTIVE_CLAUSE` 那一带）：

        | 参数组合 | 取哪些行 | 谁用 |
        |---|---|---|
        | 默认 | `active = 1` | **喂模型**（历史重放） |
        | `include_compacted=True` | `active = 1 OR compacted = 1` | **展示/排查**（多世代） |
        | `include_inactive=True` | 全部行 | 审计（含被回退的行） |
        """
        if not session_ids:
            return []
        placeholders = ", ".join("?" * len(session_ids))
        where = [f"session_id IN ({placeholders})"]
        if include_inactive:
            pass                                     # 全都取
        elif include_compacted:
            where.append("(active = 1 OR compacted = 1)")
        else:
            where.append("active = 1")
        sql = (f"SELECT * FROM messages WHERE {' AND '.join(where)} "
               f"ORDER BY timestamp, id")
        return self._conn.execute(sql, list(session_ids)).fetchall()

    def count_messages(self, session_id: str, *, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM messages WHERE session_id = ?"
        if active_only:
            sql += " AND active = 1"
        return int(self._conn.execute(sql, (session_id,)).fetchone()[0])
