package demo.integrate;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class EntitySpecTest {

    @Test
    void loadsEveryShippedSpec() {
        assertThat(EntitySpec.loadAll(Path.of("config/entities")))
                .extracting(EntitySpec::entity)
                .containsExactly("customer", "order", "order_line", "product");
    }

    @Test
    void refusesToSelectAnExcludedPiiColumn(@TempDir Path dir) throws IOException {
        Path file = dir.resolve("bad.yaml");
        Files.writeString(file, """
                entity: customer
                source_table: users
                key: id
                pii_exclude: [email]
                columns:
                  - { source: id, type: long }
                  - { source: email, type: string }
                """);

        assertThatThrownBy(() -> EntitySpec.load(file)).hasMessageContaining("email is both selected and in pii_exclude");
    }

    @Test
    void refusesAnExpectationOnAnUnknownColumn(@TempDir Path dir) throws IOException {
        Path file = dir.resolve("bad.yaml");
        Files.writeString(file, """
                entity: order
                source_table: orders
                key: id
                columns:
                  - { source: id, name: order_id, type: long }
                expectations:
                  - { name: x, kind: not_null, column: id, action: warn }
                """);

        // Expectations see output names, so "id" no longer exists after the rename
        assertThatThrownBy(() -> EntitySpec.load(file)).hasMessageContaining("unknown column id");
    }

    @Test
    void keysAreDeterministicAndRegionScoped() {
        assertThat(Keys.entityKey("order", "AU", 1)).isEqualTo(Keys.entityKey("order", "AU", 1L)).hasSize(64);
        assertThat(Keys.entityKey("order", "AU", 1)).isNotEqualTo(Keys.entityKey("order", "UK", 1));
        assertThat(Keys.entityKey("order", "AU", null)).isNull();
    }
}
