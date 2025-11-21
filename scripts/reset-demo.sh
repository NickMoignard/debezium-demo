#!/bin/bash
set -e

echo "🧹 Resetting Debezium CDC Demo Environment..."
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print step
print_step() {
    echo -e "${YELLOW}▶ $1${NC}"
}

# Function to print success
print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

# Function to print error
print_error() {
    echo -e "${RED}✗ $1${NC}"
}

# 1. Stop data generator
print_step "Stopping data generator..."
docker compose stop data-generator 2>/dev/null || true
print_success "Data generator stopped"
echo ""

# 2. Delete Debezium connector
print_step "Deleting Debezium connector..."
CONNECTOR_NAME="debezium-postgres-source"
if curl -s -f -o /dev/null "http://localhost:8083/connectors/$CONNECTOR_NAME"; then
    curl -s -X DELETE "http://localhost:8083/connectors/$CONNECTOR_NAME"
    print_success "Connector deleted"
else
    print_success "Connector does not exist (skipping)"
fi
echo ""

# 3. Drop PostgreSQL tables
print_step "Dropping PostgreSQL tables..."
docker exec postgres-source psql -U postgres -d sourcedb -c "DROP TABLE IF EXISTS products CASCADE;" 2>/dev/null || true
docker exec postgres-source psql -U postgres -d sourcedb -c "DROP TABLE IF EXISTS sales CASCADE;" 2>/dev/null || true
print_success "Tables dropped"
echo ""

# 4. Drop replication slots
print_step "Dropping replication slots..."
SLOTS=$(docker exec postgres-source psql -U postgres -d sourcedb -t -c "SELECT slot_name FROM pg_replication_slots;" 2>/dev/null | grep -v '^$' || true)
if [ -n "$SLOTS" ]; then
    while IFS= read -r slot; do
        slot=$(echo "$slot" | xargs) # trim whitespace
        if [ -n "$slot" ]; then
            echo "  Dropping slot: $slot"
            docker exec postgres-source psql -U postgres -d sourcedb -c "SELECT pg_drop_replication_slot('$slot');" 2>/dev/null || true
        fi
    done <<< "$SLOTS"
    print_success "Replication slots dropped"
else
    print_success "No replication slots to drop"
fi
echo ""

# 5. Drop publications
print_step "Dropping publications..."
PUBS=$(docker exec postgres-source psql -U postgres -d sourcedb -t -c "SELECT pubname FROM pg_publication;" 2>/dev/null | grep -v '^$' || true)
if [ -n "$PUBS" ]; then
    while IFS= read -r pub; do
        pub=$(echo "$pub" | xargs) # trim whitespace
        if [ -n "$pub" ]; then
            echo "  Dropping publication: $pub"
            docker exec postgres-source psql -U postgres -d sourcedb -c "DROP PUBLICATION IF EXISTS $pub;" 2>/dev/null || true
        fi
    done <<< "$PUBS"
    print_success "Publications dropped"
else
    print_success "No publications to drop"
fi
echo ""

# 6. Delete Kafka topics
print_step "Deleting Kafka topics..."
TOPICS=$(docker exec kafka kafka-topics --bootstrap-server localhost:9092 --list 2>/dev/null | grep -E '^cdc\.|^dbserver1\.|^docker-connect|^debezium-signal$' || true)
if [ -n "$TOPICS" ]; then
    while IFS= read -r topic; do
        if [ -n "$topic" ]; then
            echo "  Deleting topic: $topic"
            docker exec kafka kafka-topics --bootstrap-server localhost:9092 --delete --topic "$topic" 2>/dev/null || true
        fi
    done <<< "$TOPICS"
    print_success "Kafka topics deleted"
else
    print_success "No matching topics to delete"
fi
echo ""

# 7. Delete Schema Registry schemas
print_step "Deleting Schema Registry schemas..."
SUBJECTS=$(curl -s http://localhost:8081/subjects 2>/dev/null || echo "[]")
if [ "$SUBJECTS" != "[]" ] && [ -n "$SUBJECTS" ]; then
    echo "$SUBJECTS" | jq -r '.[]' 2>/dev/null | while read -r subject; do
        if [ -n "$subject" ]; then
            echo "  Deleting schema: $subject"
            curl -s -X DELETE "http://localhost:8081/subjects/$subject?permanent=true" >/dev/null 2>&1 || true
        fi
    done
    print_success "Schema Registry schemas deleted"
else
    print_success "No schemas to delete"
fi
echo ""

# 8. Reset Kafka Connect internal topics and Schema Registry data
print_step "Resetting Kafka Connect and Schema Registry topics..."
echo "  Stopping Kafka Connect and Schema Registry..."
docker compose stop kafka-connect schema-registry
sleep 2

# Delete Kafka Connect internal topics
docker exec kafka kafka-topics --bootstrap-server localhost:9092 --delete --topic docker-connect-offsets 2>/dev/null || true
docker exec kafka kafka-topics --bootstrap-server localhost:9092 --delete --topic docker-connect-configs 2>/dev/null || true
docker exec kafka kafka-topics --bootstrap-server localhost:9092 --delete --topic docker-connect-status 2>/dev/null || true

# Delete Schema Registry topic (stores all schema data)
docker exec kafka kafka-topics --bootstrap-server localhost:9092 --delete --topic _schemas 2>/dev/null || true

# Wait for topics to be deleted
echo "  Waiting for topics to be deleted..."
sleep 5

print_success "Kafka Connect and Schema Registry topics deleted"
echo ""

# 9. Restart Schema Registry and Kafka Connect to recreate internal topics
print_step "Restarting Schema Registry and Kafka Connect..."
docker compose up -d schema-registry
echo "  Waiting for Schema Registry to initialize (10s)..."
sleep 10

docker compose up -d kafka-connect
echo "  Waiting for Kafka Connect to initialize (30s)..."
sleep 30

print_success "Services restarted with clean state"
echo ""

# 10. Create debezium-signal topic
print_step "Creating debezium-signal topic..."
docker exec kafka kafka-topics \
    --bootstrap-server localhost:9092 \
    --create \
    --topic debezium-signal \
    --partitions 1 \
    --replication-factor 1 \
    --config cleanup.policy=delete \
    --config retention.ms=604800000 2>/dev/null || true
print_success "debezium-signal topic created (1 partition, 7 day retention)"
echo ""

# 11. Restart data generator
print_step "Starting data generator..."
docker compose up -d data-generator
print_success "Data generator started"
echo ""

echo -e "${GREEN}✅ Reset complete!${NC}"
echo ""
echo "Next steps:"
echo "  1. Verify Kafka Connect is ready: curl http://localhost:8083/ | jq '.'"
echo "  2. Run ./create-connector.sh to recreate the Debezium connector"
echo "  3. Verify with: curl http://localhost:8083/connectors/debezium-postgres-source/status | jq '.'"
