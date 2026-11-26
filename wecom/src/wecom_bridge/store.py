"""SQLite state for transactions, pending retries, deduplication, and mappings."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound (
    msgid TEXT PRIMARY KEY,
    chat_id TEXT,
    room_id TEXT,
    event_id TEXT,
    ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    chat_id TEXT,
    room_id TEXT,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS outbound_event ON outbound(event_id);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pending (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    ts INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS superseded (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS edits (
    edit_id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound_pending (
    msgid TEXT PRIMARY KEY,
    frame TEXT NOT NULL,
    ts INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)
        self.dropped_pending = 0

    def close(self) -> None:
        self.conn.close()

    # -- inbound WeCom dedup ----------------------------------------------

    def claim_inbound(self, msgid: str, chat_id: str, room_id: str | None) -> bool:
        """Atomically record an incoming WeCom msgid; False if already seen."""
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO inbound(msgid, chat_id, room_id, event_id, ts) VALUES(?, ?, ?, NULL, ?)",
            (msgid, chat_id, room_id, int(time.time())),
        )
        return cursor.rowcount == 1

    def set_inbound_event(self, msgid: str, event_id: str) -> None:
        self.conn.execute(
            "UPDATE inbound SET event_id = ? WHERE msgid = ?",
            (event_id, msgid),
        )

    def release_inbound(self, msgid: str) -> None:
        """Remove a claimed inbound msgid if relaying to Matrix failed before delivery."""
        self.conn.execute("DELETE FROM inbound WHERE msgid = ? AND event_id IS NULL", (msgid,))

    def enqueue_inbound(self, msgid: str, frame: dict) -> None:
        """Persist a failed inbound WeCom callback for later local retry."""
        self.conn.execute(
            """
            INSERT INTO inbound_pending(msgid, frame, ts, attempts) VALUES(?, ?, ?, 0)
            ON CONFLICT(msgid) DO NOTHING
            """,
            (msgid, json.dumps(frame, ensure_ascii=False), int(time.time())),
        )

    def claim_inbound_pending(
        self,
        *,
        max_age: int,
        max_attempts: int,
        busy: set[str] | frozenset[str] = frozenset(),
    ) -> list[tuple[str, dict]]:
        now = int(time.time())
        self.conn.execute(
            "DELETE FROM inbound_pending WHERE ts < ? OR attempts >= ?",
            (now - max_age, max_attempts),
        )
        rows = [
            row
            for row in self.conn.execute(
                "SELECT msgid, frame FROM inbound_pending ORDER BY ts, rowid"
            )
            if row["msgid"] not in busy
        ]
        for row in rows:
            self.conn.execute(
                "UPDATE inbound_pending SET attempts = attempts + 1 WHERE msgid = ?",
                (row["msgid"],),
            )
        return [(row["msgid"], json.loads(row["frame"])) for row in rows]

    def finish_inbound_pending(self, msgid: str) -> None:
        self.conn.execute("DELETE FROM inbound_pending WHERE msgid = ?", (msgid,))

    # -- outbound Matrix -> WeCom -----------------------------------------

    def record_outbound(self, event_id: str, chat_id: str, room_id: str) -> None:
        self.conn.execute(
            "INSERT INTO outbound(event_id, chat_id, room_id, ts) VALUES(?, ?, ?, ?)",
            (event_id, chat_id, room_id, int(time.time())),
        )

    def sent_count(self, event_id: str) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM outbound WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0]

    # -- edits and redactions ---------------------------------------------

    def supersede(self, event_id: str, kind: str) -> None:
        self.conn.execute(
            """
            INSERT INTO superseded(event_id, kind, ts) VALUES(?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                kind = CASE WHEN superseded.kind = 'redacted' THEN 'redacted' ELSE excluded.kind END,
                ts = excluded.ts
            """,
            (event_id, kind, int(time.time())),
        )

    def superseded(self, event_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT kind FROM superseded WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return row["kind"] if row else None

    def edit_done(self, edit_id: str) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM edits WHERE edit_id = ?", (edit_id,)).fetchone()
            is not None
        )

    def mark_edit_done(self, edit_id: str, target: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO edits(edit_id, target, ts) VALUES(?, ?, ?)",
            (edit_id, target, int(time.time())),
        )

    # -- meta -------------------------------------------------------------

    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO meta(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    # -- transactions & durable retry queue -------------------------------

    def accept_transaction(self, txn_id: str, events: list[dict]) -> list[int] | None:
        """Record a transaction and persist its events atomically."""
        now = int(time.time())
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.conn.execute(
                "SELECT 1 FROM transactions WHERE txn_id = ?", (txn_id,)
            ).fetchone():
                self.conn.execute("COMMIT")
                return None
            self.conn.execute(
                "INSERT INTO transactions(txn_id, ts) VALUES(?, ?)", (txn_id, now)
            )
            seqs = [
                self.conn.execute(
                    "INSERT INTO pending(event, ts) VALUES(?, ?)",
                    (json.dumps(event, ensure_ascii=False), now),
                ).lastrowid
                for event in events
            ]
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return seqs

    def claim_pending(
        self,
        *,
        max_age: int,
        max_attempts: int,
        idle: int = 0,
        busy: set[int] | frozenset[int] = frozenset(),
    ) -> list[tuple[int, dict]]:
        now = int(time.time())
        dropped = self.conn.execute(
            "DELETE FROM pending WHERE ts < ? OR attempts >= ?",
            (now - max_age, max_attempts),
        ).rowcount
        rows = [
            row
            for row in self.conn.execute(
                "SELECT seq, event FROM pending WHERE ts <= ? ORDER BY seq",
                (now - idle,),
            )
            if row["seq"] not in busy
        ]
        for row in rows:
            self.conn.execute(
                "UPDATE pending SET attempts = attempts + 1 WHERE seq = ?",
                (row["seq"],),
            )
        self.dropped_pending = dropped
        return [(row["seq"], json.loads(row["event"])) for row in rows]

    def finish_pending(self, seq: int) -> None:
        self.conn.execute("DELETE FROM pending WHERE seq = ?", (seq,))
