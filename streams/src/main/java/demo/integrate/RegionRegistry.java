package demo.integrate;

import java.nio.file.Path;
import java.util.List;

/**
 * The region registry: one row per geo, driving both the source topic
 * list and the values stamped onto every integrated row.
 */
public record RegionRegistry(String topicPrefix, List<Region> regions) {

    public record Region(String code, String schema, int jurisdictionId, String currencyCode, String timezone) {}

    public static RegionRegistry load(Path file) {
        RegionRegistry registry = Yaml.read(file, RegionRegistry.class);
        if (registry.regions() == null || registry.regions().isEmpty()) {
            throw new IllegalArgumentException(file + " defines no regions");
        }
        return registry;
    }

    /** Debezium names topics {prefix}.{pg schema}.{table}. */
    public String sourceTopic(Region region, String table) {
        return topicPrefix + "." + region.schema() + "." + table;
    }
}
