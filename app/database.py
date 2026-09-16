"""SQLite 连接管理与表结构初始化 / 演示数据。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = os.environ.get("CUE_DB_PATH", "/data/cues.db")

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS cues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    department  TEXT NOT NULL,
    name        TEXT NOT NULL,
    duration    REAL NOT NULL CHECK(duration >= 0),
    locked_start REAL CHECK(locked_start IS NULL OR locked_start >= 0),
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dependencies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cue_id       INTEGER NOT NULL REFERENCES cues(id) ON DELETE CASCADE,
    depends_on   INTEGER NOT NULL REFERENCES cues(id) ON DELETE CASCADE,
    delay        REAL NOT NULL DEFAULT 0 CHECK(delay >= 0),
    UNIQUE(cue_id, depends_on),
    CHECK(cue_id <> depends_on)
);

CREATE INDEX IF NOT EXISTS idx_dep_cue ON dependencies(cue_id);
CREATE INDEX IF NOT EXISTS idx_dep_on  ON dependencies(depends_on);

-- ---------------------------------------------------------- 排演执行台
-- 一场排演 = 开演时刻冻结的一份提示单快照，与后续提示单编辑完全隔离
CREATE TABLE IF NOT EXISTS rehearsals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','ended')),
    start_epoch REAL NOT NULL,          -- 开演的 Unix 时间戳（秒）
    end_epoch   REAL,                   -- 结束时刻；NULL = 进行中
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rehearsal_cues (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rehearsal_id  INTEGER NOT NULL REFERENCES rehearsals(id) ON DELETE CASCADE,
    cue_id        INTEGER NOT NULL,     -- 快照时的源提示 id（仅用于追溯）
    department    TEXT NOT NULL,
    name          TEXT NOT NULL,
    duration      REAL NOT NULL,
    locked_start  REAL,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    planned_start REAL,                 -- 冻结的计划开始（相对秒）
    planned_end   REAL,                 -- 冻结的计划结束（相对秒）
    status        TEXT NOT NULL DEFAULT 'waiting'
                  CHECK(status IN ('waiting','executing','completed')),
    actual_start  REAL,                 -- 实际开始：相对本场起点的秒数
    actual_end    REAL,                 -- 实际完成：相对本场起点的秒数
    UNIQUE(rehearsal_id, cue_id)
);

CREATE TABLE IF NOT EXISTS rehearsal_deps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rehearsal_id  INTEGER NOT NULL REFERENCES rehearsals(id) ON DELETE CASCADE,
    cue_id        INTEGER NOT NULL,
    depends_on    INTEGER NOT NULL,
    delay         REAL NOT NULL DEFAULT 0,
    UNIQUE(rehearsal_id, cue_id, depends_on)
);

CREATE INDEX IF NOT EXISTS idx_rc_rehearsal ON rehearsal_cues(rehearsal_id);
CREATE INDEX IF NOT EXISTS idx_rd_rehearsal ON rehearsal_deps(rehearsal_id);
"""


def get_connection() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """创建表结构（容器启动时执行）。"""
    with db() as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


# (部门, 名称, 时长, 锁定开场秒数或 None)
_DEMO_CUES = [
    ("灯光", "开场灯光预设", 20, 0.0),
    ("音响", "序曲播放", 90, None),
    ("机械", "幕布升起", 15, None),
    ("舞监", "演员入场", 30, None),
    ("灯光", "第一幕灯光", 120, None),
    ("音响", "第一幕配乐", 150, None),
    ("舞监", "场景切换A", 25, None),
    ("机械", "转台旋转", 40, 60.0),  # 锁定 60s，早于依赖链允许时间 -> 冲突演示
]

# (cue 序号(1-based), 前置序号, 延迟秒数)
_DEMO_DEPS = [
    (2, 1, 0.0),
    (3, 1, 5.0),
    (4, 2, 0.0),
    (4, 3, 0.0),
    (5, 4, 0.0),
    (6, 2, 10.0),
    (7, 5, 0.0),
    (8, 6, 0.0),
    (8, 7, 0.0),
]


def seed_demo(force: bool = False) -> None:
    """仅在空库（或 force）时写入演示数据。"""
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM cues").fetchone()["c"]
        if count > 0 and not force:
            return
        conn.executescript(_SCHEMA)
        if force:
            conn.execute("DELETE FROM dependencies")
            conn.execute("DELETE FROM cues")
        ids: list[int] = []
        for i, (dept, name, dur, locked) in enumerate(_DEMO_CUES):
            cur = conn.execute(
                "INSERT INTO cues (department, name, duration, locked_start, sort_order)"
                " VALUES (?, ?, ?, ?, ?)",
                (dept, name, dur, locked, i),
            )
            ids.append(cur.lastrowid)
        for cue_idx, pre_idx, delay in _DEMO_DEPS:
            conn.execute(
                "INSERT INTO dependencies (cue_id, depends_on, delay) VALUES (?, ?, ?)",
                (ids[cue_idx - 1], ids[pre_idx - 1], delay),
            )
        conn.commit()
