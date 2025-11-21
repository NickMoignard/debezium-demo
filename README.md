# Debezium CDC Demo

A complete **Change Data Capture (CDC)** pipeline demonstrating real-time data replication from PostgreSQL to Kafka using Debezium, with a Python data generator for continuous demo data. 

This is a demo project for educational purposes. Specifically testing various Kafka Connect connectors with Debezium and CDC pipelines. Please feel free to use and modify to test your various scenarios.

## 🏗️ Architecture

```
Python Data Generator → PostgreSQL Source → Debezium → Kafka → Schema Registry → Sinks
```

**Components:**
- **PostgreSQL (Source & Target)**: Source database with logical replication enabled
- **Debezium**: CDC connector capturing database changes
- **Kafka (KRaft mode)**: Event streaming platform
- **Schema Registry**: Avro schema management
- **MinIO**: S3-compatible object storage for staging
- **Python Data Generator**: Continuously generates fake products and sales data

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

### 3. Create Debezium CDC Connector

```bash
# Create the connector (first time)
./scripts/create-connector.sh

# Or update existing connector
./scripts/update-connector.sh

# Reset demo environment completely
./scripts/reset-demo.sh
```

### 4. Verify CDC Pipeline

```bash
# Check connector status
curl http://localhost:8083/connectors/debezium-postgres-source/status | jq '.'

# List Kafka topics (should see cdc.public.products and cdc.public.sales)
docker exec kafka kafka-topics --list --bootstrap-server localhost:9092

# Consume CDC events from products table
docker exec kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic cdc.public.products \
  --from-beginning

# Consume CDC events from sales table
docker exec kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic cdc.public.sales \
  --from-beginning
```

## 📊 Data Flow

1. **Python Data Generator** inserts 1-2 records/second into `products` and `sales` tables
2. **PostgreSQL** commits transactions and writes to WAL (Write-Ahead Log)
3. **Debezium** reads from WAL via logical replication slot
4. **Kafka** receives CDC events as Avro-encoded messages
5. **Schema Registry** stores and versions the Avro schemas
6. **Consumers** can subscribe to topics for downstream processing

## 🗄️ Database Schema

### Products Table
```sql
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(100),
    price NUMERIC(10, 2),
    stock_quantity INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Sales Table
```sql
CREATE TABLE sales (
    id SERIAL PRIMARY KEY,
    product_id INTEGER,
    customer_name VARCHAR(255),
    customer_email VARCHAR(255),
    quantity INTEGER,
    total_amount NUMERIC(10, 2),
    sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## 🔧 Configuration

### Debezium Connector Configuration

Edit `debezium-connector.json` to customize:
- **Tables to capture**: `table.include.list`
- **Topic prefix**: `topic.prefix`
- **Transformations**: `transforms.*` settings
- **Serialization**: Key/value converter settings

Key configuration highlights:
```json
{
  "table.include.list": "public.products,public.sales",
  "plugin.name": "pgoutput",
  "transforms": "unwrap",
  "transforms.unwrap.type": "io.debezium.transforms.ExtractNewRecordState"
}
```

### Environment Variables

**Data Generator** (`app/`):
- `DB_HOST`: PostgreSQL hostname (default: `postgres-source`)
- `DB_PORT`: PostgreSQL port (default: `5432`)
- `DB_NAME`: Database name (default: `sourcedb`)
- `DB_USER`: Database user (default: `postgres`)
- `DB_PASSWORD`: Database password (default: `postgres`)

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
curl http://localhost:8081/subjects/cdc.public.products-value/versions/latest | jq '.'
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
│   ├── reset-demo.sh             # Reset entire environment
│   └── add-tables.sh             # Add tables with incremental snapshot
├── .github/
│   └── copilot-instructions.md   # AI coding agent guidance
├── debezium-connector.json       # CDC connector configuration
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
