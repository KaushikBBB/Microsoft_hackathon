"""Verification calls and their transcripts in Postgres (tables calls, call_turns).

app.py saves each call here as it progresses. It also keeps a JSON copy per call in
calls/transcripts/, because db/load.sh drops every table when it reloads the seed data;
running this file re-imports those copies.

Usage:  uv run python calls_db.py ../calls/transcripts/*.json
"""
import json
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

load_dotenv(Path(__file__).parent.parent / ".env")

ANSWERS = ["identity_confirmed", "practicing_here", "accepts_plan",   # one column each;
           "accepting_new", "next_new_appt", "phone_correct"]           # follow-ups go to extra_answers


def connect():
    return psycopg.connect(
        host=os.environ["PGHOST"], dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"], sslmode="require",
    )


def upsert_call(conn, call_id: str, c: dict):
    """Insert or update one call (the in-memory dict app.py keeps) and replace its turns."""
    a = c.get("answers") or {}
    extra = {k: v for k, v in a.items() if k not in ANSWERS}
    conn.execute(
        f"""INSERT INTO calls (call_id, listing_id, status, started_at, ended_at, end_reason, failure,
                               {", ".join(ANSWERS)}, extra_answers, brief)
            VALUES (%s, %s, %s, %s, %s, %s, %s, {", ".join(["%s"] * len(ANSWERS))}, %s, %s)
            ON CONFLICT (call_id) DO UPDATE SET
              status = EXCLUDED.status, ended_at = EXCLUDED.ended_at,
              end_reason = EXCLUDED.end_reason, failure = EXCLUDED.failure,
              {", ".join(f"{k} = EXCLUDED.{k}" for k in ANSWERS)},
              extra_answers = EXCLUDED.extra_answers, brief = EXCLUDED.brief""",
        [call_id, c["listing"]["listing_id"], c.get("status", "ended"), c["started_at"],
         c.get("ended_at"), c.get("end_reason"), c.get("failure"), *(a.get(k) for k in ANSWERS),
         Jsonb(extra) if extra else None, Jsonb(c["brief"]) if c.get("brief") else None])
    conn.execute("DELETE FROM call_turns WHERE call_id = %s", (call_id,))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO call_turns (call_id, turn_no, spoken_at, speaker, text) VALUES (%s, %s, %s, %s, %s)",
            [(call_id, i, t.get("at"), t["speaker"], t.get("text"))
             for i, t in enumerate(c.get("transcript", []), 1)])


def load_calls(conn) -> dict:
    """Every saved call, keyed by call_id, in the shape app.py keeps in memory."""
    calls = {}
    rows = conn.execute(
        f"""SELECT c.call_id, c.status, c.started_at, c.ended_at, c.end_reason, c.failure,
                   {", ".join("c." + k for k in ANSWERS)}, c.extra_answers, c.brief,
                   l.listing_id, t.full_name, l.address, l.city, l.zip, l.phone
            FROM calls c JOIN therapist_listings l USING (listing_id) JOIN therapists t USING (therapist_id)
            ORDER BY c.started_at""").fetchall()
    for r in rows:
        call_id, status, started, ended, end_reason, failure = r[:6]
        n = 6 + len(ANSWERS)
        answers = {**dict(zip(ANSWERS, r[6:n])), **(r[n] or {})}
        calls[call_id] = {
            "listing": dict(zip(["listing_id", "full_name", "address", "city", "zip", "phone"], r[n + 2:])),
            "status": status, "started_at": started.isoformat(),
            "ended_at": ended.isoformat() if ended else None,
            "end_reason": end_reason, "failure": failure, "brief": r[n + 1],
            "answers": {k: v for k, v in answers.items() if v is not None},
            "transcript": [],
        }
    for call_id, at, speaker, text in conn.execute(
            "SELECT call_id, spoken_at, speaker, text FROM call_turns ORDER BY call_id, turn_no"):
        calls[call_id]["transcript"].append(
            {"at": at.isoformat() if at else None, "speaker": speaker, "text": text})
    return calls


if __name__ == "__main__":
    with connect() as conn:
        for f in sys.argv[1:]:
            saved = json.loads(Path(f).read_text())
            upsert_call(conn, saved.pop("call_connection_id"), saved)
        n = conn.execute("SELECT count(*) FROM calls").fetchone()[0]
    print(f"imported {len(sys.argv) - 1} call file(s); calls table now has {n}")
