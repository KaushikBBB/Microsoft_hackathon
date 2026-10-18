"""Stages 1-2 of a verification run: what claims say about each listing, and what a
wrong listing costs the plan.

Claims verdict per listing (no call needed unless 'needs_call'):
  hard_fail      rule: license not active, sanctioned, or contract ended -> suppress
  accepting      triage agent, allowed only with a new-patient intake within INTAKE_EVERY_DAYS
                 and no complaints
  not_accepting  triage agent, allowed only if still billing here and no intake for
                 INTAKE_EVERY_DAYS + CLAIMS_LAG_DAYS -> set "accepting new patients" to No
  needs_call     everything else, including any agent verdict the rules overrule, and every
                 broken promise (follow-up call, first in the queue)
  provisional    an earlier call made it provisionally accepting and its recheck isn't due yet

Exposure ($ per month) = P(wrong) x members likely to try x cost per failed attempt
  P(wrong)  logistic regression on claims/complaint/verification features. Labels come
            from true_status, the prototype's answer key (in production: past verified
            outcomes). Out-of-fold, so no listing is scored by a model that saw its label.
  demand    phone clicks on the listing, blended with members living within RADIUS_KM
  cost      by credential, language and how few alternatives are nearby
"""
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import numpy as np
from psycopg.types.json import Jsonb
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from agents import CLAIMS_LAG_DAYS, INTAKE_CODES, INTAKE_EVERY_DAYS, triage_agent
from calls_db import connect

RADIUS_KM = 10           # how far members look for a therapist
MIN_AGENT_CONFIDENCE = 0.75  # below this, the triage agent's verdict becomes a call

# Cost of one member trying a wrong listing: delayed care, call-center and complaint
# handling, and No Surprises Act exposure (member relied on the directory).
COST_BY_CREDENTIAL = {"MD": 400, "PsyD": 250, "PhD": 250, "LCSW": 150, "LPC": 150, "LMFT": 150}
LANGUAGE_FACTOR = 1.3    # speaks a language besides English: harder to replace
SCARCITY_FACTOR = 1.5    # fewer than 3 other listings within RADIUS_KM

FEATURES = ["days_since_claim", "days_since_intake", "claims_90d", "intakes_365d",
            "unlisted_claims_180d", "complaints_90d", "days_since_verified",
            "verified_by_attestation", "listed_accepting"]


def km(lat1, lon1, lat2, lon2) -> float:
    return math.hypot((float(lat1) - float(lat2)) * 111, (float(lon1) - float(lon2)) * 83)


def load_evidence(conn) -> list[dict]:
    """One row per listing still shown in the directory, with everything triage needs."""
    cur = conn.execute(f"""
        WITH office AS (
          SELECT listing_id, max(service_date) AS last_claim_on,
                 max(service_date) FILTER (WHERE procedure_code IN {INTAKE_CODES}) AS last_intake_on,
                 count(*) FILTER (WHERE service_date > current_date - 90) AS claims_90d,
                 count(*) FILTER (WHERE procedure_code IN {INTAKE_CODES}
                                  AND service_date > current_date - 365) AS intakes_365d
          FROM claims WHERE listing_id IS NOT NULL GROUP BY listing_id),
        unlisted AS (
          SELECT therapist_id, count(*) AS n FROM claims
          WHERE listing_id IS NULL AND service_date > current_date - 180 GROUP BY therapist_id),
        comp AS (
          SELECT listing_id, count(*) AS n, array_agg(DISTINCT category) AS cats FROM complaints
          WHERE received_on > current_date - 90 GROUP BY listing_id),
        clicks AS (
          SELECT listing_id, count(*) FILTER (WHERE clicked_phone) AS n FROM directory_views
          WHERE viewed_at > current_date - 90 GROUP BY listing_id)
        SELECT l.listing_id, l.therapist_id, t.full_name, t.credential, t.languages,
               t.license_status, t.license_expires_on, t.sanctioned, t.contract_end,
               l.lat, l.lon, l.accepting_new, l.last_verified_on, l.last_verified_how,
               o.last_claim_on, o.last_intake_on,
               coalesce(o.claims_90d, 0) AS claims_90d, coalesce(o.intakes_365d, 0) AS intakes_365d,
               coalesce(u.n, 0) AS unlisted_claims_180d,
               coalesce(cp.n, 0) AS complaints_90d, coalesce(cp.cats, '{{}}') AS complaint_categories,
               coalesce(ck.n, 0) AS phone_clicks_90d, l.true_status, current_date AS today
        FROM therapist_listings l JOIN therapists t USING (therapist_id)
        LEFT JOIN office o USING (listing_id) LEFT JOIN unlisted u USING (therapist_id)
        LEFT JOIN comp cp USING (listing_id) LEFT JOIN clicks ck USING (listing_id)
        WHERE NOT l.is_suppressed
        ORDER BY l.listing_id""")
    cols = [d.name for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    members = conn.execute("""SELECT home_lat, home_lon FROM patients
                              WHERE enrolled_until IS NULL OR enrolled_until >= current_date""").fetchall()
    for r in rows:
        r["members_nearby"] = sum(km(r["lat"], r["lon"], a, b) <= RADIUS_KM for a, b in members)
        r["others_nearby"] = sum(km(r["lat"], r["lon"], x["lat"], x["lon"]) <= RADIUS_KM
                                 for x in rows if x["listing_id"] != r["listing_id"])
    return rows


def days(today: date, d) -> int | None:
    return (today - d).days if d else None


def hard_fail(r: dict) -> str | None:
    """Credentialing problems: no judgment needed, the listing must go."""
    today = r["today"]
    if r["license_status"] != "active" or (r["license_expires_on"] and r["license_expires_on"] < today):
        return f"license {r['license_status']}, expires {r['license_expires_on']}"
    if r["sanctioned"]:
        return "therapist is sanctioned"
    if r["contract_end"] and r["contract_end"] <= today:
        return f"left the network on {r['contract_end']}"
    return None


def guard(r: dict, verdict: str, confidence: float) -> str | None:
    """Plan rules the triage agent may not override. Returns why a verdict is overruled."""
    since_claim, since_intake = days(r["today"], r["last_claim_on"]), days(r["today"], r["last_intake_on"])
    if confidence < MIN_AGENT_CONFIDENCE and verdict != "needs_call":
        return f"agent confidence {confidence:.2f}"
    if verdict == "accepting":
        if since_intake is None or since_intake > INTAKE_EVERY_DAYS:
            return f"no new-patient intake within {INTAKE_EVERY_DAYS} days"
        if r["complaints_90d"]:
            return "member complaints in the last 90 days"
    if verdict == "not_accepting":
        if since_claim is None or since_claim > 60:
            return "not billing at this office recently: may have moved or retired"
        if since_intake is not None and since_intake <= INTAKE_EVERY_DAYS + CLAIMS_LAG_DAYS:
            return f"an intake within {INTAKE_EVERY_DAYS + CLAIMS_LAG_DAYS} days may still be in the claims lag"
    return None


def decide(r: dict):
    """Fills claims_verdict, claims_reason, agent_confidence and agent_trace for one listing."""
    reason = hard_fail(r)
    if reason:
        r.update(claims_verdict="hard_fail", claims_reason=reason, agent_confidence=None, agent_trace=None)
        return
    if r.get("follow_up"):
        r.update(claims_verdict="needs_call", agent_confidence=None, agent_trace=None,
                 claims_reason=f"follow-up: {r['follow_up']}")
        return
    if r.get("recheck_on"):
        r.update(claims_verdict="provisional", agent_confidence=None, agent_trace=None,
                 claims_reason=f"provisionally accepting after an earlier call; claims recheck due {r['recheck_on']}")
        return
    try:
        with connect() as conn:
            v, trace = triage_agent(conn, r["listing_id"])
        verdict, confidence, reason = v["verdict"], float(v["confidence"]), v["reason"]
        overruled = guard(r, verdict, confidence)
        if overruled:
            reason = f"{reason} (agent said {verdict}; overruled: {overruled})"
            verdict = "needs_call"
    except Exception as e:  # no agent: fall back to calling
        verdict, confidence, trace = "needs_call", None, None
        reason = f"triage agent unavailable ({e.__class__.__name__}); calling to be safe"
    r.update(claims_verdict=verdict, claims_reason=reason, agent_confidence=confidence, agent_trace=trace)


def feature_row(r: dict) -> list[float]:
    cap = 400  # "never" and "very long ago" look the same to the model
    return [min(days(r["today"], r["last_claim_on"]) or cap, cap),
            min(days(r["today"], r["last_intake_on"]) or cap, cap),
            r["claims_90d"], r["intakes_365d"], r["unlisted_claims_180d"], r["complaints_90d"],
            days(r["today"], r["last_verified_on"]) or cap,
            float(r["last_verified_how"] == "attestation"), float(r["accepting_new"])]


def p_wrong(rows: list[dict]) -> np.ndarray:
    X = np.array([feature_row(r) for r in rows])
    y = np.array([r["true_status"] != "active_open" for r in rows])
    folds = min(5, int(y.sum()), int((~y).sum()))
    if folds < 2:
        return np.full(len(rows), y.mean())
    model = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=1000))
    cv = StratifiedKFold(folds, shuffle=True, random_state=0)
    return cross_val_predict(model, X, y, cv=cv, method="predict_proba")[:, 1]


def score(rows: list[dict]) -> list[dict]:
    """Adds p_wrong, demand, cost and exposure to each row (the priority tool)."""
    probs = p_wrong(rows)
    # Members per click: lets the location-based estimate speak in the same units as clicks
    total_members = sum(r["members_nearby"] for r in rows) or 1
    clicks_per_member = sum(r["phone_clicks_90d"] for r in rows) / total_members
    for r, p in zip(rows, probs):
        r["p_wrong"] = 1.0 if hard_fail(r) else round(float(p), 3)
        expected_clicks = r["members_nearby"] * clicks_per_member
        r["demand_per_month"] = round((r["phone_clicks_90d"] + expected_clicks) / 2 / 3, 2)
        cost = COST_BY_CREDENTIAL[r["credential"]]
        if len(r["languages"]) > 1:
            cost *= LANGUAGE_FACTOR
        if r["others_nearby"] < 3:
            cost *= SCARCITY_FACTOR
        r["cost_per_failure"] = round(cost, 2)
        r["exposure_per_month"] = round(r["p_wrong"] * r["demand_per_month"] * cost, 2)
    return rows


def run_triage(conn, run_id: int, limit: int | None = None, progress=lambda done, total: None,
               offset: int = 0) -> list[dict]:
    """Stage 1: decide every listing (or `limit` listings after `offset`, for testing) from claims.
    Stage 2: rank the ones that need a call: broken promises first, then by exposure."""
    rows = score(load_evidence(conn))   # the model learns from the whole directory
    ids = [r[0] for r in conn.execute("SELECT listing_id FROM therapist_listings ORDER BY listing_id")]
    scope = set(ids[offset:offset + limit] if limit else ids[offset:])   # by position in the full directory
    rows = [r for r in rows if r["listing_id"] in scope]                  # suppressed ones drop out
    follow_up = dict(conn.execute("""
        SELECT DISTINCT ON (e.listing_id) e.listing_id, e.conflicts[1] FROM evaluations e
        WHERE e.source = 'recheck' AND e.front_desk = 'not_good'
          AND NOT EXISTS (SELECT 1 FROM evaluations x WHERE x.listing_id = e.listing_id
                          AND x.source = 'call' AND x.created_at > e.created_at)
        ORDER BY e.listing_id, e.eval_id DESC""").fetchall())
    pending = dict(conn.execute("""
        SELECT DISTINCT ON (listing_id) listing_id, recheck_on FROM evaluations
        WHERE coalesce(reviewed_decision, decision) = 'provisionally_accepting'
          AND applied_at IS NOT NULL AND recheck_result IS NULL AND recheck_on > current_date
        ORDER BY listing_id, eval_id DESC""").fetchall())
    for r in rows:
        r["recheck_on"] = pending.get(r["listing_id"])
        r["follow_up"] = follow_up.get(r["listing_id"])
    done = 0
    with ThreadPoolExecutor(max_workers=5) as pool:
        for _ in pool.map(decide, rows):
            done += 1
            progress(done, len(rows))
    to_call = sorted((r for r in rows if r["claims_verdict"] == "needs_call"),
                     key=lambda r: (not r["follow_up"], -r["exposure_per_month"]))
    for i, r in enumerate(to_call, 1):
        r["call_rank"] = i
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO listing_triage (run_id, listing_id, last_claim_on, last_intake_on, claims_90d,
              intakes_365d, unlisted_claims_180d, complaints_90d, phone_clicks_90d, members_nearby,
              claims_verdict, claims_reason, agent_confidence, agent_trace, p_wrong, demand_per_month,
              cost_per_failure, exposure_per_month, call_rank)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [(run_id, r["listing_id"], r["last_claim_on"], r["last_intake_on"], r["claims_90d"],
              r["intakes_365d"], r["unlisted_claims_180d"], r["complaints_90d"], r["phone_clicks_90d"],
              r["members_nearby"], r["claims_verdict"], r["claims_reason"], r["agent_confidence"],
              Jsonb(r["agent_trace"]) if r["agent_trace"] is not None else None, r["p_wrong"],
              r["demand_per_month"], r["cost_per_failure"], r["exposure_per_month"], r.get("call_rank"))
             for r in rows])
    return rows
