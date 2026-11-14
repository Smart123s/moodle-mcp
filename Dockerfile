# Multi-arch Dockerfile for moodle-mcp (amd64 / arm64)
# Uses Astral uv for fast, reliable, reproducible dependency installations
FROM ghcr.io/astral-sh/uv:latest AS uv-bin

# Up-to-date Python 3.12 runtime
FROM python:3.12-slim

# Copy uv binaries from official Astral image
COPY --from=uv-bin /uv /uvx /bin/

# Environment configuration
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    CREDENTIALS_PATH=/data/credentials.json \
    MOODLE_CONFIG_PATH=/data/credentials.json \
    OAUTH_STORE_PATH=/data/oauth_store.json \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

# Install runtime dependencies (curl for healthcheck, ca-certificates for secure HTTPS, gosu for safe non-root privilege drop)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gosu && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user and persistent data directory
RUN groupadd -g 1000 mcpuser && \
    useradd -u 1000 -g mcpuser -m -s /bin/bash mcpuser && \
    mkdir -p /data && \
    chown -R mcpuser:mcpuser /data

WORKDIR /app

# Copy dependency specifications first for optimal Docker layer caching
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# Install the application and all dependencies with uv
RUN uv pip install --system --no-cache .

# Install entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Declare volume for persistent Moodle configuration and OAuth token storage
VOLUME ["/data"]

# Expose default HTTP port
EXPOSE 8000

# Container healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Start the MCP server via entrypoint
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
