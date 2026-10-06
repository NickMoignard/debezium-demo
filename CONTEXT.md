# Region integration

Regional source databases are captured as change events, merged into one integrated stream per
entity, and landed as current state in an EDM store. This PoC is the Kafka-side version of the
lakehouse Prep + EDM hop.

Kafka is transport only. Nothing is kept there permanently; the EDM store and the data lake's raw layer hold the durable copies.

## Language

**Region**:
One geo's source database (AU, UK, US), identified by its jurisdiction code. Each one stands in for a separate regional database.
_Avoid_: Schema, geo, market

**Entity**:
A business concept integrated across all regions, such as customer, order, order line or product.
_Avoid_: Table (that's the regional source shape)

**Entity key**:
The surrogate identity of one entity instance across all regions, derived from the entity, region and source id.
_Avoid_: ID, primary key (the source id is only unique within a region)

**Entity topic**:
The integrated stream of changes for one entity, all regions merged, keyed by entity key.
_Avoid_: Output topic, EDM topic

**Quarantine**:
Every change that failed data-quality or contract checks, with the reasons it failed, kept as an append-only log apart from the EDM store. Each failed change is recorded exactly once. A quarantined update leaves the EDM store holding the last version that passed.
_Avoid_: Dead letter, rejects

**EDM store**:
The current-state copy of every entity: one row per live entity key. A deleted entity has no row; the store keeps no record that it existed. The Target Postgres database plays this role.
_Avoid_: Target, sink, warehouse

**Orphan**:
A row in the EDM store that references an entity key the store doesn't hold. Expected while related changes are in flight, and lasting when the parent was quarantined. A data-quality signal, not an error.
_Avoid_: Broken reference, dangling row
