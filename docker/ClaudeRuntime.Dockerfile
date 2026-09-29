ARG BASE_IMAGE=claude-eval-runtime:claude-2.1.269
FROM ${BASE_IMAGE}

USER root
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       build-essential ca-certificates cmake curl file jq lsof netcat-openbsd \
       pkg-config procps psmisc python3-dev python3-pip python3-venv \
       sqlite3 tree unzip zip \
    && rm -rf /var/lib/apt/lists/*

ARG TARGETARCH
ARG GO_VERSION=1.27.1
RUN case "$TARGETARCH" in \
        arm64) go_sha256="3450b45a3f9ee8568792736a5c5e70a1f2e9b36c35a8f74958c03e51d7d92bec" ;; \
        amd64) go_sha256="63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445" ;; \
        *) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" -o /tmp/go.tar.gz \
    && echo "${go_sha256}  /tmp/go.tar.gz" | sha256sum --check - \
    && rm -rf /usr/local/go \
    && tar -C /usr/local -xzf /tmp/go.tar.gz \
    && rm /tmp/go.tar.gz \
    && ln -sf /usr/local/go/bin/go /usr/local/bin/go \
    && ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt

ENV PATH="/usr/local/go/bin:${PATH}"

RUN corepack enable \
    && npm install --global --force \
       pnpm@12.4.2 tsx@4.23.13 typescript@7.0.2 vite@8.3.0 vitest@5.0.1 \
    && npm cache clean --force

USER node
