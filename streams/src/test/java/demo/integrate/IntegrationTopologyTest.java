package demo.integrate;

import io.confluent.kafka.schemaregistry.testutil.MockSchemaRegistry;
import io.confluent.kafka.serializers.AbstractKafkaSchemaSerDeConfig;
import io.confluent.kafka.streams.serdes.avro.GenericAvroDeserializer;
import io.confluent.kafka.streams.serdes.avro.GenericAvroSerializer;
import org.apache.avro.Conversions;
import org.apache.avro.LogicalTypes;
import org.apache.avro.Schema;
import org.apache.avro.generic.GenericRecord;
import org.apache.kafka.common.serialization.StringDeserializer;
import org.apache.kafka.streams.StreamsConfig;
import org.apache.kafka.streams.TestInputTopic;
import org.apache.kafka.streams.TestOutputTopic;
import org.apache.kafka.streams.TopologyTestDriver;
import org.apache.kafka.streams.test.TestRecord;
import org.assertj.core.api.InstanceOfAssertFactories;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.nio.ByteBuffer;
import java.nio.file.Path;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

class IntegrationTopologyTest {
    private static final long CREATED_MICROS = 1_760_000_000_123_456L;

    private final String scope = "integration-" + UUID.randomUUID();
    private final Map<String, String> serdeConfig = Map.of(
            AbstractKafkaSchemaSerDeConfig.SCHEMA_REGISTRY_URL_CONFIG, "mock://" + scope);
    private TopologyTestDriver driver;
    private long lsn = 1_000;

    @BeforeEach
    void setUp() {
        RegionRegistry registry = RegionRegistry.load(Path.of("config/regions.yaml"));
        List<EntitySpec> specs = EntitySpec.loadAll(Path.of("config/entities"));
        Clock clock = Clock.fixed(Instant.parse("2026-10-06T00:00:00Z"), ZoneOffset.UTC);

        Properties props = new Properties();
        props.put(StreamsConfig.APPLICATION_ID_CONFIG, "test");
        props.put(StreamsConfig.BOOTSTRAP_SERVERS_CONFIG, "dummy:9092");
        driver = new TopologyTestDriver(IntegrationTopology.build(registry, specs, serdeConfig, clock), props);
    }

    @AfterEach
    void tearDown() {
        driver.close();
        MockSchemaRegistry.dropScope(scope);
    }

    @Test
    void unionsThreeRegionalTopicsIntoOneEntityTopic() {
        for (String region : List.of("au", "uk", "us")) {
            send(region, "orders", 1, "c", order(1, 7, "pending"));
        }

        List<TestRecord<String, GenericRecord>> out = output("edm.order").readRecordsToList();

        assertThat(out).extracting(r -> r.value().get("jurisdiction_code").toString())
                .containsExactly("AU", "UK", "US");
        assertThat(out).extracting(TestRecord::key).containsExactly(
                Keys.entityKey("order", "AU", 1), Keys.entityKey("order", "UK", 1), Keys.entityKey("order", "US", 1));
        assertThat(out).extracting(r -> r.value().get("_entity_key").toString())
                .containsExactlyElementsOf(out.stream().map(TestRecord::key).toList());
        assertThat(out).extracting(r -> r.value().get("_source_topic").toString())
                .containsExactly("cdc.au.orders", "cdc.uk.orders", "cdc.us.orders");
        // Order is not a money entity, so it carries no currency
        assertThat(out.get(0).value().getSchema().getField("currency_code")).isNull();
    }

    @Test
    void renamesRetypesAndMintsForeignKeys() {
        send("au", "orders", 5, "c", order(5, 42, "paid"));

        GenericRecord row = output("edm.order").readValue();

        assertThat(row.get("order_id")).isEqualTo(5L);  // int4 widened to long
        assertThat(row.get("customer_id")).isEqualTo(42L);
        assertThat(row.get("customer_key").toString()).isEqualTo(Keys.entityKey("customer", "AU", 42L));
        assertThat(row.get("placed_at")).isEqualTo(CREATED_MICROS);
        assertThat(row.getSchema().getField("placed_at").schema().getTypes().get(1).getLogicalType())
                .isEqualTo(LogicalTypes.timestampMicros());
        assertThat(row.getSchema().getField("created_at")).isNull();
        assertThat(row.get("_cdc_op").toString()).isEqualTo("c");
        assertThat(row.get("_source_lsn")).isEqualTo(lsn);
        assertThat(row.get("_is_deleted")).isEqualTo(false);
    }

    @Test
    void widensMoneyToDecimal19_4AndStampsNativeCurrency() {
        send("uk", "line_items", 9, "c", lineItem(9, 3, 11, 2, "12.50"));

        GenericRecord row = output("edm.order_line").readValue();

        assertThat(money(row, "discount_amount")).isEqualByComparingTo("12.5").hasScaleOf(4);
        assertThat(row.get("currency_code").toString()).isEqualTo("GBP");
        assertThat(row.get("order_key").toString()).isEqualTo(Keys.entityKey("order", "UK", 3L));
        assertThat(row.get("product_key").toString()).isEqualTo(Keys.entityKey("product", "UK", 11L));
    }

    @Test
    void customerCarriesNoDirectIdentifiers() {
        Map<String, Object> user = new HashMap<>();
        user.put("id", 3);
        user.put("first_name", "Ada");
        user.put("last_name", "Lovelace");
        user.put("email", "ada@example.com");
        user.put("phone_number", "0400 000 000");
        user.put("address_line_one", "1 Analytical St");
        user.put("city", "Melbourne");
        user.put("state", "Victoria");
        user.put("postal_code", "3000");
        user.put("country", "Australia");
        user.put("created_at", CREATED_MICROS);
        send("au", "users", 3, "r", user);

        GenericRecord row = output("edm.customer").readValue();

        assertThat(row.getSchema().getFields()).extracting(Schema.Field::name)
                .doesNotContain("first_name", "last_name", "email", "phone_number", "address_line_one", "address_line_two")
                .contains("customer_id", "state_province", "country_name", "registered_at");
        assertThat(row.toString()).doesNotContain("Ada", "Lovelace", "ada@example.com", "Analytical");
        assertThat(row.get("state_province").toString()).isEqualTo("Victoria");
    }

    @Test
    void quarantinesRowsThatFailAQuarantineExpectation() {
        send("us", "line_items", 4, "c", lineItem(4, 1, 1, 0, "0.00"));

        assertThat(output("edm.order_line").isEmpty()).isTrue();
        GenericRecord bad = output("edm.order_line.quarantine").readValue();
        assertThat(bad.get("_dq_failures")).asInstanceOf(InstanceOfAssertFactories.LIST).extracting(Object::toString).containsExactly("positive_quantity");
        assertThat(bad.get("jurisdiction_code").toString()).isEqualTo("US");
        assertThat(bad.get("_source_row_json").toString()).contains("\"quantity\": 0");
    }

    @Test
    void quarantinesUnknownStatusAndMissingCustomer() {
        send("au", "orders", 2, "u", order(2, null, "lost"));

        GenericRecord bad = output("edm.order.quarantine").readValue();
        assertThat(bad.get("_dq_failures")).asInstanceOf(InstanceOfAssertFactories.LIST).extracting(Object::toString)
                .containsExactly("has_customer", "known_status");
        assertThat(bad.get("customer_key")).isNull(); // null FK -> null key, not hash("null")
    }

    @Test
    void warnExpectationLetsTheRowThrough() {
        Map<String, Object> product = new HashMap<>();
        product.put("id", 8);
        product.put("name", "Widget");
        product.put("price", Debezium.decimalBytes("19.99", 2));
        product.put("stock_quantity", -1);
        product.put("created_at", CREATED_MICROS);
        send("au", "products", 8, "c", product);

        GenericRecord row = output("edm.product").readValue();
        assertThat(row.get("stock_quantity")).isEqualTo(-1);
        assertThat(money(row, "list_price")).isEqualByComparingTo("19.99");
        assertThat(row.get("currency_code").toString()).isEqualTo("AUD");
        assertThat(output("edm.product.quarantine").isEmpty()).isTrue();
    }

    @Test
    void deleteEmitsBeforeImageThenTombstone() {
        send("uk", "orders", 6, "d", order(6, 1, "cancelled"));
        sendTombstone("uk", "orders", 6);

        List<TestRecord<String, GenericRecord>> out = output("edm.order").readRecordsToList();
        String key = Keys.entityKey("order", "UK", 6);
        assertThat(out).hasSize(2).allSatisfy(r -> assertThat(r.key()).isEqualTo(key));
        assertThat(out.get(0).value().get("_is_deleted")).isEqualTo(true);
        assertThat(out.get(0).value().get("order_status").toString()).isEqualTo("cancelled");
        assertThat(out.get(1).value()).isNull();
    }

    @Test
    void sourceSchemaDriftIsQuarantinedNotNulled() {
        Map<String, Schema> columns = Debezium.columns("orders");
        columns.remove("promo_code");
        Schema drifted = Debezium.envelopeSchema("au", "orders", columns);
        Map<String, Object> row = order(10, 1, "pending");
        row.remove("promo_code");
        input("au", "orders").pipeInput(Debezium.key("au", "orders", 10), Debezium.event(drifted, "c", ++lsn, row));

        assertThat(output("edm.order").isEmpty()).isTrue();
        assertThat(output("edm.order.quarantine").readValue().get("_dq_failures")).asInstanceOf(InstanceOfAssertFactories.LIST)
                .extracting(Object::toString).containsExactly("missing_column:promo_code");
    }

    // --- helpers ---

    private static Map<String, Object> order(int id, Integer userId, String status) {
        Map<String, Object> row = new HashMap<>();
        row.put("id", id);
        row.put("user_id", userId);
        row.put("order_status", status);
        row.put("promo_code", null);
        row.put("created_at", CREATED_MICROS);
        row.put("updated_at", CREATED_MICROS);
        return row;
    }

    private static Map<String, Object> lineItem(int id, int orderId, int productId, int quantity, String discount) {
        Map<String, Object> row = new HashMap<>();
        row.put("id", id);
        row.put("order_id", orderId);
        row.put("product_id", productId);
        row.put("quantity", quantity);
        row.put("line_item_discount", Debezium.decimalBytes(discount, 2));
        row.put("created_at", CREATED_MICROS);
        row.put("updated_at", CREATED_MICROS);
        return row;
    }

    private void send(String region, String table, int id, String op, Map<String, Object> row) {
        Schema schema = Debezium.envelopeSchema(region, table, Debezium.columns(table));
        input(region, table).pipeInput(Debezium.key(region, table, id), Debezium.event(schema, op, ++lsn, row));
    }

    private void sendTombstone(String region, String table, int id) {
        input(region, table).pipeInput(Debezium.key(region, table, id), (GenericRecord) null);
    }

    private TestInputTopic<GenericRecord, GenericRecord> input(String region, String table) {
        GenericAvroSerializer keys = new GenericAvroSerializer();
        keys.configure(serdeConfig, true);
        GenericAvroSerializer values = new GenericAvroSerializer();
        values.configure(serdeConfig, false);
        return driver.createInputTopic("cdc." + region + "." + table, keys, values);
    }

    private TestOutputTopic<String, GenericRecord> output(String topic) {
        GenericAvroDeserializer values = new GenericAvroDeserializer();
        values.configure(serdeConfig, false);
        return driver.createOutputTopic(topic, new StringDeserializer(), values);
    }

    private static BigDecimal money(GenericRecord row, String field) {
        Schema schema = RowTransformer.nonNull(row.getSchema().getField(field).schema());
        return new Conversions.DecimalConversion().fromBytes((ByteBuffer) row.get(field), schema, schema.getLogicalType());
    }
}
