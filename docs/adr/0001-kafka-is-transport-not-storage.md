# Kafka is transport, not storage

Entity topics were compacted, which turned them into a permanent current-state store. We switch them to time-based `delete` retention because Kafka here is only a transport. The durable copies live in Postgres (the EDM store and quarantine) and, outside this repo, in the data lake's raw layer, which a separate sink fills from Kafka.

## Consequences

- A new consumer of an entity topic only sees the retention window, not the full current state of every entity.
- If the EDM store is lost, it can't be rebuilt by replaying a topic. It comes back from the raw layer or from a Debezium re-snapshot of the regional sources.
