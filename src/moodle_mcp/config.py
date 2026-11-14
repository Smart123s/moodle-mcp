import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .logger import logger
from .utils import getenv

_CACHED_CONFIG: dict[str, Any] | None = None


def get_config_path() -> Path:
    """Return the path to the credentials JSON configuration file.

    Supports CREDENTIALS_PATH and MOODLE_CONFIG_PATH environment variables.
    Defaults to /data/credentials.json in container environments, or credentials.json locally.
    """
    custom_path = os.getenv("CREDENTIALS_PATH") or os.getenv("MOODLE_CONFIG_PATH")
    if custom_path:
        return Path(custom_path)

    # In Docker / NAS environments, /data is the standard volume mount
    data_dir = Path("/data")
    if data_dir.is_dir():
        if (data_dir / "credentials.json").exists():
            return data_dir / "credentials.json"
        if (data_dir / "moodle_config.json").exists():
            return data_dir / "moodle_config.json"
        return data_dir / "credentials.json"

    if Path("credentials.json").exists():
        return Path("credentials.json")
    if Path("moodle_config.json").exists():
        return Path("moodle_config.json")
    return Path("credentials.json")


def normalize_moodle_url(url: str) -> str:
    """Normalize a Moodle base URL to point to the REST endpoint.

    Examples:
        https://moodle.example.com -> https://moodle.example.com/webservice/rest/server.php
        https://moodle.example.com/ -> https://moodle.example.com/webservice/rest/server.php
        https://moodle.example.com/webservice/rest/server.php -> unchanged
    """
    url = url.strip().rstrip("/")
    if url.endswith("/webservice/rest/server.php"):
        return url
    if url.endswith("/webservice/rest"):
        return f"{url}/server.php"
    if url.endswith("/webservice"):
        return f"{url}/rest/server.php"
    return f"{url}/webservice/rest/server.php"


def load_config(force_reload: bool = False) -> dict[str, Any] | None:
    """Load credentials from JSON file."""
    global _CACHED_CONFIG
    if _CACHED_CONFIG is not None and not force_reload:
        return _CACHED_CONFIG

    config_path = get_config_path()
    if not config_path.exists():
        # Check alternative name in the same directory
        alt_name = "moodle_config.json" if config_path.name == "credentials.json" else "credentials.json"
        alt_path = config_path.parent / alt_name
        if alt_path.exists():
            config_path = alt_path
        else:
            return None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                _CACHED_CONFIG = data
                return _CACHED_CONFIG
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read credentials from {config_path}: {e}")

    return None


def save_config(
    moodle_url: str | None = None,
    moodle_token: str | None = None,
    site_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save credentials to JSON file (credentials.json) and update cache."""
    global _CACHED_CONFIG

    # Handle both save_config(token, site_info) and save_config(url, token, site_info)
    if moodle_token is None and moodle_url is not None:
        token = moodle_url.strip()
    else:
        token = (moodle_token or "").strip()

    env_url = get_moodle_url()
    active_url = env_url or (normalize_moodle_url(moodle_url) if moodle_url else "")

    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config_data: dict[str, Any] = {
        "moodle_url": active_url,
        "moodle_token": token,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if site_info:
        config_data["site_info"] = site_info

    # Atomic write to primary file
    paths_to_write = [config_path]

    # Also keep moodle_config.json synced if primary is credentials.json (or vice-versa)
    companion_name = "moodle_config.json" if config_path.name == "credentials.json" else "credentials.json"
    companion_path = config_path.parent / companion_name
    paths_to_write.append(companion_path)

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        for path in paths_to_write:
            temp_path = path.with_suffix(".tmp")
            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(config_data, f, indent=2, ensure_ascii=False)
                temp_path.replace(path)
            except OSError:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(config_data, f, indent=2, ensure_ascii=False)
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
        logger.info(f"Credentials successfully saved to {config_path}")
    except OSError as e:
        logger.warning(
            f"Could not persist credentials to {config_path}: {e}. "
            "Credentials cached in memory for current session."
        )
        try:
            fallback_cfg = Path("/tmp/credentials.json")
            with open(fallback_cfg, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)
        except OSError:
            pass

    _CACHED_CONFIG = config_data
    return config_data


def get_moodle_url() -> str | None:
    """Get active Moodle URL strictly from environment (.env)."""
    env_url = getenv("MOODLE_URL")
    return normalize_moodle_url(env_url) if env_url else None


def get_moodle_token() -> str | None:
    """Get active Moodle token from credentials JSON, falling back to MOODLE_TOKEN env var."""
    cfg = load_config()
    if cfg and cfg.get("moodle_token"):
        return cfg["moodle_token"]
    return getenv("MOODLE_TOKEN")


def is_configured() -> bool:
    """Check if Moodle URL and token are configured."""
    return bool(get_moodle_url() and get_moodle_token())
