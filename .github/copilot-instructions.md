# Debezium CDC Demo - AI Coding Agent Instructions

## Project Overview

This is a **production-ready Change Data Capture (CDC) pipeline demo** for educational purposes, demonstrating real-time data replication using Debezium, Kafka, and PostgreSQL. The project includes utilities for testing various CDC scenarios including incremental snapshots and dynamic table additions.

## Architecture Overview

**Data Flow:**
```
Python Data Generator → PostgreSQL Source → Debezium → Kafka → Schema Registry → [Downstream Sinks]
```

**Components:**
- **Python Data Generator** → continuously inserts fake data (products, sales) using Faker library
- **Debezium PostgreSQL Connector** → captures changes via logical replication (pgoutput plugin)
- **Kafka (KRaft mode)** → streams change events (no Zookeeper dependency)
- **Kafka Connect** → orchestrates connectors with custom image
- **Schema Registry** → manages Avro schemas for CDC events
- **MinIO** → S3-compatible staging storage (optional)
- **PostgreSQL Source/Target** → demo databases with logical replication enabled

**Key Features:**
- Full Debezium envelope with transaction metadata
- Incremental snapshot support via Kafka signal topic
- Automatic replica identity configuration
- UV-based Python environment for data generator
- Custom Kafka Connect image with multiple connectors pre-installed

## Critical Setup Commands

### Build and Start Infrastructure
```bash
# Build custom Kafka Connect image with Debezium + connectors
docker compose build kafka-connect

# Build Python data generator
docker compose build data-generator

# Start all services
docker compose up -d

# Check service health
docker compose ps
docker compose logs kafka-connect
docker compose logs data-generator
```

### Deploy Debezium Connector
```bash
# Create the connector (first time)
./scripts/create-connector.sh

# Update existing connector configuration
./scripts/update-connector.sh

# Reset entire demo environment (deletes all data/offsets/schemas)
./scripts/reset-demo.sh
```

### Verify CDC Pipeline
```bash
# Check connector status
curl http://localhost:8083/connectors/debezium-postgres-source/status | jq '.'

# List Kafka topics (should see cdc.public.products, cdc.public.sales)
docker exec kafka kafka-topics --list --bootstrap-server localhost:9092

# Consume CDC events (if kaf CLI is installed)
kaf consume cdc.public.products --tail 10

# Or use kafka-console-consumer
docker exec kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic cdc.public.products \
  --from-beginning
```

## Key Configuration Patterns

### Kafka Network Listeners (Critical!)
Kafka uses **dual listeners** for host/container communication:
- `PLAINTEXT://localhost:9092` → host machine access
- `INTERNAL://kafka:29092` → container-to-container (used by Connect, Schema Registry)

**Always use `kafka:29092` in container environment variables**, not `localhost:9092`.

### Kafka Connect Image Build
Custom image (`custom-kafka-connect:latest`) is built via multi-stage Dockerfile:
1. **Gradle stage** (`connect/Dockerfile` + `build.gradle`) → downloads JDBC drivers
2. **Runtime stage** → extends `quay.io/debezium/connect:3.0`, installs Confluent Hub components

Key plugins installed:
- `confluentinc/kafka-connect-avro-converter`
- `confluentinc/kafka-connect-databricks-delta-lake`
- PostgreSQL JDBC driver (via Gradle)

### Debezium Connector Configuration

See `debezium-connector.json` for current configuration:

**Key Settings:**
- `plugin.name: "pgoutput"` → native PostgreSQL logical replication (no external plugin needed)
- `slot.name: "debezium_slot"` → replication slot name
- `publication.name: "debezium_publication"` → PostgreSQL publication for CDC
- `publication.autocreate.mode: "filtered"` → auto-creates publication with filtered tables
- `table.include.list: "public.products,public.sales"` → tables to capture
- `snapshot.mode: "when_needed"` → snapshots new tables automatically when added
- `provide.transaction.metadata: "true"` → includes transaction ID, LSN in events
- `signal.enabled.channels: "source,kafka"` → enables Kafka-based signaling for incremental snapshots
- `signal.kafka.topic: "debezium-signal"` → Kafka topic for sending snapshot signals

**PostgreSQL Requirements:**
- `wal_level=logical` (configured in docker-compose.yml)
- Tables must have `REPLICA IDENTITY FULL` for before/after values (data generator sets this automatically)

**Snapshot Modes Explained:**
- `"initial"` → Only snapshots on first run (no offsets exist). Adding new tables later requires manual incremental snapshots via signal topic.
- `"when_needed"` → Automatically snapshots tables that haven't been snapshotted yet, including newly added tables. No manual signals needed.
- `"never"` → Never snapshots, only CDC from current LSN.

**Choosing the Right Mode:**
- Use `"initial"` for production when you need explicit control over snapshot timing
- Use `"when_needed"` for development/testing when you want automatic snapshotting of new tables

## Adding New Tables to CDC Pipeline

### Method 1: Automatic Snapshotting (snapshot.mode: "when_needed")

1. Update `debezium-connector.json` to add new table to `table.include.list`
2. Run `./scripts/update-connector.sh`
3. Connector automatically snapshots the new table
4. CDC begins after snapshot completes

### Method 2: Manual Incremental Snapshot (snapshot.mode: "initial")

When using `snapshot.mode: "initial"`, new tables won't be automatically snapshotted. Use incremental snapshots:

**Step 1: Update connector configuration**
```bash
# Edit debezium-connector.json to add tables
# Then apply the update
./scripts/update-connector.sh
```

**Step 2: Trigger incremental snapshot via Kafka signal**
```bash
# Using the helper script (recommended)
./scripts/add-tables.sh "public.orders,public.customers"

# Or manually send signal to Kafka topic
echo '{"id": "snapshot-'$(date +%s)'", "type": "execute-snapshot", "data": {"data-collections": ["public.orders"], "type": "incremental"}}' | \
  docker exec -i kafka kafka-console-producer \
    --bootstrap-server localhost:9092 \
    --topic debezium-signal
```

**Step 3: Monitor snapshot progress**
```bash
# Watch for snapshot activity in connector logs
docker compose logs -f kafka-connect | grep -i snapshot

# Verify snapshot records (op: "r") appear in the new topic
kaf consume cdc.public.orders --tail 50 | grep '"op":"r"'
```

**How Incremental Snapshots Work:**
- Runs in the background without stopping the connector
- Existing CDC streams continue uninterrupted for other tables
- New table is snapshotted chunk-by-chunk (configurable chunk size)
- After snapshot completes, CDC begins for the new table
- No connector restart or offset reset required

**Signal Topic Configuration:**
- Topic: `debezium-signal` (auto-created or manually created with `./scripts/reset-demo.sh`)
- Retention: 7 days (for audit trail)
- Connector subscribes to this topic and processes signals automatically

See `ADDING_TABLES.md` for comprehensive documentation.

## Python Data Generator (`app/`)

**Features:**
- Uses **UV package manager** (modern Python dependency management)
- Generates **1-2 products/second** and **1-2 sales/second**
- Uses **Faker** library for realistic fake data
- Auto-creates tables with `REPLICA IDENTITY FULL` on startup
- Structured logging with timestamps (outputs to Docker logs)

**Tables Created:**
- `products` (id, name, category, price, stock_quantity, created_at)
- `sales` (id, product_id, customer_name, customer_email, quantity, total_amount, sale_date)

**Key Files:**
- `app/main.py` → data generation logic
- `app/logger.py` → centralized logging utility
- `app/pyproject.toml` → UV dependencies
- `app/Dockerfile` → UV-based container build

## Data Persistence & State Management

**Kafka Data:**
- Persists to `./data/` (mounted volume)
- Includes internal topics: `docker-connect-offsets`, `docker-connect-configs`, `docker-connect-status`
- Schema Registry data stored in `_schemas` Kafka topic

**PostgreSQL Data:**
- Source DB: `./.postgres-source/`
- Target DB: `./.postgres-target/`

**MinIO Data:**
- Persists to `minio_data` Docker volume

**Important:** When resetting the demo, `./scripts/reset-demo.sh` deletes:
- All Kafka topics (including `_schemas` and `debezium-signal`)
- PostgreSQL tables, replication slots, and publications
- Kafka Connect internal topics (to clear offsets)
- Schema Registry schemas
- Restarts services in correct order (Schema Registry → Kafka Connect)

## Common Issues & Solutions

### Connector fails to start
- **Check PostgreSQL logical replication:** `docker exec postgres-source psql -U postgres -c "SHOW wal_level;"` (should return `logical`)
- **Verify replication slot exists:** `docker exec postgres-source psql -U postgres -d sourcedb -c "SELECT * FROM pg_replication_slots;"`
- **Check Connect logs:** `docker compose logs kafka-connect | tail -50`
- **Verify tables exist:** `docker exec postgres-source psql -U postgres -d sourcedb -c "\dt"`

### Kafka connection refused in containers
- **Always use `kafka:29092`** in container environment variables, not `localhost:9092`
- Host machine uses `localhost:9092`, containers use `kafka:29092` (internal listener)
- Verify Kafka is healthy: `docker compose logs kafka | grep -i "started"`

### Schema Registry errors / Schema incompatibility
- **Cause:** Schemas cached in `_schemas` Kafka topic conflict with new connector configuration
- **Solution:** Delete the `_schemas` topic while Schema Registry is stopped
- **Use reset script:** `./scripts/reset-demo.sh` handles this automatically
- **Test connection:** `curl http://localhost:8081/subjects`

### No data in topics after adding new tables
- **Check snapshot.mode:** If using `"initial"`, new tables won't snapshot automatically
- **Verify connector config:** `curl http://localhost:8083/connectors/debezium-postgres-source/config | jq '.["table.include.list"]'`
- **Check for snapshot records:** Look for `"op": "r"` in messages (not `"op": "c"` which are CDC inserts)
- **Trigger incremental snapshot:** Use `./scripts/add-tables.sh` or send manual signal

### Incremental snapshot not working
- **Verify signal topic exists:** `docker exec kafka kafka-topics --list --bootstrap-server localhost:9092 | grep debezium-signal`
- **Check signal configuration:** Connector must have `signal.enabled.channels: "source,kafka"` and `signal.kafka.topic: "debezium-signal"`
- **Restart connector task:** Signal processing may require task restart: `curl -X POST http://localhost:8083/connectors/debezium-postgres-source/tasks/0/restart`
- **Monitor logs:** `docker compose logs -f kafka-connect | grep -i "incremental\|snapshot\|signal"`

## Project File Structure

```
.
├── app/                              # Python data generator
│   ├── main.py                       # Faker-based data insertion logic
│   ├── logger.py                     # Centralized logging utility
│   ├── pyproject.toml                # UV dependencies (psycopg2-binary, faker)
│   └── Dockerfile                    # UV-based container build
├── connect/                          # Custom Kafka Connect image
│   ├── Dockerfile                    # Multi-stage build (Gradle + runtime)
│   └── build.gradle                  # JDBC driver dependency management
├── scripts/                          # Utility scripts
│   ├── create-connector.sh           # Create Debezium connector (first time)
│   ├── update-connector.sh           # Update existing connector config
│   ├── reset-demo.sh                 # Reset entire environment (nuclear option)
│   └── add-tables.sh                 # Add tables with incremental snapshot
├── .github/
│   └── copilot-instructions.md       # AI coding agent guidance (this file)
├── debezium-connector.json           # Debezium CDC connector configuration
├── example-connector-cfg.json        # Databricks Delta Lake sink reference
├── ADDING_TABLES.md                  # Comprehensive guide for incremental snapshots
├── docker-compose.yml                # Full infrastructure definition
└── README.md                         # User-facing documentation
```

## Key Configuration Files

**debezium-connector.json:**
- Main CDC connector configuration
- Edit `table.include.list` to add/remove tables
- Set `snapshot.mode` based on desired snapshotting behavior
- Contains Kafka signal topic configuration

**docker-compose.yml:**
- Defines all service containers
- Critical: Kafka has dual listeners configured
- PostgreSQL has `wal_level=logical` command override
- Kafka Connect environment variables point to `kafka:29092`

**ADDING_TABLES.md:**
- Step-by-step guide for adding tables to CDC pipeline
- Explains incremental snapshot approach
- Documents snapshot.mode differences
- Provides monitoring commands

## Best Practices for Contributors

1. **Always update connector via script:** Use `./scripts/update-connector.sh` instead of manual curl commands
2. **Test incrementally:** Add one table at a time to verify CDC is working before adding more
3. **Monitor logs:** Keep an eye on `docker compose logs -f kafka-connect` during changes
4. **Use helper scripts:** The scripts in `scripts/` handle edge cases and health checks
5. **Document changes:** Update README.md and copilot-instructions.md when adding new features
6. **Check snapshot mode:** Understand whether you need `"initial"` or `"when_needed"` before modifying `table.include.list`
7. **Clean resets:** Use `./scripts/reset-demo.sh` for full environment resets, not manual topic deletions

## Testing Scenarios

**Scenario 1: Initial setup**
```bash
docker compose up -d
./scripts/create-connector.sh
# Verify: kaf consume cdc.public.products
```

**Scenario 2: Adding a table (automatic)**
```bash
# Edit debezium-connector.json: "table.include.list": "public.products,public.sales,public.orders"
./scripts/update-connector.sh
# With snapshot.mode="when_needed", orders table auto-snapshots
```

**Scenario 3: Adding a table (manual incremental snapshot)**
```bash
# Edit debezium-connector.json with snapshot.mode="initial"
./scripts/update-connector.sh
./scripts/add-tables.sh "public.orders"
# Monitor: docker compose logs -f kafka-connect | grep snapshot
```

**Scenario 4: Full reset**
```bash
./scripts/reset-demo.sh
# Wait for services to initialize (script handles timing)
./scripts/create-connector.sh
```
