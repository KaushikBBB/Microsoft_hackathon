-- Ghost Listing Verifier: prototype schema for ONE insurer, ONE plan.
-- plan_id is dropped everywhere because there is only one plan.

DROP TABLE IF EXISTS evaluations;
DROP TABLE IF EXISTS listing_triage;
DROP TABLE IF EXISTS verification_runs;
DROP TABLE IF EXISTS call_turns;
DROP TABLE IF EXISTS calls;
DROP TABLE IF EXISTS complaints;
DROP TABLE IF EXISTS directory_views;
DROP TABLE IF EXISTS claims;
DROP TABLE IF EXISTS therapist_listings;
DROP TABLE IF EXISTS patients;
DROP TABLE IF EXISTS therapists;

-- Who the therapist is (from credentialing)
CREATE TABLE therapists (
  therapist_id        TEXT PRIMARY KEY,            -- NPI, 10 digits
  full_name           TEXT NOT NULL,
  credential          TEXT NOT NULL
                      CHECK (credential IN ('LCSW','LPC','LMFT','PsyD','PhD','MD')),
  license_number      TEXT NOT NULL,
  license_state       TEXT NOT NULL,
  license_status      TEXT NOT NULL
                      CHECK (license_status IN ('active','lapsed','revoked')),
  license_expires_on  DATE,
  sanctioned          BOOLEAN NOT NULL DEFAULT FALSE,
  languages           TEXT[] NOT NULL,             -- e.g. {English,Spanish}
  offers_telehealth   BOOLEAN NOT NULL,
  contract_start      DATE NOT NULL,               -- joined the plan's network
  contract_end        DATE,                        -- null = still in network
  CHECK (therapist_id ~ '^[0-9]{10}$')
);

-- Where the therapist is listed (the thing we verify)
CREATE TABLE therapist_listings (
  listing_id          TEXT PRIMARY KEY,
  therapist_id        TEXT NOT NULL REFERENCES therapists,
  address             TEXT NOT NULL,
  city                TEXT NOT NULL,
  zip                 TEXT NOT NULL,
  lat                 NUMERIC(9,6) NOT NULL,
  lon                 NUMERIC(9,6) NOT NULL,
  phone               TEXT NOT NULL,
  accepting_new       BOOLEAN NOT NULL,            -- self-reported, often stale
  last_verified_on    DATE,
  last_verified_how   TEXT
                      CHECK (last_verified_how IN ('attestation','call','claims')),
  is_suppressed       BOOLEAN NOT NULL DEFAULT FALSE,
  -- prototype only: hidden answer key for training and scoring
  true_status         TEXT NOT NULL
                      CHECK (true_status IN ('active_open','full_caseload','moved','retired','wrong_phone'))
);
CREATE INDEX ON therapist_listings (therapist_id);
CREATE INDEX ON therapist_listings (zip);

-- Plan members (patients), no names
CREATE TABLE patients (
  patient_id          TEXT PRIMARY KEY,            -- hashed member ID
  age_band            TEXT NOT NULL
                      CHECK (age_band IN ('18-25','26-40','41-64','65+')),
  home_zip            TEXT NOT NULL,
  home_lat            NUMERIC(9,6) NOT NULL,       -- zip centroid, not exact address
  home_lon            NUMERIC(9,6) NOT NULL,
  enrolled_on         DATE NOT NULL,
  enrolled_until      DATE                         -- null = still enrolled
);
CREATE INDEX ON patients (home_zip);

-- What actually happened: paid claims (arrive weeks after the visit)
CREATE TABLE claims (
  claim_id            TEXT PRIMARY KEY,
  therapist_id        TEXT NOT NULL REFERENCES therapists,
  listing_id          TEXT REFERENCES therapist_listings,  -- office of the visit; null = an office not in the directory
  patient_id          TEXT NOT NULL REFERENCES patients,
  service_date        DATE NOT NULL,
  received_date       DATE NOT NULL,               -- lags the visit by weeks
  procedure_code      TEXT NOT NULL
                      CHECK (procedure_code IN ('90791','90792','90834','90837','90847','99214')),
                      -- 90791/90792 = new-patient intake; the rest are ongoing visits
  paid_amount         NUMERIC(8,2) NOT NULL,
  CHECK (received_date >= service_date)
);
CREATE INDEX ON claims (listing_id, service_date);
CREATE INDEX ON claims (therapist_id, service_date);
CREATE INDEX ON claims (patient_id);

-- Members looking at a listing in the online directory (last 90 days)
CREATE TABLE directory_views (
  view_id             TEXT PRIMARY KEY,
  listing_id          TEXT NOT NULL REFERENCES therapist_listings,
  patient_id          TEXT NOT NULL REFERENCES patients,
  viewed_at           TIMESTAMP NOT NULL,
  clicked_phone       BOOLEAN NOT NULL
);
CREATE INDEX ON directory_views (listing_id, viewed_at);

-- Members telling us a listing let them down
CREATE TABLE complaints (
  complaint_id        TEXT PRIMARY KEY,
  listing_id          TEXT NOT NULL REFERENCES therapist_listings,
  patient_id          TEXT NOT NULL REFERENCES patients,
  received_on         DATE NOT NULL,
  category            TEXT NOT NULL
                      CHECK (category IN ('couldnt_book','wrong_number','not_in_network'))
);
CREATE INDEX ON complaints (listing_id);

-- Verification calls placed by the voice agent (written live by agent/app.py)
CREATE TABLE calls (
  call_id             TEXT PRIMARY KEY,            -- ACS call connection id
  listing_id          TEXT NOT NULL REFERENCES therapist_listings,
  status              TEXT NOT NULL
                      CHECK (status IN ('ringing','in_progress','ended','failed')),
  started_at          TIMESTAMPTZ NOT NULL,
  ended_at            TIMESTAMPTZ,
  end_reason          TEXT,                        -- completed | wrong_office | stop_requested | hostile_or_confused | declined_or_hung_up | hung_up_early
  failure             TEXT,                        -- why the call never connected
  -- the six scripted answers, raw as the agent recorded them (null = not asked/answered)
  identity_confirmed  TEXT,
  practicing_here     TEXT,
  accepts_plan        TEXT,
  accepting_new       TEXT,
  next_new_appt       TEXT,                        -- YYYY-MM-DD or 'none'
  phone_correct       TEXT,                        -- 'yes' or the new number, read back and confirmed
  extra_answers       JSONB,                       -- follow-ups: why not accepting, existing patients, ...
  brief               JSONB                        -- claims evidence the agent could cite on the call
);
CREATE INDEX ON calls (listing_id, started_at);

-- What was said on each call, in order
CREATE TABLE call_turns (
  call_id             TEXT NOT NULL REFERENCES calls ON DELETE CASCADE,
  turn_no             INT NOT NULL,
  spoken_at           TIMESTAMPTZ,
  speaker             TEXT NOT NULL CHECK (speaker IN ('agent','clinic')),
  text                TEXT,                        -- null = clinic audio not transcribed yet
  PRIMARY KEY (call_id, turn_no)
);

-- One pass of the verification agent over the directory
CREATE TABLE verification_runs (
  run_id              SERIAL PRIMARY KEY,
  started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at         TIMESTAMPTZ,
  status              TEXT NOT NULL CHECK (status IN ('running','done','stopped','failed')),
  listing_offset      INT NOT NULL DEFAULT 0,      -- testing: skip this many listings (by listing_id)
  listing_limit       INT,                         -- testing: then check only N listings; null = all
  note                TEXT,                        -- why it stopped early, if it did
  summary             TEXT                         -- report summary written by the LLM
);

-- Claims triage (rules + triage agent) and $ exposure for every listing in a run
CREATE TABLE listing_triage (
  run_id              INT NOT NULL REFERENCES verification_runs ON DELETE CASCADE,
  listing_id          TEXT NOT NULL REFERENCES therapist_listings,
  -- evidence
  last_claim_on       DATE,                        -- last visit billed at this office
  last_intake_on      DATE,                        -- last new-patient intake (90791/90792) at this office
  claims_90d          INT NOT NULL,
  intakes_365d        INT NOT NULL,
  unlisted_claims_180d INT NOT NULL,               -- therapist billing from an office not in the directory
  complaints_90d      INT NOT NULL,
  phone_clicks_90d    INT NOT NULL,
  members_nearby      INT NOT NULL,                -- enrolled members within RADIUS_KM
  -- verdict from claims alone: hard_fail by rule, the rest by the triage agent within the plan rules
  claims_verdict      TEXT NOT NULL
                      CHECK (claims_verdict IN ('hard_fail','accepting','not_accepting','needs_call','provisional')),
  claims_reason       TEXT NOT NULL,
  agent_confidence    NUMERIC(3,2),
  agent_trace         JSONB,                       -- tools the triage agent used
  -- exposure = P(wrong) x members likely to try per month x cost per failed attempt
  p_wrong             NUMERIC(4,3) NOT NULL,
  demand_per_month    NUMERIC(7,2) NOT NULL,
  cost_per_failure    NUMERIC(8,2) NOT NULL,
  exposure_per_month  NUMERIC(10,2) NOT NULL,
  call_rank           INT,                         -- call order for needs_call listings; null = no call needed
  PRIMARY KEY (run_id, listing_id)
);

-- The agent's decision per listing, from claims alone or after a call
CREATE TABLE evaluations (
  eval_id             SERIAL PRIMARY KEY,
  run_id              INT NOT NULL REFERENCES verification_runs ON DELETE CASCADE,
  listing_id          TEXT NOT NULL REFERENCES therapist_listings,
  call_id             TEXT REFERENCES calls,       -- null = decided from claims
  source              TEXT NOT NULL CHECK (source IN ('claims','call','recheck')),
  decision            TEXT                         -- agent's proposal; null = no proposal (e.g. call failed)
                      CHECK (decision IN ('verified_accepting','provisionally_accepting','set_not_accepting','suppress_listing')),
  confidence          NUMERIC(3,2),
  front_desk          TEXT                         -- were the call answers legit? (calls only)
                      CHECK (front_desk IN ('good','plausible','not_good','unsure')),
  needs_review        BOOLEAN NOT NULL,            -- human in the loop decides
  conflicts           TEXT[] NOT NULL DEFAULT '{}',-- where the call disagrees with claims/complaints
  rationale           TEXT NOT NULL,
  old_phone           TEXT,                        -- listing before this decision
  old_address         TEXT,
  new_phone           TEXT,                        -- corrections heard on the call
  new_address         TEXT,
  agent_trace         JSONB,                       -- tools the evaluator agent used
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_decision   TEXT
                      CHECK (reviewed_decision IN ('verified_accepting','provisionally_accepting','set_not_accepting','suppress_listing','no_change')),
  reviewer_note       TEXT,
  reviewed_at         TIMESTAMPTZ,
  applied_at          TIMESTAMPTZ,                 -- when the directory listing was updated
  -- provisionally accepting: the office's story contradicts past claims but is credible.
  -- Claims must show a new-patient intake by recheck_on, or the listing is set to not accepting.
  recheck_on          DATE,
  recheck_result      TEXT CHECK (recheck_result IN ('kept','broken')),
  rechecked_at        TIMESTAMPTZ
);
CREATE INDEX ON evaluations (run_id, listing_id);
