#!/usr/bin/env python3
"""对账复跑台（编排/本机通用）：连跑多轮，数字不稳即非零退出。

用法：
  python scripts/reconcile_check.py --as-of 2026-11-16 --rounds 2
  DATABASE_PATH=/app/instance/manifest_lab.db python scripts/reconcile_check.py

判定：每轮摘要五项计数与错位明细须逐项一致，且与落库摘要一致。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db  # noqa: E402
from app.services import reconcile  # noqa: E402
from app.services.visibility import VisibilityError  # noqa: E402

KEYS = (
    "closed_batch_count",
    "open_batch_count",
    "object_count",
    "orphan_object_count",
    "mismatch_count",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="可见性对账复跑校验")
    parser.add_argument("--as-of", default="2026-11-16")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    init_db(args.db)
    try:
        result = reconcile(args.as_of, rounds=max(2, args.rounds), db_path=args.db)
    except VisibilityError as exc:
        print(json.dumps({"ok": False, "error": exc.message}, ensure_ascii=False))
        return 2

    lines = []
    for i, run in enumerate(result["runs"], 1):
        lines.append({"round": i, **{k: run[k] for k in KEYS}})
    stable = all(
        run[k] == result["runs"][0][k] for run in result["runs"] for k in KEYS
    )
    stored_ok = all(
        result["counters"][k] == result["runs"][-1][k] for k in KEYS
    )
    payload = {
        "ok": stable and stored_ok,
        "as_of_date": result["as_of_date"],
        "rounds": result["rounds"],
        "stable": stable,
        "stored_matches": stored_ok,
        "runs": lines,
        "stored": {k: result["counters"][k] for k in KEYS},
        "waterline_rows": len(result["waterline"]["items"]),
        "report_id": result["report_id"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
