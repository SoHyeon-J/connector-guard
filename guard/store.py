"""SQLite 저장소와 가상 시계.

자격증명 상태(cred_state)와 커넥터 구성(connectors.config)을 별도 테이블로 두어,
일시정지 중에도 구성이 보존되고 해제될 때만 삭제되게 한다(제안서 3.5 '데이터 분리').
이력에는 이용자의 요청 내용(도구 인자)을 저장하지 않는다.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS clock (id INTEGER PRIMARY KEY CHECK (id = 1), today TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connectors (
    id TEXT PRIMARY KEY, name TEXT, scopes TEXT, risk TEXT, schedule_days INTEGER,
    config TEXT                      -- 구성 정보. 해제 시 NULL
);
CREATE TABLE IF NOT EXISTS cred_state (
    connector_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,             -- active / notified / paused / released
    last_activity TEXT, last_reconfirm TEXT,
    notice_at TEXT, notice_reason TEXT,
    paused_at TEXT, pause_reason TEXT,
    sched_alerted INTEGER DEFAULT 0, final_notice_at TEXT
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY, day TEXT, connector_id TEXT, tool TEXT, source TEXT,
    outcome TEXT, counted INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, day TEXT, connector_id TEXT, type TEXT, actor TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY, day TEXT, connector_id TEXT, channel TEXT, kind TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS nonces (
    nonce TEXT PRIMARY KEY, connector_id TEXT, day TEXT, used INTEGER DEFAULT 0, request_label TEXT
);
"""

START_DAY = "2026-01-01"


class Store:
    def __init__(self, path: str):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        # 시연용 임시 DB이므로 디스크 동기화를 끈다(Windows에서 가상 시계 진행 속도 개선)
        self.db.execute("PRAGMA synchronous = OFF")
        self.db.execute("PRAGMA journal_mode = MEMORY")
        self.db.executescript(SCHEMA)
        if not self.one("SELECT 1 FROM clock"):
            self.exec("INSERT INTO clock (id, today) VALUES (1, ?)", START_DAY)

    # --- 기본 질의 ---
    def exec(self, sql: str, *args):
        with self.lock:
            return self.db.execute(sql, args)

    def one(self, sql: str, *args):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def all(self, sql: str, *args):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # --- 가상 시계 ---
    def today(self) -> dt.date:
        return dt.date.fromisoformat(self.one("SELECT today FROM clock")["today"])

    def day_no(self, d: dt.date | None = None) -> int:
        """시작일 기준 경과 일수(시연 출력용)."""
        return ((d or self.today()) - dt.date.fromisoformat(START_DAY)).days

    def set_today(self, d: dt.date) -> None:
        self.exec("UPDATE clock SET today = ?", d.isoformat())

    # --- 기록 ---
    def event(self, cid: str, type_: str, actor: str, detail: dict | str = "") -> None:
        if isinstance(detail, dict):
            detail = json.dumps(detail, ensure_ascii=False)
        self.exec("INSERT INTO events (day, connector_id, type, actor, detail) VALUES (?,?,?,?,?)",
                  self.today().isoformat(), cid, type_, actor, detail)

    def notify(self, cid: str, channel: str, kind: str, text: str) -> None:
        self.exec("INSERT INTO notifications (day, connector_id, channel, kind, text) VALUES (?,?,?,?,?)",
                  self.today().isoformat(), cid, channel, kind, text)
