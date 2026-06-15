import sqlite3
from pathlib import Path

import config


class Storage:
    def __init__(self, path: Path = None):
        path = path or (config.DATA_DIR / "bridge.db")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS topics (
                chat_id  INTEGER PRIMARY KEY,
                topic_id INTEGER UNIQUE
            );
            CREATE TABLE IF NOT EXISTS seen (
                message_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS names (
                user_id INTEGER PRIMARY KEY,
                name    TEXT
            );
            CREATE TABLE IF NOT EXISTS msg_map (
                tg_msg_id  INTEGER PRIMARY KEY,
                chat_id    INTEGER,
                max_msg_id TEXT
            );
            CREATE TABLE IF NOT EXISTS outbox (
                tg_msg_id INTEGER PRIMARY KEY,
                chat_id   INTEGER,
                ts        INTEGER,
                read      INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        self.db.commit()

    def get_meta(self, key: str):
        row = self.db.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str):
        self.db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    def clear_all(self):
        for t in ("topics", "names", "seen", "msg_map", "outbox"):
            self.db.execute(f"DELETE FROM {t}")
        self.db.commit()

    def add_outbox(self, tg_msg_id: int, chat_id: int, ts: int):
        self.db.execute(
            "INSERT OR REPLACE INTO outbox (tg_msg_id, chat_id, ts, read) "
            "VALUES (?, ?, ?, 0)", (tg_msg_id, chat_id, ts or 0))
        self.db.commit()

    def unread_outbox(self, chat_id: int, mark: int):
        rows = self.db.execute(
            "SELECT tg_msg_id FROM outbox WHERE chat_id=? AND read=0 AND ts<=?",
            (chat_id, mark)).fetchall()
        return [r[0] for r in rows]

    def mark_outbox_read(self, tg_msg_id: int):
        self.db.execute("UPDATE outbox SET read=1 WHERE tg_msg_id=?", (tg_msg_id,))
        self.db.commit()

    def set_msg_map(self, tg_msg_id: int, chat_id: int, max_msg_id: str):
        self.db.execute(
            "INSERT OR REPLACE INTO msg_map (tg_msg_id, chat_id, max_msg_id) "
            "VALUES (?, ?, ?)", (tg_msg_id, chat_id, str(max_msg_id)))
        self.db.commit()

    def get_max_msg(self, tg_msg_id: int):
        row = self.db.execute(
            "SELECT chat_id, max_msg_id FROM msg_map WHERE tg_msg_id=?",
            (tg_msg_id,)).fetchone()
        return (row[0], row[1]) if row else None

    def get_topic(self, chat_id: int):
        row = self.db.execute(
            "SELECT topic_id FROM topics WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row else None

    def set_topic(self, chat_id: int, topic_id: int):
        self.db.execute(
            "INSERT OR REPLACE INTO topics (chat_id, topic_id) VALUES (?, ?)",
            (chat_id, topic_id))
        self.db.commit()

    def del_topic(self, chat_id: int):
        self.db.execute("DELETE FROM topics WHERE chat_id=?", (chat_id,))
        self.db.commit()

    def chat_for_topic(self, topic_id: int):
        row = self.db.execute(
            "SELECT chat_id FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        return row[0] if row else None

    def is_seen(self, message_id: str) -> bool:
        if not message_id:
            return False
        return self.db.execute(
            "SELECT 1 FROM seen WHERE message_id=?", (message_id,)).fetchone() is not None

    def mark_seen(self, message_id: str):
        if not message_id:
            return
        self.db.execute("INSERT OR IGNORE INTO seen (message_id) VALUES (?)", (message_id,))
        self.db.commit()

    def get_name(self, user_id: int):
        row = self.db.execute(
            "SELECT name FROM names WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else None

    def set_name(self, user_id: int, name: str):
        self.db.execute(
            "INSERT OR REPLACE INTO names (user_id, name) VALUES (?, ?)",
            (user_id, name))
        self.db.commit()
