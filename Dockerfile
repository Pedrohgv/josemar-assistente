# Stage 1: Build gogcli
FROM golang:1.25.8 AS gogcli-builder

WORKDIR /build
RUN git clone --branch feat/sheets-table-manipulation \
    https://github.com/Pedrohgv/gogcli.git . && \
    make build

# Stage 2: Final image
FROM ghcr.io/openclaw/openclaw:latest

# Switch to root user to install packages
USER root

# Install Python and PDF processing dependencies
RUN apt-get update && apt-get install -y python3 python3-pip \
    && pip3 install --no-cache-dir --break-system-packages pymupdf \
    && rm -rf /var/lib/apt/lists/*

# Copy gogcli binary from builder
COPY --from=gogcli-builder /build/bin/gog /usr/local/bin/gog
RUN chmod +x /usr/local/bin/gog

# Create skills directories (skills deployed via volume mount + entrypoint)
RUN mkdir -p /root/.openclaw/skills /root/.openclaw/repo-skills

# Copy entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/

# Make entrypoint executable
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Set entrypoint
ENTRYPOINT ["docker-entrypoint.sh"]

# Set working directory
WORKDIR /app

# Default command (OpenClaw gateway)
CMD ["openclaw", "gateway", "--allow-unconfigured"]
