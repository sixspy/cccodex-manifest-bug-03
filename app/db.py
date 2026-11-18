import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_db_immediate(db_path: str | None = None):
    """写事务：进入即抢占 SQLite 写锁，读-算-清旧-写新整笔收口。

    重算走这里，两名值班并发时串行化：后到者等锁，等到的是已提交的旧摘要，
    替换后仍只有一笔摘要，不会双写。失败整笔回滚，旧摘要原样保留。
    """
    conn = connect(db_path)
    try:
        conn.isolation_level = None  # 手动控制事务边界
        _begin_immediate(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _begin_immediate(conn: sqlite3.Connection, attempts: int = 5) -> None:
    """抢写锁；WAL 下偶发 SQLITE_BUSY 再退避重试，仍抢不到才报错。"""
    for i in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))


SCHEMA = """
CREATE TABLE IF NOT EXISTS landing_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  zone_code TEXT NOT NULL,
  zone_name TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('收集中','已闭合','已作废')),
  closed_object_count INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(zone_code, biz_date)
);

CREATE TABLE IF NOT EXISTS landing_objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  object_key TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('已登记','已闭合','已作废')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(batch_id, object_key),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS close_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL UNIQUE,
  object_count INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS visibility_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  closed_batch_count INTEGER NOT NULL,
  open_batch_count INTEGER NOT NULL,
  object_count INTEGER NOT NULL,
  orphan_object_count INTEGER NOT NULL,
  mismatch_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  prev_mismatch_count INTEGER NOT NULL DEFAULT 0,
  mismatch_delta INTEGER NOT NULL DEFAULT 0,
  new_mismatch_count INTEGER NOT NULL DEFAULT 0,
  carried_mismatch_count INTEGER NOT NULL DEFAULT 0,
  cleared_mismatch_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS visibility_mismatches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id INTEGER NOT NULL,
  batch_id INTEGER NOT NULL,
  zone_code TEXT NOT NULL,
  zone_name TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  manifest_object_count INTEGER NOT NULL,
  physical_object_count INTEGER NOT NULL,
  delta INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('新增','沿用','已消除')),
  FOREIGN KEY(report_id) REFERENCES visibility_reports(id) ON DELETE CASCADE,
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE INDEX IF NOT EXISTS idx_visibility_mismatches_report
  ON visibility_mismatches(report_id);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

REPORT_EXTRA_COLUMNS = {
    "prev_mismatch_count": "INTEGER NOT NULL DEFAULT 0",
    "mismatch_delta": "INTEGER NOT NULL DEFAULT 0",
    "new_mismatch_count": "INTEGER NOT NULL DEFAULT 0",
    "carried_mismatch_count": "INTEGER NOT NULL DEFAULT 0",
    "cleared_mismatch_count": "INTEGER NOT NULL DEFAULT 0",
}

SEED_BATCHES = [
    ("bronze_orders", "订单铜区", "2026-11-14", "收集中"),
    ("bronze_payments", "支付铜区", "2026-11-14", "收集中"),
    ("bronze_events", "事件铜区", "2026-11-13", "收集中"),
    ("silver_orders", "订单银区", "2026-11-12", "已闭合"),
]


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        # WAL 是库级持久设置：读写不互斥，重算写锁只挡其它写，不挡页面读。
        # 旧库（DELETE 日志模式）在此一次性切换。
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(visibility_reports)").fetchall()
        }
        for name, decl in REPORT_EXTRA_COLUMNS.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE visibility_reports ADD COLUMN {name} {decl}")
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if row:
            return
        for zone, name, biz, status in SEED_BATCHES:
            conn.execute(
                "INSERT INTO landing_batches(zone_code, zone_name, biz_date, status, closed_object_count) "
                "VALUES (?,?,?,?,?)",
                (zone, name, biz, status, 2 if status == "已闭合" else None),
            )
        closed = conn.execute(
            "SELECT id FROM landing_batches WHERE status='已闭合'"
        ).fetchone()
        if closed:
            bid = closed["id"]
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                "VALUES (?,?,?,?,?)",
                (bid, "part-000.parquet", "hash-silver-a", 1024, "已闭合"),
            )
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                "VALUES (?,?,?,?,?)",
                (bid, "part-001.parquet", "hash-silver-b", 2048, "已闭合"),
            )
            conn.execute(
                "INSERT INTO close_runs(batch_id, object_count, ok, detail) VALUES (?,?,1,?)",
                (bid, 2, "种子闭合"),
            )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('seeded','1')"
        )
