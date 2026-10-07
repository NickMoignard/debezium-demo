# Debezium CDC Demo

A complete **Change Data Capture (CDC)** pipeline demonstrating real-time data replication from PostgreSQL to Kafka using Debezium, with a Python data generator for continuous demo data. 

This is a demo project for educational purposes. Specifically testing various Kafka Connect connectors with Debezium and CDC pipelines. Please feel free to use and modify to test your various scenarios.

## 🏗️ Architecture

```
Python Data Generator → PostgreSQL Source → Debezium → cdc.{au,uk,us}.* → Kafka Streams
  → edm.{entity}            → EDM sink        → PostgreSQL Target: edm.{entity}         (EDM store)
  → edm.{entity}.quarantine → quarantine sink → PostgreSQL Target: quarantine.{entity}  (quarantine log)
```

**Components:**
- **PostgreSQL Source**: Source database with logical replication enabled. Each region is a schema (`au`, `uk`, `us`, `eu`, `ca`) standing in for a separate regional database
- **PostgreSQL Target** (`targetdb`): the EDM store (schema `edm`, one current-state row per live entity key) and the quarantine log (schema `quarantine`, every change that failed checks)
- **Debezium**: CDC connector capturing the `au`, `uk` and `us` schemas
- **Kafka (KRaft mode)**: Event streaming platform
- **Schema Registry**: Avro schema management
- **Kafka Streams** (`streams/`): unions the three regional topics per table into one integrated topic per entity. See [streams/README.md](streams/README.md)
- **Debezium JDBC sinks**: the EDM sink and the quarantine sink, which write the Streams output into the Target database
- **MinIO**: S3-compatible object storage for staging
- **Python Data Generator** (`app_multi_region/`): products, users, orders and line items per region, with order status updates, occasional deletes and some deliberately bad rows

## 🚀 Quick Start

### 1. Start the Infrastructure

```bash
# Build custom images
docker compose build

# Start all services
docker compose up -d

# Check service health
docker compose ps
```

### 2. Verify Services

```bash
# Check Kafka Connect is ready
curl http://localhost:8083/

# Check Schema Registry
curl http://localhost:8081/subjects

# Watch data generator logs
docker compose logs -f data-generator
```

### 3. Create Topics, Connectors and Target Schemas

```bash
# Regional cdc.* topics and integrated edm.* topics (safe to rerun)
./scripts/create-integrated-topics.sh

# Start the Streams app once its topics exist (a no-op if it's already running)
docker start streams-app

# Create the connector (first time)
./scripts/create-connector.sh

# Or update existing connector
./scripts/update-connector.sh
```

To start over, run `./scripts/reset-demo.sh`. It stops the data generator and the Streams app, deletes every connector, every `cdc.*` and `edm.*` topic, the sink and Streams consumer groups and every Schema Registry subject, and drops the `edm` and `quarantine` schemas. The regional source rows stay. It then starts Kafka Connect and the generator again, but leaves the Streams app stopped. Rebuild by running this step and the sink step below in order. The reset uses `docker stop`/`docker start` by container name, so it works from any checkout.

Entity topics use time-based `delete` retention (7 days), not compaction. Kafka is transport only; the durable copies live in Postgres ([ADR 0001](docs/adr/0001-kafka-is-transport-not-storage.md)). Entity topics created compacted by an older version of the topic script switch over when you rerun it, with no reset. To check one:

```bash
docker exec kafka kafka-configs --bootstrap-server localhost:9092 \
  --describe --entity-type topics --entity-name edm.order
```

Then create the sinks. The EDM sink writes entity topics into the EDM store in `targetdb`, and the quarantine sink writes quarantine topics into the quarantine log:

```bash
# edm and quarantine schemas in targetdb (safe to rerun)
./scripts/create-target-schemas.sh

# Create or update the sink connectors from their JSON configs (safe to rerun)
./scripts/create-sink-connectors.sh
```

The EDM sink (`edm-sink-connector.json`) is a Debezium JDBC sink. It upserts each record on its entity key into `edm.{entity}` and deletes the row on a tombstone. It creates tables from the Schema Registry schema and adds columns when the schema gains a nullable field.

Timestamps land as `timestamp` columns in UTC, truncated to milliseconds, and money as `numeric(19,4)`. Each timestamp field name has its own `TimestampConverter` in both sink configs (`created_at`, `updated_at`, `placed_at`, `registered_at`, `_processed_at`).

The sink reads every entity topic by pattern and skips the `.quarantine` topics. A new entity whose timestamp fields reuse those names needs no sink config change. A new timestamp field name needs one more converter in each sink config, or it lands as a `bigint` of epoch microseconds.

There are no foreign keys between EDM store tables, so orphans are allowed. `order` is a reserved word in Postgres, so quote it when it stands alone (`"order"`). `edm.order` works as is.

The quarantine sink (`quarantine-sink-connector.json`) is the same kind of connector with the same timestamp handling. It writes `edm.{entity}.quarantine` into `quarantine.{entity}` and never deletes. Each row keeps `_dq_failures` and `_source_row_json`, and its primary key is the Kafka position: `__connect_topic` (the entity name after routing), `__connect_partition` and `__connect_offset`. A redelivered change overwrites its own row, so a connector restart or an offset reset adds nothing. A table appears with its entity's first failure. Customers have only a `warn` expectation, so `quarantine.customer` stays absent until one breaks the contract.

### 4. Verify the Pipeline

```bash
# Check connector status
curl http://localhost:8083/connectors/debezium-postgres-source/status | jq '.'

# List Kafka topics (cdc.au.orders, cdc.uk.orders, ... and edm.order, edm.order.quarantine, ...)
docker exec kafka kafka-topics --list --bootstrap-server localhost:9092

# Raw CDC events from one region
docker exec schema-registry kafka-avro-console-consumer \
  --bootstrap-server kafka:29092 --topic cdc.au.orders --from-beginning \
  --property schema.registry.url=http://localhost:8081 2>/dev/null | grep '^{'

# The same entity integrated across all three regions
docker exec schema-registry kafka-avro-console-consumer \
  --bootstrap-server kafka:29092 --topic edm.order --from-beginning \
  --property schema.registry.url=http://localhost:8081 2>/dev/null | grep '^{'

# EDM sink status, and products in the EDM store per region
curl http://localhost:8083/connectors/edm-sink/status | jq '.'
docker exec postgres-target psql -U postgres -d targetdb \
  -c "SELECT jurisdiction_code, count(*) FROM edm.product GROUP BY 1 ORDER BY 1"

# Quarantine sink status, and order line failures by reason
curl http://localhost:8083/connectors/quarantine-sink/status | jq '.'
docker exec postgres-target psql -U postgres -d targetdb \
  -c "SELECT _dq_failures, count(*) FROM quarantine.order_line GROUP BY 1 ORDER BY 2 DESC"
```

### 5. Prove the EDM Store

```bash
./scripts/verify-edm-store.sh
```

The script pauses the data generator, waits for the pipeline to drain and runs four checks. The generator starts again when the script exits, pass or fail.

- **Reconcile**: per entity and region, every live source row has an EDM store row or a quarantine row, and every EDM store row has a live source row. Entity keys are recomputed from the source with the Streams formula. The table shows `quarantined` (no EDM row because the change failed checks) and `stale` (the EDM row is older than the key's latest quarantine row, so the store keeps the last version that passed).
- **Delete**: a probe product inserted in AU reaches the EDM store, then a source delete removes it. Quarantine counts don't move.
- **Restart**: the quarantine sink is stopped, its offsets are deleted, and it re-reads every quarantine topic from the start. The quarantine tables come out identical.
- **Types**: timestamp columns are timestamps and money columns are `numeric(19,4)`.

It exits non-zero and names the entity and region on any mismatch, for example `❌ order UK: 1 source key(s) in neither the EDM store nor quarantine, 0 EDM row(s) with no live source row`. Consumer lag never reaches zero, because each Streams transaction leaves a commit marker the sinks don't consume. So "drained" means the Streams app has caught up on `cdc.*`, sink lag is at most a few offsets, and offsets and target writes hold still across two polls.

## 🔍 Demo Queries

Run these against a running stack. `order` is quoted because it is a reserved word.

### One entity across regions

The same source id exists in every region. The entity key keeps them apart, and so does every foreign key column:

```bash
# One order id, three regions, three entity keys
docker exec postgres-target psql -U postgres -d targetdb -c "
SELECT jurisdiction_code, order_id, left(_entity_key, 12) AS entity_key,
       left(customer_key, 12) AS customer_key, order_status, placed_at
FROM edm.\"order\" WHERE order_id = 42 ORDER BY 1"

# Orders per region and status
docker exec postgres-target psql -U postgres -d targetdb -c "
SELECT jurisdiction_code, order_status, count(*)
FROM edm.\"order\" GROUP BY 1, 2 ORDER BY 1, 2"
```

### Orphans and the quarantine log

An orphan is a row whose parent isn't in the EDM store. Most orphaned order lines belong to an order that was quarantined. The rest (`order_unaccounted`) belong to an order that is still in flight, or one deleted at the source while its lines were kept:

```bash
# Order lines whose order isn't in the EDM store, and whether that order was quarantined
docker exec postgres-target psql -U postgres -d targetdb -c "
SELECT ol.jurisdiction_code,
       count(*) AS orphan_lines,
       count(*) FILTER (WHERE q.order_key IS NOT NULL) AS order_quarantined,
       count(*) FILTER (WHERE q.order_key IS NULL) AS order_unaccounted
FROM edm.order_line ol
LEFT JOIN edm.\"order\" o ON o._entity_key = ol.order_key
LEFT JOIN (SELECT DISTINCT _entity_key AS order_key FROM quarantine.\"order\") q
       ON q.order_key = ol.order_key
WHERE o._entity_key IS NULL
GROUP BY 1 ORDER BY 1"

# Why those orders were quarantined (latest failure per order)
docker exec postgres-target psql -U postgres -d targetdb -c "
SELECT q._dq_failures, q.order_status, count(*) AS orphan_lines
FROM edm.order_line ol
LEFT JOIN edm.\"order\" o ON o._entity_key = ol.order_key
JOIN (SELECT DISTINCT ON (_entity_key) _entity_key, _dq_failures, order_status
      FROM quarantine.\"order\" ORDER BY _entity_key, _source_lsn DESC) q
  ON q._entity_key = ol.order_key
WHERE o._entity_key IS NULL
GROUP BY 1, 2 ORDER BY 3 DESC"
```

### End-to-end lag

`_source_ts_ms` is when the change committed in the regional database, and `_processed_at` is when the Streams app wrote it. The sink hop isn't stamped on the row, so `newest_row_age` gives the other half: how old the newest row in the store is right now.

```bash
# Source commit to Streams processing for order changes in the last 5 minutes,
# and the age of the newest row in the EDM store
docker exec postgres-target psql -U postgres -d targetdb -c "
SELECT jurisdiction_code, count(*) AS changes,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY lag) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY lag) AS p95,
       max(lag) AS worst,
       now() AT TIME ZONE 'UTC' - max(_processed_at) AS newest_row_age
FROM (SELECT jurisdiction_code, _processed_at,
             _processed_at - to_timestamp(_source_ts_ms / 1000.0) AT TIME ZONE 'UTC' AS lag
      FROM edm.\"order\"
      WHERE _processed_at > now() AT TIME ZONE 'UTC' - interval '5 minutes') recent
GROUP BY 1 ORDER BY 1"
```

### Watch a delete happen

A source delete becomes a tombstone on the entity topic, and the EDM sink removes the row. The store keeps no record of a deleted entity.

```bash
SRC="docker exec postgres-source psql -U postgres -d sourcedb"
EDM="docker exec postgres-target psql -U postgres -d targetdb"

# 1. Pick a delivered AU order that has line items
ID=$($SRC -At -c "SELECT o.id FROM au.orders o JOIN au.line_items li ON li.order_id = o.id
                   WHERE o.order_status = 'delivered' ORDER BY o.id DESC LIMIT 1")

# 2. It's in the EDM store with its lines
$EDM -c "SELECT o.order_id, o.order_status, count(ol._entity_key) AS lines
         FROM edm.\"order\" o LEFT JOIN edm.order_line ol ON ol.order_key = o._entity_key
         WHERE o.jurisdiction_code = 'AU' AND o.order_id = $ID GROUP BY 1, 2"

# 3. Delete it at the source, lines first
$SRC -c "DELETE FROM au.line_items WHERE order_id = $ID" -c "DELETE FROM au.orders WHERE id = $ID"

# 4. A second or two later the order and its lines are gone
sleep 2
$EDM -c "SELECT (SELECT count(*) FROM edm.\"order\" WHERE jurisdiction_code = 'AU' AND order_id = $ID) AS orders,
                (SELECT count(*) FROM edm.order_line WHERE jurisdiction_code = 'AU' AND order_id = $ID) AS lines"
```

## 📊 Data Flow

1. **Python Data Generator** places 1-2 orders per second per region, with line items, and moves existing orders through `pending → paid → shipped → delivered` (or `cancelled`)
2. **PostgreSQL** commits transactions and writes to WAL (Write-Ahead Log)
3. **Debezium** reads from WAL via logical replication slot and writes one topic per region and table, `cdc.{region}.{table}`, keeping the full change envelope
4. **Schema Registry** stores and versions the Avro schemas
5. **Kafka Streams** merges the regional topics, stamps region and currency, renames and retypes columns, applies DQ expectations and writes `edm.{entity}` and `edm.{entity}.quarantine`, both on 7-day delete retention
6. **EDM sink** upserts each entity topic into `edm.{entity}` in the Target database and deletes the row on a tombstone
7. **Quarantine sink** appends each quarantine topic to `quarantine.{entity}`, keyed on Kafka topic, partition and offset

## 🗄️ Database Schema

Every region schema has the same four tables:

```sql
CREATE TABLE {region}.products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(100),
    price NUMERIC(10, 2),
    stock_quantity INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE {region}.users (
    id SERIAL PRIMARY KEY,
    first_name VARCHAR(255) NOT NULL,
    last_name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    phone_number VARCHAR(20),
    address_line_one VARCHAR(255),
    address_line_two VARCHAR(255),
    city VARCHAR(100),
    state VARCHAR(100),
    postal_code VARCHAR(20),
    country VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE {region}.orders (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    order_status VARCHAR(50),
    promo_code VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE {region}.line_items (
    id SERIAL PRIMARY KEY,
    product_id INTEGER,
    order_id INTEGER,
    quantity INTEGER,
    line_item_discount NUMERIC(10, 2),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

All tables use `REPLICA IDENTITY FULL`, so updates and deletes carry the before image.

## 🔧 Configuration

### Debezium Connector Configuration

Edit `debezium-connector.json` to customize:
- **Tables to capture**: `table.include.list`
- **Topic prefix**: `topic.prefix`
- **Serialization**: Key/value converter settings

Key configuration highlights:
```json
{
  "topic.prefix": "cdc",
  "table.include.list": "au\\.(products|users|orders|line_items),uk\\.(...),us\\.(...)",
  "plugin.name": "pgoutput",
  "decimal.handling.mode": "precise"
}
```

There is no `ExtractNewRecordState` unwrap. The regional topics keep the full Debezium envelope, like a Raw layer, and the Streams app does the unwrap.

### Environment Variables

**Data Generator** (`app_multi_region/`):
- `DB_HOST`: PostgreSQL hostname (default: `postgres-source`)
- `DB_PORT`: PostgreSQL port (default: `5432`)
- `DB_NAME`: Database name (default: `sourcedb`)
- `DB_USER`: Database user (default: `postgres`)
- `DB_PASSWORD`: Database password (default: `postgres`)
- `BAD_DATA_RATE`: share of rows that deliberately fail a DQ expectation (default: `0.02`)
- `DELETE_RATE`: chance per region per second of deleting a cancelled order (default: `0.05`)

**Streams app** (`streams/`):
- `BOOTSTRAP_SERVERS`, `SCHEMA_REGISTRY_URL`, `APPLICATION_ID`, `CONFIG_DIR`

**Kafka Network**:
- Host access: `localhost:9092`
- Container access: `kafka:29092` (always use this in container configs!)

## 🛠️ Development

### Building the Custom Kafka Connect Image

The project uses a custom Kafka Connect image with:
- Debezium PostgreSQL connector
- Confluent Avro converter
- Databricks Delta Lake connector
- PostgreSQL JDBC driver

```bash
docker compose build kafka-connect
```

### Running the Data Generator Locally

```bash
cd app/

# Install dependencies with UV
uv sync

# Run locally (requires PostgreSQL running)
uv run python main.py
```

## 📝 Common Operations

### Adding New Tables to CDC (Incremental Snapshot)

When you want to add new tables to an existing CDC pipeline without disrupting existing streams:

**Step 1: Update the connector configuration**

Edit `debezium-connector.json` and add tables to `table.include.list`:
```json
{
  "table.include.list": "public.products,public.sales,public.orders"
}
```

Apply the update:
```bash
./scripts/update-connector.sh
```

**Step 2: Trigger incremental snapshot via Kafka signal**

Send a signal to snapshot only the new table(s):
```bash
echo '{"id": "snapshot-'$(date +%s)'", "type": "execute-snapshot", "data": {"data-collections": ["public.orders"], "type": "incremental"}}' | \
  docker exec -i kafka kafka-console-producer \
    --bootstrap-server localhost:9092 \
    --topic debezium-signal
```

For multiple tables:
```bash
echo '{"id": "snapshot-'$(date +%s)'", "type": "execute-snapshot", "data": {"data-collections": ["public.orders", "public.customers"], "type": "incremental"}}' | \
  docker exec -i kafka kafka-console-producer \
    --bootstrap-server localhost:9092 \
    --topic debezium-signal
```

**Step 3: Monitor the snapshot progress**

```bash
# Watch connector logs
docker compose logs -f kafka-connect | grep -i snapshot

# Check for snapshot messages (op: "r") in the new topic
docker exec kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic cdc.public.orders \
  --from-beginning
```

**How it works:**
- Incremental snapshot runs in the background
- Existing CDC streams continue uninterrupted
- New table is snapshotted chunk-by-chunk
- After snapshot completes, CDC begins for the new table
- No connector restart required!

**Alternative: Use the helper script**
```bash
./scripts/add-tables.sh "public.orders,public.customers"
```

**Note on snapshot.mode:**
- `"snapshot.mode": "initial"` - Only snapshots on first run. Adding tables requires incremental snapshot signals.
- `"snapshot.mode": "when_needed"` - Automatically snapshots newly added tables without manual signals.

### Insert Test Data Manually

```bash
# Insert sample data
docker exec -i postgres-source psql -U postgres -d sourcedb < insert.sql

# Or connect to PostgreSQL directly
docker exec -it postgres-source psql -U postgres -d sourcedb
```

### Check Logical Replication

```bash
# Verify wal_level is set to logical
docker exec postgres-source psql -U postgres -c "SHOW wal_level;"

# Check replication slots
docker exec postgres-source psql -U postgres -c "SELECT * FROM pg_replication_slots;"

# Check publications
docker exec postgres-source psql -U postgres -c "SELECT * FROM pg_publication;"
```

### Manage Connectors

```bash
# List all connectors
curl http://localhost:8083/connectors | jq '.'

# Get connector status
curl http://localhost:8083/connectors/debezium-postgres-source/status | jq '.'

# Delete connector
curl -X DELETE http://localhost:8083/connectors/debezium-postgres-source

# Restart connector
curl -X POST http://localhost:8083/connectors/debezium-postgres-source/restart
```

### View Schemas in Registry

```bash
# List all schemas
curl http://localhost:8081/subjects | jq '.'

# Get specific schema
curl http://localhost:8081/subjects/edm.order-value/versions/latest | jq '.'
```

## 🐛 Troubleshooting

### Connector fails to start

**Issue**: Connector status shows FAILED

**Solutions**:
1. Check PostgreSQL has logical replication enabled:
   ```bash
   docker exec postgres-source psql -U postgres -c "SHOW wal_level;"
   # Should return: logical
   ```

2. Check Kafka Connect logs:
   ```bash
   docker compose logs kafka-connect
   ```

3. Verify tables exist:
   ```bash
   docker exec postgres-source psql -U postgres -d sourcedb -c "\dt"
   ```

### Kafka connection refused

**Issue**: Containers can't connect to Kafka

**Solution**: Use `kafka:29092` (internal listener) in container environment variables, not `localhost:9092`

### Schema Registry errors

**Issue**: Schema Registry can't connect to Kafka

**Solution**: Verify Schema Registry is configured to use `kafka:29092`:
```bash
docker compose logs schema-registry
```

### No data in topics

**Issue**: Topics are empty despite data generator running

**Solutions**:
1. Check if connector is running:
   ```bash
   curl http://localhost:8083/connectors/debezium-postgres-source/status
   ```

2. Verify tables are in include list:
   ```bash
   cat debezium-connector.json | jq '.config."table.include.list"'
   ```

3. Check data generator is inserting data:
   ```bash
   docker compose logs data-generator
   ```

## 📂 Project Structure

```
.
├── app/                            # Python data generator
│   ├── main.py                     # Faker-based data insertion
│   ├── logger.py                   # Centralized logging utility
│   ├── pyproject.toml              # UV dependencies
│   └── Dockerfile                  # UV-based container build
├── connect/                        # Custom Kafka Connect image
│   ├── Dockerfile                  # Multi-stage build
│   └── build.gradle                # JDBC driver management
├── scripts/                        # Utility scripts
│   ├── create-integrated-topics.sh # Create cdc.* and edm.* topics
│   ├── create-connector.sh         # Create Debezium connector
│   ├── update-connector.sh         # Update connector config
│   ├── create-target-schemas.sh    # Create edm and quarantine schemas in targetdb
│   ├── create-sink-connectors.sh   # Create or update the sink connectors
│   ├── verify-edm-store.sh         # Reconcile the EDM store and run the end-to-end checks
│   ├── reset-demo.sh               # Reset entire environment
│   └── add-tables.sh               # Add tables with incremental snapshot
├── .github/
│   └── copilot-instructions.md     # AI coding agent guidance
├── debezium-connector.json         # CDC connector configuration
├── edm-sink-connector.json         # EDM sink connector configuration
├── quarantine-sink-connector.json  # Quarantine sink connector configuration
├── example-connector-cfg.json      # Databricks sink reference
├── ADDING_TABLES.md                # Guide for incremental snapshots
└── docker-compose.yml              # Full infrastructure definition
```

## 🔗 Access URLs

- **Kafka Connect API**: http://localhost:8083
- **Schema Registry**: http://localhost:8081
- **MinIO Console**: http://localhost:9001 (minioadmin/minioadmin)
- **PostgreSQL Source**: localhost:5432 (postgres/postgres)
- **PostgreSQL Target**: localhost:5433 (postgres/postgres)

## 📚 Further Reading

- [Debezium Documentation](https://debezium.io/documentation/)
- [Kafka Connect Documentation](https://docs.confluent.io/platform/current/connect/index.html)
- [PostgreSQL Logical Replication](https://www.postgresql.org/docs/current/logical-replication.html)

## 📄 License

This is a demo project for educational purposes. Specifically testing various Kafka Connect connectors with Debezium and CDC pipelines. Please feel free to use and modify to test your various scenarios.
