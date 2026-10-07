# Demo Script

A walkthrough for showing the pipeline live: regional source data on one side, the EDM store and quarantine log on the other. It assumes the stack is up and set up as in the [README](README.md) Quick Start.

Use two terminals, one per database.

## Connect

```bash
# Terminal 1: source (the three regional databases, one schema each)
docker exec -it postgres-source psql -U postgres -d sourcedb

# Terminal 2: target (EDM store + quarantine log)
docker exec -it postgres-target psql -U postgres -d targetdb
```

From your own `psql`, use `psql -h localhost -p 5432 -U postgres sourcedb` and `psql -h localhost -p 5433 -U postgres targetdb`. The password is `postgres`.

## 1. Source: three separate regions, overlapping ids, PII

```sql
-- Source terminal
SELECT 'AU' AS region, count(*) FROM au.orders
UNION ALL SELECT 'UK', count(*) FROM uk.orders
UNION ALL SELECT 'US', count(*) FROM us.orders;

-- Customer id 7 exists in every region and is a different person each time
SELECT 'AU' AS region, id, first_name, last_name, email, city, country FROM au.users WHERE id = 7
UNION ALL SELECT 'UK', id, first_name, last_name, email, city, country FROM uk.users WHERE id = 7
UNION ALL SELECT 'US', id, first_name, last_name, email, city, country FROM us.users WHERE id = 7;
```

## 2. EDM store: one table, all regions, a distinct key each, no PII

```sql
-- Target terminal
SELECT jurisdiction_code, customer_id, left(_entity_key, 12) AS entity_key,
       city, state_province, country_name, registered_at
FROM edm.customer WHERE customer_id = 7 ORDER BY 1;
```

Names, email and phone are gone. `state` became `state_province`, and the timestamp is UTC.

```sql
-- Renamed and retyped: price → list_price numeric(19,4), with currency from the region registry
SELECT jurisdiction_code, product_id, product_name, list_price, currency_code
FROM edm.product WHERE product_id = 3 ORDER BY 1;

-- Row counts per entity and region
SELECT 'customer' AS entity, jurisdiction_code, count(*) FROM edm.customer GROUP BY 2
UNION ALL SELECT 'order', jurisdiction_code, count(*) FROM edm.order GROUP BY 2
UNION ALL SELECT 'order_line', jurisdiction_code, count(*) FROM edm.order_line GROUP BY 2
UNION ALL SELECT 'product', jurisdiction_code, count(*) FROM edm.product GROUP BY 2
ORDER BY 1, 2;
```

## 3. Live: insert, a bad update, recovery, delete

Run each source step, then the target query after it. Changes show up in about a second.

```sql
-- Source: create an order, note the id it returns
INSERT INTO au.orders (user_id, order_status) VALUES (1, 'pending') RETURNING id;
```

```sql
-- Target: it's in the EDM store (replace 27300 with your id)
SELECT order_id, order_status, _cdc_op, _processed_at FROM edm.order
WHERE jurisdiction_code = 'AU' AND order_id = 27300;
```

```sql
-- Source: a status the rules don't allow
UPDATE au.orders SET order_status = 'lost', updated_at = now() WHERE id = 27300;
```

```sql
-- Target: the EDM store still says 'pending' (the last good version)...
SELECT order_id, order_status FROM edm.order WHERE jurisdiction_code = 'AU' AND order_id = 27300;
-- ...and the bad change is in quarantine, with the reason
SELECT order_id, order_status, _dq_failures, _source_row_json FROM quarantine.order
WHERE jurisdiction_code = 'AU' AND order_id = 27300;
```

```sql
-- Source: fix it
UPDATE au.orders SET order_status = 'paid', updated_at = now() WHERE id = 27300;
-- Target: the EDM store catches up to 'paid' (rerun the edm.order query)

-- Source: delete it
DELETE FROM au.orders WHERE id = 27300;
-- Target: the row is gone from edm.order; its quarantine history stays
```

The generator moves existing orders through their statuses, so it may also touch your order mid-demo. A fresh insert like this one is the least likely to be picked.

## 4. Quarantine log

```sql
-- Target: failures by rule
SELECT 'order' AS entity, _dq_failures, count(*) FROM quarantine.order GROUP BY 2
UNION ALL SELECT 'order_line', _dq_failures, count(*) FROM quarantine.order_line GROUP BY 2
UNION ALL SELECT 'product', _dq_failures, count(*) FROM quarantine.product GROUP BY 2
ORDER BY 1, 3 DESC;

-- Recent rejected order lines: the typed value next to the raw source value
SELECT jurisdiction_code, order_id, discount_amount, quantity, _dq_failures,
       substring(_source_row_json from '"line_item_discount": [^,]*') AS source_discount
FROM quarantine.order_line WHERE _dq_failures = '{discount_non_negative}'
ORDER BY _processed_at DESC LIMIT 3;
```

`quarantine.customer` may not exist. Customers only have a `warn` rule, so a customer lands in quarantine only when it breaks the contract.

## 5. Orphans and lag

```sql
-- Target: order lines whose order isn't in the EDM store, and whether that order was quarantined
SELECT l.jurisdiction_code, count(*) AS orphan_lines,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM quarantine.order q WHERE q._entity_key = l.order_key)) AS parent_quarantined
FROM edm.order_line l
WHERE NOT EXISTS (SELECT 1 FROM edm.order o WHERE o._entity_key = l.order_key)
GROUP BY 1 ORDER BY 1;

-- Source commit to Streams processing, last 5 minutes
SELECT jurisdiction_code,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM _processed_at - to_timestamp(_source_ts_ms / 1000.0) AT TIME ZONE 'UTC'))::numeric, 3) AS p50_seconds,
       round(max(extract(epoch FROM _processed_at - to_timestamp(_source_ts_ms / 1000.0) AT TIME ZONE 'UTC'))::numeric, 3) AS worst_seconds
FROM edm.order WHERE _processed_at > now() AT TIME ZONE 'UTC' - interval '5 minutes'
GROUP BY 1 ORDER BY 1;
```

Almost every orphan should have a quarantined parent. Lag is usually about half a second at p50 and under a second at worst.

## Tips while presenting

- Type `\x auto` in `psql` so wide rows like `_source_row_json` wrap readably.
- Write `edm.order` schema-qualified. A bare `order` is a reserved word and gives a syntax error.
- To close the demo, `./scripts/verify-edm-store.sh` prints the full reconciliation table. It pauses the generator for about a minute while it runs.
