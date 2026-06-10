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
            """
        )
        self.db.commit()

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
