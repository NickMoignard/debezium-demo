FROM gradle:8.14.3-jdk21 AS build
WORKDIR /src
COPY settings.gradle build.gradle ./
# Resolve dependencies in their own layer so code edits don't re-download them
RUN gradle dependencies --no-daemon -q > /dev/null
COPY src src
RUN gradle installDist --no-daemon -q -x test

FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /src/build/install/region-integration ./
# Baked-in defaults; docker-compose mounts ./config over this so registry and
# entity edits only need a restart
COPY config config
ENV CONFIG_DIR=/app/config
ENTRYPOINT ["/app/bin/region-integration"]
