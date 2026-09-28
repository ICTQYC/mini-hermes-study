"""全文检索（对应 Hermes 的 `hermes_state_search.py`）。

两条路，因为**一条路不够**：

| 查询 | 走哪条 | 为什么 |
|---|---|---|
| 英文/数字（`docker`、`deploy*`、`"exact phrase"`） | **FTS5 词索引** | 分词器按词切 → 快、能排序（BM25） |
| 含中文 | **LIKE 扫描兜底** | FTS5 的默认分词器（unicode61）**不切 CJK**：一整串中文被当成一个"词"，所以 `部署` 搜不到 `…部署文件` |

中文这条是**实测出来的**，不是猜的（探针见 `tests/test_session_storage.py` 那一组）：

```
默认分词器：'部署' → 0 命中，'部署文件' → 1 命中     ← 只能整串匹配
加 trigram 表：'部署' → 0 命中（trigram 要 ≥3 字符），'部署文' → 1 命中
```

所以真实 Hermes 那条路（`messages_fts_trigram` / `messages_fts_cjk` + `_search_messages_like_fallback`）
在 mini 里**只留最后那条兜底**：LIKE 扫描在 mini 的库规模（几百到几千条消息）完全够用，
而且零依赖（trigram 需要 ≥3 字符、`cjk_unicode61` 需要编译扩展 —— 两个都不划算）。

★ 抄的名字与形状都来自真实版：`_is_cjk` / `_CJK_RANGES`（`hermes_state_search.py:74-89`）、
  `_LIKE_ANY_COLUMN_SQL`（`:48-50`，三列一起扫 + `ESCAPE`）、`_like_params`（`:105-106`）、
  `_LIKE_SNIPPET_SQL`（`:47`，用 `instr` 定位命中处再截 120 字符）。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Sequence

# ── CJK 判定（范围照抄真实版 `_CJK_RANGES`，`hermes_state_search.py:74-77`）──
_CJK_RANGES = (
    (0x4E00, 0x9FFF),    # CJK 统一表意
    (0x3400, 0x4DBF),    # 扩展 A
    (0x20000, 0x2A6DF),  # 扩展 B
    (0x3000, 0x303F),    # CJK 标点
    (0x3040, 0x309F),    # 平假名
    (0x30A0, 0x30FF),    # 片假名
    (0xAC00, 0xD7AF),    # 谚文
)

# FTS5 的布尔操作符（大写才算操作符；小写的 and/or/not 当普通词）
_BOOLEAN_OPS = {"AND", "OR", "NOT"}

# 会当语法用、必须剥掉的裸字符（引号里的内容不动）
_SYNTAX_CHARS = str.maketrans({c: " " for c in '():^{}[]+'})

# 一个"词"里出现这些字符 → 整体引号化（否则 FTS5 会当语法解）
_NEEDS_QUOTING = None  # 延迟编译（正则导入开销小，但这里保持模块开头整洁）


def _is_cjk(cp: int) -> bool:
    """码点是不是 CJK（真实版同名函数，`hermes_state_search.py:89-90`）。"""
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def has_cjk(text: str) -> bool:
    return any(_is_cjk(ord(ch)) for ch in text or "")


def _non_operator_tokens(raw_query: str) -> List[str]:
    """去掉布尔操作符后的词（真实版同名函数，`:93-94`）—— LIKE 路径不认操作符。"""
    return [t for t in raw_query.split() if t.upper() not in _BOOLEAN_OPS]


def _escape_like(term: str) -> str:
    """LIKE 的通配符转义（`%` / `_` / `\\` 都是 LIKE 的语法字符）。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_params(term: str) -> List[str]:
    """一个 `%词%` 绑定，重复三次（三列各一份）——真实版 `_like_params`，`:105-106`。"""
    return [f"%{_escape_like(term)}%"] * 3


def sanitize_fts5_query(query: str) -> str:
    """把用户输入洗成"FTS5 能读、且语义没跑偏"的查询串。

    规则（每条都对应一种真实会炸的输入）：

    | 输入 | 问题 | 洗完 |
    |---|---|---|
    | `chat-send` | `-` 被当语法 | `"chat-send"` |
    | `my-app.config.ts` | 同上（点号也是） | `"my-app.config.ts"` |
    | `"未配对` | 引号不配对 → 语法错 | `未配对` |
    | `hello AND` | 悬空布尔操作符 → 语法错 | `hello` |
    | `p2p:8080` | `:` 会被当"列过滤" | `p2p 8080` |
    | `deploy*` | 前缀查询**要保留** | `deploy*` |
    """
    if not query:
        return ""
    import re as _re
    global _NEEDS_QUOTING
    if _NEEDS_QUOTING is None:
        _NEEDS_QUOTING = _re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*[\-.]")
    tokens: List[str] = []
    for raw in query.split():
        token = raw
        # ① 前缀星号先摘出来（清洗完再贴回去，别让引号把它吃了）
        star = token.endswith("*") and not token.startswith('"')
        body = token[:-1] if star else token
        # ② 不配对/内嵌的引号：整段剥成裸词
        if body.count('"') % 2 == 1 or (body.startswith('"') and not body.endswith('"')):
            body = body.replace('"', "")
        # ③ 剥掉会当语法的字符（引号包裹的内容不动）
        if not (body.startswith('"') and body.endswith('"') and len(body) > 1):
            body = body.translate(_SYNTAX_CHARS)
            # ④ 带连字符/点号的词整体引号化，FTS5 才会当**短语**匹配
            if _NEEDS_QUOTING.search(body):
                body = '"' + body.replace('"', '""') + '"'
        if not body:
            continue
        tokens.append(body + ("*" if star else ""))
    # ⑤ 去掉悬空/重复的布尔操作符
    cleaned: List[str] = []
    for token in tokens:
        if token in _BOOLEAN_OPS:
            if not cleaned or cleaned[-1] in _BOOLEAN_OPS:
                continue                       # 开头、或连着两个操作符 → 丢掉这个
            cleaned.append(token)
        else:
            cleaned.append(token)
    while cleaned and cleaned[-1] in _BOOLEAN_OPS:
        cleaned.pop()
    return " ".join(cleaned)


class SessionSearchMixin:
    """给 `SessionDB` 用的检索部分。"""

    _conn: sqlite3.Connection

    def search_messages(self, query: str, *, limit: int = 20, offset: int = 0,
                        source_filter: Optional[Sequence[str]] = None,
                        exclude_sources: Optional[Sequence[str]] = None,
                        role_filter: Optional[Sequence[str]] = None,
                        include_inactive: bool = False) -> List[Dict[str, Any]]:
        """跨会话检索。

        每条结果 = 片段（FTS 用 `>>>命中<<<` 标出 / LIKE 用 `instr` 截窗口）
        + 它属于哪个会话 + `route`（`"fts"` 还是 `"like"`，方便你看是哪条路服务的）。

        `active = 0 AND compacted = 0` 的行（被回退/撤销的）默认排除；
        压缩归档的世代**照样能搜到** —— 历史不该因为被压缩过就搜不到。
        """
        if not (query or "").strip():
            return []
        # ★ 路由：含中文走 LIKE 兜底（默认分词器不切 CJK，见模块 docstring 的实测）
        if has_cjk(query):
            return self._search_like(query, limit=limit, offset=offset,
                                     source_filter=source_filter,
                                     exclude_sources=exclude_sources,
                                     role_filter=role_filter,
                                     include_inactive=include_inactive)
        return self._search_fts(query, limit=limit, offset=offset,
                                source_filter=source_filter,
                                exclude_sources=exclude_sources,
                                role_filter=role_filter,
                                include_inactive=include_inactive)

    # ── 路一：FTS5 词索引 ──────────────────────────────────────────────
    def _search_fts(self, query: str, **kw: Any) -> List[Dict[str, Any]]:
        cleaned = sanitize_fts5_query(query)
        if not cleaned:
            return []
        where = ["messages_fts MATCH ?"]
        params: List[Any] = [cleaned]
        where, params = self._apply_filters(where, params, prefix="m.", **kw)
        params += [int(kw["limit"]), int(kw["offset"])]
        sql = f"""
            SELECT m.id, m.session_id, m.role, m.timestamp, s.title, s.source,
                   snippet(messages_fts, 0, '>>>', '<<<', '…', 12) AS snippet
              FROM messages_fts
              JOIN messages m ON m.id = messages_fts.rowid
              LEFT JOIN sessions s ON s.id = m.session_id
             WHERE {' AND '.join(where)}
             ORDER BY rank
             LIMIT ? OFFSET ?
        """
        return self._finish(self._conn.execute(sql, params).fetchall(), "fts")

    # ── 路二：LIKE 兜底（中文/子串）────────────────────────────────────
    def _search_like(self, query: str, **kw: Any) -> List[Dict[str, Any]]:
        tokens = [t.strip('"').strip() for t in _non_operator_tokens(query)]
        tokens = [t for t in tokens if t]
        if not tokens:
            return []
        # 三列一起扫（真实版 `_LIKE_ANY_COLUMN_SQL`，`:48-50`）：正文 / 工具名 / 工具调用参数
        conds, params = [], []
        for tok in tokens:
            conds.append("(m.content LIKE ? ESCAPE '\\' OR m.tool_name LIKE ? ESCAPE '\\'"
                         " OR m.tool_calls LIKE ? ESCAPE '\\')")
            params += _like_params(tok)
        where, params = self._apply_filters(conds, params, prefix="m.", **kw)
        # 片段：用 instr 找到命中点，往前 40 字符起截 120 字符（真实版 `_LIKE_SNIPPET_SQL`，`:47`）
        params.append(tokens[0])
        params += [int(kw["limit"]), int(kw["offset"])]
        sql = f"""
            SELECT m.id, m.session_id, m.role, m.timestamp, s.title, s.source,
                   substr(COALESCE(m.content, ''), max(1, instr(COALESCE(m.content, ''), ?) - 40), 120)
                       AS snippet
              FROM messages m
              LEFT JOIN sessions s ON s.id = m.session_id
             WHERE {' AND '.join(where)}
             ORDER BY m.timestamp DESC, m.id DESC
             LIMIT ? OFFSET ?
        """
        return self._finish(self._conn.execute(sql, params).fetchall(), "like")

    # ── 公共 ───────────────────────────────────────────────────────────
    @staticmethod
    def _apply_filters(where: List[str], params: List[Any], *, prefix: str,
                       source_filter=None, exclude_sources=None, role_filter=None,
                       include_inactive=False, **_: Any):
        """过滤条件（两条路共用，避免"只在 FTS 里生效"这种半吊子行为）。"""
        if source_filter:
            where.append(f"s.source IN ({', '.join('?' * len(source_filter))})")
            params += list(source_filter)
        if exclude_sources:
            where.append(f"(s.source IS NULL OR s.source NOT IN"
                         f" ({', '.join('?' * len(exclude_sources))}))")
            params += list(exclude_sources)
        if role_filter:
            where.append(f"{prefix}role IN ({', '.join('?' * len(role_filter))})")
            params += list(role_filter)
        if not include_inactive:
            where.append(f"({prefix}active = 1 OR {prefix}compacted = 1)")
        return where, params

    def _finish(self, rows: Sequence[sqlite3.Row], route: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in rows:
            hit = dict(row)
            hit["route"] = route
            hit["context"] = self._context_for(hit["session_id"], int(hit["id"]))
            out.append(hit)
        return out

    def _context_for(self, session_id: str, message_id: int) -> List[Dict[str, Any]]:
        """命中消息的前后各一条（内容截到 200 字符，免得一条结果拖出一整个工具输出）。"""
        rows = self._conn.execute(
            "SELECT id, role, SUBSTR(COALESCE(content, ''), 1, 200) AS content "
            "  FROM messages WHERE session_id = ? AND id IN "
            "       (SELECT MAX(id) FROM messages WHERE session_id = ? AND id < ? "
            "        UNION SELECT MIN(id) FROM messages WHERE session_id = ? AND id > ?) "
            " ORDER BY id",
            (session_id, session_id, message_id, session_id, message_id),
        ).fetchall()
        return [dict(r) for r in rows]
