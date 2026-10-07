#!/bin/bash
# Proves the EDM store holds what the regional sources hold. Pauses the data
# generator, waits for the pipeline to drain, then runs four checks:
#
#   reconcile   per entity and region, every live source key has an EDM store
#               row or a quarantine row, and every EDM store row has a live
#               source row. In other words:
#                 source = EDM rows - rows with no source + quarantined keys
#               where a quarantined key has no EDM row. Keys whose EDM row is
#               older than their latest quarantine row (_source_lsn) are shown
#               as "stale": the store keeps the last version that passed.
#   delete      a probe product inserted in one region reaches the EDM store,
#               and deleting it at the source removes the row. Quarantine row
#               counts don't move.
#   restart     the quarantine sink is stopped, its offsets are deleted and it
#               re-reads every quarantine topic from the start. Its tables come
#               out identical (row count, distinct Kafka positions, content).
#   types       timestamp_utc columns and _processed_at are timestamp columns,
#               money columns are numeric(19,4), in the EDM store and the
#               quarantine log.
#
# Entities, source tables and keys come from streams/config/entities/*.yaml,
# regions from streams/config/regions.yaml. Entity keys are recomputed on the
# source side with the Streams formula: sha256 hex of entity:REGION:id.
#
# "Drained" can't mean zero consumer lag: the Streams app writes with
# transactions, and each commit marker takes an offset the sinks never
# consume. So the pipeline counts as drained when the Streams app has no lag
# on its cdc.* inputs, each sink partition lags by at most DRAIN_SLACK
# offsets, and the Kafka offsets plus the target tables' write counters
# hold still across two polls DRAIN_POLL_S apart (longer than the Connect
# offset flush interval).
#
# Exits non-zero on any failure and names the entity and region. The data
# generator is started again on exit, pass or fail.
set -euo pipefail

cd "$(dirname "$0")/.."

KAFKA_CONNECT_URL="${KAFKA_CONNECT_URL:-http://localhost:8083}"
STREAMS_GROUP="${STREAMS_GROUP:-region-integration}"
DRAIN_POLL_S="${DRAIN_POLL_S:-12}"
DRAIN_TIMEOUT_S="${DRAIN_TIMEOUT_S:-300}"
DRAIN_SLACK="${DRAIN_SLACK:-5}"
WAIT_TIMEOUT_S="${WAIT_TIMEOUT_S:-120}"
PROBE_REGION="${PROBE_REGION:-AU}"

SOURCE_CONNECTOR=$(jq -r '.name' debezium-connector.json)
EDM_SINK=$(jq -r '.name' edm-sink-connector.json)
QUARANTINE_SINK=$(jq -r '.name' quarantine-sink-connector.json)
ENTITY_DIR=streams/config/entities
ENTITIES=$(ls "$ENTITY_DIR"/*.yaml | xargs -n1 basename | sed 's/\.yaml$//')
# CODE:schema per region, e.g. AU:au
REGIONS=$(sed -n -E 's/.*code: *([A-Z]+), *schema: *([a-z_]+).*/\1:\2/p' streams/config/regions.yaml)

FAILED=0

src_sql() { docker exec -i postgres-source psql -U postgres -d sourcedb -X -q -At -v ON_ERROR_STOP=1 "$@"; }
tgt_sql() { docker exec -i postgres-target psql -U postgres -d targetdb -X -q -At -v ON_ERROR_STOP=1 "$@"; }

fail() { echo "   ❌ $*"; FAILED=$((FAILED + 1)); }
pass() { echo "   ✓ $*"; }

# Top-level scalar from an entity spec, e.g. spec_value order source_table
spec_value() { sed -n -E "s/^$2: *([A-Za-z0-9_]+).*/\1/p" "$ENTITY_DIR/$1.yaml" | head -1; }

# Output column names of one type in an entity spec (name if renamed, else source)
spec_columns() {
    awk -v t="$2" '$0 ~ "type: *" t "[ ,}]" {
        col = ""
        if (match($0, /source: *[a-z_]+/)) { col = substr($0, RSTART, RLENGTH); sub(/source: */, "", col) }
        if (match($0, /[{ ,]name: *[a-z_]+/)) { col = substr($0, RSTART, RLENGTH); sub(/.*name: */, "", col) }
        print col
    }' "$ENTITY_DIR/$1.yaml"
}

# Quarantine tables that exist. One appears with its entity's first failure.
quarantine_tables() {
    tgt_sql -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'quarantine' ORDER BY 1"
}

connector_state() {
    curl -s "$KAFKA_CONNECT_URL/connectors/$1/status" | jq -r '[.connector.state, (.tasks[]?.state)] | join(",")'
}

# Consumer offsets for the Streams app and both sinks:
# group topic partition current log-end lag
group_offsets() {
    docker exec kafka kafka-consumer-groups --bootstrap-server kafka:29092 --describe --all-groups 2>/dev/null |
        awk -v g="^($STREAMS_GROUP|connect-$EDM_SINK|connect-$QUARANTINE_SINK)\$" \
            '$1 ~ g && $6 ~ /^[0-9]+$/ { print $1, $2, $3, $4, $5, $6 }' | sort
}

# Sum of rows written to the EDM store and quarantine log so far
target_writes() {
    tgt_sql -c "SELECT coalesce(sum(n_tup_ins + n_tup_upd + n_tup_del), 0) FROM pg_stat_user_tables WHERE schemaname IN ('edm', 'quarantine')"
}

wait_for_drain() {
    local waited=0 previous="" current offsets
    echo "⏳ Waiting for the pipeline to drain (polls every ${DRAIN_POLL_S}s)..."
    while [ "$waited" -le "$DRAIN_TIMEOUT_S" ]; do
        offsets=$(group_offsets)
        current="$offsets
writes $(target_writes)"
        if [ -n "$offsets" ] &&
            echo "$offsets" | awk -v s="$DRAIN_SLACK" -v sg="$STREAMS_GROUP" \
                '($1 == sg && $2 ~ /^cdc\./ && $6 > 0) || $6 > s { busy = 1 } END { exit busy }' &&
            [ "$current" = "$previous" ]; then
            pass "drained after ${waited}s"
            return 0
        fi
        previous="$current"
        sleep "$DRAIN_POLL_S"
        waited=$((waited + DRAIN_POLL_S))
    done
    echo "$current"
    fail "pipeline did not drain within ${DRAIN_TIMEOUT_S}s"
    return 1
}

# ---------------------------------------------------------------------------

echo "⏸  Pausing the data generator"
trap 'docker start data-generator > /dev/null && echo "▶  Data generator started"' EXIT
docker stop data-generator > /dev/null

echo "🔌 Connectors"
for c in "$SOURCE_CONNECTOR" "$EDM_SINK" "$QUARANTINE_SINK"; do
    state=$(connector_state "$c")
    if [ "$state" = "RUNNING,RUNNING" ]; then
        pass "$c RUNNING"
    else
        fail "$c is ${state:-missing}"
    fi
done
[ "$FAILED" -eq 0 ] || exit 1

wait_for_drain || exit 1

# --- reconcile -------------------------------------------------------------
echo ""
echo "🔎 Reconcile: source live rows against the EDM store and quarantine log"

QTABLES=$(quarantine_tables)
edm_union=""
q_union=""
for entity in $ENTITIES; do
    edm_union="$edm_union${edm_union:+ UNION ALL }SELECT '$entity', _entity_key, _source_lsn FROM edm.\"$entity\""
    if echo "$QTABLES" | grep -qx "$entity"; then
        q_union="$q_union${q_union:+ UNION ALL }SELECT '$entity', _entity_key, _source_lsn FROM quarantine.\"$entity\""
    fi
done
[ -n "$q_union" ] || q_union="SELECT NULL::text, NULL::text, NULL::bigint WHERE false"

REPORT=$({
    echo "CREATE TEMP TABLE src_key (entity text, region text, key text);"
    echo "COPY src_key FROM STDIN;"
    for entity in $ENTITIES; do
        table=$(spec_value "$entity" source_table)
        id=$(spec_value "$entity" key)
        for region in $REGIONS; do
            code=${region%%:*}
            schema=${region#*:}
            src_sql -c "COPY (SELECT '$entity', '$code', encode(sha256(convert_to('$entity:$code:' || $id, 'UTF8')), 'hex') FROM $schema.$table) TO STDOUT"
        done
    done
    echo '\.'
    cat <<SQL
CREATE TEMP TABLE edm_key AS SELECT * FROM ($edm_union) e(entity, key, lsn);
CREATE TEMP TABLE q_key AS SELECT entity, key, max(lsn) AS lsn FROM ($q_union) q(entity, key, lsn) GROUP BY 1, 2;
CREATE TEMP TABLE per_key AS
SELECT coalesce(s.entity, e.entity) AS entity,
       s.region AS region,
       e.key AS edm_key, s.key IS NOT NULL AS in_source, e.key IS NOT NULL AS in_edm,
       q.key IS NOT NULL AS in_quarantine, coalesce(q.lsn > e.lsn, false) AS stale
FROM src_key s
FULL JOIN edm_key e ON e.entity = s.entity AND e.key = s.key
LEFT JOIN q_key q ON q.entity = coalesce(s.entity, e.entity) AND q.key = coalesce(s.key, e.key);
-- an EDM row with no source row has no region from the source side
UPDATE per_key p SET region = r.jurisdiction_code
FROM (SELECT DISTINCT entity, key, jurisdiction_code FROM (
$(for entity in $ENTITIES; do echo "SELECT '$entity' AS entity, _entity_key AS key, jurisdiction_code FROM edm.\"$entity\" UNION ALL"; done)
SELECT NULL, NULL, NULL WHERE false) x) r
WHERE p.region IS NULL AND r.entity = p.entity AND r.key = p.edm_key;
CREATE TEMP TABLE result AS
SELECT entity, region,
       count(*) FILTER (WHERE in_source) AS source,
       count(*) FILTER (WHERE in_edm) AS edm,
       count(*) FILTER (WHERE in_source AND NOT in_edm AND in_quarantine) AS quarantined,
       count(*) FILTER (WHERE in_edm AND NOT in_source) AS no_source,
       count(*) FILTER (WHERE in_source AND NOT in_edm AND NOT in_quarantine) AS unexplained,
       count(*) FILTER (WHERE in_source AND in_edm AND stale) AS stale
FROM per_key GROUP BY 1, 2;
\pset format aligned
\pset tuples_only off
\pset footer off
SELECT entity, region, source, edm, quarantined, no_source, unexplained, stale,
       CASE WHEN unexplained = 0 AND no_source = 0
             AND source = edm - no_source + quarantined THEN 'ok' ELSE 'MISMATCH' END AS status
FROM result ORDER BY 1, 2;
\pset format unaligned
\pset tuples_only on
SELECT 'MISMATCH|' || entity || '|' || region || '|' || unexplained || '|' || no_source
FROM result WHERE unexplained > 0 OR no_source > 0 ORDER BY 1;
SQL
} | tgt_sql)

echo "$REPORT" | grep -v '^MISMATCH|' | sed 's/^/   /'
echo "   quarantined: no EDM row, has a quarantine row · no_source: EDM row, no live source row"
echo "   unexplained: neither EDM nor quarantine row · stale: EDM row older than its latest quarantine row"
mismatches=$(echo "$REPORT" | grep '^MISMATCH|' || true)
if [ -z "$mismatches" ]; then
    pass "every entity and region reconciles"
else
    while IFS='|' read -r _ entity region unexplained no_source; do
        fail "$entity $region: $unexplained source key(s) in neither the EDM store nor quarantine, $no_source EDM row(s) with no live source row"
    done <<< "$mismatches"
fi

# --- delete ----------------------------------------------------------------
echo ""
echo "🗑  Delete: a source delete removes the EDM store row"

quarantine_rows() {
    local sql="0" t
    for t in $(quarantine_tables); do sql="$sql + (SELECT count(*) FROM quarantine.\"$t\")"; done
    tgt_sql -c "SELECT $sql"
}

# wait_for_rows <sql returning a count> <expected count>
wait_for_rows() {
    local waited=0
    while [ "$(tgt_sql -c "$1")" != "$2" ]; do
        [ "$waited" -ge "$WAIT_TIMEOUT_S" ] && return 1
        sleep 1
        waited=$((waited + 1))
    done
    echo "$waited"
}

probe_schema=$(echo "$REGIONS" | sed -n "s/^$PROBE_REGION://p")
q_before=$(quarantine_rows)
probe_id=$(src_sql -c "INSERT INTO $probe_schema.products (name, category, price, stock_quantity) VALUES ('verify-edm-store probe', 'probe', 1.00, 1) RETURNING id")
probe_key=$(tgt_sql -c "SELECT encode(sha256(convert_to('product:$PROBE_REGION:$probe_id', 'UTF8')), 'hex')")
probe_sql="SELECT count(*) FROM edm.product WHERE _entity_key = '$probe_key'"
if secs=$(wait_for_rows "$probe_sql" 1); then
    pass "product $PROBE_REGION $probe_id reached the EDM store in ${secs}s"
    src_sql -c "DELETE FROM $probe_schema.products WHERE id = $probe_id"
    if secs=$(wait_for_rows "$probe_sql" 0); then
        pass "deleted at the source, gone from the EDM store in ${secs}s"
    else
        fail "product $PROBE_REGION: probe $probe_id still in the EDM store ${WAIT_TIMEOUT_S}s after its source delete"
    fi
else
    fail "product $PROBE_REGION: probe $probe_id never reached the EDM store"
    src_sql -c "DELETE FROM $probe_schema.products WHERE id = $probe_id"
fi
q_after=$(quarantine_rows)
if [ "$q_before" = "$q_after" ]; then
    pass "quarantine rows unchanged ($q_after)"
else
    fail "quarantine rows moved from $q_before to $q_after during the delete check"
fi

# --- restart ---------------------------------------------------------------
echo ""
echo "🔁 Restart: the quarantine sink re-reads everything and records nothing twice"

quarantine_fingerprint() {
    local t
    for t in $(quarantine_tables); do
        tgt_sql -c "SELECT '$t', count(*), count(DISTINCT (__connect_topic, __connect_partition, __connect_offset)),
                           md5(coalesce(string_agg(q::text, '|' ORDER BY __connect_topic, __connect_partition, __connect_offset), ''))
                    FROM quarantine.\"$t\" q"
    done
}

sink_offsets() {
    curl -s "$KAFKA_CONNECT_URL/connectors/$QUARANTINE_SINK/offsets" |
        jq -c '[.offsets[]? | [.partition.kafka_topic, .partition.kafka_partition, .offset.kafka_offset]] | sort'
}

# Rows the quarantine sink has overwritten in place. A re-read upserts every
# row it already wrote, so this grows by at least the row count.
quarantine_upserts() {
    tgt_sql -c "SELECT coalesce(sum(n_tup_upd), 0) FROM pg_stat_user_tables WHERE schemaname = 'quarantine'"
}

curl -s -f -o /dev/null -X PUT "$KAFKA_CONNECT_URL/connectors/$QUARANTINE_SINK/stop"
waited=0
until [ "$(connector_state "$QUARANTINE_SINK")" = "STOPPED" ]; do
    [ "$waited" -ge "$WAIT_TIMEOUT_S" ] && break
    sleep 1
    waited=$((waited + 1))
done
fp_before=$(quarantine_fingerprint)
upserts_before=$(quarantine_upserts)
offsets_before=$(sink_offsets)
curl -s -f -o /dev/null -X DELETE "$KAFKA_CONNECT_URL/connectors/$QUARANTINE_SINK/offsets" ||
    fail "could not delete the $QUARANTINE_SINK offsets"
offsets_reset=$(sink_offsets)
curl -s -f -o /dev/null -X PUT "$KAFKA_CONNECT_URL/connectors/$QUARANTINE_SINK/resume"
echo "   offsets $offsets_before → ${offsets_reset} → resumed"

waited=0
until [ "$(sink_offsets)" = "$offsets_before" ]; do
    if [ "$waited" -ge "$WAIT_TIMEOUT_S" ]; then
        fail "quarantine sink did not re-reach its offsets within ${WAIT_TIMEOUT_S}s"
        break
    fi
    sleep 2
    waited=$((waited + 2))
done
fp_after=$(quarantine_fingerprint)
sleep 1 # table statistics are flushed asynchronously
upserts=$(($(quarantine_upserts) - upserts_before))
rows=$(echo "$fp_after" | awk -F'|' '{ s += $2 } END { print s + 0 }')
echo "$fp_after" | awk -F'|' '{ printf "   %-12s rows %-6s distinct positions %-6s md5 %s\n", $1, $2, $3, $4 }'
if [ "$offsets_reset" != "[]" ]; then
    fail "quarantine sink offsets were not reset ($offsets_reset)"
elif [ "$fp_before" != "$fp_after" ]; then
    fail "quarantine tables changed after re-reading"
    diff <(echo "$fp_before") <(echo "$fp_after") | sed 's/^/      /' || true
elif echo "$fp_after" | awk -F'|' '$2 != $3 { dup = 1 } END { exit !dup }'; then
    fail "a Kafka position appears twice in the quarantine log"
elif [ "$upserts" -lt "$rows" ]; then
    fail "only $upserts of $rows quarantine rows were rewritten, so the re-read didn't happen"
else
    pass "re-read from the start in ${waited}s: $upserts upserts for $rows rows, content unchanged"
fi
if [ "$(connector_state "$QUARANTINE_SINK")" != "RUNNING,RUNNING" ]; then
    fail "$QUARANTINE_SINK is $(connector_state "$QUARANTINE_SINK") after the restart check"
fi

# --- types -----------------------------------------------------------------
echo ""
echo "🧾 Types: timestamps and money"

QTABLES=$(quarantine_tables)
failed_before=$FAILED
for entity in $ENTITIES; do
    schemas="edm"
    echo "$QTABLES" | grep -qx "$entity" && schemas="edm quarantine"
    for schema in $schemas; do
        for col in $(spec_columns "$entity" timestamp_utc) _processed_at; do
            type=$(tgt_sql -c "SELECT data_type FROM information_schema.columns WHERE table_schema = '$schema' AND table_name = '$entity' AND column_name = '$col'")
            case "$type" in
                timestamp*) ;;
                *) fail "$schema.$entity.$col is '${type:-missing}', not a timestamp" ;;
            esac
        done
        for col in $(spec_columns "$entity" money); do
            type=$(tgt_sql -c "SELECT data_type || '(' || numeric_precision || ',' || numeric_scale || ')' FROM information_schema.columns WHERE table_schema = '$schema' AND table_name = '$entity' AND column_name = '$col'")
            [ "$type" = "numeric(19,4)" ] || fail "$schema.$entity.$col is '${type:-missing}', not numeric(19,4)"
        done
    done
done
[ "$FAILED" -gt "$failed_before" ] || pass "timestamp columns are timestamps, money is numeric(19,4)"

echo ""
if [ "$FAILED" -eq 0 ]; then
    echo "✅ EDM store verified"
else
    echo "❌ $FAILED check(s) failed"
    exit 1
fi
