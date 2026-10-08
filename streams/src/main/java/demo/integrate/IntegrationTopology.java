package demo.integrate;

import io.confluent.kafka.streams.serdes.avro.GenericAvroSerde;
import org.apache.avro.generic.GenericRecord;
import org.apache.kafka.common.serialization.Serde;
import org.apache.kafka.common.serialization.Serdes;
import org.apache.kafka.streams.StreamsBuilder;
import org.apache.kafka.streams.Topology;
import org.apache.kafka.streams.kstream.Branched;
import org.apache.kafka.streams.kstream.Consumed;
import org.apache.kafka.streams.kstream.KStream;
import org.apache.kafka.streams.kstream.Named;
import org.apache.kafka.streams.kstream.Produced;

import java.time.Clock;
import java.util.List;
import java.util.Map;

/**
 * For each entity: one stream per regional CDC topic, merged into one, keyed
 * by the entity hash, transformed, then split into EDM and quarantine.
 *
 * Everything is stateless. The re-key never needs a repartition topic: each
 * source key lives in one source partition and maps to exactly one output key,
 * so per-key order (LSN order) carries through to the output topic.
 */
public final class IntegrationTopology {
    private IntegrationTopology() {}

    public static Topology build(RegionRegistry registry, List<EntitySpec> specs, Map<String, ?> serdeConfig, Clock clock) {
        Serde<GenericRecord> sourceKeySerde = avroSerde(serdeConfig, true);
        Serde<GenericRecord> valueSerde = avroSerde(serdeConfig, false);
        Produced<String, GenericRecord> produced = Produced.with(Serdes.String(), valueSerde);

        StreamsBuilder builder = new StreamsBuilder();
        for (EntitySpec spec : specs) {
            RowTransformer transformer = new RowTransformer(spec, clock);

            // The UNION ALL: one stream per region, region stamped from the
            // registry, merged. Region is data from here on.
            KStream<GenericRecord, CdcEvent> union = null;
            for (RegionRegistry.Region region : registry.regions()) {
                String topic = registry.sourceTopic(region, spec.sourceTable());
                KStream<GenericRecord, CdcEvent> regional = builder
                        .stream(topic, Consumed.with(sourceKeySerde, valueSerde).withName(spec.entity() + "-" + region.schema()))
                        .mapValues((key, envelope) -> new CdcEvent(region, topic, key == null ? null : key.get(spec.key()), envelope));
                union = union == null ? regional : union.merge(regional);
            }

            union.selectKey((key, event) -> Keys.entityKey(spec.entity(), event.region().code(), event.sourceId()))
                    .mapValues(transformer::apply)
                    .split(Named.as(spec.entity() + "-"))
                    .branch((key, out) -> out.route() == Outcome.Route.QUARANTINE,
                            Branched.withConsumer(s -> s.mapValues(Outcome::record).to(spec.quarantineTopic(), produced), "quarantine"))
                    .branch((key, out) -> out.route() == Outcome.Route.DROP,
                            Branched.withConsumer(s -> { }, "drop"))
                    // VALID and TOMBSTONE; a tombstone's record is null, which tells consumers to delete the key
                    .defaultBranch(Branched.withConsumer(s -> s.mapValues(Outcome::record).to(spec.edmTopic(), produced), "edm"));
        }
        return builder.build();
    }

    private static Serde<GenericRecord> avroSerde(Map<String, ?> config, boolean isKey) {
        GenericAvroSerde serde = new GenericAvroSerde();
        serde.configure(config, isKey);
        return serde;
    }
}
