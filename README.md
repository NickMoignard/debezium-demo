# Debezium CDC Demo

A complete **Change Data Capture (CDC)** pipeline demonstrating real-time data replication from PostgreSQL to Kafka using Debezium, with a Python data generator for continuous demo data. 

This is a demo project for educational purposes. Specifically testing various Kafka Connect connectors with Debezium and CDC pipelines. Please feel free to use and modify to test your various scenarios.

## 🏗️ Architecture

```
Python Data Generator → PostgreSQL Source → Debezium → cdc.{au,uk,us}.* → Kafka Streams → edm.*
```

**Components:**
- **PostgreSQL (Source & Target)**: Source database with logical replication enabled. Each region is a schema (`au`, `uk`, `us`, `eu`, `ca`) standing in for a separate regional database
- **Debezium**: CDC connector capturing the `au`, `uk` and `us` schemas
- **Kafka (KRaft mode)**: Event streaming platform
- **Schema Registry**: Avro schema management
- **Kafka Streams** (`streams/`): unions the three regional topics per table into one integrated topic per entity. See [streams/README.md](streams/README.md)
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

### 3. Create Topics and the Debezium CDC Connector

```bash
# Regional cdc.* topics and integrated edm.* topics (safe to rerun)
./scripts/create-integrated-topics.sh

# Create the connector (first time)
./scripts/create-connector.sh

# Or update existing connector
./scripts/update-connector.sh

# Reset demo environment completely
./scripts/reset-demo.sh
```

Entity topics use time-based `delete` retention (7 days), not compaction. Kafka is transport only; the durable copies live in Postgres ([ADR 0001](docs/adr/0001-kafka-is-transport-not-storage.md)). Entity topics created compacted by an older version of the topic script switch over when you rerun it, with no reset. To check one:

```bash
docker exec kafka kafka-configs --bootstrap-server localhost:9092 \
  --describe --entity-type topics --entity-name edm.order
```

Then create the EDM sink, which writes entity topics into the EDM store in `targetdb`:

```bash
# edm and quarantine schemas in targetdb (safe to rerun)
./scripts/create-target-schemas.sh

# Create or update the sink connectors from their JSON configs (safe to rerun)
./scripts/create-sink-connectors.sh
```

The EDM sink (`edm-sink-connector.json`) is a Debezium JDBC sink. It upserts each record on its entity key into `edm.{entity}` and deletes the row on a tombstone. It creates tables from the Schema Registry schema and adds columns when the schema gains a nullable field. Timestamps land as `timestamp` columns in UTC, truncated to milliseconds, and money as `numeric(19,4)`. It carries `edm.product` so far.

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
```

## 📊 Data Flow

1. **Python Data Generator** places 1-2 orders per second per region, with line items, and moves existing orders through `pending → paid → shipped → delivered` (or `cancelled`)
2. **PostgreSQL** commits transactions and writes to WAL (Write-Ahead Log)
3. **Debezium** reads from WAL via logical replication slot and writes one topic per region and table, `cdc.{region}.{table}`, keeping the full change envelope
4. **Schema Registry** stores and versions the Avro schemas
5. **Kafka Streams** merges the regional topics, stamps region and currency, renames and retypes columns, applies DQ expectations and writes `edm.{entity}` and `edm.{entity}.quarantine`, both on 7-day delete retention

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
├── app/                          # Python data generator
│   ├── main.py                   # Faker-based data insertion
│   ├── logger.py                 # Centralized logging utility
│   ├── pyproject.toml            # UV dependencies
│   └── Dockerfile                # UV-based container build
├── connect/                      # Custom Kafka Connect image
│   ├── Dockerfile                # Multi-stage build
│   └── build.gradle              # JDBC driver management
├── scripts/                      # Utility scripts
│   ├── create-connector.sh       # Create Debezium connector
│   ├── update-connector.sh       # Update connector config
│   ├── create-target-schemas.sh  # Create edm and quarantine schemas in targetdb
│   ├── create-sink-connectors.sh # Create or update the sink connectors
│   ├── reset-demo.sh             # Reset entire environment
│   └── add-tables.sh             # Add tables with incremental snapshot
├── .github/
│   └── copilot-instructions.md   # AI coding agent guidance
├── debezium-connector.json       # CDC connector configuration
├── edm-sink-connector.json       # EDM sink connector configuration
├── example-connector-cfg.json    # Databricks sink reference
├── ADDING_TABLES.md              # Guide for incremental snapshots
└── docker-compose.yml            # Full infrastructure definition
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
