#!/bin/bash
# Creates or updates the sink connectors that write entity topics into the
# Target Postgres database. Each config file holds one connector.
#
#   edm-sink-connector.json   EDM sink: edm.{entity} -> edm.{entity}, upsert on
#                             the entity key, a tombstone deletes the row.
#                             topics.regex matches every entity topic but not
#                             edm.{entity}.quarantine, so a new entity needs
#                             no change here.
#
# Transforms in the EDM sink config, in order:
#   key        wraps the plain string entity key in a struct, because the
#              Debezium JDBC sink can't upsert on a primitive key
#   entity     strips the edm. prefix from the topic; collection.name.format
#              puts it back as the schema (a dot in ${topic} becomes _)
#   the rest   one TimestampConverter per timestamp field name, epoch micros
#              to a timestamp. A record without that field, or a tombstone,
#              passes through unchanged.
#
# Create the edm and quarantine schemas first. Safe to rerun: PUT on
# /connectors/{name}/config creates the connector or replaces its config.
set -e

cd "$(dirname "$0")/.."

KAFKA_CONNECT_URL="${KAFKA_CONNECT_URL:-http://localhost:8083}"
SINK_CONFIGS="${SINK_CONFIGS:-edm-sink-connector.json}"

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
    name=$(jq -r '.name' "$config_file")

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
