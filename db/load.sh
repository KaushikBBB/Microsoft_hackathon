#!/usr/bin/env bash
# Recreate the schema, load data/seed/*.csv into Azure Postgres, then re-import
# the agent's saved calls (calls/transcripts/*.json) since the reset drops them.
# Reads connection settings from .env. Usage: ./db/load.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

psql -v ON_ERROR_STOP=1 -q -f db/schema.sql
for t in therapists therapist_listings patients claims directory_views complaints; do
  psql -v ON_ERROR_STOP=1 -q -c "\copy $t FROM 'data/seed/$t.csv' WITH (FORMAT csv, HEADER true)"
done
(cd agent && uv run --quiet python calls_db.py ../calls/transcripts/*.json)
psql -c "SELECT 'therapists' AS tbl, count(*) FROM therapists
         UNION ALL SELECT 'therapist_listings', count(*) FROM therapist_listings
         UNION ALL SELECT 'patients', count(*) FROM patients
         UNION ALL SELECT 'claims', count(*) FROM claims
         UNION ALL SELECT 'directory_views', count(*) FROM directory_views
         UNION ALL SELECT 'complaints', count(*) FROM complaints
         UNION ALL SELECT 'calls', count(*) FROM calls
         UNION ALL SELECT 'call_turns', count(*) FROM call_turns;"
