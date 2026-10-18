Here's the whole system, end to end. I haven't looked at your test run's results yet; I can go through them next.

# The big picture

We built a **verification run**. You click **Start verification**, and it works through the directory (or the first N listings) in five stages:

```
listings ──► 1 STUDY CLAIMS ──► 2 PRIORITIZE ──► 3 CALL ──► 4 EVALUATE ──► 5 REPORT
             rules + triage     ML + formula     voice      evaluator      LLM summary
             agent (LLM)        (no LLM)         agent      agent (LLM)    + tables
                 │                                              │
                 └─ decisive → decided, no call                 └─ undecided → human review
```

There are **three LLM agents**. Everything around them is ordinary Python, so the parts that must be auditable (rules, ranking, the review gate) are code, not model judgment.

| Agent | Model | Job |
|---|---|---|
| Triage agent | gpt-4.1-mini | Studies claims and decides: settled, or needs a call? |
| Voice agent | gpt-realtime | Phones the office and asks the 6 questions |
| Evaluator agent | gpt-4.1-mini | Checks the call against the evidence and decides |

---

# The data underneath

| Table | What it is | Rows |
|---|---|---|
| `therapists` | Credentialing: license, sanctions, network contract, languages | 50 |
| `therapist_listings` | The directory: address, phone, "accepting new patients", plus the hidden `true_status` answer key | 62 |
| `patients` | Plan members: home zip and location | 450 |
| `claims` | Paid visits: which office, which patient, procedure code, service and received dates | 7,278 |
| `directory_views` | Members viewing a listing and whether they clicked the phone number | 3,000 |
| `complaints` | "Couldn't book", "wrong number", "not in network" | 84 |
| `calls` / `call_turns` | Every call, its 6 answers, and the transcript line by line | live |
| `verification_runs` | One row per run, including the LLM summary | live |
| `listing_triage` | Per run and listing: evidence, verdict, the triage agent's reasoning and tools used, P(wrong), exposure, call rank | live |
| `evaluations` | Per run and listing: decision, front-desk flag, conflicts, rationale, old/new phone and address, reviewer's decision | live |

**Why claims matter:** procedure codes **90791 and 90792 are new-patient intakes**. The others (90834, 90837, 90847, 99214) are ongoing visits. "Is this therapist taking new patients?" therefore becomes "when was the last intake billed at this office?"

The synthetic data is generated so each ghost type leaves a different fingerprint:
- **Retired:** claims stop.
- **Full caseload:** visits continue, but no intakes.
- **Moved:** visits continue at an office not in the directory.
- **Wrong phone:** claims look normal, but members complain about a wrong number.

---

# Stage 1: Study claims (rules plus the triage agent)
Code: [triage.py](Microsoft_hackathon/agent/triage.py) and [agents.py](Microsoft_hackathon/agent/agents.py)

**Step 1a: hard-fail rules, no LLM.** A listing is suppressed straight away if the license isn't active or has expired, the therapist is sanctioned, or the network contract ended. Nobody needs to judge these.

**Step 1b: the triage agent.** For every other listing, gpt-4.1-mini gets the plan rule and three read-only tools:

| Tool | Returns |
|---|---|
| `get_listing(listing_id)` | Directory entry plus credentialing record: license, sanctions, contract dates, languages, telehealth |
| `get_claims_summary(listing_id)` | See the breakdown below |
| `get_complaints(listing_id)` | Complaints in the last 180 days (date and category), plus directory views and phone clicks in the last 90 days |
| `submit_triage(verdict, confidence, reason)` | The finishing tool: `accepting`, `not_accepting` or `needs_call` |

`get_claims_summary` returns:
- the last visit billed at this office and the last new-patient intake, each with days elapsed;
- visits, intakes and patients **month by month for 12 months**;
- visits by this therapist at an office *not* in the directory in the last 180 days (the "moved" signal);
- the therapist's other listed offices and their last visit;
- a note that claims arrive 1–6 weeks late.

It is told:
- **Plan rule:** a listing marked "accepting" must show a new-patient intake **every 30 days**.
- **Claims lag:** about 45 days, so a gap shorter than about **75 days** proves nothing.
- **When in doubt, call.**

**Step 1c: guardrails, in code.** The agent's verdict is overruled to `needs_call` if:
- its confidence is below **0.75**;
- it says *accepting* but there's no intake within 30 days, or there are complaints;
- it says *not accepting* but the therapist hasn't billed at this office in 60 days (they may have moved or retired, which needs a call);
- it says *not accepting* but the last intake was within 75 days (it could still be inside the claims lag).

When a guardrail fires, the stored reason records both sides, for example: "agent said accepting; overruled: no new-patient intake within 30 days". Decisive listings (hard fail, accepting, not accepting) are **decided immediately with no call**.

---

# Stage 2: Prioritize (ML plus formula, no LLM)
Code: `score()` and `p_wrong()` in [triage.py](Microsoft_hackathon/agent/triage.py)

Every listing that needs a call gets a **dollar exposure per month**, and they're called highest first:

> **Exposure ($/month) = P(wrong) × members likely to try per month × cost of one failed attempt**

### P(wrong): a machine-learning model
A **logistic regression** (scikit-learn, standardized inputs) predicts how likely the listing is to be wrong. It uses 9 features:

| Feature | Why it matters |
|---|---|
| Days since the last claim at this office | Claims stopped → retired or moved |
| Days since the last new-patient intake | Full caseload |
| Visits in the last 90 days | Activity level |
| Intakes in the last 365 days | Usual rate of taking new patients |
| Visits at an office not in the directory (180 days) | Moved |
| Complaints in the last 90 days | Members hitting the problem |
| Days since the listing was last verified | Staleness |
| Last verified by self-attestation | Weakest verification method |
| Directory says "accepting" | What members are being told |

- **Labels:** the training labels come from the hidden `true_status`. That only works in this prototype; in production they would be past verified outcomes, such as earlier runs' decisions.
- **Out-of-fold scoring:** we use 5-fold cross-validation, so **no listing is scored by a model that saw its own answer**. That keeps the score honest. It measured **0.85 AUC**, meaning that in 85% of (ghost, healthy) pairs the ghost scores higher.
- The model trains on the whole directory even when you only check 10 listings.
- Hard-fail listings get P(wrong) = 1.

### Members likely to try (demand): a formula
Two signals are blended:
- **Observed:** phone clicks on this listing in the last 90 days.
- **Location-based:** enrolled members living within **10 km**, times the directory-wide average clicks per nearby member. This puts it in the same units as clicks.

`demand per month = (clicks_90d + expected_clicks) / 2 / 3`

Blending smooths out noisy click counts on small listings, and it's how location enters the score.

### Cost of one failed attempt: a formula
`cost = base by credential × language factor × scarcity factor`

| Factor | Value | Why |
|---|---|---|
| Base: psychiatrist (MD) | $400 | Scarcest, highest stakes |
| Base: psychologist (PsyD/PhD) | $250 | |
| Base: counselor (LCSW/LPC/LMFT) | $150 | |
| × 1.3 | Speaks a non-English language | Hard to replace for that member |
| × 1.5 | Fewer than 3 other listings within 10 km | No nearby alternative |

The cost stands for delayed care, call-center and complaint handling, and No Surprises Act exposure (the member relied on a wrong directory).

**Example from our data:** L0024 has P(wrong) 0.96 × 7.3 tries a month × $400 (psychiatrist) ≈ **$2,800 a month**, so it's called first. It turned out to be a real full-caseload ghost.

---

# Stage 3: Call
Code: [app.py](Microsoft_hackathon/agent/app.py)

How a call works:
- **Azure Communication Services (ACS)** places a web call to the "clinic" identity. That's the clinic page in a browser, because the sandbox can't dial real phones.
- ACS streams the call's audio both ways over a WebSocket to our server, which relays it to **Azure OpenAI gpt-realtime**: a speech-to-speech model, 24 kHz audio, voice "alloy", 1.1× speed.
- Audio is streamed **per speaker**, and only the clinic's audio goes to the model, so it never hears itself.
- **Server voice-activity detection:** 0.5 seconds of silence ends the clinic's turn.
- If the clinic starts talking, we stop the agent's queued audio ("barge-in").
- We pad silence when nobody speaks, so turn detection keeps working.

**The voice agent's script** (fixed order, one question at a time):
0. A disclosure: "automated assistant calling for Lakeshore Health Plan…"
1. Is this the office of the listed therapist at the listed address? (If they moved, ask for the new address.)
2. Does the therapist still see patients here?
3. Do you accept Lakeshore Health Plan?
4. Are you taking new patients?
5. When is the earliest new-patient appointment?
6. Is this the best phone number for new patients? (If not, get the right one and read it back.)

**The voice agent's tools:**

| Tool | What it does |
|---|---|
| `record_answer(field, value)` | Saves one answer as soon as it's clear, with a fixed set of allowed values per field. The server replies with which questions are still missing. |
| `end_call(reason)` | Says goodbye and hangs up. Reason is `completed`, `wrong_office`, `stop_requested` or `hostile_or_confused`. **The server refuses `completed` if any answer is missing,** so the agent has to go back and ask. |

The run places calls **one after another**:
- It waits until each call ends or fails (6-minute timeout), then evaluates it, then moves to the next.
- If the first call isn't answered because the clinic page isn't signed in, the **run stops** instead of burning through the queue.
- **Transcripts are saved to the DB** (`calls` and `call_turns`) when the call is placed, when it connects and when it ends. A JSON backup also goes to `calls/transcripts/`, like the file you have open.

---

# Stage 4: Evaluate (rules plus the evaluator agent)
Code: [evaluator.py](Microsoft_hackathon/agent/evaluator.py)

**Step 4a: rule check, in code.** This compares the answers with the evidence and lists conflicts:

| Answer | Proposed decision | Conflict flagged if… |
|---|---|---|
| Wrong office / "not this office" | Suppress | — |
| Therapist retired / no longer here | Suppress | Claims show a visit here in the last 45 days |
| Moved, with a new address | Keep the listing, change the address | Claims still show visits at the old address; no address captured |
| Doesn't accept the plan | Suppress | Claims were paid here in the last 90 days |
| Not accepting / waitlist | Set not accepting | — |
| Accepting | Verified accepting | No intake in 75 days; complaints; earliest appointment more than **10 business days** out; no appointment to offer |

**Step 4b: the evaluator agent** (gpt-4.1-mini) gets the rule check's output and these tools:

| Tool | Returns |
|---|---|
| `get_listing`, `get_claims_summary`, `get_complaints` | Same tools as the triage agent |
| `get_call(listing_id)` | The 6 recorded answers, how the call ended, and the full transcript |
| `submit_evaluation(...)` | **front_desk**: `good` / `not_good` / `unsure`. **decision**: verified accepting, set not accepting, suppress, or unsure. Plus **new_address**, **new_phone**, confidence, conflicts and a 2–3 sentence rationale. |

It's told that front desks often say "yes, accepting" out of habit, so it should trust claims and complaints over a bare yes. A moved therapist keeps the listing with a new address rather than being suppressed.

**Step 4c: review gate, in code.** The listing goes to **human review** if:
- the call didn't complete;
- the agent decided "unsure";
- the agent can't tell whether the front desk is legit;
- the agent's confidence is below **0.7**;
- the agent would verify "accepting" while the claims evidence contradicts it.

A front desk flagged **not good** doesn't go to review by itself. If the agent confidently decides (for example "set not accepting"), that decision is applied and the flag is recorded.

**Step 4d: apply.** For a final decision, the listing is updated:
- the accepting flag is flipped, or the listing is suppressed;
- if the listing stays, the **new phone or address** is written in;
- `last_verified_on` is set to today and `last_verified_how` to `call` or `claims`.

The old phone and address are kept on the evaluation, so the report can show what each was before and after.

**Human review:** the operator page's **Needs human review** panel shows the agent's proposal, every conflict, the rationale and a "view call" link. Its buttons are: Verified accepting / Not accepting / Suppressed / No change. Your choice is saved and applied the same way.

---

# Stage 5: Report
Code: [report.py](Microsoft_hackathon/agent/report.py) and [report.html](Microsoft_hackathon/agent/static/report.html)

The report has:
- **An executive summary** written by gpt-4.1-mini from the run's numbers.
- **Tiles:**
  - listings checked, decided from claims, and calls made;
  - front desks flagged not good, and listings waiting for review;
  - exposure removed versus still open (in $ a month);
  - contact details updated.
- **Outcome per listing** as a bar breakdown.
- **Needs human review:** each item's proposal and the reasons it needs a human.
- **Calls made:**
  - why each listing was called and how the call ended;
  - the front-desk flag, the decision and the evaluator's reasoning.
- **Contact details updated:** each address or phone, before and after.
- **All listings:** the claims verdict, P(wrong), tries per month, cost per try, exposure and the final decision.
- **Prototype check:** every final decision scored against the hidden `true_status`, split into decided-from-claims and decided-after-a-call, with the misses listed.

---

# How an agent works under the hood
`run_agent()` in [agents.py](Microsoft_hackathon/agent/agents.py) is a standard tool-calling loop:
1. Send the model the instructions, the task ("Triage listing L0005") and the tool list.
2. Every turn the model **must** call a tool (`tool_choice="required"`). We run the tool, return the result, and loop.
3. When it calls its **submit** tool, the loop ends and returns the decision. The submit tool uses a strict schema, so the output is always well-formed.
4. It's capped at 8 turns, and every tool it used is saved as the **agent trace** in the DB.
5. If the LLM fails: triage falls back to calling the listing, and evaluation falls back to human review.

---

# Settings in one place

| Setting | Value | File |
|---|---|---|
| New-patient intake required every | 30 days | `agents.py` |
| Claims lag allowance | 45 days | `agents.py` |
| Triage agent minimum confidence | 0.75 | `triage.py` |
| Evaluator minimum confidence | 0.70 | `evaluator.py` |
| Earliest-appointment standard | 10 business days | `evaluator.py` |
| Member search radius | 10 km | `triage.py` |
| Cost by credential and multipliers | $400 / $250 / $150, ×1.3, ×1.5 | `triage.py` |

---
