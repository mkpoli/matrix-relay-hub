"""SQLite state: member names, message ids, replayed events, transaction ids."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    group_openid TEXT NOT NULL,
    member_openid TEXT NOT NULL,
    username TEXT,
    role TEXT,
    updated INTEGER NOT NULL,
    PRIMARY KEY (group_openid, member_openid)
);

CREATE TABLE IF NOT EXISTS messages (
    qq_id TEXT PRIMARY KEY,
    group_openid TEXT,
    room_id TEXT,
    event_id TEXT,
    ref_idx TEXT,
    direction TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_event ON messages(event_id);
CREATE INDEX IF NOT EXISTS messages_ref ON messages(group_openid, ref_idx);

CREATE TABLE IF NOT EXISTS relayed (
    event_id TEXT PRIMARY KEY,
    qq_id TEXT,
    group_openid TEXT,
    room_id TEXT,
    ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS faces (
    face_id INTEGER PRIMARY KEY,
    mxc TEXT NOT NULL,
    updated INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        # Bindings moved into the configuration; a state file from the single-room
        # layout keeps its reply map under a table this version never reads.
        self.conn.execute("DROP TABLE IF EXISTS bindings")

    def close(self) -> None:
        self.conn.close()

    # -- members ----------------------------------------------------------

    def set_member(
        self, group_openid: str, member_openid: str, username: str | None, role: str | None
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO members(group_openid, member_openid, username, role, updated)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(group_openid, member_openid) DO UPDATE SET
                username = COALESCE(excluded.username, members.username),
                role = COALESCE(excluded.role, members.role),
                updated = excluded.updated
            """,
            (group_openid, member_openid, username, role, int(time.time())),
        )

    def member_name(self, group_openid: str, member_openid: str) -> str | None:
        row = self.conn.execute(
            "SELECT username FROM members WHERE group_openid = ? AND member_openid = ?",
            (group_openid, member_openid),
        ).fetchone()
        return row["username"] if row else None

    # -- messages ---------------------------------------------------------

    def add_message(
        self,
        qq_id: str,
        group_openid: str,
        room_id: str | None,
        event_id: str | None,
        ref_idx: str | None,
        direction: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO messages(qq_id, group_openid, room_id, event_id, ref_idx, direction, ts)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(qq_id) DO UPDATE SET
                event_id = COALESCE(excluded.event_id, messages.event_id),
                ref_idx = COALESCE(excluded.ref_idx, messages.ref_idx)
            """,
            (qq_id, group_openid, room_id, event_id, ref_idx, direction, int(time.time())),
        )

    def event_for_ref(self, group_openid: str, ref_idx: str) -> str | None:
        row = self.conn.execute(
            "SELECT event_id FROM messages WHERE group_openid = ? AND ref_idx = ? AND event_id IS NOT NULL",
            (group_openid, ref_idx),
        ).fetchone()
        return row["event_id"] if row else None

    def event_for_qq(self, qq_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT event_id FROM messages WHERE qq_id = ?", (qq_id,)
        ).fetchone()
        return row["event_id"] if row else None

    def qq_for_event(self, event_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM messages WHERE event_id = ? AND direction = 'matrix'",
            (event_id,),
        ).fetchone()

    # -- replayed history -------------------------------------------------

    def mark_relayed(
        self, event_id: str, qq_id: str, group_openid: str, room_id: str
    ) -> None:
        """Record an event carried to QQ inside a batch message."""
        self.conn.execute(
            """
            INSERT INTO relayed(event_id, qq_id, group_openid, room_id, ts)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (event_id, qq_id, group_openid, room_id, int(time.time())),
        )

    def relayed_qq(self, event_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT qq_id FROM relayed WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row["qq_id"] if row else None

    def relayed_count(self) -> int:
        return self.conn.execute("SELECT count(*) FROM relayed").fetchone()[0]

    # -- faces ------------------------------------------------------------

    def face_mxc(self, face_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT mxc FROM faces WHERE face_id = ?", (face_id,)
        ).fetchone()
        return row["mxc"] if row else None

    def set_face(self, face_id: int, mxc: str) -> None:
        self.conn.execute(
            """
            INSERT INTO faces(face_id, mxc, updated) VALUES(?, ?, ?)
            ON CONFLICT(face_id) DO UPDATE SET mxc = excluded.mxc, updated = excluded.updated
            """,
            (face_id, mxc, int(time.time())),
        )

    # -- transactions -----------------------------------------------------

    def seen_transaction(self, txn_id: str) -> bool:
        if self.conn.execute(
            "SELECT 1 FROM transactions WHERE txn_id = ?", (txn_id,)
        ).fetchone():
            return True
        self.conn.execute(
            "INSERT INTO transactions(txn_id, ts) VALUES(?, ?)",
            (txn_id, int(time.time())),
        )
        return False
