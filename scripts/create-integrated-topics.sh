#!/bin/bash
# Pre-creates the topics the region-integration Streams app reads and writes.
#
#   cdc.{region}.{table}   regional Debezium topics, created up front so the
#                          Streams app can start before the connector snapshots
#   edm.{entity}           integrated entity, compacted: the current-state image
#   edm.{entity}.quarantine  DQ rejects, normal delete retention
set -e

cd "$(dirname "$0")/.."

REGIONS="${REGIONS:-au uk us}"
TABLES="${TABLES:-products users orders line_items}"
EDM_PARTITIONS="${EDM_PARTITIONS:-3}"
ENTITIES=$(ls streams/config/entities/*.yaml | xargs -n1 basename | sed 's/\.yaml$//')

create() {
    docker exec kafka kafka-topics --bootstrap-server localhost:9092 \
        --create --if-not-exists --replication-factor 1 "$@" > /dev/null
}

echo "📥 Regional CDC topics"
for region in $REGIONS; do
    for table in $TABLES; do
        create --topic "cdc.$region.$table" --partitions 1
        echo "   ✓ cdc.$region.$table"
    done
done

echo "📤 Integrated topics"
for entity in $ENTITIES; do
    create --topic "edm.$entity" --partitions "$EDM_PARTITIONS" \
        --config cleanup.policy=compact --config min.compaction.lag.ms=60000
    create --topic "edm.$entity.quarantine" --partitions 1
    echo "   ✓ edm.$entity (compacted)  edm.$entity.quarantine"
done
