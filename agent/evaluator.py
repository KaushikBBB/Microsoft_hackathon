"""Stages 4-5 of a verification run: decide each listing and update the directory.

From claims alone (triage verdicts that need no call), or after a call:
  1. Rules check the front desk's answers against claims and complaints and list conflicts
     (e.g. "says accepting, but no new-patient intake in 200 days").
  2. The evaluator agent (agents.py) investigates with tools and flags the front desk's answers
     good / plausible / not_good / unsure, decides what the directory should say, and picks up a
     new address or phone number confirmed on the call.
  3. The listing goes to human review when it is undecided: the call didn't finish, the agent is
     unsure or not confident, or it would verify "accepting" against conflicting claims.
     Otherwise the decision and any contact changes are applied to the listing.

Claims show the past; the call tells the present. A credible "we just reopened" that past claims
contradict becomes provisionally_accepting with a recheck date: the promised appointment plus the
claims lag, or, if the office admitted unsubmitted new-patient claims, the CLAIMS_SUBMIT_DAYS deadline
they agreed to. run_rechecks() then looks for new-patient claims received since the call:
kept -> verified; broken -> set to not accepting, front desk not good, and a follow-up call goes to
the top of the next run's queue (triage.py).
"""
import json
import re
from datetime import date, timedelta

from psycopg.types.json import Jsonb

from agents import (CLAIMS_LAG_DAYS, CLAIMS_SUBMIT_DAYS, INTAKE_CODES, INTAKE_EVERY_DAYS, LLM_MODEL,
                    evaluator_agent, llm)

DECISIONS = ["verified_accepting", "provisionally_accepting", "set_not_accepting", "suppress_listing"]
KEEPS_LISTING = ("verified_accepting", "provisionally_accepting", "set_not_accepting")
MIN_CONFIDENCE = 0.7
MAX_WAIT_BUSINESS_DAYS = 10  # timely-access standard for a new outpatient behavioral health visit

CLAIMS_DECISION = {  # triage verdicts that need no call
    "hard_fail": "suppress_listing",
    "accepting": "verified_accepting",
    "not_accepting": "set_not_accepting",
    "provisional": "provisionally_accepting",   # an earlier call's promise, not yet due for recheck
}


def triage_row(conn, run_id: int, listing_id: str) -> dict:
    cur = conn.execute("""SELECT t.*, l.accepting_new AS listed_accepting, l.phone, l.address
                          FROM listing_triage t JOIN therapist_listings l USING (listing_id)
                          WHERE t.run_id = %s AND t.listing_id = %s""", (run_id, listing_id))
    return dict(zip([d.name for d in cur.description], cur.fetchone()))


def save(conn, run_id: int, listing_id: str, source: str, decision, confidence, needs_review: bool,
         conflicts: list[str], rationale: str, call_id=None, new_phone=None, new_address=None,
         front_desk=None, trace=None, recheck_on=None) -> int:
    old_phone, old_address = conn.execute(
        "SELECT phone, address FROM therapist_listings WHERE listing_id = %s", (listing_id,)).fetchone()
    eval_id = conn.execute("""
        INSERT INTO evaluations (run_id, listing_id, call_id, source, decision, confidence, front_desk,
                                 needs_review, conflicts, rationale, old_phone, old_address, new_phone,
                                 new_address, agent_trace, recheck_on)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING eval_id""",
        (run_id, listing_id, call_id, source, decision, confidence, front_desk, needs_review, conflicts,
         rationale, old_phone, old_address, new_phone, new_address,
         Jsonb(trace) if trace is not None else None, recheck_on)).fetchone()[0]
    if not needs_review:
        apply(conn, eval_id)
    return eval_id


def apply(conn, eval_id: int):
    """Update the directory listing with the final decision (reviewer's, if any) and any new
    address or phone. A new address keeps the old map location until it is geocoded."""
    listing_id, source, decision, reviewed, new_phone, new_address = conn.execute(
        """SELECT listing_id, source, decision, reviewed_decision, new_phone, new_address
           FROM evaluations WHERE eval_id = %s""", (eval_id,)).fetchone()
    final = reviewed or decision
    if final in ("verified_accepting", "provisionally_accepting"):
        conn.execute("UPDATE therapist_listings SET accepting_new = true WHERE listing_id = %s", (listing_id,))
    elif final == "set_not_accepting":
        conn.execute("UPDATE therapist_listings SET accepting_new = false WHERE listing_id = %s", (listing_id,))
    elif final == "suppress_listing":
        conn.execute("UPDATE therapist_listings SET is_suppressed = true WHERE listing_id = %s", (listing_id,))
    if final in KEEPS_LISTING:
        if new_phone:
            conn.execute("UPDATE therapist_listings SET phone = %s WHERE listing_id = %s", (new_phone, listing_id))
        if new_address:
            conn.execute("UPDATE therapist_listings SET address = %s WHERE listing_id = %s", (new_address, listing_id))
    if final != "no_change":
        conn.execute("""UPDATE therapist_listings SET last_verified_on = current_date, last_verified_how = %s
                        WHERE listing_id = %s""", ("claims" if source == "recheck" else source, listing_id))
    conn.execute("UPDATE evaluations SET applied_at = now() WHERE eval_id = %s", (eval_id,))


def decide_from_claims(conn, run_id: int, rows: list[dict]):
    """Listings whose claims are decisive: decided and applied without a call."""
    for r in rows:
        if r["claims_verdict"] in CLAIMS_DECISION:
            who = "Triage agent" if r["agent_trace"] else "Rule"
            save(conn, run_id, r["listing_id"], "claims", CLAIMS_DECISION[r["claims_verdict"]],
                 r["agent_confidence"] or 1.0, False, [], f"{who}, from claims: {r['claims_reason']}",
                 trace=r["agent_trace"])   # a carried-forward provisional keeps its recheck on the original


def business_days_until(d: date, today: date) -> int:
    n, step = 0, today
    while step < d:
        step = date.fromordinal(step.toordinal() + 1)
        n += step.weekday() < 5
    return n


def format_phone(raw: str | None) -> str | None:
    digits = re.sub(r"\D", "", raw or "")[-10:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else None


def rule_check(t: dict, call: dict) -> tuple[str | None, list[str], str | None, str | None]:
    """(decision, conflicts, new_phone, new_address) from the call answers vs. the evidence."""
    a, end = call.get("answers") or {}, call.get("end_reason")
    today = date.today()
    since_claim = (today - t["last_claim_on"]).days if t["last_claim_on"] else None
    since_intake = (today - t["last_intake_on"]).days if t["last_intake_on"] else None
    conflicts = []
    phone = a.get("phone_correct")
    new_phone = format_phone(phone) if phone and phone != "yes" else None
    identity = a.get("identity_confirmed", "")
    new_address = identity.split(":", 1)[1].strip() or None if identity.startswith("moved:") else None

    if call["status"] == "failed" or not a:
        return None, [f"call did not complete ({call.get('failure') or end or 'no answers'})"], None, None
    if end == "wrong_office" or identity == "no":
        # Listed phone reaches someone else: hide the listing until a correct number is found
        return "suppress_listing", conflicts, None, None
    if a.get("practicing_here") in ("no", "retired"):
        if since_claim is not None and since_claim <= CLAIMS_LAG_DAYS:
            conflicts.append(f"front desk says the therapist no longer practices here, but claims show a "
                             f"visit here {since_claim} days ago")
        return "suppress_listing", conflicts, None, None
    if identity.startswith("moved"):
        if since_claim is not None and since_claim <= CLAIMS_LAG_DAYS:
            conflicts.append(f"front desk says the therapist moved, but claims show a visit at the listed "
                             f"address {since_claim} days ago")
        if not new_address:
            conflicts.append("therapist moved but no new address was captured")
    if a.get("accepts_plan") == "no":
        if t["claims_90d"]:
            conflicts.append(f"front desk says they don't accept the plan, but {t['claims_90d']} claims "
                             f"were paid here in the last 90 days")
        return "suppress_listing", conflicts, new_phone, new_address
    if a.get("accepting_new") in ("no", "waitlist"):
        return "set_not_accepting", conflicts, new_phone, new_address
    if a.get("accepting_new") == "yes":
        decision = "verified_accepting"
        slot = appointment_date(a)
        if since_intake is None or since_intake > INTAKE_EVERY_DAYS + CLAIMS_LAG_DAYS:
            # Contradicts the past, not necessarily false: provisional if they explained it with
            # unsubmitted claims they promised to send, or named a real appointment date
            pending = a.get("claims_pending") == "yes"
            decision = "provisionally_accepting" if (slot or pending) else None
            conflicts.append("front desk says accepting new patients, but "
                             + ("no new-patient intake was ever billed here" if since_intake is None
                                else f"the last new-patient intake billed was {since_intake} days ago")
                             + (f"; they say new-patient claims are unsubmitted and will send them within "
                                f"{CLAIMS_SUBMIT_DAYS} days" if pending else "")
                             + ("" if slot or pending else ", and they gave no concrete appointment date"))
        if t["complaints_90d"]:
            conflicts.append(f"front desk says accepting, but {t['complaints_90d']} member complaint(s) "
                             f"in the last 90 days")
        if slot:
            wait = business_days_until(slot, today)
            if wait > MAX_WAIT_BUSINESS_DAYS:
                conflicts.append(f"earliest new-patient appointment is {wait} business days out "
                                 f"(standard: {MAX_WAIT_BUSINESS_DAYS})")
        elif a.get("next_new_appt") == "none":
            conflicts.append("front desk says accepting, but has no new-patient appointment to offer")
        return decision, conflicts, new_phone, new_address
    return None, ["the call did not establish whether they take new patients"], new_phone, new_address


def appointment_date(a: dict) -> date | None:
    try:
        return date.fromisoformat(a.get("next_new_appt", ""))
    except ValueError:
        return None


def evaluate_call(conn, run_id: int, listing_id: str, call_id: str, call: dict) -> int:
    t = triage_row(conn, run_id, listing_id)
    rule = rule_check(t, call)
    rule_decision, rule_conflicts, rule_phone, rule_address = rule
    try:
        v, trace = evaluator_agent(conn, listing_id, call, rule)
    except Exception as e:  # no agent: the human decides
        v, trace = {"front_desk": "unsure", "decision": "unsure", "confidence": 0, "new_address": None,
                    "new_phone": None, "conflicts": [],
                    "rationale": f"Evaluator agent unavailable ({e.__class__.__name__}); rule check: {rule_decision}."}, None
    decision = v["decision"] if v["decision"] in DECISIONS else rule_decision
    confidence = round(float(v["confidence"]), 2)
    conflicts = list(dict.fromkeys(rule_conflicts + v["conflicts"]))
    why_review = []
    if call["status"] == "failed" or not call.get("answers"):
        why_review.append("the call did not complete")
    if v["decision"] == "unsure" or decision is None:
        why_review.append("the agent could not decide")
    if v["front_desk"] == "unsure":
        why_review.append("the agent can't tell if the front desk's answers are legit")
    if confidence < MIN_CONFIDENCE:
        why_review.append(f"agent confidence {confidence:.2f}")
    if decision == "verified_accepting" and any("says accepting new patients" in c for c in rule_conflicts):
        why_review.append("verifying 'accepting' would contradict the claims evidence")
    answers = call.get("answers") or {}
    slot = appointment_date(answers)
    recheck_on = None
    if decision == "provisionally_accepting":
        if answers.get("claims_pending") == "yes":
            recheck_on = date.today() + timedelta(days=CLAIMS_SUBMIT_DAYS)   # the deadline they agreed to
        elif slot:
            recheck_on = max(slot, date.today()) + timedelta(days=CLAIMS_LAG_DAYS)
        else:
            why_review.append("provisionally accepting needs a concrete appointment date "
                              "or a promise to submit pending claims")
    return save(conn, run_id, listing_id, "call", decision, confidence, bool(why_review),
                why_review + conflicts, v["rationale"], call_id,
                format_phone(v["new_phone"]) or rule_phone, v["new_address"] or rule_address,
                v["front_desk"], trace, recheck_on)


def provisional_due(conn, force: bool = False) -> list[tuple]:
    """Applied provisional decisions not yet rechecked (due now, or all of them when forced)."""
    return conn.execute("""
        SELECT e.eval_id, e.run_id, e.listing_id, e.created_at::date, e.recheck_on
        FROM evaluations e
        WHERE coalesce(e.reviewed_decision, e.decision) = 'provisionally_accepting'
          AND e.applied_at IS NOT NULL AND e.recheck_result IS NULL AND e.recheck_on IS NOT NULL
          AND (%s OR e.recheck_on <= current_date)
        ORDER BY e.recheck_on""", (force,)).fetchall()


def run_rechecks(conn, force: bool = False) -> list[dict]:
    """Did new-patient claims arrive since the call (pending ones submitted, or the promised new
    patient seen)? Kept -> verified; broken -> not accepting, and a follow-up call is queued."""
    results = []
    for eval_id, run_id, listing_id, promised_on, recheck_on in provisional_due(conn, force):
        intake = conn.execute(f"""SELECT min(service_date) FROM claims WHERE listing_id = %s
                                  AND procedure_code IN {INTAKE_CODES} AND received_date >= %s""",
                              (listing_id, promised_on)).fetchone()[0]
        kept = intake is not None
        conn.execute("""UPDATE evaluations SET recheck_result = %s, rechecked_at = now()
                        WHERE eval_id = %s""", ("kept" if kept else "broken", eval_id))
        if kept:
            save(conn, run_id, listing_id, "recheck", "verified_accepting", 1.0, False, [],
                 f"Promise kept: a new-patient claim (visit on {intake}) arrived after the call on {promised_on}.",
                 front_desk="good")
        else:
            save(conn, run_id, listing_id, "recheck", "set_not_accepting", 1.0, False,
                 [f"office said it was accepting on {promised_on}, but no new-patient claim arrived by {recheck_on}"],
                 f"Promise broken: no new-patient claim arrived since the call on {promised_on}. "
                 f"Set to not accepting; a follow-up call is queued.",
                 front_desk="not_good")
        results.append({"listing_id": listing_id, "result": "kept" if kept else "broken", "intake_on": intake})
    return results


def review(conn, eval_id: int, decision: str, note: str | None):
    conn.execute("""UPDATE evaluations SET reviewed_decision = %s, reviewer_note = %s, reviewed_at = now(),
                      recheck_on = CASE WHEN %s = 'provisionally_accepting'
                                   THEN coalesce(recheck_on, current_date + %s) END
                    WHERE eval_id = %s""",
                 (decision, note, decision, INTAKE_EVERY_DAYS + CLAIMS_LAG_DAYS, eval_id))
    apply(conn, eval_id)


def write_summary(conn, run_id: int, report: dict) -> str:
    """Short narrative for the top of the operator's report."""
    x = report["exposure"]
    facts = {k: report[k] for k in ("run", "totals", "decisions", "front_desk", "changes", "provisional", "rechecks")}
    facts["exposure_usd_per_month"] = {  # expected monthly cost of members trying wrong listings
        "all_listings_checked": x["total_per_month"],
        "removed_by_fixing_listings_this_run": x["corrected_per_month"],
        "listings_confirmed_correct": x["confirmed_per_month"],
        "still_open_waiting_for_human_review": x["pending_review_per_month"],
    }
    facts["needs_review"] = [{"listing": e["listing_id"], "therapist": e["full_name"], "why": e["conflicts"]}
                             for e in report["listings"] if e["final"] == "pending_review"]
    try:
        r = llm.chat.completions.create(model=LLM_MODEL, temperature=0.2, messages=[
            {"role": "system", "content": "Write a 4-6 sentence executive summary of a provider-directory "
             "verification run for the plan's network operations lead. Plain language, concrete numbers, "
             "dollar figures per month, which front desks gave answers the evidence contradicts, which listings are "
             "provisionally accepting and when they will be rechecked against claims, what contact details "
             "changed, and what the human reviewers need to do next. No headings or lists."},
            {"role": "user", "content": json.dumps(facts, default=str)}])
        text = r.choices[0].message.content.strip()
    except Exception as e:
        text = f"(summary unavailable: {e.__class__.__name__})"
    conn.execute("UPDATE verification_runs SET summary = %s WHERE run_id = %s", (text, run_id))
    return text
