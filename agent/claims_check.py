"""Agent 2: flag claims tied to a wrong or ineligible listing, for a person to review.

It never pays, denies or changes a claim. Each rule writes rows to claim_flags; a reviewer
then confirms or dismisses them (POST /api/claim-flags/{id}).

Rules, over claims received in the last LOOKBACK_DAYS:
  sanctioned_provider     the therapist is sanctioned
  license_inactive        license not active, or the visit is after it expired
  contract_ended          visit after the therapist's network contract ended
  office_not_in_directory billed from an office that isn't in the directory (moved?)
  listing_removed         visit at a listing the verifier removed after a call, dated after the
                          office said the therapist left (or after the decision, if no date was given)

Usage:  uv run python claims_check.py      (also runs at the end of every verification run)
"""
from psycopg.types.json import Jsonb

LOOKBACK_DAYS = 180

TABLE = """CREATE TABLE IF NOT EXISTS claim_flags (
  flag_id      SERIAL PRIMARY KEY,
  claim_id     TEXT NOT NULL REFERENCES claims,
  listing_id   TEXT REFERENCES therapist_listings,
  therapist_id TEXT NOT NULL REFERENCES therapists,
  flag_type    TEXT NOT NULL,
  reason       TEXT NOT NULL,
  amount       NUMERIC(8,2) NOT NULL,
  evidence     JSONB,
  status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','confirmed','dismissed')),
  reviewer_note TEXT,
  flagged_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_at  TIMESTAMPTZ,
  UNIQUE (claim_id, flag_type))"""

# flag_type -> SQL selecting (claim_id, listing_id, therapist_id, paid_amount, reason, evidence)
RULES = {
    "sanctioned_provider": """
        SELECT c.claim_id, c.listing_id, c.therapist_id, c.paid_amount,
               t.full_name || ' is sanctioned; claim for ' || c.service_date, jsonb_build_object('service_date', c.service_date)
        FROM claims c JOIN therapists t USING (therapist_id) WHERE t.sanctioned""",
    "license_inactive": """
        SELECT c.claim_id, c.listing_id, c.therapist_id, c.paid_amount,
               CASE WHEN t.license_status <> 'active' THEN 'license is ' || t.license_status
                    ELSE 'visit on ' || c.service_date || ' is after the license expired on ' || t.license_expires_on END,
               jsonb_build_object('license_status', t.license_status, 'expires_on', t.license_expires_on,
                                  'service_date', c.service_date)
        FROM claims c JOIN therapists t USING (therapist_id)
        WHERE NOT t.sanctioned AND (t.license_status <> 'active' OR c.service_date > t.license_expires_on)""",
    "contract_ended": """
        SELECT c.claim_id, c.listing_id, c.therapist_id, c.paid_amount,
               'visit on ' || c.service_date || ' is after the network contract ended on ' || t.contract_end,
               jsonb_build_object('contract_end', t.contract_end, 'service_date', c.service_date)
        FROM claims c JOIN therapists t USING (therapist_id) WHERE c.service_date > t.contract_end""",
    "office_not_in_directory": """
        SELECT c.claim_id, NULL, c.therapist_id, c.paid_amount,
               t.full_name || ' billed from an office that is not in the directory (listed: '
                 || coalesce((SELECT string_agg(l.address, '; ') FROM therapist_listings l
                              WHERE l.therapist_id = c.therapist_id), 'none') || ')',
               jsonb_build_object('service_date', c.service_date)
        FROM claims c JOIN therapists t USING (therapist_id) WHERE c.listing_id IS NULL""",
    "listing_removed": """
        WITH removed AS (
          SELECT DISTINCT ON (e.listing_id) e.listing_id, e.created_at::date AS decided_on,
                 coalesce(k.extra_answers->>'left_on', k.extra_answers->>'moved_on') AS left_on,
                 CASE WHEN k.practicing_here = 'retired' THEN 'therapist retired'
                      WHEN k.practicing_here = 'no' THEN 'therapist no longer practices here'
                      WHEN k.identity_confirmed = 'no' THEN 'wrong number, not this office'
                      WHEN k.accepts_plan = 'no' THEN 'does not accept the plan' END AS answer
          FROM evaluations e LEFT JOIN calls k ON k.call_id = e.call_id
          WHERE coalesce(e.reviewed_decision, e.decision) = 'suppress_listing' AND e.applied_at IS NOT NULL
          ORDER BY e.listing_id, e.eval_id DESC)
        SELECT c.claim_id, c.listing_id, c.therapist_id, c.paid_amount,
               'visit on ' || c.service_date || ' at a listing removed on ' || r.decided_on
                 || coalesce(' (office said: ' || r.answer || coalesce(', left ' || r.left_on, '') || ')', ''),
               jsonb_build_object('service_date', c.service_date, 'removed_on', r.decided_on, 'left_on', r.left_on)
        FROM claims c JOIN removed r USING (listing_id)
        WHERE c.service_date >= CASE WHEN r.left_on ~ '^\\d{4}-\\d{2}-\\d{2}$' THEN r.left_on::date
                                     ELSE r.decided_on END""",
}


def ensure_table(conn):
    conn.execute(TABLE)


def run_claims_check(conn) -> dict:
    """Add new flags (existing ones keep their review status). Returns new flags per rule."""
    ensure_table(conn)
    added = {}
    for flag_type, sql in RULES.items():
        rows = conn.execute(f"""SELECT * FROM ({sql}) x(claim_id, listing_id, therapist_id, amount, reason, evidence)
                                WHERE claim_id IN (SELECT claim_id FROM claims
                                                   WHERE received_date > current_date - {LOOKBACK_DAYS})""").fetchall()
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO claim_flags (claim_id, listing_id, therapist_id, flag_type, amount, reason, evidence)
                               VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                            [(r[0], r[1], r[2], flag_type, r[3], r[4], Jsonb(r[5])) for r in rows])
            added[flag_type] = cur.rowcount if rows else 0
    return added


def list_flags(conn) -> dict:
    ensure_table(conn)
    cur = conn.execute("""
        SELECT f.flag_id, f.claim_id, f.listing_id, f.flag_type, f.reason, f.amount, f.status, f.flagged_at,
               f.reviewer_note, c.service_date, c.procedure_code, t.full_name, t.credential,
               coalesce(l.address || ', ' || l.city, 'office not in directory') AS office
        FROM claim_flags f JOIN claims c USING (claim_id) JOIN therapists t ON t.therapist_id = f.therapist_id
        LEFT JOIN therapist_listings l ON l.listing_id = f.listing_id
        ORDER BY f.status = 'open' DESC, c.service_date DESC, f.flag_id""")
    flags = [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]
    summary = {}
    for f in flags:
        s = summary.setdefault(f["flag_type"], {"count": 0, "open": 0, "amount": 0.0})
        s["count"] += 1
        s["open"] += f["status"] == "open"
        s["amount"] += float(f["amount"])
    return {"summary": summary, "flags": flags,
            "open_amount": round(sum(float(f["amount"]) for f in flags if f["status"] == "open"), 2)}


def review_flag(conn, flag_id: int, status: str, note: str | None):
    conn.execute("""UPDATE claim_flags SET status = %s, reviewer_note = %s, reviewed_at = now()
                    WHERE flag_id = %s""", (status, note, flag_id))


if __name__ == "__main__":
    from calls_db import connect
    with connect() as conn:
        print(run_claims_check(conn))
