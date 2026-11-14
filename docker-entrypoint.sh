#!/bin/sh
set -e

# Ensure /data volume is writable before launching
if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    chown -R mcpuser:mcpuser /data 2>/dev/null || chmod -R 777 /data 2>/dev/null || true
    exec gosu mcpuser python -m moodle_mcp.server "$@"
fi

exec python -m moodle_mcp.server "$@"
