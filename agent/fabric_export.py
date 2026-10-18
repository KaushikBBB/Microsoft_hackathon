"""Export the verifier's tables to CSV for a Microsoft Fabric Lakehouse (portal upload).

Fabric: Lakehouse lh_ghost > Files > Upload > folder fabric_export/, then on each file
"Load to Tables". Build the Power BI report on the Lakehouse's semantic model.

Usage:  uv run python fabric_export.py      -> ../fabric_export/*.csv
"""
from pathlib import Path

import csv

from calls_db import connect
from report import build_report

TABLES = ["therapists", "therapist_listings", "claims", "complaints", "calls", "verification_runs",
          "listing_triage", "evaluations", "claim_flags"]
OUT = Path(__file__).parent.parent / "fabric_export"
OUTCOME = {"verified_accepting": "Confirmed", "provisionally_accepting": "Provisional",
           "set_not_accepting": "Corrected: not accepting", "suppress_listing": "Removed",
           "pending_review": "Needs review", "not_called": "Not called yet", "no_change": "No change"}
FLAG_LABEL = """CASE flag_type WHEN 'sanctioned_provider' THEN 'Sanctioned provider'
  WHEN 'license_inactive' THEN 'License inactive' WHEN 'contract_ended' THEN 'Contract ended'
  WHEN 'office_not_in_directory' THEN 'Office not in directory'
  WHEN 'listing_removed' THEN 'Listing removed after call' ELSE flag_type END"""

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    with connect() as conn, conn.cursor() as cur:
        for t in TABLES:
            src = t if t != "claim_flags" else f"(SELECT *, {FLAG_LABEL} AS flag_reason FROM claim_flags)"
            with (OUT / f"{t}.csv").open("wb") as f, cur.copy(f"COPY {src} TO STDOUT WITH (FORMAT csv, HEADER true)") as copy:
                for chunk in copy:
                    f.write(chunk)
            print(f"{t}: {(OUT / f'{t}.csv').stat().st_size:,} bytes")
        # one row per listing in the latest run with its final outcome: what the dashboard reads
        report = build_report(conn)
        cols = ["run_id", "listing_id", "full_name", "credential", "city", "claims_verdict", "p_wrong",
                "demand_per_month", "cost_per_failure", "exposure_per_month", "call_rank", "source", "final",
                "front_desk", "needs_review", "true_status", "expected", "correct", "outcome"]
        with (OUT / "run_outcomes.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for e in (report or {}).get("listings", []):
                w.writerow([e.get(c) for c in cols[:-1]] + [OUTCOME.get(e['final'], e['final'])])
        print(f"run_outcomes: {len((report or {}).get('listings', []))} listings")
