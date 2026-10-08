package demo.integrate;

import io.confluent.kafka.serializers.AbstractKafkaSchemaSerDeConfig;
import org.apache.kafka.clients.consumer.ConsumerConfig;
import org.apache.kafka.streams.KafkaStreams;
import org.apache.kafka.streams.StreamsConfig;
import org.apache.kafka.streams.Topology;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.file.Path;
import java.time.Clock;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.CountDownLatch;

public final class App {
    private static final Logger LOG = LoggerFactory.getLogger(App.class);

    public static void main(String[] args) throws InterruptedException {
        Path configDir = Path.of(env("CONFIG_DIR", "config"));
        RegionRegistry registry = RegionRegistry.load(configDir.resolve("regions.yaml"));
        List<EntitySpec> specs = EntitySpec.loadAll(configDir.resolve("entities"));

        Map<String, String> serdeConfig = Map.of(
                AbstractKafkaSchemaSerDeConfig.SCHEMA_REGISTRY_URL_CONFIG, env("SCHEMA_REGISTRY_URL", "http://localhost:8081"));
        Topology topology = IntegrationTopology.build(registry, specs, serdeConfig, Clock.systemUTC());

        Properties props = new Properties();
        props.put(StreamsConfig.APPLICATION_ID_CONFIG, env("APPLICATION_ID", "region-integration"));
        props.put(StreamsConfig.BOOTSTRAP_SERVERS_CONFIG, env("BOOTSTRAP_SERVERS", "localhost:9092"));
        props.put(StreamsConfig.PROCESSING_GUARANTEE_CONFIG, StreamsConfig.EXACTLY_ONCE_V2);
        props.put(StreamsConfig.REPLICATION_FACTOR_CONFIG, 1);
        props.put(ConsumerConfig.AUTO_OFFSET_RESET_CONFIG, "earliest");

        for (RegionRegistry.Region r : registry.regions()) {
            LOG.info("Region {} ({}, {}) from {}.{}.*", r.code(), r.currencyCode(), r.timezone(), registry.topicPrefix(), r.schema());
        }
        for (EntitySpec s : specs) {
            LOG.info("Entity {}: {} x {} regions -> {} / {}", s.entity(), s.sourceTable(), registry.regions().size(), s.edmTopic(), s.quarantineTopic());
        }
        LOG.info("Topology:\n{}", topology.describe());

        KafkaStreams streams = new KafkaStreams(topology, props);
        CountDownLatch done = new CountDownLatch(1);
        streams.setStateListener((now, was) -> {
            LOG.info("State {} -> {}", was, now);
            if (now == KafkaStreams.State.ERROR || now == KafkaStreams.State.NOT_RUNNING) {
                done.countDown();
            }
        });
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            streams.close();
            done.countDown();
        }));
        streams.start();
        done.await();
        // Exit non-zero on ERROR so the container restart policy kicks in
        System.exit(streams.state() == KafkaStreams.State.ERROR ? 1 : 0);
    }

    private static String env(String name, String fallback) {
        String value = System.getenv(name);
        return value == null || value.isBlank() ? fallback : value;
    }
}
