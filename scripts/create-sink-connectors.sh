#!/bin/bash
# Creates or updates the sink connectors that write entity topics into the
# Target Postgres database. Each config file holds one connector.
#
#   edm-sink-connector.json   EDM sink: edm.{entity} -> edm.{entity}, upsert on
#                             the entity key, a tombstone deletes the row.
#                             topics.regex matches every entity topic but not
#                             edm.{entity}.quarantine, so a new entity needs
#                             no change here.
#   quarantine-sink-connector.json
#                             quarantine sink: edm.{entity}.quarantine ->
#                             quarantine.{entity}, append-only. The primary key
#                             is the Kafka topic, partition and offset, so a
#                             redelivered change overwrites its own row instead
#                             of adding a second one.
#
# Transforms in the EDM sink config, in order (the quarantine sink uses the
# same ones except key, which it doesn't need):
#   key        wraps the plain string entity key in a struct, because the
#              Debezium JDBC sink can't upsert on a primitive key
#   entity     cuts the topic down to the entity name (and drops .quarantine
#              in the quarantine sink); collection.name.format adds the schema
#              (a dot in ${topic} would become _)
#   the rest   one TimestampConverter per timestamp field name, epoch micros
#              to a timestamp. A record without that field, or a tombstone,
#              passes through unchanged.
#
# Create the edm and quarantine schemas first. Safe to rerun: PUT on
# /connectors/{name}/config creates the connector or replaces its config.
set -e

cd "$(dirname "$0")/.."

KAFKA_CONNECT_URL="${KAFKA_CONNECT_URL:-http://localhost:8083}"
SINK_CONFIGS="${SINK_CONFIGS:-edm-sink-connector.json quarantine-sink-connector.json}"

echo "⏳ Waiting for Kafka Connect to be ready..."
until curl -s -f -o /dev/null "$KAFKA_CONNECT_URL"; do
    echo "   Kafka Connect not ready yet, retrying in 5s..."
    sleep 5
done
echo "✓ Kafka Connect is ready"
echo ""

for config_file in $SINK_CONFIGS; do
    if [ ! -f "$config_file" ]; then
        echo "❌ Error: Config file '$config_file' not found"
        exit 1
    fi
    name=$(jq -r '.name // empty' "$config_file")
    if [ -z "$name" ]; then
        echo "❌ Error: '$config_file' has no .name"
        exit 1
    fi

    echo "📤 $name ($config_file)"
    RESPONSE=$(curl -s -w "\n%{http_code}" -X PUT \
        -H "Content-Type: application/json" \
        --data "$(jq '.config' "$config_file")" \
        "$KAFKA_CONNECT_URL/connectors/$name/config")

    HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
    BODY=$(echo "$RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" -eq 201 ] || [ "$HTTP_CODE" -eq 200 ]; then
        echo "   ✓ $([ "$HTTP_CODE" -eq 201 ] && echo created || echo updated)"
        echo "   Status: curl $KAFKA_CONNECT_URL/connectors/$name/status | jq '.'"
    else
        echo "❌ Failed to create or update $name (HTTP $HTTP_CODE)"
        echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
        exit 1
    fi
done
