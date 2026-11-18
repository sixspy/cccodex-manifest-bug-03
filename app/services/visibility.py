"""可见性对账：错位列表 / 报告详情 / 批次水线 / 对账复跑台共用同一快照。

落库形态（拍板）：整笔回滚替换。
  同一 as_of_date 的旧摘要连同旧错位明细先清掉，再写入本次快照；
  旧错位不会以"历史悬挂"的方式残留在列表里，只会作为新报告的
  “已消除”行留痕（同报告内可视，不跨报告挂着）。
写入在一个 BEGIN IMMEDIATE 事务里完成，失败整笔回滚，旧摘要原样保留。

摘要与实物批对齐校验：写库前在同一事务内回读实物表重算一遍计数，
与待写摘要逐项比对；有任何一项偏了就整笔停下，不落半截摘要。
"""

from dataclasses import dataclass

from app.db import get_db as _read_db
from app.db import get_db_immediate


class VisibilityError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class Snapshot:
    as_of_date: str
    closed_batch_count: int
    open_batch_count: int
    object_count: int
    orphan_object_count: int
    mismatch_count: int
    summary: str
    waterline: list[dict]
    mismatches: list[dict]


def list_reports(db_path: str | None = None) -> list[dict]:
    with _read_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM visibility_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_report(report_id: int, db_path: str | None = None) -> dict | None:
    with _read_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM visibility_reports WHERE id=?", (report_id,)
        ).fetchone()
        if not row:
            return None
        report = dict(row)
        report["mismatches"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM visibility_mismatches WHERE report_id=? "
                "ORDER BY CASE state WHEN '新增' THEN 0 WHEN '沿用' THEN 1 ELSE 2 END, "
                "delta DESC, batch_id",
                (report_id,),
            ).fetchall()
        ]
        return report


def batch_waterline(as_of_date: str, db_path: str | None = None) -> dict:
    """批次水线：每条批次同一时点上的清单数与实物数，以及是否对齐。"""
    day = _normalize_day(as_of_date)
    with _read_db(db_path) as conn:
        snap = _snapshot(conn, day)
        return {
            "as_of_date": day,
            "watermark": snap.waterline[0]["watermark"] if snap.waterline else None,
            "mismatch_count": snap.mismatch_count,
            "items": snap.waterline,
        }


def run_visibility(
    as_of_date: str,
    db_path: str | None = None,
    rounds: int = 1,
) -> dict:
    """重算并整笔替换当日摘要。rounds>=2 时先做多轮一致性对照再落库。"""
    day = _normalize_day(as_of_date)
    rounds = max(1, min(int(rounds), 5))

    with get_db_immediate(db_path) as conn:
        snapshots = [_snapshot(conn, day) for _ in range(rounds)]
        _assert_rounds_stable(snapshots)
        snap = snapshots[-1]

        prev = conn.execute(
            "SELECT * FROM visibility_reports WHERE as_of_date=? ORDER BY id DESC LIMIT 1",
            (day,),
        ).fetchone()
        prev_mismatch_count = int(prev["mismatch_count"]) if prev else 0
        prev_batches = set()
        if prev:
            prev_batches = {
                int(r["batch_id"])
                for r in conn.execute(
                    "SELECT batch_id FROM visibility_mismatches WHERE report_id=? "
                    "AND state != '已消除'",
                    (prev["id"],),
                ).fetchall()
            }

        current = {m["batch_id"]: m for m in snap.mismatches if m["state"] != "已消除"}
        for m in current.values():
            m["state"] = "沿用" if (prev and m["batch_id"] in prev_batches) else "新增"

        cleared: list[dict] = []
        if prev:
            for r in conn.execute(
                "SELECT * FROM visibility_mismatches WHERE report_id=? "
                "AND state != '已消除' ORDER BY batch_id",
                (prev["id"],),
            ).fetchall():
                bid = int(r["batch_id"])
                if bid in current:
                    continue
                # 已消除行保留上轮登记的原值，作为历史事实留痕一次。
                cleared.append(
                    {
                        "batch_id": bid,
                        "zone_code": r["zone_code"],
                        "zone_name": r["zone_name"],
                        "biz_date": r["biz_date"],
                        "manifest_object_count": int(r["manifest_object_count"]),
                        "physical_object_count": int(r["physical_object_count"]),
                        "delta": int(r["delta"]),
                        "state": "已消除",
                    }
                )
        snap = Snapshot(
            as_of_date=snap.as_of_date,
            closed_batch_count=snap.closed_batch_count,
            open_batch_count=snap.open_batch_count,
            object_count=snap.object_count,
            orphan_object_count=snap.orphan_object_count,
            mismatch_count=snap.mismatch_count,
            summary=snap.summary,
            waterline=snap.waterline,
            mismatches=list(current.values()) + cleared,
        )

        newly = sum(1 for m in current.values() if m["state"] == "新增")
        carried = sum(1 for m in current.values() if m["state"] == "沿用")
        cleared_n = len(cleared)
        mismatch_delta = newly - cleared_n

        # 摘要与实物批对齐校验：同一事务内回读实物表复核。
        _assert_aligned(conn, day, snap)

        if prev:
            # 整笔替换：同日可能残留多笔历史摘要，连同其错位明细一并清掉，
            # 不留孤儿错位行，也不让旧错位跨报告悬挂。
            conn.execute(
                "DELETE FROM visibility_mismatches WHERE report_id IN ("
                " SELECT id FROM visibility_reports WHERE as_of_date=?)",
                (day,),
            )
            conn.execute(
                "DELETE FROM visibility_reports WHERE as_of_date=?", (day,)
            )

        summary = snap.summary
        cur = conn.execute(
            "INSERT INTO visibility_reports("
            "as_of_date, closed_batch_count, open_batch_count, object_count, "
            "orphan_object_count, mismatch_count, summary, "
            "prev_mismatch_count, mismatch_delta, new_mismatch_count, "
            "carried_mismatch_count, cleared_mismatch_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                day,
                snap.closed_batch_count,
                snap.open_batch_count,
                snap.object_count,
                snap.orphan_object_count,
                snap.mismatch_count,
                summary,
                prev_mismatch_count,
                mismatch_delta,
                newly,
                carried,
                cleared_n,
            ),
        )
        report_id = int(cur.lastrowid)
        for m in snap.mismatches:
            conn.execute(
                "INSERT INTO visibility_mismatches("
                "report_id, batch_id, zone_code, zone_name, biz_date, "
                "manifest_object_count, physical_object_count, delta, state) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    report_id,
                    m["batch_id"],
                    m["zone_code"],
                    m["zone_name"],
                    m["biz_date"],
                    m["manifest_object_count"],
                    m["physical_object_count"],
                    m["delta"],
                    m["state"],
                ),
            )

        report = dict(
            conn.execute(
                "SELECT * FROM visibility_reports WHERE id=?", (report_id,)
            ).fetchone()
        )
        report["mismatches"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM visibility_mismatches WHERE report_id=? "
                "ORDER BY CASE state WHEN '新增' THEN 0 WHEN '沿用' THEN 1 ELSE 2 END, "
                "delta DESC, batch_id",
                (report_id,),
            ).fetchall()
        ]
        report["waterline"] = snap.waterline
        report["stable_rounds"] = rounds
        return report


def reconcile(as_of_date: str, rounds: int = 2, db_path: str | None = None) -> dict:
    """对账复跑台：连跑多轮，数字必须逐项一致才算过；最终摘要落库。"""
    day = _normalize_day(as_of_date)
    rounds = max(2, min(int(rounds), 5))
    runs: list[dict] = []
    for _ in range(rounds):
        report = run_visibility(day, db_path=db_path)
        runs.append(_counters(report))
    stable = all(c == runs[0] for c in runs[1:])
    if not stable:
        raise VisibilityError("复跑数字不稳，对账未过", 409)
    latest = list_reports(db_path)
    latest_same_day = next((r for r in latest if r["as_of_date"] == day), None)
    stored = _counters(latest_same_day) if latest_same_day else None
    if stored != runs[-1]:
        raise VisibilityError("摘要与复跑结果不齐，对账未过", 409)
    return {
        "as_of_date": day,
        "rounds": rounds,
        "stable": True,
        "counters": runs[-1],
        "runs": runs,
        "report_id": latest_same_day["id"] if latest_same_day else None,
        "summary": latest_same_day["summary"] if latest_same_day else "",
        "waterline": batch_waterline(day, db_path=db_path),
    }


def _normalize_day(as_of_date: str | None) -> str:
    day = (as_of_date or "").strip()
    return day or "2026-11-16"


def _counters(report: dict | None) -> dict:
    if not report:
        return {}
    return {
        "as_of_date": report.get("as_of_date"),
        "closed_batch_count": int(report.get("closed_batch_count") or 0),
        "open_batch_count": int(report.get("open_batch_count") or 0),
        "object_count": int(report.get("object_count") or 0),
        "orphan_object_count": int(report.get("orphan_object_count") or 0),
        "mismatch_count": int(report.get("mismatch_count") or 0),
    }


def _snapshot(conn, day: str) -> Snapshot:
    closed = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM landing_batches "
            "WHERE status='已闭合' AND biz_date<=?",
            (day,),
        ).fetchone()["c"]
    )
    open_n = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM landing_batches "
            "WHERE status='收集中' AND biz_date<=?",
            (day,),
        ).fetchone()["c"]
    )
    objects = int(
        conn.execute("SELECT COUNT(*) AS c FROM landing_objects").fetchone()["c"]
    )
    orphan = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM landing_objects o "
            "JOIN landing_batches b ON b.id=o.batch_id WHERE b.status='已作废'"
        ).fetchone()["c"]
    )

    rows = conn.execute(
        "SELECT b.id, b.zone_code, b.zone_name, b.biz_date, b.status, "
        "b.closed_object_count, "
        "(SELECT COUNT(*) FROM landing_objects o "
        " WHERE o.batch_id=b.id AND o.status='已闭合') AS physical "
        "FROM landing_batches b ORDER BY b.biz_date DESC, b.id"
    ).fetchall()

    watermark = next(
        (r["biz_date"] for r in rows if r["status"] == "已闭合" and r["biz_date"] <= day),
        None,
    )
    waterline: list[dict] = []
    mismatches: list[dict] = []
    for r in rows:
        physical = int(r["physical"])
        manifest = r["closed_object_count"]
        manifest_n = int(manifest) if manifest is not None else None
        aligned = manifest_n is None or manifest_n == physical
        item = {
            "batch_id": r["id"],
            "zone_code": r["zone_code"],
            "zone_name": r["zone_name"],
            "biz_date": r["biz_date"],
            "status": r["status"],
            "manifest_object_count": manifest_n,
            "physical_object_count": physical,
            "delta": (manifest_n - physical) if manifest_n is not None else 0,
            "aligned": aligned,
            "watermark": watermark,
        }
        waterline.append(item)
        if r["status"] == "已闭合" and manifest_n is not None and not aligned:
            mismatches.append(
                {
                    "batch_id": r["id"],
                    "zone_code": r["zone_code"],
                    "zone_name": r["zone_name"],
                    "biz_date": r["biz_date"],
                    "manifest_object_count": manifest_n,
                    "physical_object_count": physical,
                    "delta": manifest_n - physical,
                    "state": "新增",
                }
            )

    mismatch_count = len(mismatches)
    summary = (
        f"截至 {day}：已闭合 {closed} 批，收集中 {open_n} 批，"
        f"对象 {objects}，孤儿 {orphan}，错位 {mismatch_count}"
    )
    return Snapshot(
        as_of_date=day,
        closed_batch_count=closed,
        open_batch_count=open_n,
        object_count=objects,
        orphan_object_count=orphan,
        mismatch_count=mismatch_count,
        summary=summary,
        waterline=waterline,
        mismatches=mismatches,
    )


def _assert_rounds_stable(snapshots: list[Snapshot]) -> None:
    base = snapshots[0]
    for snap in snapshots[1:]:
        same = (
            snap.closed_batch_count == base.closed_batch_count
            and snap.open_batch_count == base.open_batch_count
            and snap.object_count == base.object_count
            and snap.orphan_object_count == base.orphan_object_count
            and snap.mismatch_count == base.mismatch_count
            and snap.mismatches == base.mismatches
            and snap.waterline == base.waterline
        )
        if not same:
            raise VisibilityError("复跑数字不稳，重算已停", 409)


def _assert_aligned(conn, day: str, snap: Snapshot) -> None:
    """摘要逐项对实物表复核；偏一项就停，不落半截摘要。"""
    physical = _snapshot(conn, day)
    checks = (
        ("已闭合批", snap.closed_batch_count, physical.closed_batch_count),
        ("收集中批", snap.open_batch_count, physical.open_batch_count),
        ("对象", snap.object_count, physical.object_count),
        ("孤儿", snap.orphan_object_count, physical.orphan_object_count),
        ("错位", snap.mismatch_count, physical.mismatch_count),
    )
    for label, written, actual in checks:
        if written != actual:
            raise VisibilityError(f"{label}摘要与实物批不齐，重算已停", 409)

    open_rows = [m for m in snap.mismatches if m["state"] != "已消除"]
    if len(open_rows) != snap.mismatch_count:
        raise VisibilityError("错位列表与错位条数不齐，重算已停", 409)
    physical_by_id = {m["batch_id"]: m for m in physical.mismatches}
    if {m["batch_id"]: m["delta"] for m in open_rows} != {
        bid: m["delta"] for bid, m in physical_by_id.items()
    }:
        raise VisibilityError("错位明细与实物批不齐，重算已停", 409)

    expected_summary = (
        f"截至 {snap.as_of_date}：已闭合 {snap.closed_batch_count} 批，"
        f"收集中 {snap.open_batch_count} 批，对象 {snap.object_count}，"
        f"孤儿 {snap.orphan_object_count}，错位 {snap.mismatch_count}"
    )
    if snap.summary != expected_summary:
        raise VisibilityError("摘要文案与数字不齐，重算已停", 409)
