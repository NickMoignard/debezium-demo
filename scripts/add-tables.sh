#!/bin/bash
# Script to add new tables to Debezium connector with incremental snapshot
set -e

CONNECTOR_NAME="debezium-postgres-source"
NEW_TABLES="$1"

if [ -z "$NEW_TABLES" ]; then
    echo "Usage: $0 'table1,table2,table3'"
    echo "Example: $0 'public.orders,public.customers'"
    exit 1
fi

echo "🔄 Adding new tables to Debezium connector with incremental snapshot..."
echo "   New tables: $NEW_TABLES"
echo ""

# Step 1: Get current configuration
echo "📥 Fetching current connector configuration..."
CURRENT_CONFIG=$(curl -s http://localhost:8083/connectors/$CONNECTOR_NAME/config)
CURRENT_TABLES=$(echo "$CURRENT_CONFIG" | jq -r '."table.include.list"')

echo "   Current tables: $CURRENT_TABLES"

# Step 2: Merge table lists
ALL_TABLES="$CURRENT_TABLES,$NEW_TABLES"
echo "   Updated tables: $ALL_TABLES"
echo ""

# Step 3: Update connector configuration
echo "📝 Updating connector configuration..."
UPDATED_CONFIG=$(echo "$CURRENT_CONFIG" | jq --arg tables "$ALL_TABLES" '."table.include.list" = $tables')

curl -s -X PUT \
    -H "Content-Type: application/json" \
    --data "$UPDATED_CONFIG" \
    "http://localhost:8083/connectors/$CONNECTOR_NAME/config" | jq '.'

echo ""
echo "✅ Connector configuration updated!"
echo ""

# Step 4: Trigger incremental snapshot via Kafka signal topic
echo "📸 Triggering incremental snapshot for new tables via Kafka signal..."
echo ""

IFS=',' read -ra TABLES_ARRAY <<< "$NEW_TABLES"
DATA_COLLECTIONS=""
for table in "${TABLES_ARRAY[@]}"; do
    table=$(echo "$table" | xargs) # trim whitespace
    if [ -z "$DATA_COLLECTIONS" ]; then
        DATA_COLLECTIONS="\"$table\""
    else
        DATA_COLLECTIONS="$DATA_COLLECTIONS, \"$table\""
    fi
done

SIGNAL_ID="ad-hoc-$(date +%s)"
SIGNAL_MESSAGE="{\"id\": \"$SIGNAL_ID\", \"type\": \"execute-snapshot\", \"data\": {\"data-collections\": [$DATA_COLLECTIONS], \"type\": \"incremental\"}}"

echo "Sending signal to Kafka topic 'debezium-signal'..."
echo "$SIGNAL_MESSAGE" | docker exec -i kafka kafka-console-producer \
    --bootstrap-server localhost:9092 \
    --topic debezium-signal

echo ""
echo "✅ Incremental snapshot signal sent!"
echo "   Signal ID: $SIGNAL_ID"
echo "   Tables: $NEW_TABLES"
echo ""
echo "💡 Monitor progress with:"
echo "   docker compose logs -f kafka-connect | grep -i 'snapshot\|incremental'"
