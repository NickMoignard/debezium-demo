package demo.integrate;

import demo.integrate.EntitySpec.Expectation;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;

/**
 * DQ expectations, evaluated on the typed, renamed row. A null fails every
 * kind: in_set, gt and gte have nothing to compare, and we fail closed rather
 * than follow SQL's "unknown passes a CHECK" rule.
 */
final class Expectations {
    private Expectations() {}

    static List<Expectation> violated(EntitySpec spec, Map<String, Object> row) {
        return spec.expectations().stream().filter(e -> !passes(e, row.get(e.column()))).toList();
    }

    private static boolean passes(Expectation e, Object value) {
        if (value == null) {
            return false;
        }
        return switch (e.kind()) {
            case NOT_NULL -> true;
            case IN_SET -> e.values().contains(value.toString());
            case GT -> numeric(value).compareTo(e.value()) > 0;
            case GTE -> numeric(value).compareTo(e.value()) >= 0;
        };
    }

    private static BigDecimal numeric(Object value) {
        if (value instanceof BigDecimal bd) {
            return bd;
        }
        if (value instanceof Number n) {
            return BigDecimal.valueOf(n.longValue());
        }
        throw new IllegalArgumentException("Not numeric: " + value);
    }
}
