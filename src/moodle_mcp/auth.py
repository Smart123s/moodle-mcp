import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from pydantic import AnyUrl

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    OAuthClientInformationFull,
    OAuthToken,
    RefreshToken,
    construct_redirect_uri,
)
from .config import (
    get_config_path,
    get_moodle_token,
    get_moodle_url,
    is_configured,
    load_config,
    normalize_moodle_url,
    save_config,
)
from .logger import logger

_ACTIVE_PROVIDER: "MoodleOAuthProvider | None" = None


def get_active_provider() -> "MoodleOAuthProvider | None":
    return _ACTIVE_PROVIDER


def set_active_provider(provider: "MoodleOAuthProvider") -> None:
    global _ACTIVE_PROVIDER
    _ACTIVE_PROVIDER = provider


def get_oauth_store_path() -> Path:
    """Return the path to the persistent OAuth store file."""
    custom_path = os.getenv("OAUTH_STORE_PATH")
    if custom_path:
        return Path(custom_path)

    config_path = get_config_path()
    return config_path.parent / "oauth_store.json"


@dataclass
class PendingSession:
    client: OAuthClientInformationFull
    params: AuthorizationParams
    created_at: float = field(default_factory=time.time)


class MoodleOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """OAuth 2.0 Authorization Server Provider for Gemini Web and MCP clients.

    Implements RFC 6749, RFC 7591 (Dynamic Client Registration), and RFC 7636 (PKCE).
    Stores registered clients, access tokens, and refresh tokens persistently.
    """

    def __init__(self, store_path: Path | None = None):
        self.store_path = store_path or get_oauth_store_path()
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.access_tokens: dict[str, AccessToken] = {}
        self.refresh_tokens: dict[str, RefreshToken] = {}
        self.auth_codes: dict[str, AuthorizationCode] = {}
        self.pending_sessions: dict[str, PendingSession] = {}
        self.token_moodle_keys: dict[str, str] = {}
        self.refresh_moodle_keys: dict[str, str] = {}
        self.code_moodle_tokens: dict[str, str] = {}
        self.token_moodle_urls: dict[str, str] = {}
        self.refresh_moodle_urls: dict[str, str] = {}
        self.code_moodle_urls: dict[str, str] = {}

        self._load_store()
        self._init_default_clients()

    def get_moodle_token_for_access_token(self, access_token: str) -> str | None:
        """Get the Moodle API key linked to a specific OAuth access token."""
        return self.token_moodle_keys.get(access_token)

    def get_moodle_url_for_access_token(self, access_token: str) -> str | None:
        """Get the Moodle base URL linked to a specific OAuth access token."""
        return self.token_moodle_urls.get(access_token)

    def _init_default_clients(self) -> None:
        """Pre-configure default clients such as Gemini if configured via env."""
        gemini_client_id = os.getenv("GEMINI_CLIENT_ID", "gemini")
        gemini_client_secret = os.getenv("GEMINI_CLIENT_SECRET")

        if gemini_client_id not in self.clients:
            # Allow common Gemini redirect URIs
            self.clients[gemini_client_id] = OAuthClientInformationFull(
                client_id=gemini_client_id,
                client_secret=gemini_client_secret,
                redirect_uris=[
                    AnyUrl("https://oauth-redirect.googleusercontent.com/r/mcp"),
                    AnyUrl("https://vertexaisearch.cloud.google.com/oauth-redirect"),
                ],
                token_endpoint_auth_method="client_secret_post" if gemini_client_secret else "none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            )

    def _load_store(self) -> None:
        """Load persistent clients and tokens from disk."""
        if not self.store_path.exists():
            return

        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for client_data in data.get("clients", []):
                try:
                    client = OAuthClientInformationFull.model_validate(client_data)
                    self.clients[client.client_id] = client
                except Exception as e:
                    logger.warning(f"Failed to load client from store: {e}")

            now = int(time.time())
            for token_data in data.get("access_tokens", []):
                try:
                    token = AccessToken.model_validate(token_data)
                    if token.expires_at is None or token.expires_at > now:
                        self.access_tokens[token.token] = token
                except Exception as e:
                    logger.warning(f"Failed to load access token from store: {e}")

            for token_data in data.get("refresh_tokens", []):
                try:
                    token = RefreshToken.model_validate(token_data)
                    if token.expires_at is None or token.expires_at > now:
                        self.refresh_tokens[token.token] = token
                except Exception as e:
                    logger.warning(f"Failed to load refresh token from store: {e}")

            self.token_moodle_keys = {
                k: v for k, v in data.get("token_moodle_keys", {}).items()
                if k in self.access_tokens
            }
            self.refresh_moodle_keys = {
                k: v for k, v in data.get("refresh_moodle_keys", {}).items()
                if k in self.refresh_tokens
            }
            self.token_moodle_urls = {
                k: v for k, v in data.get("token_moodle_urls", {}).items()
                if k in self.access_tokens
            }
            self.refresh_moodle_urls = {
                k: v for k, v in data.get("refresh_moodle_urls", {}).items()
                if k in self.refresh_tokens
            }

            logger.info(
                f"Loaded OAuth store: {len(self.clients)} clients, "
                f"{len(self.access_tokens)} active tokens."
            )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read OAuth store at {self.store_path}: {e}")

    def _save_store(self) -> None:
        """Persist registered clients and active tokens to disk."""
        now = int(time.time())

        # Clean expired tokens before saving
        valid_access = [
            t.model_dump(mode="json")
            for t in self.access_tokens.values()
            if t.expires_at is None or t.expires_at > now
        ]
        valid_refresh = [
            t.model_dump(mode="json")
            for t in self.refresh_tokens.values()
            if t.expires_at is None or t.expires_at > now
        ]
        clients_data = [c.model_dump(mode="json") for c in self.clients.values()]

        valid_access_keys = set(self.access_tokens.keys())
        valid_refresh_keys = set(self.refresh_tokens.keys())
        self.token_moodle_keys = {
            k: v for k, v in self.token_moodle_keys.items() if k in valid_access_keys
        }
        self.refresh_moodle_keys = {
            k: v for k, v in self.refresh_moodle_keys.items() if k in valid_refresh_keys
        }
        self.token_moodle_urls = {
            k: v for k, v in self.token_moodle_urls.items() if k in valid_access_keys
        }
        self.refresh_moodle_urls = {
            k: v for k, v in self.refresh_moodle_urls.items() if k in valid_refresh_keys
        }

        data = {
            "clients": clients_data,
            "access_tokens": valid_access,
            "refresh_tokens": valid_refresh,
            "token_moodle_keys": self.token_moodle_keys,
            "refresh_moodle_keys": self.refresh_moodle_keys,
            "token_moodle_urls": self.token_moodle_urls,
            "refresh_moodle_urls": self.refresh_moodle_urls,
            "updated_at": now,
        }

        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.store_path.with_suffix(".tmp")
            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                temp_path.replace(self.store_path)
            except OSError:
                with open(self.store_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
        except OSError as e:
            logger.warning(
                f"Could not persist OAuth store to {self.store_path}: {e}. "
                "Tokens and clients are held in memory. "
                "Ensure storage directory is writable (e.g., chown -R 1000:1000 ./data)."
            )
            # Attempt saving to temporary fallback directory
            try:
                fallback_path = Path("/tmp/oauth_store.json")
                with open(fallback_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except OSError:
                pass

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Retrieve client by client_id.

        Also supports dynamic fallback for Gemini OAuth redirect requests.
        """
        if client_id in self.clients:
            return self.clients[client_id]

        # Check environment variable configured client
        env_client_id = os.getenv("GEMINI_CLIENT_ID")
        if env_client_id and client_id == env_client_id:
            self._init_default_clients()
            return self.clients.get(client_id)

        return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Register a new OAuth client via Dynamic Client Registration (RFC 7591)."""
        max_clients = int(os.getenv("OAUTH_MAX_CLIENTS", "2000"))
        if client_info.client_id not in self.clients and len(self.clients) >= max_clients:
            logger.warning(
                f"Rejecting client registration: maximum clients limit reached ({max_clients})"
            )
            raise ValueError(f"Maximum number of registered OAuth clients reached ({max_clients})")
        self.clients[client_info.client_id] = client_info
        self._save_store()
        logger.info(f"Registered OAuth client: {client_info.client_id}")

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Handle /authorize and redirect to the Moodle credentials setup form."""
        session_id = secrets.token_urlsafe(32)
        self.pending_sessions[session_id] = PendingSession(
            client=client,
            params=params,
            created_at=time.time(),
        )

        # Cleanup old pending sessions (> 15 minutes old)
        now = time.time()
        expired = [sid for sid, s in self.pending_sessions.items() if now - s.created_at > 900]
        for sid in expired:
            self.pending_sessions.pop(sid, None)

        return f"/auth/setup?session_id={session_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        """Load and validate an authorization code."""
        code_obj = self.auth_codes.get(authorization_code)
        if not code_obj:
            return None

        if code_obj.client_id != client.client_id:
            return None

        if code_obj.expires_at < time.time():
            self.auth_codes.pop(authorization_code, None)
            return None

        return code_obj

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Exchange an authorization code for access and refresh tokens."""
        # Single use code: remove immediately
        self.auth_codes.pop(authorization_code.code, None)

        now = int(time.time())
        token_lifetime = int(os.getenv("ACCESS_TOKEN_LIFETIME_SECONDS", 3600))  # 1 hour
        refresh_lifetime = int(os.getenv("REFRESH_TOKEN_LIFETIME_SECONDS", 7776000))  # 90 days

        access_token_str = secrets.token_urlsafe(32)
        refresh_token_str = secrets.token_urlsafe(32)

        access_token = AccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + token_lifetime,
            resource=authorization_code.resource,
            subject=authorization_code.subject,
        )
        refresh_token = RefreshToken(
            token=refresh_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + refresh_lifetime,
            resource=authorization_code.resource,
            subject=authorization_code.subject,
        )

        self.access_tokens[access_token_str] = access_token
        self.refresh_tokens[refresh_token_str] = refresh_token

        # Link Moodle API key and URL associated with this authorization code to tokens
        moodle_token = self.code_moodle_tokens.pop(authorization_code.code, None)
        moodle_url = self.code_moodle_urls.pop(authorization_code.code, None)
        if moodle_token:
            self.token_moodle_keys[access_token_str] = moodle_token
            self.refresh_moodle_keys[refresh_token_str] = moodle_token
        if moodle_url:
            self.token_moodle_urls[access_token_str] = moodle_url
            self.refresh_moodle_urls[refresh_token_str] = moodle_url

        self._save_store()

        logger.info(f"Issued access token for client {client.client_id}")

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=token_lifetime,
            refresh_token=refresh_token_str,
            scope=" ".join(authorization_code.scopes),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Load and verify an access token."""
        token_obj = self.access_tokens.get(token)
        if not token_obj:
            return None

        if token_obj.expires_at and token_obj.expires_at < int(time.time()):
            self.access_tokens.pop(token, None)
            self.token_moodle_keys.pop(token, None)
            self.token_moodle_urls.pop(token, None)
            self._save_store()
            return None

        return token_obj

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        """Load a refresh token."""
        token_obj = self.refresh_tokens.get(refresh_token)
        if not token_obj:
            return None

        if token_obj.client_id != client.client_id:
            return None

        if token_obj.expires_at and token_obj.expires_at < int(time.time()):
            self.refresh_tokens.pop(refresh_token, None)
            self.refresh_moodle_keys.pop(refresh_token, None)
            self.refresh_moodle_urls.pop(refresh_token, None)
            self._save_store()
            return None

        return token_obj

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate refresh token and issue new access token."""
        self.refresh_tokens.pop(refresh_token.token, None)

        now = int(time.time())
        token_lifetime = int(os.getenv("ACCESS_TOKEN_LIFETIME_SECONDS", 3600))
        refresh_lifetime = int(os.getenv("REFRESH_TOKEN_LIFETIME_SECONDS", 7776000))

        new_access_str = secrets.token_urlsafe(32)
        new_refresh_str = secrets.token_urlsafe(32)

        new_access = AccessToken(
            token=new_access_str,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            expires_at=now + token_lifetime,
            resource=refresh_token.resource,
            subject=refresh_token.subject,
        )
        new_refresh = RefreshToken(
            token=new_refresh_str,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            expires_at=now + refresh_lifetime,
            resource=refresh_token.resource,
            subject=refresh_token.subject,
        )

        self.access_tokens[new_access_str] = new_access
        self.refresh_tokens[new_refresh_str] = new_refresh

        # Rotate linked Moodle token and URL to the new access and refresh tokens
        moodle_token = self.refresh_moodle_keys.pop(refresh_token.token, None)
        moodle_url = self.refresh_moodle_urls.pop(refresh_token.token, None)
        if moodle_token:
            self.token_moodle_keys[new_access_str] = moodle_token
            self.refresh_moodle_keys[new_refresh_str] = moodle_token
        if moodle_url:
            self.token_moodle_urls[new_access_str] = moodle_url
            self.refresh_moodle_urls[new_refresh_str] = moodle_url

        self._save_store()

        return OAuthToken(
            access_token=new_access_str,
            token_type="Bearer",
            expires_in=token_lifetime,
            refresh_token=new_refresh_str,
            scope=" ".join(new_access.scopes),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke an access token or refresh token."""
        if isinstance(token, AccessToken):
            self.access_tokens.pop(token.token, None)
            self.token_moodle_keys.pop(token.token, None)
            self.token_moodle_urls.pop(token.token, None)
        elif isinstance(token, RefreshToken):
            self.refresh_tokens.pop(token.token, None)
            self.refresh_moodle_keys.pop(token.token, None)
            self.refresh_moodle_urls.pop(token.token, None)
        self._save_store()
