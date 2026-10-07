#!/bin/bash
# Resets the demo to an empty pipeline: no connectors, no CDC or entity
# topics, no consumer offsets, no Schema Registry subjects and no EDM store or
# quarantine log. The regional source tables and their rows stay, so the
# Debezium connector re-snapshots them when it is created again.
#
# Uses docker stop/start by container name rather than docker compose, so it
# works from any checkout of the repo. The containers must already exist.
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

# 1. Stop everything that writes: the data generator and the Streams app.
# The Streams app stays stopped until the topics exist again (see Next steps).
print_step "Stopping data generator and Streams app..."
docker stop data-generator streams-app > /dev/null 2>&1 || true
print_success "Data generator and Streams app stopped"
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

# 3. Delete sink connectors (EDM sink, quarantine sink)
print_step "Deleting sink connectors..."
SINKS=$(curl -s "http://localhost:8083/connectors?expand=info" 2>/dev/null | jq -r 'to_entries[] | select(.value.info.type == "sink") | .key' 2>/dev/null || true)
if [ -n "$SINKS" ]; then
    while IFS= read -r sink; do
        echo "  Deleting connector: $sink"
        curl -s -X DELETE "http://localhost:8083/connectors/$sink"
    done <<< "$SINKS"
    print_success "Sink connectors deleted"
else
    print_success "No sink connectors to delete"
fi
echo ""

# 4. Drop the EDM store and quarantine schemas in the Target Postgres database
print_step "Dropping target schemas..."
docker exec postgres-target psql -U postgres -d targetdb -c "DROP SCHEMA IF EXISTS edm CASCADE;" 2>/dev/null || true
docker exec postgres-target psql -U postgres -d targetdb -c "DROP SCHEMA IF EXISTS quarantine CASCADE;" 2>/dev/null || true
print_success "Schemas edm and quarantine dropped"
echo ""

# 5. Drop PostgreSQL tables
print_step "Dropping PostgreSQL tables..."
docker exec postgres-source psql -U postgres -d sourcedb -c "DROP TABLE IF EXISTS products CASCADE;" 2>/dev/null || true
docker exec postgres-source psql -U postgres -d sourcedb -c "DROP TABLE IF EXISTS sales CASCADE;" 2>/dev/null || true
print_success "Tables dropped"
echo ""

# 6. Drop replication slots
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

# 7. Drop publications
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

# 8. Delete Kafka topics: regional CDC topics, entity and quarantine topics,
# and any Streams internal topics. Records written before the reset carry
# schema IDs that the Schema Registry wipe below deletes, so no consumer may
# meet them afterwards.
print_step "Deleting Kafka topics..."
TOPICS=$(docker exec kafka kafka-topics --bootstrap-server localhost:9092 --list 2>/dev/null | grep -E '^cdc\.|^edm\.|^region-integration-|^dbserver1\.|^docker-connect|^debezium-signal$' || true)
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

# 9. Delete Schema Registry schemas
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

# 10. Reset Kafka Connect internal topics, Schema Registry data and consumer
# groups. With Kafka Connect and the Streams app both stopped, the sink groups
# (connect-*) and the Streams group (region-integration) have no active
# members, so they can be deleted. A recreated sink or Streams app then starts
# from the beginning of the recreated topics instead of from stale offsets.
print_step "Resetting Kafka Connect, Schema Registry and consumer groups..."
echo "  Stopping Kafka Connect and Schema Registry..."
docker stop kafka-connect schema-registry > /dev/null
sleep 2

# Delete Kafka Connect internal topics
docker exec kafka kafka-topics --bootstrap-server localhost:9092 --delete --topic docker-connect-offsets 2>/dev/null || true
docker exec kafka kafka-topics --bootstrap-server localhost:9092 --delete --topic docker-connect-configs 2>/dev/null || true
docker exec kafka kafka-topics --bootstrap-server localhost:9092 --delete --topic docker-connect-status 2>/dev/null || true

# Delete Schema Registry topic (stores all schema data)
docker exec kafka kafka-topics --bootstrap-server localhost:9092 --delete --topic _schemas 2>/dev/null || true

# Delete sink and Streams consumer groups. A member can take a few seconds to
# time out after its process stops, so retry until none are left.
pipeline_groups() {
    docker exec kafka kafka-consumer-groups --bootstrap-server localhost:9092 --list 2>/dev/null | grep -E '^connect-|^region-integration$' || true
}
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    CONSUMER_GROUPS=$(pipeline_groups)
    [ -z "$CONSUMER_GROUPS" ] && break
    while IFS= read -r group; do
        echo "  Deleting consumer group: $group"
        docker exec kafka kafka-consumer-groups --bootstrap-server localhost:9092 --delete --group "$group" > /dev/null 2>&1 || true
    done <<< "$CONSUMER_GROUPS"
    sleep 3
done
CONSUMER_GROUPS=$(pipeline_groups)
if [ -n "$CONSUMER_GROUPS" ]; then
    print_error "Consumer groups still present: $(echo $CONSUMER_GROUPS)"
    exit 1
fi

# Wait for topics to be deleted
echo "  Waiting for topics to be deleted..."
sleep 5

print_success "Kafka Connect, Schema Registry and consumer groups reset"
echo ""

# 11. Restart Schema Registry and Kafka Connect to recreate internal topics
print_step "Restarting Schema Registry and Kafka Connect..."
docker start schema-registry > /dev/null
echo "  Waiting for Schema Registry to initialize (10s)..."
sleep 10

docker start kafka-connect > /dev/null
echo "  Waiting for Kafka Connect to initialize (30s)..."
sleep 30

print_success "Services restarted with clean state"
echo ""

# 12. Create debezium-signal topic
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

# 13. Restart data generator
print_step "Starting data generator..."
docker start data-generator > /dev/null
print_success "Data generator started"
echo ""

echo -e "${GREEN}✅ Reset complete!${NC}"
echo ""
echo "Next steps (the same as README step 3):"
echo "  1. Verify Kafka Connect is ready: curl http://localhost:8083/ | jq '.'"
echo "  2. ./scripts/create-integrated-topics.sh"
echo "  3. docker start streams-app"
echo "  4. ./scripts/create-connector.sh"
echo "  5. ./scripts/create-target-schemas.sh"
echo "  6. ./scripts/create-sink-connectors.sh"
echo "  7. After a few minutes of generator traffic: ./scripts/verify-edm-store.sh"
