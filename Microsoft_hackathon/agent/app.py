"""Ghost Listing Verifier — agent server.

A verification run (POST /api/runs) works through the directory (or its first N listings):
  0. evaluator.py  recheck due "provisionally accepting" promises against new claims
  1. triage.py     study claims: hard-fail rules, then the triage agent (agents.py) decides
                   per listing whether claims settle "taking new patients?" or a call is needed
  2. triage.py     priority tool: $ exposure (ML P(wrong) x demand x cost) orders the calls
  3. this file     the voice agent calls them one after another, probing answers that contradict
                   the claims brief (agents.call_brief); transcripts go to the DB
  4. evaluator.py  the evaluator agent checks each call against claims and complaints, flags
                   the front desk good / not good, updates address and phone, or sends the
                   listing to human review when it can't decide
  5. report.py     the operator's report (/report)

  GET  /                 clinic softphone page (static/index.html): only rings and answers
  GET  /operator         operator page (static/operator.html): runs, call queue, live call, reviews
  GET  /report           report for the latest run (static/report.html)
  GET  /api/token        ACS token for the shared "clinic" identity the agent calls
  GET  /api/queue        listings with the latest run's triage, decision and last call
  POST /api/call         agent places one web call to the clinic about a listing (manual)
  POST /api/runs         start a verification run;  POST /api/runs/stop  stops after the current call
  GET  /api/runs/current progress of the current or last run
  GET  /api/reviews      decisions waiting for a human;  POST /api/evaluations/{id}/review  decides one
  GET  /api/report       report data for the latest run (or ?run_id=)
  GET  /api/provisional  provisionally accepting listings and their recheck dates
  POST /api/rechecks     recheck promises now ({"force": true} = even if not yet due; demo)
  POST /api/demo/intake/{listing_id}  demo only: bill a new-patient intake today, as if the promise was kept
  GET  /api/role-card/{listing_id}  demo only: how the clinic should answer (hidden answer key)
  GET  /api/calls        every call with its transcript (live view polls it)
  POST /acs-events       Call Automation event callbacks
  WS   /ws/media         ACS bidirectional audio <-> Azure OpenAI gpt-realtime

Calls and transcripts are saved to Postgres (calls, call_turns) via calls_db.py,
with a JSON copy per call in ../calls/transcripts/.

Run:  uv run uvicorn app:app --port 8080   (PUBLIC_URL in ../.env must reach it)
"""
import asyncio
import base64
import json
import os
import re
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import websockets
from azure.communication.callautomation import (
    AudioFormat,
    CallAutomationClient,
    CommunicationUserIdentifier,
    MediaStreamingAudioChannelType,
    MediaStreamingContentType,
    MediaStreamingOptions,
    StreamingTransportType,
)
from azure.communication.identity import CommunicationIdentityClient, CommunicationTokenScope
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agents
import evaluator
import triage
from calls_db import ANSWERS, connect, load_calls, upsert_call
from report import build_report

ROOT = Path(__file__).parent
load_dotenv(ROOT.parent / ".env")

PLAN_NAME = "Lakeshore Health Plan"  # fictional plan for the prototype
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
ACS_CS = os.environ["ACS_CONNECTION_STRING"]
AOAI_WS = (f"wss://{os.environ['AZ_AOAI_NAME']}.openai.azure.com/openai/v1/realtime"
           f"?model={os.environ['AOAI_DEPLOYMENT']}")
TRANSCRIPTS = ROOT.parent / "calls" / "transcripts"
TRANSCRIPTS.mkdir(parents=True, exist_ok=True)

acs = CallAutomationClient.from_connection_string(ACS_CS)
identity = CommunicationIdentityClient.from_connection_string(ACS_CS)
app = FastAPI()
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

# call_connection_id -> {"listing", "status", "transcript", "answers", "end_reason", ...}
# Seeded from the database so the operator page keeps history across restarts.
with connect() as _conn:
    calls: dict[str, dict] = load_calls(_conn)
save_lock = threading.Lock()  # the media socket and the event callback can both save a call


def clinic_identity() -> str:
    """One stable ACS identity that plays 'the clinic' in the demo."""
    f = ROOT / ".clinic_identity"
    if not f.exists():
        f.write_text(identity.create_user().properties["id"])
    return f.read_text().strip()


def get_listing(listing_id: str) -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT l.listing_id, t.full_name, l.address, l.city, l.zip, l.phone
               FROM therapist_listings l JOIN therapists t USING (therapist_id)
               WHERE l.listing_id = %s""", (listing_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, f"listing {listing_id} not found")
    return dict(zip(["listing_id", "full_name", "address", "city", "zip", "phone"], row))


def instructions(l: dict, brief: dict | None) -> str:
    """The voice agent's script for one listing, cross-questioning with that office's own claims."""
    b = brief or {}
    name, evidence = l["full_name"], b.get("evidence") or "no claims evidence available"
    last_intake = (f"{name}'s last new-patient visit billed here was on {b['last_intake_said']}"
                   if b.get("last_intake_said") else f"no new-patient visit has ever been billed here for {name}")
    existing = f", while visits with {b['patients_90d']} existing patients continue" if b.get("patients_90d") else ""
    recent_intake = b.get("days_since_intake") is not None and b["days_since_intake"] <= 75
    recent_visit = b.get("days_since_visit") is not None and b["days_since_visit"] <= 45

    if b.get("probe"):
        accepting_yes = f"""   - YES, but our claims disagree. Cross-question politely, one question at a time:
       a. "I ask because our claims show {last_intake}{existing}. If {name} is taking new patients, why
          aren't we seeing new-patient claims? Has {name} seen new patients recently whose claims haven't
          been submitted to {PLAN_NAME} yet?"  -> claims_pending (yes | no)
       b. If YES (unsubmitted claims): "When did the most recent new patient start?" -> last_new_patient_on.
          Then say: "Please submit those claims to {PLAN_NAME} within 14 days, by {b['submit_by']}. We'll
          check our records again then, and follow up if we don't see them."
          If NO: "Then what changed recently, so that {name} can take new patients now?" -> recent_change,
          and "When did {name} last start a new patient?" -> last_new_patient_on.
          If their answers conflict with the evidence, point it out once, politely, and ask them to clarify.
     Then question 5, and insist on a specific date for the first new-patient appointment."""
    else:
        accepting_yes = "   - YES: go to question 5."
    closed_q = (f"""       d. "Our claims show a new patient started here as recently as {b['last_intake_said']}. When did
          {name} stop taking new patients?"  -> closed_since
""" if recent_intake else "")
    left_q = (f""" Our claims show visits billed at this address as
   recently as {b['last_visit_said']}, so also ask "When did {name} leave?" -> left_on.""" if recent_visit else "")
    moved_q = (f""" Our claims show visits billed at the listed address as recently as {b['last_visit_said']},
     so also ask "When did the move happen?" -> moved_on.""" if recent_visit else "")

    return f"""You are an automated assistant calling a therapist's office on behalf of {PLAN_NAME}
to verify one provider directory listing. Be polite, natural and brief. Speak English.
Today is {datetime.now():%A, %B %d, %Y}.

Listing: {name}, {l['address']}, {l['city']}, IL {l['zip']}. Listed phone: {l['phone']}.
What our claims say about this office: {evidence}.
You may quote these dates and counts: they are about the therapist's own billing to {PLAN_NAME}. Never
mention or ask about any individual patient. Read dates naturally ("April 4th"), not as digits.

Ask ONE question at a time, then stop and wait. Follow this flow; rephrase if they are confused.
0. "Hi, I'm an automated assistant calling for {PLAN_NAME} to update our provider directory. This takes
   a couple of minutes." Then, in the same turn, ask question 1.
1. "Is this the office of {name} at {l['address']}?"
   - A bare "no"/"nope" is ambiguous: before recording anything, ask which part is wrong: "Just to check:
     is this {name}'s office, and only the address is different?"
   - YES -> identity_confirmed = yes.
   - It IS {name}'s office but the address is different (they moved): ask for the current street address,
     read it back, then record identity_confirmed = "moved: <address>". Then ask: "Could you also update
     that address in the {PLAN_NAME} provider portal, so the directory stays correct?" -> portal_update_asked.{moved_q}
     Do NOT end the call; continue with question 2 about the new address.
   - NOT {name}'s office at all (wrong number): ask once whether they know how to reach {name}, record
     identity_confirmed = no, say goodbye and end_call(wrong_office).
2. "Does {name} still see patients there?"  If no/retired: record it.{left_q} Ask if they know where
   {name} practices now, say goodbye and end_call(not_practicing).
3. "Do you accept {PLAN_NAME}?"
4. "Is {name} taking new patients on {PLAN_NAME}?"
   - NO or WAITLIST: find out why, one at a time:
       a. "May I ask why? Is {name}'s caseload full, or is something else going on, like leave or reduced
          hours?"  -> not_accepting_reason
       b. "Our claims show {name} saw {b.get('patients_90d') or 'some'} existing patients here in the last three
          months. Is {name} able to keep seeing all of their current patients on their regular schedule?"
          -> existing_patients
       c. "When do you expect {name} to take new patients again?"  -> expected_reopen
{closed_q}     Then skip question 5 and go to question 6.
{accepting_yes}
5. "What is the earliest available appointment for a new patient?"  Push gently for a specific date.
6. "Is {l['phone']} the best number for new patients to call?"
   - If NOT: ask for the right number, then read it back digit by digit ("That's 7 7 3, 5 5 5, 0 1 2 3. Is that
     right?"). Only after they confirm, record phone_correct = the 10 digits. If they correct you, read it back
     again.

Recording answers:
- As soon as an answer is clear, in the SAME turn call record_answer and ask the next question. The tool
  result tells you what is still needed next; follow it.
- Keep turns short. No filler like "great" or "thank you for confirming"; at most "Thanks."
- If an answer is unclear, very short, or unrelated (just "okay", "thank you", noise), do NOT record it and do
  NOT end the call: repeat or rephrase the question.

Ending the call: say a short goodbye ("Thank you, that's everything. Goodbye.") then call end_call:
- completed: the tool says nothing more is needed.
- wrong_office / not_practicing: as described above.
- stop_requested: they clearly ask you to stop calling. Apologise and say you'll note it.
- hostile_or_confused: after two attempts the conversation still cannot continue.
If end_call is refused, continue with what it says is missing.
Never discuss individual patients or medical information. If asked, say you only verify directory details.
"""


FIELDS = {  # answer -> allowed values; the first six are columns in the calls table
    "identity_confirmed": "yes | no (not the therapist's office: wrong number) | moved: <new street address, city>",
    "practicing_here": "yes | no | retired",
    "accepts_plan": "yes | no | not_sure",
    "accepting_new": "yes | no | waitlist",
    "next_new_appt": "YYYY-MM-DD | none",
    "phone_correct": "yes | the corrected 10-digit number, only after reading it back and the office confirming it",
    "portal_update_asked": "will_update | declined (after asking them to update the address in the provider portal)",
    "not_accepting_reason": "short phrase in their words, e.g. caseload full, on leave until March",
    "existing_patients": "short phrase, e.g. seeing all current patients weekly | reduced schedule",
    "expected_reopen": "YYYY-MM-DD | unknown",
    "recent_change": "short phrase in their words | none",
    "last_new_patient_on": "YYYY-MM-DD | unknown",
    "claims_pending": "yes (they saw new patients whose claims aren't submitted yet) | no",
    "closed_since": "YYYY-MM-DD | unknown (when they stopped taking new patients)",
    "left_on": "YYYY-MM-DD | unknown (when the therapist left this office)",
    "moved_on": "YYYY-MM-DD | unknown (when the office moved)",
}
assert set(ANSWERS) <= set(FIELDS)  # one column per core answer in the calls table
TOOLS = [
    {"type": "function", "name": "record_answer",
     "description": "Save one answer as soon as it is clear. Allowed values: "
                    + "; ".join(f"{k}: {v}" for k, v in FIELDS.items()),
     "parameters": {"type": "object", "required": ["field", "value"], "properties": {
         "field": {"type": "string", "enum": list(FIELDS)},
         "value": {"type": "string"}}}},
    {"type": "function", "name": "end_call",
     "description": "Hang up after saying goodbye. Refused if reason is 'completed' but answers are missing.",
     "parameters": {"type": "object", "required": ["reason"], "properties": {
         "reason": {"type": "string", "enum": ["completed", "wrong_office", "not_practicing",
                                                "stop_requested", "hostile_or_confused"]}}}},
]
DATE_FIELDS = {"next_new_appt": "none", "expected_reopen": "unknown", "last_new_patient_on": "unknown",
               "closed_since": "unknown", "left_on": "unknown", "moved_on": "unknown"}


def check_answer(field: str, value: str) -> str | None:
    """Why an answer can't be saved as given, or None."""
    v = value.strip().lower()
    if field == "phone_correct" and v != "yes" and len(re.sub(r"\D", "", value)) != 10:
        return "need 'yes' or a 10-digit number, read back and confirmed by the office"
    if field == "identity_confirmed" and v.startswith("moved") and len(value.split(":", 1)[-1].strip()) < 5:
        return "record the new street address after 'moved:'"
    if field in DATE_FIELDS and v != DATE_FIELDS[field]:
        try:
            date.fromisoformat(value.strip())
        except ValueError:
            return f"need a date as YYYY-MM-DD or '{DATE_FIELDS[field]}'"
    return None


def missing_answers(c: dict) -> list[str]:
    """What the call still needs, in asking order. Follows the answers given so far."""
    a, b = c["answers"], c.get("brief") or {}
    recent_intake = b.get("days_since_intake") is not None and b["days_since_intake"] <= 75
    recent_visit = b.get("days_since_visit") is not None and b["days_since_visit"] <= 45
    need = ["identity_confirmed"]
    identity = a.get("identity_confirmed", "")
    if identity == "no":
        return [f for f in need if f not in a]
    if identity.startswith("moved"):
        need += ["portal_update_asked"] + (["moved_on"] if recent_visit else [])
    need.append("practicing_here")
    if a.get("practicing_here") in ("no", "retired"):
        need += ["left_on"] if recent_visit else []
        return [f for f in need if f not in a]
    need += ["accepts_plan", "accepting_new"]
    if a.get("accepting_new") in ("no", "waitlist"):
        need += ["not_accepting_reason", "existing_patients", "expected_reopen"]
        need += ["closed_since"] if recent_intake else []
    elif a.get("accepting_new") == "yes":
        if b.get("probe"):
            need.append("claims_pending")
            need += ["last_new_patient_on"] if a.get("claims_pending") == "yes" else ["recent_change", "last_new_patient_on"]
        need.append("next_new_appt")
    need.append("phone_correct")
    return [f for f in need if f not in a]


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/token")
def token():
    user = CommunicationUserIdentifier(clinic_identity())
    t = identity.get_token(user, scopes=[CommunicationTokenScope.VOIP])
    return {"token": t.token, "userId": user.raw_id}


@app.get("/operator")
def operator():
    return FileResponse(ROOT / "static" / "operator.html")


@app.get("/api/queue")
def queue():
    """Listings for the operator with the latest run's triage and decision: the call queue
    (by exposure) first, then the rest. Before any run, oldest-verified first."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            WITH run AS (SELECT max(run_id) AS run_id FROM verification_runs),
            ev AS (SELECT DISTINCT ON (listing_id) * FROM evaluations
                   WHERE run_id = (SELECT run_id FROM run) ORDER BY listing_id, eval_id DESC)
            SELECT l.listing_id, t.full_name, t.credential, l.address, l.city, l.zip, l.phone,
                   l.accepting_new, l.is_suppressed, l.last_verified_on, l.last_verified_how,
                   current_date - l.last_verified_on AS days_since_verified,
                   tr.claims_verdict, tr.claims_reason, tr.p_wrong, tr.exposure_per_month, tr.call_rank,
                   ev.eval_id, ev.source, ev.decision, ev.front_desk, ev.needs_review, ev.reviewed_decision
            FROM therapist_listings l JOIN therapists t USING (therapist_id)
            LEFT JOIN listing_triage tr ON tr.listing_id = l.listing_id AND tr.run_id = (SELECT run_id FROM run)
            LEFT JOIN ev ON ev.listing_id = l.listing_id
            WHERE CASE WHEN (SELECT run_id FROM run) IS NULL THEN NOT l.is_suppressed
                       ELSE tr.listing_id IS NOT NULL END
            ORDER BY tr.call_rank NULLS LAST, tr.exposure_per_month DESC NULLS LAST,
                     days_since_verified DESC, l.listing_id""")
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    last = {}
    for cid, c in calls.items():  # latest call per listing
        lid = c["listing"]["listing_id"]
        if lid not in last or c["started_at"] > last[lid]["started_at"]:
            last[lid] = {"call_id": cid, "started_at": c["started_at"], "status": c["status"],
                         "end_reason": c.get("end_reason"), "answered": len(c.get("answers", {}))}
    for r in rows:
        r["last_call"] = last.get(r["listing_id"])
    return rows


@app.get("/api/calls")
def list_calls():
    out = []
    for cid, c in sorted(calls.items(), key=lambda kv: kv[1]["started_at"], reverse=True):
        out.append({"call_id": cid, **{k: v for k, v in c.items() if k != "transcript"},
                    "transcript": [{k: v for k, v in l.items() if k != "item_id"}
                                   for l in c["transcript"]]})
    return out


class CallRequest(BaseModel):
    listing_id: str


@app.post("/api/call")
def place_call(req: CallRequest):
    if run_state["status"] == "running":
        raise HTTPException(409, "a verification run is using the clinic phone")
    return {"call_connection_id": start_call(req.listing_id)}


def start_call(listing_id: str) -> str:
    listing = get_listing(listing_id)
    with connect() as conn:
        brief = agents.call_brief(conn, listing_id)
    media = MediaStreamingOptions(
        transport_url=PUBLIC_URL.replace("https://", "wss://") + "/ws/media",
        transport_type=StreamingTransportType.WEBSOCKET,
        content_type=MediaStreamingContentType.AUDIO,
        audio_channel_type=MediaStreamingAudioChannelType.UNMIXED,  # per speaker; we forward only the clinic
        start_media_streaming=True,
        enable_bidirectional=True,
        audio_format=AudioFormat.PCM24_K_MONO,  # matches gpt-realtime's pcm16 @ 24 kHz
    )
    result = acs.create_call(
        CommunicationUserIdentifier(clinic_identity()),
        callback_url=PUBLIC_URL + "/acs-events",
        source_display_name=f"{PLAN_NAME} directory verification",
        operation_context=listing["listing_id"],
        media_streaming=media,
    )
    calls[result.call_connection_id] = {"listing": listing, "brief": brief, "status": "ringing", "transcript": [],
                                         "answers": {}, "end_reason": None, "failure": None,
                                         "started_at": datetime.now(timezone.utc).isoformat()}
    print(f"[call] placed {result.call_connection_id[:8]} for {listing['listing_id']}", flush=True)
    save_call(result.call_connection_id)
    return result.call_connection_id


@app.post("/acs-events")
async def acs_events(request: Request):
    for ev in await request.json():
        kind = ev.get("type", "").split(".")[-1]
        data = ev.get("data", {})
        info = data.get("resultInformation") or {}
        print(f"[acs] {kind} call={data.get('callConnectionId', '')[:8]} "
              f"code={info.get('code')} sub={info.get('subCode')} {info.get('message', '')[:120]}", flush=True)
        c = calls.get(data.get("callConnectionId", ""))
        if not c:
            continue
        call_id = data["callConnectionId"]
        if kind == "CallConnected":
            c["status"] = "in_progress"
        elif kind == "CreateCallFailed":
            c["status"] = "failed"
            c["failure"] = ("No answer: the clinic phone page isn't signed in" if info.get("code") == 480
                            else f"{info.get('code')}/{info.get('subCode')} {info.get('message', '')[:120]}")
        elif kind == "CallDisconnected" and c["status"] != "failed":
            c["status"] = "ended"
            if not c.get("end_reason"):
                c["end_reason"] = "declined_or_hung_up" if not c["answers"] else "hung_up_early"
        if kind in ("CallDisconnected", "CreateCallFailed"):
            c["ended_at"] = c.get("ended_at") or datetime.now(timezone.utc).isoformat()
        if kind in ("CallConnected", "CallDisconnected", "CreateCallFailed"):
            await asyncio.to_thread(save_call, call_id)
    return {}


def save_call(call_id: str):
    """Write the call and its transcript to the database, plus a JSON copy."""
    c = calls.get(call_id)
    if not c:
        return
    lines = [{k: v for k, v in l.items() if k != "item_id"} for l in c["transcript"]]
    record = {**c, "transcript": lines}
    with save_lock:
        try:
            with connect() as conn:
                upsert_call(conn, call_id, record)
        except Exception as e:  # never let a DB hiccup break a live call
            print(f"[call] DB save failed for {call_id[:8]}: {e}", flush=True)
        out = TRANSCRIPTS / f"{c['listing']['listing_id']}_{call_id[:8]}.json"
        out.write_text(json.dumps({"call_connection_id": call_id, **record}, indent=2, default=str))
    print(f"[call] saved {call_id[:8]} ({c['status']}, {len(lines)} turns)", flush=True)


@app.websocket("/ws/media")
async def media(ws: WebSocket):
    await ws.accept()
    call_id = ws.headers.get("x-ms-call-connection-id", "")
    c = calls.get(call_id)
    if not c:
        print(f"[media] unknown call {call_id}", flush=True)
        await ws.close()
        return
    print(f"[media] ACS audio connected for {call_id[:8]}", flush=True)

    async with websockets.connect(AOAI_WS, additional_headers={"api-key": os.environ["AOAI_KEY"]}) as ai:
        pcm24k = {"type": "audio/pcm", "rate": 24000}  # same as ACS Pcm24KMono
        await ai.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime",
            "instructions": instructions(c["listing"], c.get("brief")),
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": pcm24k,
                    "noise_reduction": {"type": "near_field"},
                    "transcription": {"model": "gpt-4o-transcribe", "language": "en"},
                    # ~0.5 s of silence ends the clinic's turn: short scripted answers
                    "turn_detection": {"type": "server_vad", "threshold": 0.5,
                                       "prefix_padding_ms": 300, "silence_duration_ms": 500},
                },
                "output": {"format": pcm24k, "voice": "alloy", "speed": 1.1},
            },
            "tools": TOOLS,
        }}))
        # Agent speaks first: the disclosure line (question 0) and question 1
        await ai.send(json.dumps({"type": "response.create"}))

        clinic_id = clinic_identity()
        loop = asyncio.get_running_loop()
        last_clinic_audio = loop.time()
        SILENCE_20MS = base64.b64encode(bytes(960)).decode()  # 24 kHz * 16-bit * 20 ms

        async def acs_to_ai():
            nonlocal last_clinic_audio
            seen = set()
            try:
                while True:
                    msg = json.loads(await ws.receive_text())
                    if msg.get("kind") != "AudioData":
                        continue
                    a = msg["audioData"]
                    pid = a.get("participantRawID")
                    if pid not in seen:
                        seen.add(pid)
                        print(f"[media] audio from {pid} ({'clinic' if pid == clinic_id else 'NOT clinic, ignored'})", flush=True)
                    if pid != clinic_id:
                        continue  # never let the agent hear itself
                    last_clinic_audio = loop.time()
                    await ai.send(json.dumps({"type": "input_audio_buffer.append", "audio": a["data"]}))
            except WebSocketDisconnect:
                pass

        async def pad_silence():
            # Unmixed streams may pause when nobody talks; VAD needs continuous audio
            while True:
                await asyncio.sleep(0.02)
                if loop.time() - last_clinic_audio > 0.06:
                    await ai.send(json.dumps({"type": "input_audio_buffer.append", "audio": SILENCE_20MS}))

        async def tool_result(call_id_: str, output: str):
            await ai.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": call_id_, "output": output}}))

        def say(speaker: str, text, item_id=None):
            c["transcript"].append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "speaker": speaker, "text": text, "item_id": item_id})

        async def ai_to_acs():
            needs_followup = False   # a tool result was sent; ask the model to continue
            hang_up_after = None     # reason, once end_call is accepted
            goodbye_asked = False
            audio_bytes = 0          # agent audio sent in the current response
            first_audio_at = None
            speech_stopped_at = None
            async for raw in ai:
                ev = json.loads(raw)
                t = ev["type"]
                if t == "response.created":
                    audio_bytes, first_audio_at = 0, None
                elif t == "response.output_audio.delta":
                    audio_bytes += len(ev["delta"]) * 3 // 4
                    if first_audio_at is None:
                        first_audio_at = loop.time()
                        if speech_stopped_at:
                            print(f"  [latency] {first_audio_at - speech_stopped_at:.1f}s from clinic silence to agent voice", flush=True)
                            speech_stopped_at = None
                    await ws.send_text(json.dumps({"kind": "AudioData", "audioData": {"data": ev["delta"]}}))
                elif t == "input_audio_buffer.speech_stopped":
                    speech_stopped_at = loop.time()
                elif t == "input_audio_buffer.speech_started":
                    # Barge-in: the clinic started talking, stop the agent's queued audio
                    await ws.send_text(json.dumps({"kind": "StopAudio", "audioData": None, "stopAudio": {}}))
                elif t == "input_audio_buffer.committed":
                    # Reserve the clinic's turn now so it stays in order; text arrives later
                    say("clinic", None, ev["item_id"])
                elif t == "response.output_audio_transcript.done":
                    say("agent", ev["transcript"])
                    print(f"  AGENT : {ev['transcript']}", flush=True)
                elif t in ("conversation.item.input_audio_transcription.completed",
                           "conversation.item.input_audio_transcription.failed"):
                    text = ev["transcript"].strip() if "transcript" in ev else "[inaudible]"
                    for line in c["transcript"]:
                        if line["item_id"] == ev["item_id"]:
                            line["text"] = text
                    print(f"  CLINIC: {text}", flush=True)
                elif t == "response.function_call_arguments.done":
                    args = json.loads(ev.get("arguments") or "{}")
                    if ev["name"] == "record_answer":
                        problem = check_answer(args.get("field", ""), args.get("value", ""))
                        if problem:
                            print(f"  [answer rejected] {args} ({problem})", flush=True)
                            await tool_result(ev["call_id"], f"NOT saved: {problem}. Ask again or clarify.")
                        else:
                            value = args["value"].strip()
                            if args["field"] == "phone_correct" and value.lower() != "yes":
                                value = re.sub(r"\D", "", value)
                            c["answers"][args["field"]] = value
                            print(f"  [answer] {args['field']} = {value}", flush=True)
                            missing = missing_answers(c)
                            await tool_result(ev["call_id"], "saved. " + (
                                f"next: {missing[0]} (then: {', '.join(missing[1:]) or 'nothing'})" if missing
                                else "nothing more is needed: say goodbye and end_call(completed)"))
                        needs_followup = True
                    elif ev["name"] == "end_call":
                        missing = missing_answers(c)
                        if args.get("reason") == "completed" and missing:
                            print(f"  [end_call refused] missing {missing}", flush=True)
                            await tool_result(ev["call_id"], "REFUSED: not finished. Do not say goodbye. "
                                              f"Still ask, in order: {', '.join(missing)}")
                            needs_followup = True
                        else:
                            c["end_reason"] = args.get("reason")
                            print(f"  [end_call] {c['end_reason']}", flush=True)
                            hang_up_after = c["end_reason"]
                            await tool_result(ev["call_id"], "ok, ending the call")
                elif t == "response.done":
                    if hang_up_after and not audio_bytes and not goodbye_asked:
                        # It ended silently: make sure the office hears a goodbye first
                        goodbye_asked = True
                        await ai.send(json.dumps({"type": "response.create", "response": {
                            "instructions": "Say only: Thank you, that's everything. Goodbye.",
                            "tool_choice": "none"}}))
                        continue
                    if hang_up_after:
                        # Wait until the goodbye has finished playing (24 kHz 16-bit = 48000 B/s)
                        played = loop.time() - (first_audio_at or 0)
                        await asyncio.sleep(max(1.5, audio_bytes / 48000 - played + 1.5) if first_audio_at else 1.5)
                        try:
                            acs.get_call_connection(call_id).hang_up(is_for_everyone=True)
                        except Exception as e:
                            print(f"[call] hang_up: {e}", flush=True)
                        return
                    if needs_followup and not audio_bytes:
                        # Recorded an answer silently: ask it to continue with the next question
                        await ai.send(json.dumps({"type": "response.create"}))
                    needs_followup = False
                elif t == "error":
                    print(f"[ai] error: {ev.get('error')}", flush=True)

        tasks = [asyncio.create_task(acs_to_ai()), asyncio.create_task(ai_to_acs())]
        padder = asyncio.create_task(pad_silence())
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        padder.cancel()
        for task in tasks:
            task.cancel()
    await asyncio.to_thread(save_call, call_id)


# ---- verification runs -------------------------------------------------------------

run_state = {"run_id": None, "status": "idle", "stage": 0, "phase": "", "queue": [], "position": 0,
             "current_call": None, "note": None, "stop": False}
with connect() as _conn:  # the operator page suggests the listings after the last run's range
    _row = _conn.execute("""SELECT listing_offset + listing_limit FROM verification_runs
                            WHERE listing_limit IS NOT NULL ORDER BY run_id DESC LIMIT 1""").fetchone()
    run_state["next_offset"] = _row[0] if _row else 0


class RunRequest(BaseModel):
    listing_offset: int = 0          # testing: skip this many listings (by listing_id) ...
    listing_limit: int | None = 10   # ... then check N; null = the rest of the directory


@app.post("/api/runs")
async def start_run(req: RunRequest):
    if run_state["status"] == "running":
        raise HTTPException(409, "a run is already in progress")
    if any(c["status"] in ("ringing", "in_progress") for c in calls.values()):
        raise HTTPException(409, "a call is in progress")
    limit = req.listing_limit if req.listing_limit and req.listing_limit > 0 else None
    offset = max(0, req.listing_offset)
    run_state["next_offset"] = offset + limit if limit else 0
    run_state.update(run_id=None, status="running", stage=1, phase="Studying claims",
                     queue=[], position=0, current_call=None, note=None, stop=False)
    run_state["task"] = asyncio.create_task(run_verification(limit, offset))
    return {"ok": True}


@app.post("/api/runs/stop")
def stop_run():
    run_state["stop"] = True
    return {"ok": True}


@app.get("/api/runs/current")
def current_run():
    return {k: v for k, v in run_state.items() if k != "task"}


async def run_verification(limit: int | None, offset: int = 0):
    def progress(done, total):
        run_state["phase"] = f"Studying claims: {done} of {total} listings"

    def begin():
        with connect() as conn:
            run_id = conn.execute("""INSERT INTO verification_runs (status, listing_offset, listing_limit)
                                     VALUES ('running', %s, %s) RETURNING run_id""", (offset, limit)).fetchone()[0]
            conn.commit()
            run_state.update(run_id=run_id, phase="Rechecking provisional promises against claims")
            checked = evaluator.run_rechecks(conn)
            if checked:
                print(f"[run {run_id}] rechecks: {checked}", flush=True)
            conn.commit()
            rows = triage.run_triage(conn, run_id, limit, progress, offset)
            run_state.update(stage=2, phase="Ranking the calls by $ exposure")
            evaluator.decide_from_claims(conn, run_id, rows)
        return run_id, rows

    def evaluate(run_id, listing_id, call_id):
        with connect() as conn:
            evaluator.evaluate_call(conn, run_id, listing_id, call_id, calls[call_id])

    def finish(run_id, status, note):
        with connect() as conn:
            conn.execute("""UPDATE verification_runs SET status = %s, note = %s, finished_at = now()
                            WHERE run_id = %s""", (status, note, run_id))
            evaluator.write_summary(conn, run_id, build_report(conn, run_id))

    run_id, note = None, None
    try:
        run_id, rows = await asyncio.to_thread(begin)
        queue = [r["listing_id"] for r in sorted((r for r in rows if r.get("call_rank")),
                                                 key=lambda r: r["call_rank"])]
        decided = len(rows) - len(queue)
        print(f"[run {run_id}] triage: {decided} decided from claims, calling {len(queue)}", flush=True)
        run_state.update(run_id=run_id, queue=queue)
        retried = set()
        i = 0
        while i < len(queue):
            listing_id = queue[i]
            i += 1
            if run_state["stop"]:
                note = "stopped by the operator"
                break
            run_state.update(stage=3, position=i, queue=queue, phase=f"Calling {listing_id} ({i} of {len(queue)})")
            call_id = await asyncio.to_thread(start_call, listing_id)
            run_state["current_call"] = call_id
            c = await wait_for_call(call_id)
            if c["status"] == "failed" and (c.get("failure") or "").startswith("No answer"):
                note = "stopped: the clinic phone page isn't signed in"
                break
            if c["status"] == "failed" and listing_id not in retried:
                retried.add(listing_id)   # rang out unanswered: try once more at the end of the queue
                queue.append(listing_id)
                await asyncio.sleep(3)
                continue
            run_state.update(stage=4, phase=f"Evaluating the call to {listing_id}")
            await asyncio.to_thread(evaluate, run_id, listing_id, call_id)
            await asyncio.sleep(3)  # a breather for whoever answers the clinic phone
        status = "stopped" if note else "done"
        run_state.update(stage=5, phase="Writing the report")
        await asyncio.to_thread(finish, run_id, status, note)
        run_state.update(status=status, stage=6, phase="Report ready", note=note, current_call=None)
    except Exception as e:
        print(f"[run] failed: {e!r}", flush=True)
        run_state.update(status="failed", phase="Run failed", note=str(e), current_call=None)
        run_id = run_id or run_state.get("run_id")
        if run_id:
            await asyncio.to_thread(finish, run_id, "failed", str(e))


async def wait_for_call(call_id: str, timeout_s: int = 600) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    c = calls[call_id]
    while c["status"] not in ("ended", "failed"):
        if loop.time() > deadline:
            try:
                acs.get_call_connection(call_id).hang_up(is_for_everyone=True)
            except Exception as e:
                print(f"[run] hang_up after timeout: {e}", flush=True)
            c["status"], c["end_reason"] = "ended", c.get("end_reason") or "timed_out"
            break
        await asyncio.sleep(1)
    await asyncio.sleep(2)  # let the media socket finish saving the transcript
    return c


@app.get("/api/reviews")
def reviews():
    with connect() as conn:
        cur = conn.execute("""
            SELECT e.eval_id, e.run_id, e.listing_id, e.call_id, e.decision, e.confidence, e.conflicts,
                   e.rationale, e.new_phone, e.new_address, e.created_at, t.full_name, l.address, l.city
            FROM evaluations e JOIN therapist_listings l USING (listing_id) JOIN therapists t USING (therapist_id)
            WHERE e.needs_review AND e.reviewed_decision IS NULL ORDER BY e.eval_id""")
        return [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]


class ReviewRequest(BaseModel):
    decision: str
    note: str | None = None


@app.post("/api/evaluations/{eval_id}/review")
def review(eval_id: int, req: ReviewRequest):
    if req.decision not in evaluator.DECISIONS + ["no_change"]:
        raise HTTPException(400, f"unknown decision {req.decision}")
    with connect() as conn:
        evaluator.review(conn, eval_id, req.decision, req.note)
    return {"ok": True}


@app.get("/report")
def report_page():
    return FileResponse(ROOT / "static" / "report.html")


@app.get("/api/report")
def report(run_id: int | None = None):
    with connect() as conn:
        r = build_report(conn, run_id)
    if not r:
        raise HTTPException(404, "no verification run yet")
    return r


ROLE_CARDS = {  # demo only: the hidden answer key, so whoever plays the clinic can act it out
    "active_open": "The therapist works here and IS taking new patients. Offer a new-patient "
                   "appointment about a week from now. If the agent says claims show no recent new "
                   "patients, explain that you saw two new patients this month but haven't submitted "
                   "those claims yet, and agree to submit them -> expect PROVISIONALLY accepting.",
    "full_caseload": "Truth: the caseload is FULL (existing patients weekly). Try one of: "
                     "(a) honest: not accepting, explain the full caseload, reopening around spring; "
                     "(b) credible yes: 'she just reopened her panel two weeks ago', give a specific "
                     "slot next week, or say new-patient claims haven't been submitted yet "
                     "-> expect PROVISIONALLY accepting with a recheck date; "
                     "(c) vague yes: 'yes, we're accepting' but dodge dates when probed -> expect "
                     "front desk NOT GOOD.",
    "moved": "The therapist MOVED to another office a few months ago. Say the address is wrong and give "
             "the new one, e.g. 2200 N Clark St, Chicago. Also try giving a new phone number.",
    "retired": "The therapist RETIRED and no longer sees patients here.",
    "wrong_phone": "This number is NOT the therapist's office: say it's a different business "
                   "and they have the wrong number.",
}


@app.get("/api/role-card/{listing_id}")
def role_card(listing_id: str):
    with connect() as conn:
        row = conn.execute("SELECT true_status FROM therapist_listings WHERE listing_id = %s",
                           (listing_id,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown listing")
    return {"true_status": row[0], "card": ROLE_CARDS[row[0]]}


@app.get("/api/provisional")
def provisional():
    with connect() as conn:
        cur = conn.execute("""
            SELECT e.eval_id, e.listing_id, t.full_name, l.address, l.city, e.created_at::date AS promised_on,
                   c.next_new_appt, e.recheck_on, e.recheck_result, e.rechecked_at,
                   EXISTS (SELECT 1 FROM claims x WHERE x.listing_id = e.listing_id AND x.claim_id LIKE 'DEMO-%%'
                           AND x.service_date >= e.created_at::date) AS demo_intake
            FROM evaluations e JOIN therapist_listings l USING (listing_id) JOIN therapists t USING (therapist_id)
            LEFT JOIN calls c ON c.call_id = e.call_id
            WHERE coalesce(e.reviewed_decision, e.decision) = 'provisionally_accepting'
              AND e.applied_at IS NOT NULL AND e.recheck_on IS NOT NULL
            ORDER BY e.recheck_result NULLS FIRST, e.recheck_on""")
        return [dict(zip([d.name for d in cur.description], r)) for r in cur.fetchall()]


class RecheckRequest(BaseModel):
    force: bool = False


@app.post("/api/rechecks")
def rechecks(req: RecheckRequest):
    with connect() as conn:
        return evaluator.run_rechecks(conn, req.force)


@app.post("/api/demo/intake/{listing_id}")
def demo_intake(listing_id: str):
    """Demo only: the promised new patient was seen today and the claim arrived."""
    with connect() as conn:
        row = conn.execute("""SELECT l.therapist_id, t.credential FROM therapist_listings l
                              JOIN therapists t USING (therapist_id) WHERE listing_id = %s""", (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "unknown listing")
        patient = conn.execute("""SELECT patient_id FROM patients
                                  WHERE enrolled_until IS NULL OR enrolled_until >= current_date
                                  ORDER BY random() LIMIT 1""").fetchone()[0]
        code = "90792" if row[1] == "MD" else "90791"
        claim_id = f"DEMO-{os.urandom(3).hex()}"
        conn.execute("""INSERT INTO claims (claim_id, therapist_id, listing_id, patient_id, service_date,
                                            received_date, procedure_code, paid_amount)
                        VALUES (%s, %s, %s, %s, current_date, current_date, %s, 175)""",
                     (claim_id, row[0], listing_id, patient, code))
    return {"claim_id": claim_id, "procedure_code": code}
