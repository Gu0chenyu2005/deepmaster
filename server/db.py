# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""SQLite 数据访问与版本化迁移。"""

import os
import sqlite3
import time
from contextlib import contextmanager

DB_ENV = "DM_DB"
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deepmaster.db")

def db_path():
    return os.environ.get(DB_ENV, DEFAULT_DB)


@contextmanager
def connect():
    """创建连接；上下文退出时统一提交或回滚。"""
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


MIGRATIONS = []


def migration(version, name):
    def deco(fn):
        MIGRATIONS.append((version, name, fn))
        return fn
    return deco


@migration(1, "initial schema")
def _m1(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      account       TEXT    NOT NULL UNIQUE,
      password_hash TEXT    NOT NULL,
      role          TEXT    NOT NULL DEFAULT 'operator',   -- operator | reviewer | admin
      created_at    TEXT    NOT NULL,
      last_login_at TEXT
    );

    CREATE TABLE IF NOT EXISTS settings (
      key        TEXT PRIMARY KEY,
      value      TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sessions (
      id           TEXT PRIMARY KEY,                        -- S<epoch_ms>
      folder       TEXT    NOT NULL,
      dir_path     TEXT    NOT NULL,
      started_at   TEXT    NOT NULL,
      ended_at     TEXT,
      duration_s   INTEGER NOT NULL DEFAULT 0,
      state        TEXT    NOT NULL DEFAULT 'running',      -- running | done | error
      error        TEXT,
      created_by   TEXT    REFERENCES users(account) ON DELETE SET NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_dir ON sessions(dir_path);

    CREATE TABLE IF NOT EXISTS records (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      dir_path   TEXT    NOT NULL,
      name       TEXT    NOT NULL,
      size       INTEGER NOT NULL DEFAULT 0,
      mtime      INTEGER NOT NULL DEFAULT 0,
      session_id TEXT    REFERENCES sessions(id) ON DELETE SET NULL,
      synced_at  TEXT    NOT NULL,
      UNIQUE(dir_path, name)
    );
    CREATE INDEX IF NOT EXISTS idx_records_dir ON records(dir_path);
    CREATE INDEX IF NOT EXISTS idx_records_session ON records(session_id);

    CREATE TABLE IF NOT EXISTS reviews (
      dir_path    TEXT    NOT NULL,
      episode     INTEGER NOT NULL,
      status      TEXT    NOT NULL DEFAULT '',              -- good | bad | pending | ''
      comment     TEXT    NOT NULL DEFAULT '',
      reviewed_at TEXT    NOT NULL,
      reviewed_by TEXT    REFERENCES users(account) ON DELETE SET NULL,
      PRIMARY KEY (dir_path, episode)
    );
    CREATE INDEX IF NOT EXISTS idx_reviews_dir ON reviews(dir_path);

    CREATE TABLE IF NOT EXISTS segments (
      dir_path  TEXT    NOT NULL,
      episode   INTEGER NOT NULL,
      seq       INTEGER NOT NULL,
      from_frame INTEGER NOT NULL,
      to_frame  INTEGER NOT NULL,
      label     TEXT,
      PRIMARY KEY (dir_path, episode, seq)
    );
    CREATE INDEX IF NOT EXISTS idx_segments_dir ON segments(dir_path);
    """)


@migration(2, "audit log")
def _m2(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS audit_log (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      at         TEXT NOT NULL,
      account    TEXT,
      action     TEXT NOT NULL,
      target     TEXT,
      detail     TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);
    """)


@migration(3, "review dwell seconds")
def _m3(conn):
    """记录逐条审核时长。"""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS review_seconds (
      dir_path   TEXT    NOT NULL,
      episode    INTEGER NOT NULL,
      seconds    REAL    NOT NULL DEFAULT 0,
      updated_at TEXT    NOT NULL,
      account    TEXT,
      PRIMARY KEY (dir_path, episode)
    );
    CREATE INDEX IF NOT EXISTS idx_review_seconds_dir ON review_seconds(dir_path);
    """)


@migration(4, "password reset codes")
def _m4(conn):
    """添加一次性密码重置码。"""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS reset_codes (
      code       TEXT PRIMARY KEY,           -- 一次性随机码
      account    TEXT NOT NULL,              -- 目标账号
      created_at TEXT NOT NULL,
      created_by TEXT,
      expires_at TEXT NOT NULL,
      used_at    TEXT,                        -- 用过就记时间，不再可用
      used_ip    TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_reset_account ON reset_codes(account);
    """)


@migration(5, "merge operator and reviewer into user")
def _m5(conn):
    """将采集员与审核员角色统一为 user。"""
    conn.execute("UPDATE users SET role='user' WHERE role IN ('operator','reviewer')")


@migration(6, "display name and collector marker")
def _m6(conn):
    """添加展示名与采集者字段。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "display_name" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT")

    conn.execute(
        "UPDATE users SET display_name=account WHERE display_name IS NULL OR display_name=''")

    cols = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
    if "collector" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN collector TEXT")
    if "collector_name" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN collector_name TEXT")

@migration(7, "annotation time ledger")
def _m7(conn):
    """添加审核时长流水。"""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS review_time_ledger (
      id       INTEGER PRIMARY KEY AUTOINCREMENT,
      at       TEXT    NOT NULL,              -- 本次上报的落库时间
      dir_path TEXT    NOT NULL,              -- 规范化后的目录（rel_to_data）
      episode  INTEGER NOT NULL,
      seconds  REAL    NOT NULL,
      account  TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_rtl_at ON review_time_ledger(at);
    CREATE INDEX IF NOT EXISTS idx_rtl_account ON review_time_ledger(account);
    CREATE INDEX IF NOT EXISTS idx_rtl_dir ON review_time_ledger(dir_path);
    """)
@migration(9, "normalize annotation time into ledger")
def _m8(conn):
    """将历史审核时长归一至 review_time_ledger，避免重复计数。"""
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_time_ledger'"
    ).fetchone()
    if not has:
        return
    if conn.execute("SELECT value FROM settings WHERE key='rtl_normalized'").fetchone():
        return

    rows = conn.execute(
        "SELECT dir_path, episode, seconds, updated_at, account FROM review_seconds"
    ).fetchall()
    for r in rows:
        secs = float(r["seconds"] or 0)
        if secs <= 0:
            continue
        conn.execute(
            "INSERT INTO review_time_ledger(at, dir_path, episode, seconds, account) "
            "VALUES (?,?,?,?,?)",
            (r["updated_at"] or now(), r["dir_path"], int(r["episode"]),
             secs, r["account"]))
    conn.execute("DELETE FROM review_seconds")
    conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES ('rtl_normalized','done',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (now(),))

@migration(10, "error log")
def _m10(conn):
    """记录前端及接口错误，供统计查询。"""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS error_log (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      at         TEXT    NOT NULL,
      kind       TEXT    NOT NULL,
      message    TEXT    NOT NULL,
      detail     TEXT,
      url        TEXT,
      account    TEXT,
      created_at TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_error_at ON error_log(at);
    CREATE INDEX IF NOT EXISTS idx_error_kind ON error_log(kind);
    """)


def migrate():
    """执行未应用的迁移；executescript 自行提交，不使用外层事务。"""
    applied = []
    with connect() as conn:
        conn.execute("""
          CREATE TABLE IF NOT EXISTS _migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
          )""")
        done = {r["version"] for r in conn.execute("SELECT version FROM _migrations")}
        for version, name, fn in sorted(MIGRATIONS, key=lambda m: m[0]):
            if version in done:
                continue
            fn(conn)
            conn.execute(
                "INSERT OR REPLACE INTO _migrations(version, name, applied_at) VALUES (?,?,?)",
                (version, name, now()))
            applied.append((version, name))
    return applied


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now()))


def audit(conn, account, action, target=None, detail=None):
    conn.execute(
        "INSERT INTO audit_log(at, account, action, target, detail) VALUES (?,?,?,?,?)",
        (now(), account, action, target, detail))


if __name__ == "__main__":
    ran = migrate()
    print("db:", db_path())
    if ran:
        for v, n in ran:
            print("  applied", v, n)
    else:
        print("  already up to date")
