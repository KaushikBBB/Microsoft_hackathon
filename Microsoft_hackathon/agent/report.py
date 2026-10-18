"""The operator's report for one verification run (served at /report)."""
from collections import Counter

# What a correct final decision looks like for each hidden true_status (prototype scoring only)
EXPECTED = {
    "active_open": "verified_accepting",
    "full_caseload": "set_not_accepting",
    "moved": "suppress_listing",
    "retired": "suppress_listing",
    "wrong_phone": "suppress_listing",
}


def build_report(conn, run_id: int | None = None) -> dict | None:
    if run_id is None:
        row = conn.execute("SELECT max(run_id) FROM verification_runs").fetchone()
        run_id = row[0]
        if run_id is None:
            return None
    cur = conn.execute("""SELECT run_id, started_at, finished_at, status, listing_offset, listing_limit, note, summary
                          FROM verification_runs WHERE run_id = %s""", (run_id,))
    run = dict(zip([d.name for d in cur.description], cur.fetchone()))

    cur = conn.execute("""
        WITH ev AS (SELECT DISTINCT ON (listing_id) * FROM evaluations WHERE run_id = %s
                    ORDER BY listing_id, eval_id DESC)
        SELECT tr.*, t.full_name, t.credential, l.address, l.city, l.zip, l.true_status,
               ev.eval_id, ev.call_id, ev.source, ev.decision, ev.confidence, ev.front_desk, ev.needs_review,
               ev.conflicts, ev.rationale, ev.old_phone, ev.old_address, ev.new_phone, ev.new_address,
               ev.agent_trace AS eval_trace, ev.reviewed_decision, ev.reviewer_note, ev.applied_at,
               ev.recheck_on, c.extra_answers, c.next_new_appt,
               c.end_reason, c.status AS call_status
        FROM listing_triage tr
        JOIN therapist_listings l USING (listing_id) JOIN therapists t USING (therapist_id)
        LEFT JOIN ev USING (listing_id) LEFT JOIN calls c ON c.call_id = ev.call_id
        WHERE tr.run_id = %s
        ORDER BY tr.call_rank NULLS LAST, tr.exposure_per_month DESC""", (run_id, run_id))
    cols = [d.name for d in cur.description]
    listings = [dict(zip(cols, r)) for r in cur.fetchall()]

    for e in listings:
        if e["reviewed_decision"]:
            e["final"] = e["reviewed_decision"]
        elif e["decision"] and not e["needs_review"]:
            e["final"] = e["decision"]
        elif e["eval_id"]:
            e["final"] = "pending_review"
        else:
            e["final"] = "not_called"  # needed a call, but the run stopped first
        expected = "suppress_listing" if e["claims_verdict"] == "hard_fail" else EXPECTED[e["true_status"]]
        e["expected"] = expected
        # provisional = accepting on the office's word; scored once the claims recheck settles it
        e["correct"] = None if e["final"] in ("pending_review", "not_called", "no_change", "provisionally_accepting") \
            else e["final"] == expected

    def total(rows):
        return round(float(sum(r["exposure_per_month"] for r in rows)), 2)

    resolved = [e for e in listings if e["final"] not in ("pending_review", "not_called")]
    changed = [e for e in resolved if e["final"] in ("set_not_accepting", "suppress_listing")]
    scored = [e for e in listings if e["correct"] is not None]
    changes = []
    for e in listings:
        if not e["applied_at"] or e["final"] not in ("verified_accepting", "provisionally_accepting", "set_not_accepting"):
            continue
        if e["new_address"] and e["new_address"] != e["old_address"]:
            changes.append({"listing_id": e["listing_id"], "field": "address", "old": e["old_address"], "new": e["new_address"]})
        if e["new_phone"] and e["new_phone"] != e["old_phone"]:
            changes.append({"listing_id": e["listing_id"], "field": "phone", "old": e["old_phone"], "new": e["new_phone"]})
    cur = conn.execute("""
        SELECT e.listing_id, t.full_name, e.created_at::date AS promised_on, e.recheck_on,
               e.recheck_result, e.rechecked_at, e.run_id
        FROM evaluations e JOIN therapist_listings l USING (listing_id) JOIN therapists t USING (therapist_id)
        WHERE coalesce(e.reviewed_decision, e.decision) = 'provisionally_accepting'
          AND e.recheck_on IS NOT NULL AND e.applied_at IS NOT NULL
          AND (e.run_id = %s OR e.rechecked_at >= %s)
        ORDER BY e.recheck_on""", (run_id, run["started_at"]))
    promises = [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]
    return {
        "run": run,
        "provisional": [p for p in promises if not p["recheck_result"]],
        "rechecks": [p for p in promises if p["recheck_result"]],
        "totals": {
            "listings": len(listings),
            "decided_by_claims": sum(e["source"] == "claims" for e in listings),
            "called": sum(e["source"] == "call" for e in listings),
            "needed_call": sum(e["claims_verdict"] == "needs_call" for e in listings),
            "not_called": sum(e["final"] == "not_called" for e in listings),
            "pending_review": sum(e["final"] == "pending_review" for e in listings),
            "reviewed_by_human": sum(bool(e["reviewed_decision"]) for e in listings),
        },
        "decisions": dict(Counter(e["final"] for e in listings)),
        "front_desk": dict(Counter(e["front_desk"] for e in listings if e["source"] == "call")),
        "changes": changes,
        "exposure": {
            "total_per_month": total(listings),
            "corrected_per_month": total(changed),       # listing fixed: suppressed or set to not accepting
            "confirmed_per_month": total([e for e in resolved if e not in changed]),
            "pending_review_per_month": total([e for e in listings if e["final"] == "pending_review"]),
            "not_called_per_month": total([e for e in listings if e["final"] == "not_called"]),
        },
        "accuracy": {
            "scored": len(scored),
            "correct": sum(e["correct"] for e in scored),
            "by_source": {s: {"scored": sum(e["source"] == s for e in scored),
                              "correct": sum(e["correct"] for e in scored if e["source"] == s)}
                          for s in ("claims", "call", "recheck")},
            "misses": [{"listing_id": e["listing_id"], "true_status": e["true_status"],
                        "final": e["final"], "source": e["source"]} for e in scored if not e["correct"]],
        },
        "listings": listings,
    }
