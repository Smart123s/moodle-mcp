import html
import os
import secrets
import time
from typing import Any
from urllib.parse import urlparse

import requests
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from mcp.server.auth.provider import AuthorizationCode, construct_redirect_uri

from .auth import MoodleOAuthProvider
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


def _get_page_styles() -> str:
    return """
    :root {
        --primary: #f27922;
        --primary-hover: #d66415;
        --gemini: #1a73e8;
        --bg: #f8fafc;
        --card-bg: #ffffff;
        --text: #1e293b;
        --text-muted: #64748b;
        --border: #e2e8f0;
        --error-bg: #fef2f2;
        --error-border: #fecaca;
        --error-text: #991b1b;
        --success-bg: #f0fdf4;
        --success-border: #bbf7d0;
        --success-text: #166534;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        background-color: var(--bg);
        color: var(--text);
        line-height: 1.5;
        display: flex;
        justify-content: center;
        align-items: center;
        min-height: 100vh;
        padding: 20px;
    }
    .container {
        background: var(--card-bg);
        border: 1px solid var(--border);
        border-radius: 12px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.05);
        max-width: 520px;
        width: 100%;
        padding: 32px;
    }
    .header {
        text-align: center;
        margin-bottom: 24px;
    }
    .logo-container {
        display: flex;
        justify-content: center;
        align-items: center;
        gap: 12px;
        margin-bottom: 16px;
    }
    .logo-badge {
        font-weight: 700;
        font-size: 20px;
        padding: 6px 14px;
        border-radius: 8px;
    }
    .badge-moodle { background: #fff3eb; color: var(--primary); border: 1px solid #ffd6b8; }
    .badge-gemini { background: #e8f0fe; color: var(--gemini); border: 1px solid #c2e7ff; }
    h1 { font-size: 22px; font-weight: 700; color: var(--text); margin-bottom: 8px; }
    p.subtitle { font-size: 14px; color: var(--text-muted); }
    .alert {
        padding: 12px 16px;
        border-radius: 8px;
        font-size: 14px;
        margin-bottom: 20px;
    }
    .alert-error {
        background: var(--error-bg);
        border: 1px solid var(--error-border);
        color: var(--error-text);
    }
    .alert-success {
        background: var(--success-bg);
        border: 1px solid var(--success-border);
        color: var(--success-text);
    }
    .form-group {
        margin-bottom: 18px;
    }
    label {
        display: block;
        font-size: 13px;
        font-weight: 600;
        margin-bottom: 6px;
        color: var(--text);
    }
    input[type="text"], input[type="password"] {
        width: 100%;
        padding: 10px 12px;
        font-size: 14px;
        border: 1px solid var(--border);
        border-radius: 6px;
        background: #fff;
        color: var(--text);
        outline: none;
        transition: border-color 0.2s;
    }
    input[type="text"]:focus, input[type="password"]:focus {
        border-color: var(--gemini);
        box-shadow: 0 0 0 3px rgba(26, 115, 232, 0.15);
    }
    .help-text {
        font-size: 12px;
        color: var(--text-muted);
        margin-top: 4px;
    }
    .btn {
        width: 100%;
        padding: 12px;
        background: var(--primary);
        color: #fff;
        font-size: 15px;
        font-weight: 600;
        border: none;
        border-radius: 6px;
        cursor: pointer;
        transition: background-color 0.2s;
        margin-top: 8px;
    }
    .btn:hover {
        background: var(--primary-hover);
    }
    .footer {
        text-align: center;
        margin-top: 24px;
        font-size: 12px;
        color: var(--text-muted);
    }
    .info-box {
        background: #f1f5f9;
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 14px;
        font-size: 13px;
        margin-bottom: 20px;
    }
    .status-dot {
        display: inline-block;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        margin-right: 6px;
    }
    .dot-green { background: #22c55e; }
    .dot-orange { background: #f97316; }
    """


def render_setup_page(
    session_id: str | None = None,
    error_message: str | None = None,
    success_message: str | None = None,
    moodle_url: str | None = None,
    site_info: dict[str, Any] | None = None,
) -> HTMLResponse:
    """Render the HTML authorization & setup form."""
    config = load_config()
    env_url = get_moodle_url()
    current_url = moodle_url or env_url or (config.get("moodle_url") if config else None) or ""
    current_site = site_info or (config.get("site_info") if config else None)

    error_html = (
        f'<div class="alert alert-error">{html.escape(error_message)}</div>'
        if error_message
        else ""
    )
    success_html = (
        f'<div class="alert alert-success">{html.escape(success_message)}</div>'
        if success_message
        else ""
    )

    status_html = ""
    if current_site:
        sitename = html.escape(str(current_site.get("sitename", "Moodle")))
        username = html.escape(str(current_site.get("username", "")))
        status_html = f"""
        <div class="info-box">
            <span class="status-dot dot-green"></span>
            <strong>Currently linked:</strong> {sitename} ({username})
        </div>
        """
    elif env_url:
        status_html = f"""
        <div class="info-box">
            <span class="status-dot dot-green"></span>
            <strong>Status:</strong> Server configured with {html.escape(env_url)}. Ready for user authentication.
        </div>
        """
    elif is_configured():
        status_html = """
        <div class="info-box">
            <span class="status-dot dot-green"></span>
            <strong>Status:</strong> Moodle server URL configured. Ready for user authentication.
        </div>
        """
    else:
        status_html = """
        <div class="info-box">
            <span class="status-dot dot-orange"></span>
            <strong>Status:</strong> Enter your Moodle credentials below to authorize this session.
        </div>
        """

    if not session_id:
        notice_html = """
        <div class="info-box" style="background: #fffbeb; border-color: #fef3c7; color: #92400e;">
            <strong>Notice:</strong> This authorization page is intended to be accessed through an OAuth flow from Gemini Web or your MCP client. Please start the connection from your client application.
        </div>
        """
    else:
        notice_html = ""

    session_input = (
        f'<input type="hidden" name="session_id" value="{html.escape(session_id)}">'
        if session_id
        else ""
    )
    btn_disabled = "disabled" if not session_id else ""
    btn_text = "Authorize & Link Gemini" if session_id else "Session Missing"

    # If MOODLE_URL is fixed in the server environment (.env), show it as read-only badge.
    # Otherwise, allow the user to input their Moodle instance URL.
    if env_url:
        url_input_html = f"""
            <div class="form-group">
                <label>Moodle Server URL (configured in .env)</label>
                <div class="info-box" style="margin-bottom: 0; word-break: break-all; background: #f8fafc;">
                    <code>{html.escape(env_url)}</code>
                </div>
                <div class="help-text">The Moodle server address is managed securely in the server environment (.env).</div>
            </div>
        """
    else:
        url_input_html = f"""
            <div class="form-group">
                <label for="moodle_url">Moodle Server URL</label>
                <input type="text" id="moodle_url" name="moodle_url" required
                       placeholder="https://moodle.your-university.edu"
                       value="{html.escape(current_url)}"
                       {"disabled" if not session_id else ""}>
                <div class="help-text">
                    The web address of your Moodle installation (e.g. <code>https://moodle.example.edu</code>).
                </div>
            </div>
        """

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Moodle MCP - Gemini Authentication</title>
    <style>{_get_page_styles()}</style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo-container">
                <span class="logo-badge badge-moodle">Moodle</span>
                <span style="font-weight: bold; color: var(--text-muted);">&harr;</span>
                <span class="logo-badge badge-gemini">Gemini</span>
            </div>
            <h1>Connect Moodle to Gemini</h1>
            <p class="subtitle">Enter your personal Moodle API token to authenticate this Model Context Protocol session.</p>
        </div>

        {error_html}
        {success_html}
        {status_html}
        {notice_html}

        <form method="POST" action="/auth/setup">
            {session_input}
            {url_input_html}

            <div class="form-group">
                <label for="moodle_token">Moodle Web Service Token (API Key)</label>
                <input type="password" id="moodle_token" name="moodle_token" required
                       placeholder="Enter your personal Moodle token" {"disabled" if not session_id else ""}>
                <div class="help-text">
                    In Moodle: Profile / Preferences &rarr; <strong>Security keys</strong> (or Mobile web services) &rarr; Copy token.
                </div>
            </div>

            <button type="submit" class="btn" {btn_disabled}>{btn_text}</button>
        </form>

        <div class="footer">
            Moodle MCP Server &bull; Securely running on your private NAS
        </div>
    </div>
</body>
</html>
"""
    return HTMLResponse(content=html_content)


def render_home_page() -> HTMLResponse:
    """Render the public landing page with connection status and guide."""
    config = load_config()
    configured = is_configured()
    site_info = config.get("site_info") if config else None

    if configured and site_info:
        status_badge = '<span class="status-dot dot-green"></span> <strong style="color: #166534;">Connected to Moodle</strong>'
        details = f"""
        <p><strong>Site:</strong> {html.escape(site_info.get('sitename', 'Moodle'))}</p>
        <p><strong>User:</strong> {html.escape(site_info.get('fullname', site_info.get('username', '')))}</p>
        <p><strong>Endpoint:</strong> {html.escape(config.get('moodle_url', ''))}</p>
        """
    elif configured:
        status_badge = '<span class="status-dot dot-green"></span> <strong style="color: #166534;">Configured</strong>'
        details = f"<p><strong>Endpoint:</strong> {html.escape(config.get('moodle_url', ''))}</p>"
    else:
        status_badge = '<span class="status-dot dot-orange"></span> <strong style="color: #c2410c;">Pending Moodle Configuration</strong>'
        details = "<p>Moodle credentials have not been configured yet. They will be requested when connecting from Gemini Web, or can be set via <a href='/auth/setup'>Setup</a>.</p>"

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Moodle MCP Server</title>
    <style>{_get_page_styles()}</style>
</head>
<body>
    <div class="container" style="max-width: 600px;">
        <div class="header">
            <div class="logo-container">
                <span class="logo-badge badge-moodle">Moodle</span>
                <span class="logo-badge badge-gemini">MCP Server</span>
            </div>
            <h1>Moodle Model Context Protocol Server</h1>
            <p class="subtitle">Streamable HTTP interface for Gemini Web &amp; AI Assistants</p>
        </div>

        <div class="info-box">
            <div style="margin-bottom: 10px;">{status_badge}</div>
            {details}
        </div>

        <div style="font-size: 14px; margin-bottom: 20px;">
            <h3 style="font-size: 15px; margin-bottom: 8px;">How to Connect to Gemini Web:</h3>
            <ol style="padding-left: 20px; line-height: 1.8;">
                <li>Open <strong>Gemini Web</strong> (<code>gemini.google.com</code>).</li>
                <li>Go to <strong>Settings</strong> &rarr; <strong>Connected Apps</strong>.</li>
                <li>Click <strong>+ Custom App</strong>.</li>
                <li>Enter your public MCP URL: <code>https://&lt;your-nas-domain&gt;/mcp</code>.</li>
                <li>When prompted, authenticate and link your Moodle URL &amp; Token.</li>
                <li>Ask Gemini questions about courses, assignments, deadlines, and grades!</li>
            </ol>
        </div>

        <div style="text-align: center;">
            <a href="/auth/setup" style="display: inline-block; font-size: 14px; color: var(--gemini); text-decoration: none; font-weight: 600;">
                &rarr; Manage Moodle Connection Settings
            </a>
        </div>

        <div class="footer">
            Protected with OAuth 2.0 Bearer Authentication &bull; Model Context Protocol
        </div>
    </div>
</body>
</html>
"""
    return HTMLResponse(content=html_content)


def create_openid_metadata_handler(public_url: str):
    """Create handler for /.well-known/openid-configuration discovery endpoint."""
    async def openid_configuration_get(request: Request) -> Response:
        metadata = {
            "issuer": public_url,
            "authorization_endpoint": f"{public_url}/oauth/authorize",
            "token_endpoint": f"{public_url}/oauth/token",
            "registration_endpoint": f"{public_url}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
                "none",
            ],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["mcp", "openid"],
        }
        return JSONResponse(
            metadata,
            headers={
                "Cache-Control": "public, max-age=3600",
                "Access-Control-Allow-Origin": "*",
            },
        )

    return openid_configuration_get


def create_web_routes(provider: MoodleOAuthProvider):
    """Create Starlette route handlers for UI and health endpoints."""

    async def setup_get(request: Request) -> Response:
        session_id = request.query_params.get("session_id")
        return render_setup_page(session_id=session_id)

    async def setup_post(request: Request) -> Response:
        form = await request.form()
        session_id = form.get("session_id")
        moodle_token = str(form.get("moodle_token", "")).strip()

        # SEC-01: Setup must be initiated by an active OAuth session (Gemini Web)
        if not session_id or str(session_id) not in provider.pending_sessions:
            return render_setup_page(
                session_id=str(session_id) if session_id else None,
                error_message="Invalid or expired OAuth session. Please initiate connection from your OAuth client (e.g., Gemini Web).",
            )

        # Moodle server URL: from .env if configured, otherwise from form or saved config
        env_url = get_moodle_url()
        input_url = str(form.get("moodle_url", "")).strip()
        raw_url = env_url or input_url
        if not raw_url:
            cfg = load_config()
            raw_url = (cfg.get("moodle_url") if cfg else "") or ""

        if not raw_url:
            return render_setup_page(
                session_id=str(session_id),
                error_message="Moodle Server URL is required.",
            )

        moodle_url = normalize_moodle_url(raw_url)
        parsed = urlparse(moodle_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return render_setup_page(
                session_id=str(session_id),
                moodle_url=raw_url,
                error_message="Invalid Moodle Server URL. Must start with https:// or http://.",
            )

        if not moodle_token:
            return render_setup_page(
                session_id=str(session_id),
                moodle_url=moodle_url,
                error_message="Moodle Web Service Token is required.",
            )

        # Test connection to Moodle using POST to avoid leaking tokens in query strings (SEC-06)
        try:
            test_params = {
                "wstoken": moodle_token,
                "wsfunction": "core_webservice_get_site_info",
                "moodlewsrestformat": "json",
            }
            rsp = requests.post(moodle_url, data=test_params, timeout=15)
            if rsp.status_code != 200:
                return render_setup_page(
                    session_id=str(session_id),
                    moodle_url=moodle_url,
                    error_message=f"Could not reach Moodle server: HTTP {rsp.status_code}. Please check the Moodle URL.",
                )

            data = rsp.json()
            if isinstance(data, dict) and ("errorcode" in data or "exception" in data):
                msg = data.get("message", data.get("errorcode", "Authentication failed"))
                return render_setup_page(
                    session_id=str(session_id),
                    moodle_url=moodle_url,
                    error_message=f"Moodle error: {msg}. Please check your token.",
                )

            site_info = {
                "sitename": data.get("sitename", "Moodle"),
                "username": data.get("username", ""),
                "fullname": data.get("fullname", ""),
                "release": data.get("release", ""),
            }
        except requests.RequestException as e:
            logger.warning(f"Failed to connect to Moodle at {moodle_url}: {e}")
            return render_setup_page(
                session_id=str(session_id),
                moodle_url=moodle_url,
                error_message=f"Connection failed: {type(e).__name__}. Ensure the Moodle server is accessible.",
            )

        # Save credentials as fallback and update in-memory modules
        save_config(moodle_token=moodle_token, site_info=site_info, moodle_url=moodle_url)

        from . import moodle as moodle_mod
        from . import downloads as downloads_mod

        moodle_mod.MOODLE_URL = moodle_url
        moodle_mod.MOODLE_TOKEN = moodle_token
        downloads_mod.MOODLE_URL = moodle_url
        downloads_mod.MOODLE_TOKEN = moodle_token

        # Complete OAuth code grant and link API key & URL to the authorization code
        pending = provider.pending_sessions.pop(str(session_id))
        code = secrets.token_urlsafe(32)

        auth_code = AuthorizationCode(
            code=code,
            scopes=pending.params.scopes or ["mcp"],
            expires_at=time.time() + 300,
            client_id=pending.client.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
            subject=site_info.get("username", "moodle-user"),
        )
        provider.auth_codes[code] = auth_code
        provider.code_moodle_tokens[code] = moodle_token
        provider.code_moodle_urls[code] = moodle_url

        # Redirect user back to client callback
        redirect_url = construct_redirect_uri(
            str(pending.params.redirect_uri),
            code=code,
            state=pending.params.state,
        )
        logger.info(f"Moodle verified. Redirecting back to OAuth callback: {redirect_url}")
        return RedirectResponse(redirect_url, status_code=302)

    async def home_get(request: Request) -> Response:
        return render_home_page()

    async def health_get(request: Request) -> Response:
        cfg = load_config()
        return JSONResponse(
            {
                "status": "ok",
                "configured": is_configured(),
                "site": cfg.get("site_info", {}).get("sitename") if cfg else None,
            }
        )

    return setup_get, setup_post, home_get, health_get
