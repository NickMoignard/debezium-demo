# Region integration with Kafka Streams

This app reads the per-region Debezium topics (`cdc.au.orders`, `cdc.uk.orders`, `cdc.us.orders`, and the
same for `products`, `users` and `line_items`) and writes one integrated topic per entity. The work is
what the lakehouse Prep + EDM hop does in DLT. This version does it on Kafka, before anything reaches
Databricks.

```
cdc.au.orders ─┐
cdc.uk.orders ─┼─ merge ─ key: sha256(order:{region}:{id}) ─ unwrap, rename, retype, DQ ─┬─ edm.order             (compacted)
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

Swap in `edm.order.quarantine` to see rejects. Each one carries `_dq_failures` and the source row as JSON.
The generator breaks about 2% of rows on purpose (`BAD_DATA_RATE`) and now and then deletes a
cancelled order (`DELETE_RATE`), so both paths always have traffic.

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

## How it maps to the DLT design

| Lakehouse doc (§6.1) | Here |
|---|---|
| `UNION ALL` view over the three regional Raw tables | one `KStream` per regional topic, `merge()`d |
| Region registry stamps `jurisdiction_code`, `currency_code` | `regions.yaml`, captured in the per-region stream at build time |
| `sha2(concat_ws(':', entity, region, id), 256)` keys (D12) | `Keys.entityKey`, the same formula |
| DLT expectations + quarantine table | `split()` into the entity topic and a quarantine topic |
| `AUTO CDC ... SCD TYPE 1`, sequenced by `_lsn` | compacted topic keyed by the entity hash. The latest record per key is the current state |
| Contract tests for schema drift | missing column goes to quarantine; output schema lives in Schema Registry |

The ordering argument is the one `prep_edm/CONTEXT.md` makes for `sequence_by = lsn`. Each source key
comes from one WAL and lives in one source partition, and it maps to exactly one output key. Re-keying
therefore never needs a repartition topic, and per-key LSN order carries through to the output.
Every step is stateless. Processing runs with `exactly_once_v2`.

## Where this is weaker than DLT

I think the union, typing and DQ steps are a better fit here than in DLT. They are row-level, they
run in milliseconds, and the integrated topics are reusable by anything that reads Kafka, not only
Databricks. A few things don't come for free, though:

- Compaction gives you SCD1 eventually, not at read time. A consumer reading the topic sees every
  version until the cleaner runs. Loading it into Delta still needs a MERGE or AUTO CDC on `_entity_key`.
- SCD2 (`user_history`, `market_history`) would need a state store and is not attempted.
- Ordering is only guaranteed per key. That is fine for AUTO CDC downstream, which sequences per
  key anyway.
- Schema evolution moves from DLT into Schema Registry compatibility rules (BACKWARD by default). A
  spec change that breaks them, such as adding a required field with no default, fails schema
  registration on the first produce and stops the app.
- DQ metrics are log lines and topic counts, not the DLT event log.
- It is one more service to run, and its offsets and transactions need watching.
