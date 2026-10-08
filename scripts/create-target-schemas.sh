#!/bin/bash
# Creates the schemas the sink connectors write into, in the Target Postgres
# database. The sinks auto-create tables but not schemas.
#
#   edm         the EDM store, one table per entity
#   quarantine  the quarantine log, one table per entity
#
# Safe to rerun.
set -e

echo "🗄️  Creating target schemas in targetdb..."
docker exec postgres-target psql -U postgres -d targetdb -v ON_ERROR_STOP=1 -q \
    -c "SET client_min_messages TO warning;" \
    -c "CREATE SCHEMA IF NOT EXISTS edm;" \
    -c "CREATE SCHEMA IF NOT EXISTS quarantine;"
echo "   ✓ edm  quarantine"
