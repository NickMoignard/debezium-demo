package demo.integrate;

import org.apache.avro.generic.GenericRecord;

/**
 * A Debezium change event tagged with the region it came from. The region is
 * known when the per-region stream is built, so stamping it costs nothing and
 * survives the merge. {@code envelope} is null for a Debezium tombstone.
 */
public record CdcEvent(RegionRegistry.Region region, String sourceTopic, Object sourceId, GenericRecord envelope) {}
