"""会话持久化（简版）。

对应 Hermes 的 hermes_state.py（真实版本是 SQLite + FTS5 全文检索 +
会话 lineage：压缩会派生一个"子会话"，靠 lineage_id 串起来）。
我们这一章存 JSON，够用就行 —— session-storage 那一章再换成 SQLite，
你会看到为什么必须换（全文检索、并发写、会话分叉）。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import sessions_dir


def new_session_id() -> str:
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def save_session(session_id: str, messages: List[Dict[str, Any]], meta: Optional[Dict] = None) -> Path:
    path = sessions_dir() / f"{session_id}.json"
    payload = {
        "session_id": session_id,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "messages": messages,
        "meta": meta or {},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_session(session_id: str) -> Optional[Dict[str, Any]]:
    path = sessions_dir() / f"{session_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def latest_session() -> Optional[Dict[str, Any]]:
    files = sorted(sessions_dir().glob("*.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))
