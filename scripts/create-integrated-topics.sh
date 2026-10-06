#!/bin/bash
# Pre-creates the topics the region-integration Streams app reads and writes.
#
#   cdc.{region}.{table}   regional Debezium topics, created up front so the
#                          Streams app can start before the connector snapshots
#   edm.{entity}           entity topic, time-based delete retention
#   edm.{entity}.quarantine  changes that failed checks, time-based delete
#                            retention
#
# Kafka is transport only (ADR 0001), so nothing here is compacted. The EDM
# store and quarantine in Postgres hold the durable copies. Safe to rerun:
# retention is reapplied to topics that already exist, which also switches
# entity topics left compacted by an older run over to delete.
set -e

cd "$(dirname "$0")/.."

REGIONS="${REGIONS:-au uk us}"
TABLES="${TABLES:-products users orders line_items}"
EDM_PARTITIONS="${EDM_PARTITIONS:-3}"
RETENTION_MS="${RETENTION_MS:-604800000}"   # 7 days
ENTITIES=$(ls streams/config/entities/*.yaml | xargs -n1 basename | sed 's/\.yaml$//')

create() {
    docker exec kafka kafka-topics --bootstrap-server localhost:9092 \
        --create --if-not-exists --replication-factor 1 "$@" > /dev/null
}

# --if-not-exists leaves an existing topic's config alone, so set retention
# explicitly and drop the compaction lag an older run may have left behind.
retain() {
    docker exec kafka kafka-configs --bootstrap-server localhost:9092 \
        --alter --entity-type topics --entity-name "$1" \
        --add-config "cleanup.policy=delete,retention.ms=$RETENTION_MS" \
        --delete-config min.compaction.lag.ms > /dev/null
}

echo "📥 Regional CDC topics"
for region in $REGIONS; do
    for table in $TABLES; do
        create --topic "cdc.$region.$table" --partitions 1
        echo "   ✓ cdc.$region.$table"
    done
done

echo "📤 Integrated topics (cleanup.policy=delete, retention.ms=$RETENTION_MS)"
for entity in $ENTITIES; do
    create --topic "edm.$entity" --partitions "$EDM_PARTITIONS"
    create --topic "edm.$entity.quarantine" --partitions 1
    retain "edm.$entity"
    retain "edm.$entity.quarantine"
    echo "   ✓ edm.$entity  edm.$entity.quarantine"
done
