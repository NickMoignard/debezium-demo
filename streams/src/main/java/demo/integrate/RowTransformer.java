package demo.integrate;

import demo.integrate.EntitySpec.Column;
import demo.integrate.EntitySpec.Expectation;
import org.apache.avro.Conversions;
import org.apache.avro.LogicalType;
import org.apache.avro.LogicalTypes;
import org.apache.avro.Schema;
import org.apache.avro.generic.GenericData;
import org.apache.avro.generic.GenericRecord;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.time.Clock;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Turns one regional Debezium event into one integrated row: unwraps the
 * envelope, renames, retypes, mints keys, stamps the region, then runs the
 * entity's expectations to pick a route.
 */
public final class RowTransformer {
    private static final Logger LOG = LoggerFactory.getLogger(RowTransformer.class);
    private static final Conversions.DecimalConversion DECIMALS = new Conversions.DecimalConversion();

    private final EntitySpec spec;
    private final Schema edmSchema;
    private final Schema quarantineSchema;
    private final Clock clock;

    public RowTransformer(EntitySpec spec, Clock clock) {
        this.spec = spec;
        this.edmSchema = TargetSchemas.edm(spec);
        this.quarantineSchema = TargetSchemas.quarantine(spec);
        this.clock = clock;
    }

    public Outcome apply(CdcEvent event) {
        if (event.envelope() == null) {
            // Debezium's post-delete tombstone. Forward it so consumers delete the key.
            return event.sourceId() == null ? Outcome.drop() : Outcome.tombstone();
        }

        GenericRecord envelope = event.envelope();
        RegionRegistry.Region region = event.region();
        List<String> failures = new ArrayList<>();

        String op = String.valueOf(envelope.get("op"));
        GenericRecord after = (GenericRecord) envelope.get("after");
        GenericRecord before = (GenericRecord) envelope.get("before");
        boolean deleted = op.equals("d");
        GenericRecord source = switch (op) {
            case "r", "c", "u" -> after;
            case "d" -> before;
            default -> {
                failures.add("unknown_op:" + op);
                yield after != null ? after : before;
            }
        };
        if (source == null) {
            failures.add("missing_row_image");
        }
        if (event.sourceId() == null) {
            failures.add("missing_key");
        }

        // Logical values first (BigDecimal money, epoch-micros timestamps) so the
        // expectations compare numbers, not Avro byte buffers.
        Map<String, Object> values = new LinkedHashMap<>();
        values.put("_entity_key", Keys.entityKey(spec.entity(), region.code(), event.sourceId()));
        values.put("jurisdiction_id", region.jurisdictionId());
        values.put("jurisdiction_code", region.code());
        if (spec.money()) {
            values.put("currency_code", region.currencyCode());
        }
        for (Column c : spec.columns()) {
            Object value = null;
            Schema.Field field = source == null ? null : source.getSchema().getField(c.source());
            if (source != null && field == null) {
                // Schema drift: the source stopped sending a column we promised downstream
                failures.add("missing_column:" + c.source());
            } else if (field != null) {
                try {
                    value = convert(nonNull(field.schema()), source.get(c.source()), c.type());
                } catch (RuntimeException e) {
                    failures.add("bad_value:" + c.source() + ":" + e.getMessage());
                }
            }
            values.put(c.name(), value);
            if (c.fkKey() != null) {
                values.put(c.fkKey(), Keys.entityKey(c.fk(), region.code(), value));
            }
        }
        values.put("_cdc_op", op);
        GenericRecord sourceInfo = envelope.getSchema().getField("source") == null ? null : (GenericRecord) envelope.get("source");
        values.put("_source_lsn", sourceInfo == null ? null : sourceInfo.get("lsn"));
        values.put("_source_ts_ms", sourceInfo == null ? null : sourceInfo.get("ts_ms"));
        values.put("_source_topic", event.sourceTopic());
        values.put("_is_deleted", deleted);
        values.put("_processed_at", ChronoUnit.MICROS.between(Instant.EPOCH, clock.instant()));

        // A delete must reach EDM even if the row it removes would fail DQ today
        boolean drop = false;
        if (!deleted && failures.isEmpty()) {
            for (Expectation e : Expectations.violated(spec, values)) {
                switch (e.action()) {
                    case QUARANTINE -> failures.add(e.name());
                    case DROP -> drop = true;
                    case WARN -> LOG.warn("{} {} failed expectation {} ({}={})",
                            spec.entity(), values.get("_entity_key"), e.name(), e.column(), values.get(e.column()));
                }
            }
        }

        if (!failures.isEmpty()) {
            GenericRecord q = encode(quarantineSchema, values);
            q.put("_dq_failures", failures);
            q.put("_source_row_json", source == null ? null : source.toString());
            return Outcome.quarantine(q);
        }
        return drop ? Outcome.drop() : Outcome.valid(encode(edmSchema, values));
    }

    private static GenericRecord encode(Schema schema, Map<String, Object> values) {
        GenericRecord record = new GenericData.Record(schema);
        values.forEach((name, value) -> {
            if (value instanceof BigDecimal bd) {
                value = DECIMALS.toBytes(bd, TargetSchemas.MONEY, TargetSchemas.MONEY.getLogicalType());
            }
            record.put(name, value);
        });
        return record;
    }

    static Object convert(Schema sourceSchema, Object raw, EntitySpec.ColumnType type) {
        if (raw == null) {
            return null;
        }
        return switch (type) {
            case STRING -> raw.toString();
            case INT -> Math.toIntExact(((Number) raw).longValue());
            case LONG -> ((Number) raw).longValue();
            case MONEY -> money(decimal(sourceSchema, raw));
            case TIMESTAMP_UTC -> epochMicros(sourceSchema, raw);
        };
    }

    /** Widen to DECIMAL(19,4). Refuses to round: a source with scale > 4 is a contract break. */
    static BigDecimal money(BigDecimal value) {
        BigDecimal scaled = value.setScale(TargetSchemas.MONEY_SCALE); // throws if rounding needed
        if (scaled.precision() > TargetSchemas.MONEY_PRECISION) {
            throw new ArithmeticException("exceeds DECIMAL(19,4): " + value);
        }
        return scaled;
    }

    /** Debezium decimals: bytes with a scale (precise), a VariableScaleDecimal struct, a string, or a double. */
    static BigDecimal decimal(Schema schema, Object raw) {
        if (raw instanceof BigDecimal bd) {
            return bd;
        }
        if (raw instanceof ByteBuffer buf) {
            return new BigDecimal(new BigInteger(bytes(buf)), scaleOf(schema));
        }
        if (raw instanceof GenericRecord vsd) {
            return new BigDecimal(new BigInteger(bytes((ByteBuffer) vsd.get("value"))), (Integer) vsd.get("scale"));
        }
        if (raw instanceof CharSequence || raw instanceof Number) {
            return new BigDecimal(raw.toString());
        }
        throw new IllegalArgumentException("unsupported decimal encoding " + raw.getClass().getSimpleName());
    }

    private static int scaleOf(Schema schema) {
        if (schema.getLogicalType() instanceof LogicalTypes.Decimal d) {
            return d.getScale();
        }
        Object scale = schema.getObjectProp("scale");
        if (scale instanceof Number n) {
            return n.intValue();
        }
        throw new IllegalArgumentException("decimal without a scale");
    }

    /** Debezium timestamps, by the semantic type it puts in connect.name. All become UTC epoch micros. */
    static long epochMicros(Schema schema, Object raw) {
        if (raw instanceof CharSequence s) {
            // ZonedTimestamp (timestamptz): ISO-8601 with offset
            return ChronoUnit.MICROS.between(Instant.EPOCH, Instant.parse(s));
        }
        if (raw instanceof Instant i) {
            return ChronoUnit.MICROS.between(Instant.EPOCH, i);
        }
        long v = ((Number) raw).longValue();
        String connectName = schema.getProp("connect.name");
        LogicalType logical = schema.getLogicalType();
        if ("io.debezium.time.MicroTimestamp".equals(connectName) || logical instanceof LogicalTypes.TimestampMicros) {
            return v;
        }
        if ("io.debezium.time.NanoTimestamp".equals(connectName)) {
            return v / 1_000;
        }
        if ("io.debezium.time.Timestamp".equals(connectName)
                || "org.apache.kafka.connect.data.Timestamp".equals(connectName)
                || logical instanceof LogicalTypes.TimestampMillis) {
            return Math.multiplyExact(v, 1_000L);
        }
        throw new IllegalArgumentException("unknown timestamp encoding " + connectName);
    }

    private static byte[] bytes(ByteBuffer buf) {
        ByteBuffer dup = buf.duplicate();
        byte[] out = new byte[dup.remaining()];
        dup.get(out);
        return out;
    }

    static Schema nonNull(Schema schema) {
        if (schema.getType() != Schema.Type.UNION) {
            return schema;
        }
        return schema.getTypes().stream().filter(s -> s.getType() != Schema.Type.NULL).findFirst().orElse(schema);
    }
}
