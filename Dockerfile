# syntax=docker/dockerfile:1
FROM rust:1-slim-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked

FROM debian:bookworm-slim
# git: knowledge history and optional push; ca-certificates: https remotes and the optional model API
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --create-home --uid 10001 jbrain \
 && mkdir /data && chown jbrain /data
COPY --from=build /src/target/release/jbrain /usr/local/bin/jbrain
USER jbrain
ENV JBRAIN_DATA_DIR=/data JBRAIN_HOST=0.0.0.0 JBRAIN_PORT=7331
VOLUME /data
EXPOSE 7331
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD ["jbrain", "health"]
CMD ["jbrain", "serve"]
