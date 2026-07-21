import sqlite3
import uuid
import time

DB_NAME = "meeting.db"


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 話者マスター（1者1行）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS speakers (
        speaker_id   TEXT PRIMARY KEY,
        display_name TEXT NOT NULL
    )
    """)

    # 発言ログ（IDで話者を参照）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS speeches (
        id           TEXT PRIMARY KEY,
        speaker_id   TEXT NOT NULL,
        text         TEXT NOT NULL,
        timestamp    REAL NOT NULL,
        parent_id    TEXT,
        summary      TEXT,
        intents      TEXT,
        topic_id     TEXT,
        FOREIGN KEY (speaker_id) REFERENCES speakers(speaker_id)
    )
    """)

    # 話題ノード（Git風フロー）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS topics (
        topic_id     TEXT PRIMARY KEY,
        label        TEXT NOT NULL,
        parent_id    TEXT,
        is_digression INTEGER DEFAULT 0,
        created_at   REAL NOT NULL
    )
    """)

    # 会議メタ情報（本題など）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS meeting_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)

    conn.commit()
    conn.close()


def get_or_create_speaker(speaker_id: str, display_name: str) -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO speakers (speaker_id, display_name) VALUES (?, ?)",
        (speaker_id, display_name)
    )
    conn.commit()
    conn.close()
    return speaker_id


def update_speaker_name(speaker_id: str, new_name: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE speakers SET display_name = ? WHERE speaker_id = ?",
        (new_name, speaker_id)
    )
    conn.commit()
    conn.close()


def save_speech(speaker_id: str, text: str, parent_id: str = None, topic_id: str = None) -> str:
    speech_id = str(uuid.uuid4())
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO speeches (id, speaker_id, text, timestamp, parent_id, topic_id)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (speech_id, speaker_id, text, time.time(), parent_id, topic_id))
    conn.commit()
    conn.close()
    return speech_id


def update_speech_analysis(speech_id: str, summary: str, intents: str):
    """要約・要望をあとから埋める（30秒遅延処理）"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE speeches SET summary = ?, intents = ? WHERE id = ?",
        (summary, intents, speech_id)
    )
    conn.commit()
    conn.close()


def save_topic(label: str, parent_id: str = None, is_digression: bool = False) -> str:
    topic_id = str(uuid.uuid4())
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO topics (topic_id, label, parent_id, is_digression, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (topic_id, label, parent_id, int(is_digression), time.time()))
    conn.commit()
    conn.close()
    return topic_id


def get_all_topics() -> list:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT topic_id, label, parent_id, is_digression FROM topics ORDER BY created_at ASC")
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_all_speeches() -> list:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT s.id, sp.display_name, s.text, s.timestamp, s.summary, s.intents, s.topic_id
        FROM speeches s
        JOIN speakers sp ON s.speaker_id = sp.speaker_id
        ORDER BY s.timestamp ASC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows


def set_meeting_goal(goal: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO meeting_meta (key, value) VALUES ('goal', ?)",
        (goal,)
    )
    conn.commit()
    conn.close()


def get_meeting_goal() -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM meeting_meta WHERE key = 'goal'")
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def clear_session_data():
    """現在の会議セッションのデータをすべて削除する"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM speeches")
    cursor.execute("DELETE FROM topics")
    cursor.execute("DELETE FROM meeting_meta")
    cursor.execute("DELETE FROM speakers")
    conn.commit()
    conn.close()
