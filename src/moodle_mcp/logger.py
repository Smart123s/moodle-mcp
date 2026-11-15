import logging


class HealthCheckFilter(logging.Filter):
    """Filter out noisy healthcheck endpoint access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Check uvicorn access log tuple args: (client_addr, method, full_path, http_version, status_code)
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            path = record.args[2]
            if isinstance(path, str) and (
                path == "/health"
                or path.startswith("/health?")
                or path.startswith("/health/")
            ):
                return False
        # Fallback check on formatted message
        try:
            msg = record.getMessage()
            return "/health" not in msg
        except Exception:
            return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s:%(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("moodle-mcp")

# Apply health check filter to uvicorn access logger
health_filter = HealthCheckFilter()
uvicorn_access = logging.getLogger("uvicorn.access")
uvicorn_access.addFilter(health_filter)
for handler in uvicorn_access.handlers:
    handler.addFilter(health_filter)
