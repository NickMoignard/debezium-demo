#!/bin/bash
set -e

KAFKA_CONNECT_URL="${KAFKA_CONNECT_URL:-http://localhost:8083}"
CONFIG_FILE="${CONFIG_FILE:-debezium-connector.json}"

echo "🔄 Updating Debezium PostgreSQL Source Connector..."
echo "   Config file: $CONFIG_FILE"
echo "   Kafka Connect: $KAFKA_CONNECT_URL"
echo ""

# Check if config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ Error: Config file '$CONFIG_FILE' not found"
    exit 1
fi

# Extract connector name from config file
CONNECTOR_NAME=$(jq -r '.name' "$CONFIG_FILE")

if [ -z "$CONNECTOR_NAME" ] || [ "$CONNECTOR_NAME" = "null" ]; then
    echo "❌ Error: Could not extract connector name from config file"
    exit 1
fi

echo "📝 Connector name: $CONNECTOR_NAME"
echo ""

# Check if connector exists
echo "🔍 Checking if connector exists..."
if ! curl -s -f "$KAFKA_CONNECT_URL/connectors/$CONNECTOR_NAME" > /dev/null 2>&1; then
    echo "❌ Error: Connector '$CONNECTOR_NAME' does not exist"
    echo "   Use create-connector.sh to create it first"
    exit 1
fi
echo "✓ Connector exists"
echo ""

# Extract just the config section
CONFIG_ONLY=$(jq '.config' "$CONFIG_FILE")

# Update the connector
echo "📤 Updating connector configuration..."
RESPONSE=$(curl -s -w "\n%{http_code}" -X PUT \
    -H "Content-Type: application/json" \
    --data "$CONFIG_ONLY" \
    "$KAFKA_CONNECT_URL/connectors/$CONNECTOR_NAME/config")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | sed '$d')

echo ""
if [ "$HTTP_CODE" -eq 200 ] || [ "$HTTP_CODE" -eq 201 ]; then
    echo "✅ Connector updated successfully!"
    echo "$BODY" | jq '.'
    echo ""
    echo "🔍 To check connector status:"
    echo "   curl $KAFKA_CONNECT_URL/connectors/$CONNECTOR_NAME/status | jq '.'"
else
    echo "❌ Failed to update connector (HTTP $HTTP_CODE)"
    echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
    exit 1
fi
