# Stage 1: Dependency management with Gradle
FROM gradle:8.11.1-jdk21 AS deps
WORKDIR /app
COPY build.gradle gradle.properties ./
COPY *.jar ./
RUN gradle prepareConnectDependencies --no-daemon

RUN echo "Dependencies prepared:" && \
    find /app -name "*.jar" -o -name "*.tar.gz" | head -20 && \
    echo "Connect lib:" && ls -la /app/connect-lib/ || echo "No connect-lib found" && \
    echo "Debezium connectors:" && ls -la /app/debezium-connectors/ || echo "No debezium-connectors found" && \
    echo "Custom JARs:" && ls -la /app/custom-jars/ || echo "No custom-jars found"

# Stage 2: Runtime image with Debezium Connect
FROM quay.io/debezium/connect:3.0 AS runtime

USER root

# Install necessary tools
RUN microdnf install -y wget tar gzip && \
    microdnf clean all

# Create necessary directories
RUN mkdir -p /kafka/config \
             /usr/share/confluent-hub/lib \
             /usr/share/confluent-hub/bin \
             /usr/share/confluent-hub-components

# Create minimal connect-distributed.properties if it doesn't exist
RUN touch /kafka/config/connect-distributed.properties

# Copy dependencies from the deps stage
COPY --from=deps /app/connect-lib/* /usr/share/confluent-hub/lib/
COPY --from=deps /app/custom-jars/* /kafka/connect/debezium-connector-postgres

RUN wget http://client.hub.confluent.io/confluent-hub-client-latest.tar.gz \ 
    && tar -xzf confluent-hub-client-latest.tar.gz -C /usr/share/confluent-hub \
    && rm confluent-hub-client-latest.tar.gz \
    && ln -s /usr/share/confluent-hub/bin/confluent-hub /usr/local/bin/confluent-hub

# # Make confluent-hub executable and create symlink
# RUN chmod +x /usr/share/confluent-hub/bin/confluent-hub && \
#     ln -sf /usr/share/confluent-hub/bin/confluent-hub /usr/local/bin/confluent-hub

# Install Confluent components
RUN confluent-hub install --no-prompt \
    --component-dir /usr/share/confluent-hub/lib \
    --worker-configs /kafka/config/connect-distributed.properties \
    confluentinc/kafka-connect-avro-converter:latest

# Set proper permissions for the kafka user
RUN chown -R kafka:kafka /usr/share/confluent-hub/lib \
                        /usr/share/confluent-hub-components \
                        /kafka/config

# Set the plugin path to include all component directories
ENV CONNECT_PLUGIN_PATH="/usr/share/java,/usr/share/confluent-hub/lib,/usr/share/confluent-hub-components,/usr/share/confluent-hub/lib/debezium-jdbc-connector"

# Verify final setup
RUN echo "Final plugin path contents:" && \
    echo "=== /usr/share/java ===" && \
    ls -la /usr/share/java/ | head -10 && \
    echo "=== /usr/share/confluent-hub/lib ===" && \
    ls -la /usr/share/confluent-hub/lib/ | head -10 && \
    echo "=== /usr/share/confluent-hub-components ===" && \
    ls -la /usr/share/confluent-hub-components/ | head -10 && \
    echo "=== Custom JARs in confluent-hub-components ===" && \
    find /usr/share/confluent-hub-components -name "*.jar" | head -10

USER kafka