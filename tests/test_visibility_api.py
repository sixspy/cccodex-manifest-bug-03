"""可见性重算的 API 级验收：四处同查、失败短句、复跑对照一致。"""


def _inject_mismatch(db_path, delta=2):
    from app.db import get_db

    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT id, closed_object_count FROM landing_batches "
            "WHERE status='已闭合' ORDER BY id LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE landing_batches SET closed_object_count=? WHERE id=?",
            (int(row["closed_object_count"]) + delta, row["id"]),
        )
        return row["id"], int(row["closed_object_count"])


def test_report_endpoint_returns_list_detail_and_delta(authed):
    import os

    db_path = os.environ["DATABASE_PATH"]
    run = authed.post(
        "/api/visibility-reports", json={"as_of_date": "2026-11-16"}
    )
    assert run.status_code == 200
    first = run.json()["report"]
    assert first["mismatch_count"] == 0
    assert "错位 0" in first["summary"]

    _inject_mismatch(db_path)
    second = authed.post(
        "/api/visibility-reports", json={"as_of_date": "2026-11-16"}
    ).json()["report"]
    assert second["mismatch_count"] == 1
    assert second["new_mismatch_count"] == 1
    assert second["mismatch_delta"] == 1
    assert second["mismatches"][0]["state"] == "新增"

    items = authed.get("/api/visibility-reports").json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == second["id"]

    detail = authed.get(f"/api/visibility-reports/{second['id']}").json()["report"]
    assert detail["mismatch_count"] == second["mismatch_count"]
    assert detail["mismatches"] == second["mismatches"]


def test_waterline_and_report_share_numbers(authed):
    db_path = __import__("os").environ["DATABASE_PATH"]
    _inject_mismatch(db_path)
    report = authed.post(
        "/api/visibility-reports", json={"as_of_date": "2026-11-16"}
    ).json()["report"]
    water = authed.get(
        "/api/visibility-waterline", params={"as_of_date": "2026-11-16"}
    ).json()
    assert water["mismatch_count"] == report["mismatch_count"]
    assert water["watermark"] == "2026-11-12"
    open_rows = [m for m in report["mismatches"] if m["state"] != "已消除"]
    assert len(open_rows) == report["mismatch_count"]


def test_reconcile_endpoint_two_rounds(authed):
    result = authed.post(
        "/api/visibility-reconcile",
        json={"as_of_date": "2026-11-16", "rounds": 2},
    )
    assert result.status_code == 200
    payload = result.json()["result"]
    assert payload["stable"] is True
    assert payload["runs"][0] == payload["runs"][1]
    assert payload["report_id"] is not None

    stored = authed.get("/api/visibility-reports").json()["items"][0]
    assert stored["id"] == payload["report_id"]
    assert stored["mismatch_count"] == payload["counters"]["mismatch_count"]


def test_report_detail_404(authed):
    r = authed.get("/api/visibility-reports/9999")
    assert r.status_code == 404


def test_recalc_failure_returns_short_sentence(authed, monkeypatch):
    from app.services import visibility as vis

    def boom(conn, day, snap):
        raise vis.VisibilityError("错位摘要与实物批不齐，重算已停", 409)

    monkeypatch.setattr(vis, "_assert_aligned", boom)
    r = authed.post("/api/visibility-reports", json={"as_of_date": "2026-11-16"})
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "错位摘要与实物批不齐，重算已停"
    assert len(body["error"]) <= 20
