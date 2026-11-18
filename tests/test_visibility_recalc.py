import sqlite3
import threading

import pytest

from app.db import get_db
from app.services import (
    batch_waterline,
    close_batch,
    get_report,
    list_reports,
    reconcile,
    run_visibility,
)
from app.services.visibility import VisibilityError


def _inject_mismatch(db_path, delta=3):
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT id, closed_object_count FROM landing_batches "
            "WHERE status='已闭合' ORDER BY id LIMIT 1"
        ).fetchone()
        original = int(row["closed_object_count"])
        conn.execute(
            "UPDATE landing_batches SET closed_object_count=? WHERE id=?",
            (original + delta, row["id"]),
        )
        return row["id"], original


def _fix_mismatch(db_path, batch_id, count):
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE landing_batches SET closed_object_count=? WHERE id=?",
            (count, batch_id),
        )


def _report_count(db_path, day="2026-11-16"):
    with get_db(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM visibility_reports WHERE as_of_date=?",
            (day,),
        ).fetchone()["c"]


@pytest.fixture()
def db(tmp_path):
    from app.db import init_db

    path = str(tmp_path / "vis.db")
    init_db(path)
    return path


def test_mismatch_count_is_stable_not_wall_noise(db):
    """错位条只在实物批真的错位时出现，不随已闭合批数奇偶冒消。"""
    first = run_visibility("2026-11-16", db_path=db)
    second = run_visibility("2026-11-16", db_path=db)
    assert first["mismatch_count"] == 0
    assert second["mismatch_count"] == 0
    assert first["summary"] == second["summary"]


def test_recalc_replaces_same_day_report(db):
    run_visibility("2026-11-16", db_path=db)
    run_visibility("2026-11-16", db_path=db)
    assert _report_count(db) == 1

    reports = list_reports(db_path=db)
    assert len(reports) == 1
    detail = get_report(reports[0]["id"], db_path=db)
    assert detail["mismatches"] == []


def test_old_mismatch_does_not_hang_after_recalc(db):
    batch_id, closed = _inject_mismatch(db)

    bad = run_visibility("2026-11-16", db_path=db)
    assert bad["mismatch_count"] == 1
    assert bad["new_mismatch_count"] == 1
    assert bad["mismatch_delta"] == 1
    assert [m["state"] for m in bad["mismatches"]] == ["新增"]
    assert bad["mismatches"][0]["batch_id"] == batch_id

    carried = run_visibility("2026-11-16", db_path=db)
    assert carried["carried_mismatch_count"] == 1
    assert carried["mismatch_delta"] == 0

    _fix_mismatch(db, batch_id, closed)
    cleared = run_visibility("2026-11-16", db_path=db)
    assert cleared["mismatch_count"] == 0
    assert cleared["cleared_mismatch_count"] == 1
    assert cleared["mismatch_delta"] == -1

    # 整笔替换：旧报告与旧错位行都不再挂库，只剩本次“已消除”留痕。
    reports = list_reports(db_path=db)
    assert len(reports) == 1
    detail = get_report(reports[0]["id"], db_path=db)
    assert [m["state"] for m in detail["mismatches"]] == ["已消除"]
    # 留痕记的是上一轮错位本身：清单数偏了 3，实物数未变。
    row = detail["mismatches"][0]
    assert row["manifest_object_count"] == closed + 3
    assert row["physical_object_count"] == closed
    assert row["delta"] == 3


def test_summary_matches_batches_and_waterline(db):
    _inject_mismatch(db)
    report = run_visibility("2026-11-16", db_path=db)
    water = batch_waterline("2026-11-16", db_path=db)
    assert report["mismatch_count"] == water["mismatch_count"] == 1
    assert water["watermark"] == "2026-11-12"
    by_id = {w["batch_id"]: w for w in water["items"]}
    for m in report["mismatches"]:
        if m["state"] == "已消除":
            continue
        assert by_id[m["batch_id"]]["manifest_object_count"] == m["manifest_object_count"]
        assert by_id[m["batch_id"]]["physical_object_count"] == m["physical_object_count"]
        assert by_id[m["batch_id"]]["aligned"] is False


def test_reconcile_two_rounds_stable_and_stored(db):
    result = reconcile("2026-11-16", rounds=2, db_path=db)
    assert result["stable"] is True
    assert result["runs"][0] == result["runs"][1]
    stored = next(
        r for r in list_reports(db_path=db) if r["as_of_date"] == "2026-11-16"
    )
    assert stored["mismatch_count"] == result["counters"]["mismatch_count"]
    assert stored["closed_batch_count"] == result["counters"]["closed_batch_count"]


def test_concurrent_recalc_single_report_no_double_write(db):
    errors = []

    def worker():
        try:
            run_visibility("2026-11-16", db_path=db, rounds=2)
        except Exception as exc:  # pragma: no cover - 失败时用于报错
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert _report_count(db) == 1
    with get_db(db) as conn:
        # 摘要与实物批仍对齐
        mismatch = conn.execute(
            "SELECT COUNT(*) AS c FROM landing_batches b "
            "WHERE b.status='已闭合' AND b.closed_object_count IS NOT NULL "
            "AND b.closed_object_count != ("
            "  SELECT COUNT(*) FROM landing_objects o "
            "  WHERE o.batch_id=b.id AND o.status='已闭合')"
        ).fetchone()["c"]
        report = conn.execute(
            "SELECT mismatch_count FROM visibility_reports WHERE as_of_date='2026-11-16'"
        ).fetchone()
    assert report["mismatch_count"] == mismatch


def test_legacy_duplicate_same_day_reports_are_all_cleared(db):
    """修复前可能已有多笔同日摘要；重算须一并清掉，不留孤儿错位行。"""
    for _ in range(3):
        run_visibility("2026-11-16", db_path=db)
    with get_db(db) as conn:
        row = conn.execute(
            "SELECT id FROM landing_batches WHERE status='已闭合' LIMIT 1"
        ).fetchone()
        for i in range(2):  # 手工塞两笔历史摘要 + 错位行
            cur = conn.execute(
                "INSERT INTO visibility_reports("
                "as_of_date, closed_batch_count, open_batch_count, object_count, "
                "orphan_object_count, mismatch_count, summary) VALUES (?,?,?,?,?,?,?)",
                ("2026-11-16", 1, 3, 2, 0, 1, f"旧摘要 {i}"),
            )
            conn.execute(
                "INSERT INTO visibility_mismatches("
                "report_id, batch_id, zone_code, zone_name, biz_date, "
                "manifest_object_count, physical_object_count, delta, state) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (cur.lastrowid, row["id"], "silver_orders", "订单银区", "2026-11-12", 5, 2, 3, "新增"),
            )
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM visibility_reports WHERE as_of_date='2026-11-16'"
        ).fetchone()["c"] == 3

    run_visibility("2026-11-16", db_path=db)
    with get_db(db) as conn:
        reps = conn.execute(
            "SELECT COUNT(*) AS c FROM visibility_reports WHERE as_of_date='2026-11-16'"
        ).fetchone()["c"]
        orphan_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM visibility_mismatches m "
            "LEFT JOIN visibility_reports r ON r.id=m.report_id WHERE r.id IS NULL"
        ).fetchone()["c"]
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT report_id, state, manifest_object_count, physical_object_count "
                "FROM visibility_mismatches"
            ).fetchall()
        ]
    assert reps == 1
    assert orphan_rows == 0
    # 旧的三笔同日摘要全部清掉；旧错位只作为本次“已消除”留痕一行。
    assert len(rows) == 1
    assert rows[0]["state"] == "已消除"
    assert rows[0]["manifest_object_count"] == 5
    assert rows[0]["physical_object_count"] == 2


def test_failed_recalc_keeps_previous_report(db, monkeypatch):
    run_visibility("2026-11-16", db_path=db)
    before = list_reports(db_path=db)

    from app.services import visibility as vis

    def boom(conn, day, snap):
        raise VisibilityError("错位摘要与实物批不齐，重算已停", 409)

    monkeypatch.setattr(vis, "_assert_aligned", boom)
    with pytest.raises(VisibilityError):
        run_visibility("2026-11-16", db_path=db)

    after = list_reports(db_path=db)
    assert len(after) == len(before) == 1
    assert after[0]["id"] == before[0]["id"]
    assert after[0]["summary"] == before[0]["summary"]
