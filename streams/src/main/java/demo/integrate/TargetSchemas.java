package demo.integrate;

import org.apache.avro.JsonProperties;
import org.apache.avro.LogicalTypes;
import org.apache.avro.Schema;

import java.util.ArrayList;
import java.util.List;

/**
 * The output contract for an entity, as an Avro schema registered in Schema
 * Registry. Business columns are nullable so a quarantined row with gaps can
 * still be written; the integration columns are always present.
 */
public final class TargetSchemas {
    static final String NAMESPACE = "demo.edm";
    public static final int MONEY_PRECISION = 19;
    public static final int MONEY_SCALE = 4;

    static final Schema MONEY = LogicalTypes.decimal(MONEY_PRECISION, MONEY_SCALE).addToSchema(Schema.create(Schema.Type.BYTES));
    static final Schema TIMESTAMP = LogicalTypes.timestampMicros().addToSchema(Schema.create(Schema.Type.LONG));

    private TargetSchemas() {}

    public static Schema edm(EntitySpec spec) {
        return Schema.createRecord(spec.entity(), "Integrated " + spec.entity() + " (all regions)", NAMESPACE, false, fields(spec));
    }

    /** The EDM row plus why it was rejected and the source row as JSON. */
    public static Schema quarantine(EntitySpec spec) {
        List<Schema.Field> fields = fields(spec);
        fields.add(new Schema.Field("_dq_failures", Schema.createArray(Schema.create(Schema.Type.STRING)),
                "Expectations or contract checks this row failed"));
        fields.add(nullable("_source_row_json", Schema.create(Schema.Type.STRING), "Source row as received"));
        return Schema.createRecord(spec.entity() + "_quarantine", "Rows of " + spec.entity() + " that failed DQ", NAMESPACE, false, fields);
    }

    private static List<Schema.Field> fields(EntitySpec spec) {
        List<Schema.Field> fields = new ArrayList<>();
        fields.add(nullable("_entity_key", Schema.create(Schema.Type.STRING), "sha256(entity:region:source_id)"));
        fields.add(new Schema.Field("jurisdiction_id", Schema.create(Schema.Type.INT), "From the region registry"));
        fields.add(new Schema.Field("jurisdiction_code", Schema.create(Schema.Type.STRING), "From the region registry"));
        if (spec.money()) {
            fields.add(new Schema.Field("currency_code", Schema.create(Schema.Type.STRING), "Native currency, from the region registry"));
        }
        for (EntitySpec.Column c : spec.columns()) {
            fields.add(nullable(c.name(), avroType(c.type()), "source: " + spec.sourceTable() + "." + c.source()));
            if (c.fkKey() != null) {
                fields.add(nullable(c.fkKey(), Schema.create(Schema.Type.STRING), "sha256(" + c.fk() + ":region:" + c.name() + ")"));
            }
        }
        fields.add(new Schema.Field("_cdc_op", Schema.create(Schema.Type.STRING), "Debezium op: r, c, u, d"));
        fields.add(nullable("_source_lsn", Schema.create(Schema.Type.LONG), "Postgres WAL position of the change"));
        fields.add(nullable("_source_ts_ms", Schema.create(Schema.Type.LONG), "Commit time in the source database"));
        fields.add(new Schema.Field("_source_topic", Schema.create(Schema.Type.STRING), "Regional CDC topic the row came from"));
        fields.add(new Schema.Field("_is_deleted", Schema.create(Schema.Type.BOOLEAN), "True for a delete; the row is the before image"));
        fields.add(new Schema.Field("_processed_at", TIMESTAMP, "When the stream processed the row"));
        return fields;
    }

    private static Schema avroType(EntitySpec.ColumnType type) {
        return switch (type) {
            case STRING -> Schema.create(Schema.Type.STRING);
            case INT -> Schema.create(Schema.Type.INT);
            case LONG -> Schema.create(Schema.Type.LONG);
            case MONEY -> MONEY;
            case TIMESTAMP_UTC -> TIMESTAMP;
        };
    }

    private static Schema.Field nullable(String name, Schema type, String doc) {
        return new Schema.Field(name, Schema.createUnion(Schema.create(Schema.Type.NULL), type), doc, JsonProperties.NULL_VALUE);
    }
}
