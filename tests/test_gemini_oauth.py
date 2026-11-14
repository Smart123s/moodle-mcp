import json
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from pydantic import AnyUrl
from starlette.testclient import TestClient

from moodle_mcp import config
from moodle_mcp.auth import MoodleOAuthProvider
from moodle_mcp.server import configure_http_server, mcp
from mcp.server.auth.provider import AuthorizationParams, OAuthClientInformationFull, ProviderTokenVerifier


def test_normalize_moodle_url():
    assert config.normalize_moodle_url("https://moodle.example.com") == "https://moodle.example.com/webservice/rest/server.php"
    assert config.normalize_moodle_url("https://moodle.example.com/") == "https://moodle.example.com/webservice/rest/server.php"
    assert config.normalize_moodle_url("https://moodle.example.com/webservice/rest") == "https://moodle.example.com/webservice/rest/server.php"
    assert config.normalize_moodle_url("https://moodle.example.com/webservice/rest/server.php") == "https://moodle.example.com/webservice/rest/server.php"


def test_config_save_and_load(tmp_path, monkeypatch):
    cfg_file = tmp_path / "credentials.json"
    monkeypatch.setenv("CREDENTIALS_PATH", str(cfg_file))
    monkeypatch.setenv("MOODLE_URL", "https://moodle.university.edu")
    config._CACHED_CONFIG = None

    assert not config.is_configured()

    saved = config.save_config(
        moodle_token="token123",
        site_info={"sitename": "Uni Moodle", "username": "student"},
    )

    assert saved["moodle_token"] == "token123"
    assert saved["moodle_url"] == "https://moodle.university.edu/webservice/rest/server.php"
    assert cfg_file.exists()

    loaded = config.load_config(force_reload=True)
    assert loaded["moodle_token"] == "token123"
    assert config.is_configured()
    assert config.get_moodle_token() == "token123"
    assert config.get_moodle_url() == "https://moodle.university.edu/webservice/rest/server.php"

    # Verify companion moodle_config.json is also synced
    companion = tmp_path / "moodle_config.json"
    assert companion.exists()


@pytest.mark.anyio
async def test_oauth_provider_flow(tmp_path):
    store_file = tmp_path / "oauth_store.json"
    provider = MoodleOAuthProvider(store_path=store_file)

    # 1. Register Client
    client = OAuthClientInformationFull(
        client_id="gemini-client-1",
        client_secret="gemini-secret-1",
        redirect_uris=[AnyUrl("https://oauth-redirect.googleusercontent.com/r/mcp")],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    await provider.register_client(client)
    retrieved = await provider.get_client("gemini-client-1")
    assert retrieved is not None
    assert retrieved.client_id == "gemini-client-1"

    # 2. Authorize
    params = AuthorizationParams(
        state="xyz123",
        scopes=["mcp"],
        code_challenge="dummy-challenge",
        redirect_uri=AnyUrl("https://oauth-redirect.googleusercontent.com/r/mcp"),
        redirect_uri_provided_explicitly=True,
    )
    redirect_target = await provider.authorize(client, params)
    assert redirect_target.startswith("/auth/setup?session_id=")
    session_id = redirect_target.split("session_id=")[1]
    assert session_id in provider.pending_sessions

    # 3. Simulate code generation
    from mcp.server.auth.provider import AuthorizationCode
    auth_code = AuthorizationCode(
        code="auth-code-123",
        scopes=["mcp"],
        expires_at=time.time() + 300,
        client_id=client.client_id,
        code_challenge="dummy-challenge",
        redirect_uri=params.redirect_uri,
        redirect_uri_provided_explicitly=True,
    )
    provider.auth_codes["auth-code-123"] = auth_code

    loaded_code = await provider.load_authorization_code(client, "auth-code-123")
    assert loaded_code is not None

    # 4. Exchange code for tokens
    tokens = await provider.exchange_authorization_code(client, loaded_code)
    assert tokens.access_token is not None
    assert tokens.token_type == "Bearer"

    # 5. Verify token with ProviderTokenVerifier
    verifier = ProviderTokenVerifier(provider)
    access_token_obj = await verifier.verify_token(tokens.access_token)
    assert access_token_obj is not None
    assert access_token_obj.client_id == "gemini-client-1"

    # 6. Verify non-existent token fails
    assert await verifier.verify_token("invalid-token") is None


def test_discovery_and_oauth_endpoints(tmp_path, monkeypatch):
    cfg_file = tmp_path / "credentials.json"
    oauth_file = tmp_path / "oauth_store.json"
    monkeypatch.setenv("CREDENTIALS_PATH", str(cfg_file))
    monkeypatch.setenv("OAUTH_STORE_PATH", str(oauth_file))
    config._CACHED_CONFIG = None

    public_url = "https://mcp.nas.example.com"
    provider = configure_http_server(mcp, public_url=public_url)
    app = mcp.streamable_http_app(host="127.0.0.1")
    client = TestClient(app)

    # 1. Discovery Metadata: RFC 8414 OAuth Authorization Server
    r_oauth_meta = client.get("/.well-known/oauth-authorization-server")
    assert r_oauth_meta.status_code == 200
    oauth_meta = r_oauth_meta.json()
    assert oauth_meta["issuer"] == public_url
    assert "S256" in oauth_meta["code_challenge_methods_supported"]
    assert "authorization_code" in oauth_meta["grant_types_supported"]

    # 2. Discovery Metadata: OpenID Configuration
    r_oidc_meta = client.get("/.well-known/openid-configuration")
    assert r_oidc_meta.status_code == 200
    oidc_meta = r_oidc_meta.json()
    assert oidc_meta["issuer"] == public_url
    assert oidc_meta["authorization_endpoint"] == f"{public_url}/oauth/authorize"
    assert oidc_meta["token_endpoint"] == f"{public_url}/oauth/token"
    assert "S256" in oidc_meta["code_challenge_methods_supported"]

    # 3. OAuth Authorization Endpoints (/authorize and /oauth/authorize)
    # When accessed without params, returns 400 invalid_request (confirming handler is listening)
    r_auth1 = client.get("/authorize")
    assert r_auth1.status_code == 400
    r_auth2 = client.get("/oauth/authorize")
    assert r_auth2.status_code == 400

    # 4. OAuth Token Endpoints (/token and /oauth/token)
    # When accessed without credentials, returns 401 Unauthorized
    r_token1 = client.post("/token")
    assert r_token1.status_code == 401
    r_token2 = client.post("/oauth/token")
    assert r_token2.status_code == 401

    # 5. Setup UI: verify NO password field exists
    r_setup = client.get("/auth/setup")
    assert r_setup.status_code == 200
    assert "setup_password" not in r_setup.text
    assert "Administrator Password" not in r_setup.text

    # 6. Protected resource /mcp requires Bearer auth
    r_mcp_unauth = client.get("/mcp")
    assert r_mcp_unauth.status_code == 401
    assert "WWW-Authenticate" in r_mcp_unauth.headers


def test_setup_submission_with_oauth_session(tmp_path, monkeypatch):
    cfg_file = tmp_path / "credentials.json"
    oauth_file = tmp_path / "oauth_store.json"
    monkeypatch.setenv("CREDENTIALS_PATH", str(cfg_file))
    monkeypatch.setenv("OAUTH_STORE_PATH", str(oauth_file))
    monkeypatch.setenv("MOODLE_URL", "https://moodle.myuni.edu")
    config._CACHED_CONFIG = None

    provider = configure_http_server(mcp, public_url="http://127.0.0.1:8000")
    app = mcp.streamable_http_app(host="127.0.0.1")
    client = TestClient(app)

    oauth_client = OAuthClientInformationFull(
        client_id="gemini",
        redirect_uris=[AnyUrl("https://oauth-redirect.googleusercontent.com/r/mcp-test")],
        grant_types=["authorization_code"],
        response_types=["code"],
    )
    provider.clients["gemini"] = oauth_client

    import anyio
    session_url = anyio.run(
        provider.authorize,
        oauth_client,
        AuthorizationParams(
            state="state123",
            scopes=["mcp"],
            code_challenge="challenge",
            redirect_uri=AnyUrl("https://oauth-redirect.googleusercontent.com/r/mcp-test"),
            redirect_uri_provided_explicitly=True,
        ),
    )
    session_id = session_url.split("session_id=")[1]

    # Mock Moodle API call
    mock_response = Mock(status_code=200)
    mock_response.json.return_value = {
        "sitename": "Test University Moodle",
        "username": "prof_smith",
        "fullname": "Professor Smith",
    }

    # SEC-06: Moodle authentication uses POST, and URL is read strictly from .env (SEC-02)
    with patch("moodle_mcp.web.requests.post", return_value=mock_response):
        post_rsp = client.post(
            "/auth/setup",
            data={
                "session_id": session_id,
                "moodle_token": "secret-token-456",
            },
            follow_redirects=False,
        )

    # Must redirect back to Gemini redirect URI with code and state
    assert post_rsp.status_code == 302
    redirect_location = post_rsp.headers["location"]
    assert redirect_location.startswith("https://oauth-redirect.googleusercontent.com/r/mcp-test")
    assert "code=" in redirect_location
    assert "state=state123" in redirect_location

    code = redirect_location.split("code=")[1].split("&")[0]
    assert provider.code_moodle_tokens.get(code) == "secret-token-456"

    # Verify code exchange links the Moodle API key to the OAuth access token
    loaded_code = anyio.run(provider.load_authorization_code, oauth_client, code)
    tokens = anyio.run(provider.exchange_authorization_code, oauth_client, loaded_code)
    assert provider.get_moodle_token_for_access_token(tokens.access_token) == "secret-token-456"

    # Must have saved to credentials.json
    assert cfg_file.exists()
    with open(cfg_file, "r", encoding="utf-8") as f:
        saved_data = json.load(f)
    assert saved_data["moodle_token"] == "secret-token-456"
    assert saved_data["moodle_url"] == "https://moodle.myuni.edu/webservice/rest/server.php"
    assert saved_data["site_info"]["sitename"] == "Test University Moodle"


def test_setup_rejected_without_valid_session(tmp_path, monkeypatch):
    """SEC-01: Direct unauthenticated POSTs without valid session_id are rejected."""
    monkeypatch.setenv("MOODLE_URL", "https://moodle.myuni.edu")
    provider = configure_http_server(mcp, public_url="http://127.0.0.1:8000")
    app = mcp.streamable_http_app(host="127.0.0.1")
    client = TestClient(app)

    # Missing session_id
    r1 = client.post("/auth/setup", data={"moodle_token": "token123"})
    assert r1.status_code == 200
    assert "Invalid or expired OAuth session" in r1.text

    # Forged session_id
    r2 = client.post("/auth/setup", data={"session_id": "fake-session", "moodle_token": "token123"})
    assert r2.status_code == 200
    assert "Invalid or expired OAuth session" in r2.text


@pytest.mark.anyio
async def test_dynamic_client_registration_limit(tmp_path, monkeypatch):
    """SEC-07: Test client registration limit (configurable via OAUTH_MAX_CLIENTS)."""
    monkeypatch.setenv("OAUTH_MAX_CLIENTS", "2")
    store_file = tmp_path / "oauth_store.json"
    provider = MoodleOAuthProvider(store_path=store_file)

    # Default client 'gemini' takes 1 slot
    c2 = OAuthClientInformationFull(
        client_id="client-2",
        redirect_uris=[AnyUrl("https://example.com/callback")],
    )
    await provider.register_client(c2)

    # 3rd client exceeds limit of 2
    c3 = OAuthClientInformationFull(
        client_id="client-3",
        redirect_uris=[AnyUrl("https://example.com/callback")],
    )
    with pytest.raises(ValueError, match="Maximum number of registered OAuth clients reached"):
        await provider.register_client(c3)


def test_downloads_output_dir_confinement():
    """SEC-09: Unbounded output_dir is blocked from system paths."""
    from moodle_mcp.downloads import _resolve_output_dir
    with pytest.raises(ValueError, match="output_dir"):
        _resolve_output_dir("/etc/malicious", 42)


def test_server_main_stdio(monkeypatch):
    from moodle_mcp import server
    monkeypatch.setattr("sys.argv", ["moodle-mcp", "--transport", "stdio"])
    with patch.object(server.mcp, "run") as mock_run:
        server.main()
        mock_run.assert_called_once_with(transport="stdio")


def test_server_main_streamable_http(monkeypatch):
    from moodle_mcp import server
    monkeypatch.setattr(
        "sys.argv",
        ["moodle-mcp", "--transport", "streamable-http", "--host", "127.0.0.1", "--port", "8000", "--public-url", "https://example.com"],
    )
    with patch.object(server.mcp, "run") as mock_run:
        server.main()
        mock_run.assert_called_once()
        kwargs = mock_run.call_args.kwargs
        assert kwargs["transport"] == "streamable-http"
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 8000


def test_module_execution():
    import os
    import subprocess
    import sys
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent.parent / "src"))
    for mod in ["moodle_mcp.server", "moodle_mcp"]:
        result = subprocess.run(
            [sys.executable, "-m", mod, "--help"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        assert "Moodle MCP Server" in result.stdout


def test_dynamic_client_registration_endpoints(tmp_path, monkeypatch):
    cfg_file = tmp_path / "credentials.json"
    oauth_file = tmp_path / "oauth_store.json"
    monkeypatch.setenv("CREDENTIALS_PATH", str(cfg_file))
    monkeypatch.setenv("OAUTH_STORE_PATH", str(oauth_file))
    config._CACHED_CONFIG = None

    public_url = "https://mcp.nas.example.com"
    configure_http_server(mcp, public_url=public_url)
    app = mcp.streamable_http_app(host="127.0.0.1")
    client = TestClient(app)

    reg_payload = {
        "client_name": "Gemini Web",
        "redirect_uris": ["https://oauth-redirect.googleusercontent.com/r/mcp"],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }

    # 1. Standard RFC 7591 endpoint /register
    r1 = client.post("/register", json=reg_payload)
    assert r1.status_code == 201
    d1 = r1.json()
    assert "client_id" in d1
    assert "client_secret" in d1
    assert d1["client_name"] == "Gemini Web"

    # 2. Alias endpoint /oauth/register
    r2 = client.post("/oauth/register", json=reg_payload)
    assert r2.status_code == 201
    d2 = r2.json()
    assert "client_id" in d2
    assert d2["client_id"] != d1["client_id"]


def test_storage_resilience_on_unwritable_disk(tmp_path, monkeypatch):
    """Verify that when disk storage is unwritable (e.g., Docker volume permission error),
    registration and config saving gracefully continue in memory instead of crashing with 500."""
    cfg_file = tmp_path / "credentials.json"
    oauth_file = tmp_path / "unwritable" / "oauth_store.json"
    monkeypatch.setenv("CREDENTIALS_PATH", str(cfg_file))
    monkeypatch.setenv("OAUTH_STORE_PATH", str(oauth_file))
    config._CACHED_CONFIG = None

    public_url = "https://mcp.nas.example.com"
    provider = configure_http_server(mcp, public_url=public_url)
    app = mcp.streamable_http_app(host="127.0.0.1")
    client = TestClient(app)

    reg_payload = {
        "client_name": "Gemini Web",
        "redirect_uris": ["https://oauth-redirect.googleusercontent.com/r/mcp"],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }

    # Mock open to raise PermissionError during disk write
    orig_open = open
    def mock_open(file, *args, **kwargs):
        if "oauth_store" in str(file) or "credentials" in str(file):
            raise PermissionError("[Errno 13] Permission denied")
        return orig_open(file, *args, **kwargs)

    with patch("builtins.open", side_effect=mock_open):
        # Registration must NOT return 500
        r = client.post("/register", json=reg_payload)
        assert r.status_code == 201
        data = r.json()
        assert "client_id" in data
        assert data["client_id"] in provider.clients

        # Config save must NOT crash
        saved = config.save_config(
            moodle_url="https://moodle.test.edu",
            moodle_token="token_test",
        )
        assert saved["moodle_token"] == "token_test"


def test_setup_flow_with_custom_moodle_url(tmp_path, monkeypatch):
    """Verify setup screen when MOODLE_URL is not set in env: user can input URL + Token."""
    cfg_file = tmp_path / "credentials.json"
    oauth_file = tmp_path / "oauth_store.json"
    monkeypatch.setenv("CREDENTIALS_PATH", str(cfg_file))
    monkeypatch.setenv("OAUTH_STORE_PATH", str(oauth_file))
    monkeypatch.delenv("MOODLE_URL", raising=False)
    config._CACHED_CONFIG = None

    provider = configure_http_server(mcp, public_url="http://127.0.0.1:8000")
    app = mcp.streamable_http_app(host="127.0.0.1")
    client = TestClient(app)

    oauth_client = OAuthClientInformationFull(
        client_id="gemini-custom",
        redirect_uris=[AnyUrl("https://oauth-redirect.googleusercontent.com/r/mcp")],
        grant_types=["authorization_code"],
        response_types=["code"],
    )
    provider.clients["gemini-custom"] = oauth_client

    import anyio
    session_url = anyio.run(
        provider.authorize,
        oauth_client,
        AuthorizationParams(
            state="state_custom",
            scopes=["mcp"],
            code_challenge="challenge_custom",
            redirect_uri=AnyUrl("https://oauth-redirect.googleusercontent.com/r/mcp"),
            redirect_uri_provided_explicitly=True,
        ),
    )
    session_id = session_url.split("session_id=")[1]

    # Verify setup page provides moodle_url input field
    r_get = client.get(session_url)
    assert r_get.status_code == 200
    assert 'name="moodle_url"' in r_get.text
    assert 'name="moodle_token"' in r_get.text
    assert 'disabled' not in r_get.text.split('<button type="submit"')[1].split('>')[0]

    # Submit with mock Moodle server response
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "sitename": "Custom University",
        "username": "custom_student",
        "fullname": "Custom Student",
    }

    with patch("moodle_mcp.web.requests.post", return_value=mock_response):
        r_post = client.post(
            "/auth/setup",
            data={
                "session_id": session_id,
                "moodle_url": "https://moodle.custom.edu",
                "moodle_token": "token_abc123",
            },
            follow_redirects=False,
        )
        assert r_post.status_code == 302
        redirect_location = r_post.headers["location"]
        assert "https://oauth-redirect.googleusercontent.com/r/mcp" in redirect_location
        assert "code=" in redirect_location

        # Extract code and verify both token and url are linked
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(redirect_location).query)
        auth_code_str = qs["code"][0]
        assert auth_code_str in provider.auth_codes
        assert provider.code_moodle_tokens[auth_code_str] == "token_abc123"
        assert provider.code_moodle_urls[auth_code_str] == "https://moodle.custom.edu/webservice/rest/server.php"

        # Exchange authorization code
        auth_code_obj = provider.auth_codes[auth_code_str]
        tokens = anyio.run(provider.exchange_authorization_code, oauth_client, auth_code_obj)
        assert tokens.access_token is not None
        assert provider.get_moodle_token_for_access_token(tokens.access_token) == "token_abc123"
        assert provider.get_moodle_url_for_access_token(tokens.access_token) == "https://moodle.custom.edu/webservice/rest/server.php"


