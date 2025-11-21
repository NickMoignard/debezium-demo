#!/bin/bash
set -e

KAFKA_CONNECT_URL="${KAFKA_CONNECT_URL:-http://localhost:8083}"
CONFIG_FILE="${CONFIG_FILE:-debezium-connector.json}"

echo "🔌 Creating Debezium PostgreSQL Source Connector..."
echo "   Config file: $CONFIG_FILE"
echo "   Kafka Connect: $KAFKA_CONNECT_URL"
echo ""

# Check if config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ Error: Config file '$CONFIG_FILE' not found"
    exit 1
fi

# Wait for Kafka Connect to be ready
echo "⏳ Waiting for Kafka Connect to be ready..."
until curl -s -f -o /dev/null "$KAFKA_CONNECT_URL"; do
    echo "   Kafka Connect not ready yet, retrying in 5s..."
    sleep 5
done
echo "✓ Kafka Connect is ready"
echo ""

# Create the connector
echo "📤 Posting connector configuration..."
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
    -H "Content-Type: application/json" \
    --data @"$CONFIG_FILE" \
    "$KAFKA_CONNECT_URL/connectors")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | sed '$d')

echo ""
if [ "$HTTP_CODE" -eq 201 ] || [ "$HTTP_CODE" -eq 200 ]; then
    echo "✅ Connector created successfully!"
    echo "$BODY" | jq '.'
    echo ""
    echo "🔍 To check connector status:"
    echo "   curl $KAFKA_CONNECT_URL/connectors/$(echo "$BODY" | jq -r '.name')/status | jq '.'"
else
    echo "❌ Failed to create connector (HTTP $HTTP_CODE)"
    echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
    exit 1
fi
