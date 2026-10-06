package demo.integrate;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.List;
import java.util.Objects;
import java.util.Set;
import java.util.stream.Stream;

/**
 * One integrated entity, read from config/entities/{entity}.yaml.
 *
 * Only listed columns reach the output; anything else on the source row is
 * ignored. A listed column missing from the source is a quarantine, not a null.
 */
public record EntitySpec(
        String entity,
        String sourceTable,
        String key,
        boolean money,
        List<Column> columns,
        List<String> piiExclude,
        List<Expectation> expectations) {

    public enum ColumnType { STRING, INT, LONG, MONEY, TIMESTAMP_UTC }

    public enum Kind { NOT_NULL, IN_SET, GT, GTE }

    public enum Action { WARN, DROP, QUARANTINE }

    /** {@code name} defaults to {@code source}; {@code fk} + {@code fkKey} mint a null-guarded FK hash. */
    public record Column(String source, String name, ColumnType type, String fk, String fkKey) {
        public String name() {
            return name != null ? name : source;
        }
    }

    public record Expectation(String name, Kind kind, String column, Action action, List<String> values, BigDecimal value) {}

    public EntitySpec {
        piiExclude = piiExclude == null ? List.of() : piiExclude;
        expectations = expectations == null ? List.of() : expectations;
    }

    public String edmTopic() {
        return "edm." + entity;
    }

    public String quarantineTopic() {
        return "edm." + entity + ".quarantine";
    }

    public static List<EntitySpec> loadAll(Path dir) {
        try (Stream<Path> files = Files.list(dir)) {
            List<EntitySpec> specs = files
                    .filter(f -> f.toString().endsWith(".yaml"))
                    .sorted()
                    .map(EntitySpec::load)
                    .toList();
            if (specs.isEmpty()) {
                throw new IllegalArgumentException("No entity specs in " + dir);
            }
            return specs;
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    public static EntitySpec load(Path file) {
        EntitySpec spec = Yaml.read(file, EntitySpec.class);
        spec.validate(file);
        return spec;
    }

    private void validate(Path file) {
        Objects.requireNonNull(entity, file + ": entity");
        Objects.requireNonNull(sourceTable, file + ": source_table");
        Objects.requireNonNull(key, file + ": key");
        if (columns == null || columns.isEmpty()) {
            throw new IllegalArgumentException(file + ": no columns");
        }
        Set<String> sources = new HashSet<>();
        Set<String> outputs = new HashSet<>();
        for (Column c : columns) {
            Objects.requireNonNull(c.source(), file + ": column without source");
            Objects.requireNonNull(c.type(), file + ": " + c.source() + " has no type");
            if (piiExclude.contains(c.source())) {
                throw new IllegalArgumentException(file + ": " + c.source() + " is both selected and in pii_exclude");
            }
            if ((c.fk() == null) != (c.fkKey() == null)) {
                throw new IllegalArgumentException(file + ": " + c.source() + " needs both fk and fk_key");
            }
            if (!sources.add(c.source()) || !outputs.add(c.name()) || (c.fkKey() != null && !outputs.add(c.fkKey()))) {
                throw new IllegalArgumentException(file + ": duplicate column " + c.source());
            }
        }
        if (!sources.contains(key)) {
            throw new IllegalArgumentException(file + ": key " + key + " is not a selected column");
        }
        for (Expectation e : expectations) {
            if (!outputs.contains(e.column())) {
                throw new IllegalArgumentException(file + ": expectation " + e.name() + " references unknown column " + e.column());
            }
            boolean needsValue = e.kind() == Kind.GT || e.kind() == Kind.GTE;
            if (needsValue && e.value() == null || e.kind() == Kind.IN_SET && e.values() == null) {
                throw new IllegalArgumentException(file + ": expectation " + e.name() + " is missing its value(s)");
            }
        }
    }
}
