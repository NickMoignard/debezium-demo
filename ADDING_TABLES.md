# Adding New Tables to Debezium CDC

## The Problem
When you add new tables to `table.include.list` and restart the connector:
- Debezium won't snapshot the new tables (snapshot already completed globally)
- CDC starts immediately but misses historical data
- Offsets are tracked globally, not per-table

## Solution: Incremental Snapshots (Recommended)

Debezium supports **ad-hoc incremental snapshots** via a Kafka signal topic that lets you snapshot new tables without stopping CDC for existing tables.

### Setup (One-time)

**Update connector config** to enable Kafka-based signaling:
```json
"signal.enabled.channels": "source,kafka",
"signal.kafka.topic": "debezium-signal",
"signal.kafka.bootstrap.servers": "kafka:29092",
"signal.kafka.groupId": "debezium-signal-group"
```

Update the connector:
```bash
./update-connector.sh
```

### Adding New Tables

**Option 1: Use the helper script**:
```bash
./add-tables.sh "public.orders,public.customers"
```

**Option 2: Manual steps**:

1. Update `table.include.list` in connector config:
```bash
./update-connector.sh
# Or manually update via REST API
```

2. Trigger incremental snapshot via Kafka signal topic:
```bash
echo '{"id": "snapshot-123", "type": "execute-snapshot", "data": {"data-collections": ["public.orders", "public.customers"], "type": "incremental"}}' | \
  docker exec -i kafka kafka-console-producer \
    --bootstrap-server localhost:9092 \
    --topic debezium-signal
```

Or with `kaf` (if installed):
```bash
echo '{"id": "snapshot-123", "type": "execute-snapshot", "data": {"data-collections": ["public.orders"], "type": "incremental"}}' | \
  kaf produce debezium-signal
```

## How It Works

1. **Signal sent to Kafka topic** `debezium-signal` (no database table needed)
2. **Incremental snapshot runs in background** - no connector restart needed
3. **Existing CDC continues uninterrupted** for products/sales
4. **New tables are snapshotted** chunk-by-chunk (configurable)
5. **After snapshot completes**, CDC begins for new tables

## Monitoring Incremental Snapshot

Check connector logs:
```bash
docker compose logs -f kafka-connect | grep -i snapshot
```

Check signal topic for processed messages:
```bash
kaf consume debezium-signal --tail 10
```

## Alternative: Reset Offsets (Destructive)

If you want to re-snapshot everything:

1. Delete the connector
2. Clear offsets:
```bash
# Delete connector
curl -X DELETE http://localhost:8083/connectors/debezium-postgres-source

# Clear offsets (Kafka Connect must be stopped)
docker compose stop kafka-connect
docker exec kafka kafka-topics --bootstrap-server localhost:9092 --delete --topic docker-connect-offsets
docker compose up -d kafka-connect
```

3. Recreate connector with all tables

**Downside**: You lose all offset tracking and may get duplicates.

## References
- [Debezium Ad-hoc Snapshots](https://debezium.io/documentation/reference/stable/connectors/postgresql.html#postgresql-ad-hoc-snapshots)
- [Incremental Snapshots](https://debezium.io/documentation/reference/stable/connectors/postgresql.html#postgresql-incremental-snapshots)
