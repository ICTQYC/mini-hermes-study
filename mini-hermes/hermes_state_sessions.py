"""会话行的读写 + 系统提示词去重（对应 Hermes 的 `hermes_state_sessions.py`）。

★ 抄的第一个决策：**upsert 永不覆盖先写者**。
  真实版 docstring（`hermes_state_sessions.py:284-285`）逐字：

      Upsert a session row, never overwriting what an earlier writer set (the gateway
      creates a bare row before create_session carries the real model/prompt)

  为什么会有"先写一行空壳"这件事：会话是在**知道要跑**的时候就被建出来的，
  但那时模型/提示词/父会话还可能没定。与其等齐了再插，不如先插一行、后面补 ——
  前提就是"补的时候不许把已有值冲掉"。SQLite 的做法很省事：
  `ON CONFLICT DO UPDATE SET` 里**只列要保留的列**，没列到的列自动保留旧值。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

# upsert 时要"谁先写谁赢"的列（对应真实版 `_UPSERT_KEEP_EXISTING_SQL` 的列清单）
_KEEP_EXISTING_COLS = ("model", "model_config", "system_prompt_hash",
                       "parent_session_id", "title", "started_at")


class SessionSessionsMixin:
    """给 `SessionDB` 用的会话部分。"""

    _conn: sqlite3.Connection

    # ── 系统提示词去重（真实版 `hermes_state.py:489-497`）────────────────
    def _store_system_prompt(self, conn: sqlite3.Connection,
                             system_prompt: Optional[str]) -> Optional[str]:
        """把提示词存进去重表，返回 sha256 哈希（同一份提示词只存一次）。

        ★ 为什么值得单开一张表：系统提示词是**每轮都发、每个会话都一样**的大文本
          （真实 Hermes 那份有几万字符）。会话行里再存一份 = 同样的字节存 N 遍。
        """
        if system_prompt is None:
            return None
        prompt_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO system_prompts (hash, prompt) VALUES (?, ?)",
            (prompt_hash, system_prompt),
        )
        return prompt_hash

    # ── 建 / 结束 / 重开 ───────────────────────────────────────────────
    def create_session(self, session_id: str, source: str, *,
                       model: Optional[str] = None,
                       model_config: Optional[Any] = None,
                       system_prompt: Optional[str] = None,
                       parent_session_id: Optional[str] = None,
                       title: Optional[str] = None,
                       session_key: Optional[str] = None,
                       started_at: Optional[float] = None) -> str:
        """建会话（已存在则**只补空缺**，不覆盖已有值）。

        `session_key`（第 7 章）：网关的路由键。为什么落库而不是只放内存——
        因为**网关会重启**，而"同一个聊天对应同一个会话"这件事必须活过重启。
        cli 会话不传（保持 NULL）。
        """
        keep = ",\n                       ".join(
            f"{c} = COALESCE(sessions.{c}, excluded.{c})" for c in _KEEP_EXISTING_COLS
        )
        sql = f"""
            INSERT INTO sessions (id, source, model, model_config, system_prompt_hash,
                                  parent_session_id, started_at, title, session_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                {keep}
        """

        def _do(conn: sqlite3.Connection) -> str:
            conn.execute(sql, (
                session_id, source, model,
                json.dumps(model_config, ensure_ascii=False) if isinstance(model_config, dict) else model_config,
                self._store_system_prompt(conn, system_prompt),
                parent_session_id,
                float(started_at if started_at is not None else time.time()),
                title,
                session_key,
            ))
            return session_id

        return self._execute_write(_do)

    def latest_session_id_for_key(self, session_key: str, *,
                                  source: Optional[str] = None) -> Optional[str]:
        """按路由键找回**当前**会话（只找没结束的，时间倒序第一条）。

        ★ 为什么必须排除已结束的：`/new` 结束掉的会话如果还被"找回"，
        用户就会发现自己重置之后又回到了旧对话里 ——
        这是"重启连续性"这个功能最容易做错的地方。
        """
        sql = ("SELECT id FROM sessions WHERE session_key = ? AND ended_at IS NULL")
        params: List[Any] = [session_key]
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY started_at DESC LIMIT 1"
        row = self._conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row is not None else None

    # ══ 第 8 章：会话生命周期状态（对应真实版 SessionEntry 的那批标志位）══════
    def route_entry(self, session_key: str) -> Optional[Dict[str, Any]]:
        """取这个会话键**最近一条**会话行（**不管它是否已结束**）+ 它的生命周期状态。

        ★ 为什么与 `latest_session_id_for_key()` 分开：那个是给"**找回一个还能用的会话**"
        （`--resume`）用的，会**过滤掉已结束的**；而状态机要的是"**上一次那条是什么状态**" ——
        已结束本身就是它需要看到的一个事实（结束过 → 该开新的）。
        两个问题问的不是同一件事，所以两个方法。
        """
        row = self._conn.execute(
            "SELECT id, ended_at, end_reason, suspended, resume_pending, resume_reason,"
            " is_fresh_reset, updated_at, started_at"
            " FROM sessions WHERE session_key = ?"
            " ORDER BY started_at DESC, rowid DESC LIMIT 1", (session_key,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["open"] = row["ended_at"] is None
        return out

    def set_route_state(self, session_id: str, *, suspended: Optional[bool] = None,
                        resume_pending: Optional[bool] = None,
                        resume_reason: Optional[str] = None,
                        is_fresh_reset: Optional[bool] = None,
                        touch_activity: bool = False) -> None:
        """只更新调用方**给出来的**那些字段（`None` = 不动这一列）。

        ★ 为什么不是"一次写全"：状态位是**按事件分别置位**的（`/stop` 置 suspended、
        drain 超时置 resume_pending…）。写全的话，每个调用方都得先读一遍再写回去 ——
        两次之间别的事件插进来，就会**把别人的置位抹掉**（经典的丢更新）。
        """
        sets, params = [], []
        if suspended is not None:
            sets.append("suspended = ?")
            params.append(1 if suspended else 0)
        if resume_pending is not None:
            sets.append("resume_pending = ?")
            params.append(1 if resume_pending else 0)
            sets.append("last_resume_marked_at = ?")
            params.append(time.time() if resume_pending else None)
        if resume_reason is not None:
            sets.append("resume_reason = ?")
            params.append(resume_reason)
        if is_fresh_reset is not None:
            sets.append("is_fresh_reset = ?")
            params.append(1 if is_fresh_reset else 0)
        if touch_activity:
            sets.append("updated_at = ?")
            params.append(time.time())
        if not sets:
            return
        params.append(session_id)
        self._execute_write(lambda conn: conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", tuple(params)))

    def recently_active_session_ids(self, max_age_seconds: float = 120.0) -> List[str]:
        """最近 `max_age_seconds` 秒内活跃过的会话（崩溃恢复的输入）。

        ★ 判据用 `updated_at`，而**不是** `started_at`：一个开了三天、五分钟前还在说话的
        会话是"活跃"的；一个三天没动过的会话不是 —— 恢复只该管"刚刚在跑的"。
        `updated_at` 为 NULL（第 8 章之前建的老行）视为不活跃。
        """
        cutoff = time.time() - float(max_age_seconds)
        # ★ 只挑**开着的**会话：已结束的会话不需要"恢复"（真实版靠"遍历内存里的
        #   entries 索引"天然排除，mini 是查库，所以要显式写出来）。
        rows = self._conn.execute(
            "SELECT id FROM sessions WHERE updated_at IS NOT NULL AND updated_at >= ?"
            " AND ended_at IS NULL"
            " ORDER BY updated_at DESC", (cutoff,)).fetchall()
        return [str(r[0]) for r in rows]

    def session_key_for_id(self, session_id: str) -> Optional[str]:
        """按会话 id 反查它的路由键（启动恢复要"从会话回到键"才能记连续重启计数）。"""
        row = self._conn.execute("SELECT session_key FROM sessions WHERE id = ?",
                                 (session_id,)).fetchone()
        return None if row is None else row[0]

    def end_session(self, session_id: str, end_reason: str) -> None:
        """结束会话（真实版 `hermes_state_sessions.py:442`）。"""
        self._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
            (time.time(), end_reason, session_id),
        ))

    def reopen_session(self, session_id: str) -> None:
        """重开会话：把 ended_at / end_reason 清掉（真实版 `:458`）。"""
        self._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
            (session_id,),
        ))

    # ── 读 ─────────────────────────────────────────────────────────────
    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row is not None else None

    def latest_session_id(self) -> Optional[str]:
        """最近一个**有消息**的会话（给 `--resume` 用）。

        ★ 为什么排除"没有消息的会话"：会话行是在跑之前就建出来的（见模块 docstring），
          所以库里会躺着一些空壳。恢复时挑到空壳，用户会以为"历史丢了"。
        """
        row = self._conn.execute(
            "SELECT id FROM sessions WHERE message_count > 0 "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return None if row is None else str(row[0])

    def list_sessions(self, limit: int = 10, source: Optional[str] = None) -> List[Dict[str, Any]]:
        """最近会话 + 预览（预览 = 这个会话**第一条用户消息**的前 63 个字符）。

        预览那条子查询抄自官方文档的"Recent Sessions with Preview"示例 SQL：
        取不到就退回 `started_at` 当"最后活动时间"。
        """
        sql = """
            SELECT s.id, s.source, s.session_key, s.title, s.started_at, s.message_count,
                   s.end_reason,
                   COALESCE(
                       (SELECT SUBSTR(m.content, 1, 63) FROM messages m
                         WHERE m.session_id = s.id AND m.role = 'user'
                           AND m.content IS NOT NULL AND m.active = 1
                         ORDER BY m.timestamp, m.id LIMIT 1),
                       ''
                   ) AS preview,
                   COALESCE(
                       (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                       s.started_at
                   ) AS last_active
              FROM sessions s
             WHERE (? IS NULL OR s.source = ?)
             ORDER BY s.started_at DESC, s.rowid DESC
             LIMIT ?
        """
        rows = self._conn.execute(sql, (source, source, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def session_lineage(self, session_id: str) -> List[str]:
        """血缘：从最早的祖先到 `session_id` 本身（含两端）。

        ★ 为什么需要它：上下文压缩触发**会话轮转**时，新会话会把老会话设成父亲。
          于是"这个会话的历史"其实是**一条链**上的历史 —— 只读最后一段会丢掉前面。
        （对应官方文档的 "Session Lineage" 递归查询）
        """
        rows = self._conn.execute(
            """
            WITH RECURSIVE lineage AS (
                SELECT id, parent_session_id, started_at FROM sessions WHERE id = ?
                UNION ALL
                SELECT s.id, s.parent_session_id, s.started_at
                  FROM sessions s JOIN lineage l ON s.id = l.parent_session_id
            )
            SELECT id FROM lineage ORDER BY started_at
            """,
            (session_id,),
        ).fetchall()
        return [str(r[0]) for r in rows]

    # ── 用量计数（mini 的 `meta.usage` 落到会话行上）──────────────────────
    def record_usage(self, session_id: str, *, input_tokens: int = 0,
                     output_tokens: int = 0, api_calls: int = 0) -> None:
        self._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET input_tokens = input_tokens + ?,"
            " output_tokens = output_tokens + ?,"
            " api_call_count = api_call_count + ? WHERE id = ?",
            (int(input_tokens), int(output_tokens), int(api_calls), session_id),
        ))

    def get_stored_system_prompt(self, session_id: str) -> Optional[str]:
        """取这个会话**存盘时的**系统提示词正文（经 `system_prompt_hash` 连过去）。

        用途：`--resume` 时和"刚拼出来的那份"比一比 —— 提示词在两次运行之间
        变过的话，恢复出来的会话其实换了一个人格/环境（`SOUL.md` / `AGENTS.md`
        可能改了），这件事必须**说出来**，而不是让用户自己发现。
        （真实版对应 `_stored_prompt_matches_runtime()`，用运行时环境块的边界当锚点。）
        """
        row = self._conn.execute(
            "SELECT sp.prompt FROM sessions s JOIN system_prompts sp"
            " ON sp.hash = s.system_prompt_hash WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    # ── 标题（真实版 `hermes_state_titles.py`）──────────────────────────
    def set_session_title(self, session_id: str, title: str) -> bool:
        """设标题。非 NULL 标题之间必须唯一 —— 撞了返回 False，不抛。

        ★ 唯一性由**部分索引**保证（`WHERE title IS NOT NULL`）：
          这样"没有标题"的会话可以有无数个，而一旦起了名就不能重。
        """
        try:
            self._execute_write(lambda conn: conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ?", (title, session_id)))
        except sqlite3.IntegrityError:
            return False
        return True

    def resolve_session_by_title(self, title: str) -> Optional[str]:
        """按标题找会话（血缘里最新的那个）。"""
        row = self._conn.execute(
            "SELECT id FROM sessions WHERE title = ? ORDER BY started_at DESC LIMIT 1",
            (title,),
        ).fetchone()
        return None if row is None else str(row[0])

    def get_next_title_in_lineage(self, base_title: str) -> str:
        """血缘里的下一个标题：`Fix Docker Build` → `Fix Docker Build #2` → `#3`…

        （官方文档的例子就是这个：`get_next_title_in_lineage("Fix Docker Build")`）
        """
        rows = self._conn.execute(
            "SELECT title FROM sessions WHERE title = ? OR title LIKE ?",
            (base_title, f"{base_title} #%"),
        ).fetchall()
        seen = {str(r[0]) for r in rows}
        if base_title not in seen and not any(t.startswith(f"{base_title} #") for t in seen):
            return base_title                      # 这个名字还没人用 → 就用它
        n = 2
        while f"{base_title} #{n}" in seen:
            n += 1
        return f"{base_title} #{n}"
