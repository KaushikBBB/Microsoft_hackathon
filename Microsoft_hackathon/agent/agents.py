"""The LLM agents of a verification run (gpt-4.1-mini with tool calling).

  triage agent     studies one listing's claims and complaints and decides whether claims
                   alone settle "is this therapist taking new patients?" or the office must
                   be called
  evaluator agent  after a call, checks the front desk's answers against claims and complaints:
                   are they legit, what should the directory say, and did the address or
                   phone change?
call_brief() is the claims evidence the voice agent may cite on the call (app.py).

Agents investigate with read-only tools and must finish by calling their submit tool.
triage.py and evaluator.py enforce the plan rules on top of what the agents decide.
"""
import json
import os
from datetime import date, timedelta

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

INTAKE_EVERY_DAYS = 30   # plan rule: a listing marked "accepting" shows a new-patient intake this often
CLAIMS_LAG_DAYS = 45     # claims arrive 1-6 weeks after the visit
CLAIMS_SUBMIT_DAYS = 14  # an office with unsubmitted new-patient claims is asked to submit them this fast
INTAKE_CODES = ("90791", "90792")

llm = OpenAI(base_url=f"https://{os.environ['AZ_AOAI_NAME']}.openai.azure.com/openai/v1/",
             api_key=os.environ["AOAI_KEY"])
LLM_MODEL = os.environ.get("AOAI_TEXT_DEPLOYMENT", "gpt-4.1-mini")

PLAN_RULE = f"""Plan rule: a listing marked "accepting new patients" must show a new-patient intake
(CPT 90791 or 90792) at that office at least every {INTAKE_EVERY_DAYS} days. Claims arrive 1-6 weeks
after the visit, so the last ~{CLAIMS_LAG_DAYS} days of claims are incomplete: a gap shorter than
about {INTAKE_EVERY_DAYS + CLAIMS_LAG_DAYS} days is not evidence that they stopped taking new patients."""


# ---- evidence tools (read-only) -----------------------------------------------------

def get_listing(conn, listing_id: str) -> dict:
    cur = conn.execute("""
        SELECT l.listing_id, l.address, l.city, l.zip, l.phone, l.accepting_new AS directory_says_accepting,
               l.last_verified_on, l.last_verified_how, t.therapist_id, t.full_name, t.credential,
               t.license_status, t.license_expires_on, t.sanctioned, t.languages, t.offers_telehealth,
               t.contract_start, t.contract_end
        FROM therapist_listings l JOIN therapists t USING (therapist_id) WHERE l.listing_id = %s""",
        (listing_id,))
    row = cur.fetchone()
    return dict(zip([d.name for d in cur.description], row)) if row else {"error": "no such listing"}


def get_claims_summary(conn, listing_id: str) -> dict:
    today = date.today()
    last_claim, last_intake = conn.execute(f"""
        SELECT max(service_date), max(service_date) FILTER (WHERE procedure_code IN {INTAKE_CODES})
        FROM claims WHERE listing_id = %s""", (listing_id,)).fetchone()
    monthly = conn.execute(f"""
        SELECT to_char(date_trunc('month', service_date), 'YYYY-MM') AS month, count(*) AS visits,
               count(*) FILTER (WHERE procedure_code IN {INTAKE_CODES}) AS new_patient_intakes,
               count(DISTINCT patient_id) AS patients
        FROM claims WHERE listing_id = %s AND service_date > current_date - 365
        GROUP BY 1 ORDER BY 1""", (listing_id,)).fetchall()
    therapist_id = conn.execute("SELECT therapist_id FROM therapist_listings WHERE listing_id = %s",
                                (listing_id,)).fetchone()[0]
    unlisted = conn.execute("""
        SELECT count(*), max(service_date) FROM claims
        WHERE therapist_id = %s AND listing_id IS NULL AND service_date > current_date - 180""",
        (therapist_id,)).fetchone()
    others = conn.execute("""
        SELECT l.listing_id, l.address, l.city, max(c.service_date)
        FROM therapist_listings l LEFT JOIN claims c USING (listing_id)
        WHERE l.therapist_id = %s AND l.listing_id <> %s GROUP BY 1, 2, 3""",
        (therapist_id, listing_id)).fetchall()
    return {
        "today": today,
        "last_visit_billed_here": last_claim,
        "days_since_last_visit_here": (today - last_claim).days if last_claim else None,
        "last_new_patient_intake_here": last_intake,
        "days_since_last_intake_here": (today - last_intake).days if last_intake else None,
        "by_month_last_12": [dict(zip(["month", "visits", "new_patient_intakes", "patients"], m)) for m in monthly],
        "therapist_visits_at_an_office_not_in_directory_last_180d": unlisted[0],
        "last_visit_at_unlisted_office": unlisted[1],
        "therapist_other_listed_offices": [dict(zip(["listing_id", "address", "city", "last_visit"], o))
                                           for o in others],
        "note": f"claims arrive 1-6 weeks late; the last ~{CLAIMS_LAG_DAYS} days are incomplete",
    }


def get_complaints(conn, listing_id: str) -> dict:
    rows = conn.execute("""SELECT received_on, category FROM complaints
                           WHERE listing_id = %s AND received_on > current_date - 180
                           ORDER BY received_on DESC""", (listing_id,)).fetchall()
    views = conn.execute("""SELECT count(*), count(*) FILTER (WHERE clicked_phone) FROM directory_views
                            WHERE listing_id = %s AND viewed_at > current_date - 90""", (listing_id,)).fetchone()
    return {"complaints_last_180d": [{"received_on": r[0], "category": r[1]} for r in rows],
            "directory_views_last_90d": views[0], "phone_clicks_last_90d": views[1]}


def call_brief(conn, listing_id: str) -> dict:
    """What claims and complaints say about this office, for the voice agent to probe with.
    Aggregates about the therapist's own billing only: never anything about a patient."""
    today = date.today()
    last_visit, last_intake, visits_90, patients_90, intakes_12m = conn.execute(f"""
        SELECT max(service_date), max(service_date) FILTER (WHERE procedure_code IN {INTAKE_CODES}),
               count(*) FILTER (WHERE service_date > current_date - 90),
               count(DISTINCT patient_id) FILTER (WHERE service_date > current_date - 90),
               count(*) FILTER (WHERE procedure_code IN {INTAKE_CODES} AND service_date > current_date - 365)
        FROM claims WHERE listing_id = %s""", (listing_id,)).fetchone()
    complaints = dict(conn.execute("""SELECT category, count(*) FROM complaints
                                      WHERE listing_id = %s AND received_on > current_date - 90
                                      GROUP BY category""", (listing_id,)).fetchall())
    since_intake = (today - last_intake).days if last_intake else None
    since_visit = (today - last_visit).days if last_visit else None
    gap = since_intake is None or since_intake > INTAKE_EVERY_DAYS + CLAIMS_LAG_DAYS
    fmt = lambda d: f"{d:%B} {d.day}, {d.year}"
    lines = []
    if last_intake:
        lines.append(f"the last new-patient visit billed at this office was on {fmt(last_intake)} "
                     f"({since_intake} days ago)")
    else:
        lines.append("no new-patient visit has ever been billed at this office")
    lines.append(f"over the last 90 days, claims show {visits_90} visits with {patients_90} different "
                 f"existing patients" if visits_90 else "no visits were billed here in the last 90 days")
    if complaints.get("couldnt_book"):
        lines.append(f"{complaints['couldnt_book']} plan member(s) reported they couldn't book a new-patient "
                     f"appointment in the last 90 days")
    return {
        "last_intake_on": last_intake.isoformat() if last_intake else None, "days_since_intake": since_intake,
        "last_intake_said": f"{fmt(last_intake)}, {since_intake} days ago" if last_intake else None,
        "last_visit_on": last_visit.isoformat() if last_visit else None, "days_since_visit": since_visit,
        "last_visit_said": f"{fmt(last_visit)}, {since_visit} days ago" if last_visit else None,
        "visits_90d": visits_90, "patients_90d": patients_90, "intakes_12m": intakes_12m,
        "submit_by": fmt(today + timedelta(days=CLAIMS_SUBMIT_DAYS)),
        "complaints_90d": complaints,
        "probe": gap or bool(complaints.get("couldnt_book")),  # an "accepting" answer must be probed
        "evidence": "; ".join(lines),
    }


EVIDENCE_TOOLS = {
    "get_listing": ("The directory entry and the therapist's credentialing record "
                    "(license, sanctions, network contract).", get_listing),
    "get_claims_summary": ("Claims at this office: last visit, last new-patient intake, visits and intakes "
                           "by month for 12 months, and whether the therapist bills from other offices.",
                           get_claims_summary),
    "get_complaints": ("Member complaints about this listing in the last 180 days, and how often members "
                       "viewed it or clicked its phone number.", get_complaints),
}


def tool_schema(name: str, description: str, properties: dict, strict: bool = False) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description, "strict": strict,
        "parameters": {"type": "object", "properties": properties, "required": list(properties),
                       "additionalProperties": False}}}


def run_agent(system: str, user: str, tools: dict, submit: dict, max_turns: int = 8) -> tuple[dict, list]:
    """Tool-calling loop. tools: name -> (description, fn(**args)) taking a listing_id;
    submit: the schema of the finishing tool. Returns (submitted arguments, tool trace)."""
    specs = [tool_schema(n, d, {"listing_id": {"type": "string"}}) for n, (d, _) in tools.items()]
    specs.append(submit)
    submit_name = submit["function"]["name"]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    trace = []
    for _ in range(max_turns):
        msg = llm.chat.completions.create(model=LLM_MODEL, temperature=0, messages=messages,
                                          tools=specs, tool_choice="required").choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        for tc in msg.tool_calls or []:
            args = json.loads(tc.function.arguments or "{}")
            if tc.function.name == submit_name:
                return args, trace
            if tc.function.name in tools:
                out = tools[tc.function.name][1](**args)
            else:
                out = {"error": f"unknown tool {tc.function.name}"}
            trace.append({"tool": tc.function.name, "args": args})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(out, default=str)})
    raise RuntimeError("agent did not submit a decision")


def bind(conn, tools: dict) -> dict:
    return {n: (d, lambda f=f, **a: f(conn, **a)) for n, (d, f) in tools.items()}


# ---- triage agent -----------------------------------------------------------------

TRIAGE_SYSTEM = f"""You triage one listing in a health plan's therapist directory. Question: is this
therapist taking new patients at this office, and do claims alone settle it, or must we phone the office?

{PLAN_RULE}

Verdicts:
- accepting: claims show a new-patient intake here within the last {INTAKE_EVERY_DAYS} days, the therapist is
  still billing here, and there are no member complaints.
- not_accepting: the therapist clearly still practices here (visits in the last ~60 days) but has taken
  no new patients for far longer than {INTAKE_EVERY_DAYS} days plus the claims lag (e.g. 3+ months), so the
  plan rule is plainly unmet.
- needs_call: anything else: no recent visits here (moved? retired?), member complaints, mixed signals,
  or too little data. When in doubt, call: a call is cheap, a wrong listing is not.
Look at the evidence with the tools first, then call submit_triage once."""

SUBMIT_TRIAGE = tool_schema("submit_triage", "Record the verdict for this listing.", {
    "verdict": {"type": "string", "enum": ["accepting", "not_accepting", "needs_call"]},
    "confidence": {"type": "number", "description": "0 to 1"},
    "reason": {"type": "string", "description": "one sentence citing the evidence (dates, counts)"},
}, strict=True)


def triage_agent(conn, listing_id: str) -> tuple[dict, list]:
    return run_agent(TRIAGE_SYSTEM, f"Triage listing {listing_id}.", bind(conn, EVIDENCE_TOOLS), SUBMIT_TRIAGE)


# ---- evaluator agent --------------------------------------------------------------

EVALUATOR_SYSTEM = f"""You check one verification call to a therapist's office against the health
plan's evidence (claims and member complaints).

{PLAN_RULE}

Claims show the PAST (and lag 1-6 weeks); the call tells you the PRESENT. An answer that contradicts
past claims is not automatically false: a therapist may have just reopened their panel, or seen new
patients whose claims weren't submitted yet. The voice agent cross-questioned such answers with the
claims evidence (see the call's brief and follow-up answers). If the office admits unsubmitted
new-patient claims (claims_pending = yes) and agreed to submit them within {CLAIMS_SUBMIT_DAYS} days, that is a
credible explanation: provisionally_accepting, front_desk plausible.

Decide:
1. front_desk: were the answers legit?
   good = consistent with the evidence.
   plausible = contradicts past claims, but the office gave a credible, specific explanation (what
     changed, when they last took a new patient, a concrete new-patient appointment date) and no
     CURRENT evidence (recent "couldn't book" complaints) contradicts it.
   not_good = contradicted by current evidence, or vague/evasive when probed (a bare "yes" with no
     date or reason), or inconsistent within the call.
   unsure = the call was cut short or garbled, or you can't tell.
2. decision: what the directory should say.
   verified_accepting: accepting, and claims agree.
   provisionally_accepting: accepting per a plausible office, but claims don't show it yet; requires a
     concrete new-patient appointment date or a promise to submit pending claims. Claims are rechecked
     later.
   set_not_accepting: not taking new patients (full caseload, waitlist, on leave), or a not_good "yes".
   suppress_listing: retired, gone, wrong number (not the therapist's office), doesn't take the plan.
   unsure: a human must decide.
   If the therapist MOVED, the listing moves with them: give new_address and decide accepting / not
   accepting; do not suppress.
3. Contact changes confirmed on the call: new_address (street, city) if they moved, new_phone if the
   front desk gave and confirmed a different number for new patients. Otherwise null.
A rule check has already listed conflicts; weigh them. Use the tools (including get_call), then call
submit_evaluation once."""

SUBMIT_EVALUATION = tool_schema("submit_evaluation", "Record the evaluation of this call.", {
    "front_desk": {"type": "string", "enum": ["good", "plausible", "not_good", "unsure"]},
    "decision": {"type": "string", "enum": ["verified_accepting", "provisionally_accepting", "set_not_accepting",
                                             "suppress_listing", "unsure"]},
    "confidence": {"type": "number", "description": "0 to 1"},
    "new_address": {"type": ["string", "null"]},
    "new_phone": {"type": ["string", "null"]},
    "conflicts": {"type": "array", "items": {"type": "string"},
                  "description": "specific contradictions between the call and the evidence"},
    "rationale": {"type": "string", "description": "2-3 plain sentences for the operator"},
}, strict=True)


def evaluator_agent(conn, listing_id: str, call: dict, rule: tuple) -> tuple[dict, list]:
    def get_call(listing_id: str) -> dict:
        return {"answers": call.get("answers") or {}, "end_reason": call.get("end_reason"),
                "brief_the_voice_agent_had": (call.get("brief") or {}).get("evidence"),
                "status": call.get("status"), "failure": call.get("failure"),
                "transcript": [f"{x['speaker'].upper()}: {x.get('text') or '[inaudible]'}"
                               for x in call.get("transcript", [])]}
    tools = bind(conn, EVIDENCE_TOOLS)
    tools["get_call"] = ("The call: recorded answers (incl. follow-ups), the claims brief the voice agent "
                         "probed with, how it ended, and the transcript.", get_call)
    user = (f"Evaluate the verification call for listing {listing_id}.\n"
            f"Rule check: proposed decision {rule[0]}; conflicts: {json.dumps(rule[1])}")
    return run_agent(EVALUATOR_SYSTEM, user, tools, SUBMIT_EVALUATION)
