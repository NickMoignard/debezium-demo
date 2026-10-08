package demo.integrate;

import org.apache.avro.JsonProperties;
import org.apache.avro.LogicalTypes;
import org.apache.avro.Schema;
import org.apache.avro.generic.GenericData;
import org.apache.avro.generic.GenericRecord;

import java.math.BigDecimal;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Builds key/envelope records shaped like the ones Debezium's Postgres
 * connector writes through the Confluent AvroConverter: same connect.name
 * annotations, same decimal-as-bytes encoding, same envelope fields.
 */
final class Debezium {
    static final Schema INT = Schema.create(Schema.Type.INT);
    static final Schema STRING = Schema.create(Schema.Type.STRING);

    private Debezium() {}

    static Schema decimal(int precision, int scale) {
        // addToSchema writes the precision/scale props Debezium also sets
        Schema s = LogicalTypes.decimal(precision, scale).addToSchema(Schema.create(Schema.Type.BYTES));
        s.addProp("connect.name", "org.apache.kafka.connect.data.Decimal");
        return s;
    }

    static Schema microTimestamp() {
        Schema s = Schema.create(Schema.Type.LONG);
        s.addProp("connect.name", "io.debezium.time.MicroTimestamp");
        return s;
    }

    static ByteBuffer decimalBytes(String value, int scale) {
        return ByteBuffer.wrap(new BigDecimal(value).setScale(scale).unscaledValue().toByteArray());
    }

    /** Postgres DDL for each generator table, as Debezium would type it. id is the only non-null column. */
    static Map<String, Schema> columns(String table) {
        Map<String, Schema> c = new LinkedHashMap<>();
        c.put("id", INT);
        switch (table) {
            case "products" -> {
                c.put("name", STRING);
                c.put("category", STRING);
                c.put("price", decimal(10, 2));
                c.put("stock_quantity", INT);
                c.put("created_at", microTimestamp());
            }
            case "users" -> {
                for (String f : List.of("first_name", "last_name", "email", "phone_number", "address_line_one",
                        "address_line_two", "city", "state", "postal_code", "country")) {
                    c.put(f, STRING);
                }
                c.put("created_at", microTimestamp());
                c.put("updated_at", microTimestamp());
            }
            case "orders" -> {
                c.put("user_id", INT);
                c.put("order_status", STRING);
                c.put("promo_code", STRING);
                c.put("created_at", microTimestamp());
                c.put("updated_at", microTimestamp());
            }
            case "line_items" -> {
                c.put("product_id", INT);
                c.put("order_id", INT);
                c.put("quantity", INT);
                c.put("line_item_discount", decimal(10, 2));
                c.put("created_at", microTimestamp());
                c.put("updated_at", microTimestamp());
            }
            default -> throw new IllegalArgumentException(table);
        }
        return c;
    }

    static Schema keySchema(String pgSchema, String table) {
        return Schema.createRecord("Key", null, "cdc." + pgSchema + "." + table, false,
                List.of(new Schema.Field("id", INT)));
    }

    static Schema envelopeSchema(String pgSchema, String table, Map<String, Schema> columns) {
        String ns = "cdc." + pgSchema + "." + table;
        List<Schema.Field> fields = new ArrayList<>();
        columns.forEach((name, type) -> fields.add(name.equals("id")
                ? new Schema.Field(name, type)
                : new Schema.Field(name, nullable(type), null, JsonProperties.NULL_VALUE)));
        Schema value = Schema.createRecord("Value", null, ns, false, fields);

        Schema source = Schema.createRecord("Source", null, "io.debezium.connector.postgresql", false, List.of(
                new Schema.Field("version", STRING),
                new Schema.Field("connector", STRING),
                new Schema.Field("ts_ms", Schema.create(Schema.Type.LONG)),
                new Schema.Field("schema", STRING),
                new Schema.Field("table", STRING),
                new Schema.Field("lsn", nullable(Schema.create(Schema.Type.LONG)), null, JsonProperties.NULL_VALUE)));

        return Schema.createRecord("Envelope", null, ns, false, List.of(
                new Schema.Field("before", nullable(value), null, JsonProperties.NULL_VALUE),
                new Schema.Field("after", nullable(value), null, JsonProperties.NULL_VALUE),
                new Schema.Field("source", source),
                new Schema.Field("op", STRING),
                new Schema.Field("ts_ms", nullable(Schema.create(Schema.Type.LONG)), null, JsonProperties.NULL_VALUE)));
    }

    static GenericRecord key(String pgSchema, String table, int id) {
        GenericRecord key = new GenericData.Record(keySchema(pgSchema, table));
        key.put("id", id);
        return key;
    }

    /** An envelope for op c/u/r (row goes in after) or d (row goes in before). */
    static GenericRecord event(Schema envelopeSchema, String op, long lsn, Map<String, Object> row) {
        Schema valueSchema = RowTransformer.nonNull(envelopeSchema.getField("after").schema());
        GenericRecord image = new GenericData.Record(valueSchema);
        row.forEach(image::put);

        Schema sourceSchema = envelopeSchema.getField("source").schema();
        GenericRecord source = new GenericData.Record(sourceSchema);
        source.put("version", "3.0.0.Final");
        source.put("connector", "postgresql");
        source.put("ts_ms", 1_760_000_000_000L);
        source.put("schema", envelopeSchema.getNamespace().split("\\.")[1]);
        source.put("table", envelopeSchema.getNamespace().split("\\.")[2]);
        source.put("lsn", lsn);

        GenericRecord envelope = new GenericData.Record(envelopeSchema);
        envelope.put(op.equals("d") ? "before" : "after", image);
        envelope.put("source", source);
        envelope.put("op", op);
        envelope.put("ts_ms", 1_760_000_000_100L);
        return envelope;
    }

    private static Schema nullable(Schema s) {
        return Schema.createUnion(Schema.create(Schema.Type.NULL), s);
    }
}
