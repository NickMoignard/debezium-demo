# Region integration with Kafka Streams

This app reads the per-region Debezium topics (`cdc.au.orders`, `cdc.uk.orders`, `cdc.us.orders`, and the
same for `products`, `users` and `line_items`) and writes one integrated topic per entity.
It standardizes keys and types, removes direct identifiers from customer records, and routes
failed checks to quarantine topics.

```
cdc.au.orders ─┐
cdc.uk.orders ─┼─ merge ─ key: sha256(order:{region}:{id}) ─ unwrap, rename, retype, DQ ─┬─ edm.order
cdc.us.orders ─┘                                                                         └─ edm.order.quarantine
```

| Source table | Entity topic | What changes on the way |
|---|---|---|
| products | `edm.product` | `price` becomes `list_price` as DECIMAL(19,4), plus `currency_code` |
| users | `edm.customer` | names, email, phone and street address lines are dropped. `state` becomes `state_province` |
| orders | `edm.order` | `created_at` becomes `placed_at`, `user_id` becomes `customer_id` with a `customer_key` hash |
| line_items | `edm.order_line` | `line_item_discount` becomes `discount_amount` as DECIMAL(19,4), plus `order_key` and `product_key` |

Every row also gets `_entity_key`, `jurisdiction_id`, `jurisdiction_code`, `_cdc_op`, `_source_lsn`,
`_source_ts_ms`, `_source_topic`, `_is_deleted` and `_processed_at`. Ids widen from int4 to long.
Debezium's epoch-micros timestamps become Avro `timestamp-micros` in UTC.

## Running it

From the repo root:

```bash
docker compose up -d --build
./scripts/create-integrated-topics.sh   # before the app starts consuming, see below
./scripts/create-connector.sh
./scripts/create-target-schemas.sh      # these two land entity topics in the EDM store
./scripts/create-sink-connectors.sh
docker compose logs -f streams-app
```

Kafka Streams refuses to start if a source topic is missing. If `streams-app` comes up before the
topics exist, it exits, Docker restarts it, and it settles once the script has run. That is expected.

Read an integrated topic:

```bash
docker exec schema-registry kafka-avro-console-consumer --bootstrap-server kafka:29092 \
  --topic edm.order_line --from-beginning --max-messages 20 \
  --property schema.registry.url=http://localhost:8081 2>/dev/null | grep '^{'
```

Swap in `edm.order.quarantine` to see quarantined changes. Each one carries `_dq_failures` and the source row as JSON, with money written as a decimal string at the source scale (`"12.50"`).
The generator injects bad data at a default rate of 2% and includes deletes in its workload.
Configure these with `setup --bad-data-rate` and `--operation-mix`; see the
[generator guide](../app_multi_region/README.md).

Tests run against `TopologyTestDriver` and a mock Schema Registry, so they need no Docker:

```bash
cd streams && gradle test
```

## Configuration, not code

`config/regions.yaml` is the region registry. `config/entities/*.yaml` holds one spec per entity:
columns, renames, types, FK keys, the PII deny list and expectations. Compose mounts `config/` into
the container, so a change only needs `docker compose restart streams-app`.

Adding a region takes a registry row and the new schema in the connector's `table.include.list`.
The new region's rows then land in the same `edm.*` topics. Adding an entity takes one YAML file and
a rerun of the topic script.

Expectations come in four kinds (`not_null`, `in_set`, `gt`, `gte`) with three actions:

- `warn` logs and passes the row through
- `drop` discards it
- `quarantine` routes it to `edm.{entity}.quarantine`

They run on output column names, after renames and retyping. A null fails every kind. The app also
quarantines anything that breaks the contract instead of guessing: a selected column missing from
the source schema, a decimal that would need rounding to fit DECIMAL(19,4), or an unknown CDC op.
Deletes skip expectations, because a delete has to reach the entity topic even if the row it removes
would fail DQ today.

## Integration and ordering

Each regional topic becomes a `KStream`. The topology merges these streams, stamps region
metadata from `regions.yaml`, and computes entity keys as SHA-256 hashes of
`entity:region:source_id`. It validates each output row and sends it to either the entity
topic or the quarantine topic. The PostgreSQL sink upserts entity rows on `_entity_key`.

Each source key comes from one PostgreSQL write-ahead log and stays in one source partition.
It maps to exactly one output key, so the topology needs no repartition topic. Changes for
that key retain their source order.
Every step is stateless. Processing runs with `exactly_once_v2`.

## Limits

- Kafka retains seven days of changes. Current state lives in the EDM store. Rebuilding it
  after that window requires a Debezium re-snapshot or a separately maintained archive.
  See [ADR 0001](../docs/adr/0001-kafka-is-transport-not-storage.md).
- The demo stores current state only. It does not maintain historical row versions.
- Ordering is guaranteed per key, not across different entities or regions.
- Schema Registry enforces schema compatibility. A required field without a default can
  fail registration and stop the app.
- Data-quality results are available through logs and quarantine topics.
- Kafka Streams offsets and transactions need operational monitoring.
