# Multi-stage Dockerfile for the Programming in Goose remote playground runner.
# Builds aardappel/goose from source and serves a sandboxed HTTP API on $PORT (8080).

FROM debian:bookworm-slim AS builder

ARG GOOSE_REPO=https://github.com/aardappel/goose.git
ARG GOOSE_REF=master

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        cmake \
        g++ \
        gcc \
        git \
        ninja-build \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --branch "${GOOSE_REF}" "${GOOSE_REPO}" goose \
    && cd goose \
    && git submodule update --init --depth 1 third_party/tinycc third_party/cute_spirv third_party/subprocess || true

WORKDIR /src/goose
RUN cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --target goose -j"$(nproc)" \
    && cmake --install build --prefix /opt/goose || ( \
         mkdir -p /opt/goose/bin /opt/goose/share/goose && \
         cp build/goose /opt/goose/bin/goose && \
         cp -r stdlib /opt/goose/share/goose/stdlib && \
         (cp -r build/tcclib /opt/goose/share/goose/tcclib || true) \
       )

# --- Minimal runtime image ------------------------------------------------

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
        python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/goose/ /usr/local/

RUN useradd --create-home --shell /usr/sbin/nologin runner \
    && mkdir -p /app /tmp/goose_home \
    && chown -R runner:runner /app /tmp/goose_home

WORKDIR /app
COPY server.py /app/server.py

# Pre-warm the cached C runtime object for fast native `goose build` calls
USER runner
ENV PORT=8080 \
    HOST=0.0.0.0 \
    GOOSE_HOME=/tmp/goose_home \
    MAX_CONCURRENT=4 \
    RATE_LIMIT_PER_MIN=30 \
    RUN_TIMEOUT_SEC=6 \
    BUILD_TIMEOUT_SEC=20 \
    ALLOW_EXTERN=0

RUN printf 'fn main() { print("ok"); }\n' > /tmp/warmup.goose \
    && goose build -o /tmp/warmup /tmp/warmup.goose \
    && rm -f /tmp/warmup /tmp/warmup.goose

EXPOSE 8080
CMD ["python3", "/app/server.py"]
