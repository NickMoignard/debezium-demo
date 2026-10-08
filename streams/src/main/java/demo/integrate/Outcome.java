package demo.integrate;

import org.apache.avro.generic.GenericRecord;

/** Where a transformed event goes: the EDM topic, quarantine, nowhere, or a tombstone. */
public record Outcome(Route route, GenericRecord record) {

    public enum Route { VALID, QUARANTINE, DROP, TOMBSTONE }

    static Outcome valid(GenericRecord record) {
        return new Outcome(Route.VALID, record);
    }

    static Outcome quarantine(GenericRecord record) {
        return new Outcome(Route.QUARANTINE, record);
    }

    static Outcome drop() {
        return new Outcome(Route.DROP, null);
    }

    static Outcome tombstone() {
        return new Outcome(Route.TOMBSTONE, null);
    }
}
