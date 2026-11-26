"""SQLite state: member names, message ids, replayed events, transactions and
Matrix events still on their way to QQ."""

from __future__ import annotations

import json
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

CREATE TABLE IF NOT EXISTS pending (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    ts INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS faces (
    face_id INTEGER PRIMARY KEY,
    mxc TEXT NOT NULL,
    updated INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS superseded (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS edits (
    edit_id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    ts INTEGER NOT NULL,
    sent_ts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tweets (
    group_openid TEXT NOT NULL,
    status_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (group_openid, status_id)
);

CREATE TABLE IF NOT EXISTS media_progress (
    event_id TEXT PRIMARY KEY,
    labels_sent INTEGER NOT NULL DEFAULT 0,
    labels_done INTEGER NOT NULL DEFAULT 0,
    media_sent INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS member_avatars (
    member_openid TEXT PRIMARY KEY,
    avatar_key TEXT NOT NULL,
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
        # FULL, so a transaction acknowledged to the homeserver survives power loss.
        self.conn.execute("PRAGMA synchronous=FULL")
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

    def member_avatar(self, member_openid: str) -> tuple[str, str, int] | None:
        row = self.conn.execute(
            "SELECT avatar_key, mxc, updated FROM member_avatars WHERE member_openid = ?",
            (member_openid,),
        ).fetchone()
        return (row["avatar_key"], row["mxc"], int(row["updated"])) if row else None

    def set_member_avatar(self, member_openid: str, avatar_key: str, mxc: str) -> None:
        self.conn.execute(
            """
            INSERT INTO member_avatars(member_openid, avatar_key, mxc, updated)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(member_openid) DO UPDATE SET
                avatar_key = excluded.avatar_key,
                mxc = excluded.mxc,
                updated = excluded.updated
            """,
            (member_openid, avatar_key, mxc, int(time.time())),
        )

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

    def sent_count(self, event_id: str) -> int:
        """QQ messages posted for a Matrix event (a long text posts several)."""
        return self.conn.execute(
            "SELECT count(*) FROM messages WHERE event_id = ? AND direction = 'matrix'",
            (event_id,),
        ).fetchone()[0]

    def qq_messages_for_event(self, event_id: str) -> list[sqlite3.Row]:
        """Every QQ message a Matrix event was sent as, in the order they went out."""
        return self.conn.execute(
            "SELECT * FROM messages WHERE event_id = ? AND direction = 'matrix' ORDER BY rowid",
            (event_id,),
        ).fetchall()

    def forget_messages(self, qq_ids: list[str]) -> None:
        self.conn.executemany("DELETE FROM messages WHERE qq_id = ?", [(qq_id,) for qq_id in qq_ids])

    def media_progress(self, event_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT labels_sent, labels_done, media_sent, done FROM media_progress WHERE event_id = ?",
            (event_id,),
        ).fetchone()

    def note_media_progress(
        self,
        event_id: str,
        *,
        labels_sent: int | None = None,
        labels_done: bool | None = None,
        media_sent: int | None = None,
        done: bool | None = None,
    ) -> None:
        if not event_id:
            return
        row = self.media_progress(event_id)
        cur_labels = int(row["labels_sent"]) if row else 0
        cur_labels_done = int(row["labels_done"]) if row else 0
        cur_media = int(row["media_sent"]) if row else 0
        cur_done = int(row["done"]) if row else 0
        if labels_sent is not None:
            cur_labels = max(cur_labels, labels_sent)
        if labels_done is not None:
            cur_labels_done = 1 if labels_done else cur_labels_done
        if media_sent is not None:
            cur_media = max(cur_media, media_sent)
        if done is not None:
            cur_done = 1 if done else cur_done
        self.conn.execute(
            """
            INSERT INTO media_progress(event_id, labels_sent, labels_done, media_sent, done, ts)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                labels_sent = excluded.labels_sent,
                labels_done = excluded.labels_done,
                media_sent = excluded.media_sent,
                done = excluded.done,
                ts = excluded.ts
            """,
            (event_id, cur_labels, cur_labels_done, cur_media, cur_done, int(time.time())),
        )

    # -- edits and redactions -------------------------------------------------

    def supersede(self, event_id: str, kind: str) -> None:
        """Record that an event was edited or redacted, so its old text is never sent again."""
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
            "SELECT kind FROM superseded WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row["kind"] if row else None

    def edit_done(self, edit_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM edits WHERE edit_id = ?", (edit_id,)
        ).fetchone() is not None

    def mark_edit_done(self, edit_id: str, target: str, sent_ts: int = 0) -> None:
        """Record an edit as applied; `sent_ts` is when its author made it, to order edits."""
        self.conn.execute(
            "INSERT OR IGNORE INTO edits(edit_id, target, ts, sent_ts) VALUES(?, ?, ?, ?)",
            (edit_id, target, int(time.time()), sent_ts),
        )

    def newest_edit(self, target: str) -> int:
        """When the newest edit applied to an event was made, or 0."""
        row = self.conn.execute(
            "SELECT max(sent_ts) AS ts FROM edits WHERE target = ?", (target,)
        ).fetchone()
        return int(row["ts"] or 0)

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

    def clear_faces(self) -> None:
        self.conn.execute("DELETE FROM faces")

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

    # -- expanded tweets --------------------------------------------------

    def tweet_recent(self, group_openid: str, status_id: str, seconds: int) -> bool:
        """Whether this group already got the post within `seconds`."""
        row = self.conn.execute(
            "SELECT 1 FROM tweets WHERE group_openid = ? AND status_id = ? AND ts >= ?",
            (group_openid, status_id, int(time.time()) - seconds),
        ).fetchone()
        return row is not None

    def mark_tweet(self, group_openid: str, status_id: str) -> None:
        now = int(time.time())
        # A day is far longer than the duplicate window; keeping the table
        # small costs one delete per expansion.
        self.conn.execute("DELETE FROM tweets WHERE ts < ?", (now - 86_400,))
        self.conn.execute(
            """
            INSERT INTO tweets(group_openid, status_id, ts) VALUES(?, ?, ?)
            ON CONFLICT(group_openid, status_id) DO UPDATE SET ts = excluded.ts
            """,
            (group_openid, status_id, now),
        )

    # -- transactions -----------------------------------------------------

    def accept_transaction(self, txn_id: str, events: list[dict]) -> list[int] | None:
        """Record a transaction and keep its events until they reach QQ.

        Returns each event's pending id, or None for a transaction already seen.
        Both happen in one database transaction, so a crash cannot lose events
        the homeserver was told were delivered.
        """
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
        """Unfinished events to try again, oldest first, counting the attempt.

        Rows older than `max_age` or tried `max_attempts` times are dropped;
        rows received less than `idle` seconds ago, or in `busy`, are left alone.
        """
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
                "UPDATE pending SET attempts = attempts + 1 WHERE seq = ?", (row["seq"],)
            )
        self.dropped_pending = dropped
        return [(row["seq"], json.loads(row["event"])) for row in rows]

    def finish_pending(self, seq: int) -> None:
        self.conn.execute("DELETE FROM pending WHERE seq = ?", (seq,))
