"""Google OAuth 2.0 flow + token storage.

One auth flow unlocks Calendar + Docs APIs + user login. Tokens are stored
as JSON at `~/.pokeboo/google_tokens.json` and auto-refreshed when they expire.

Scopes we request:
- https://www.googleapis.com/auth/calendar  (read/write calendar events)
- https://www.googleapis.com/auth/documents (create/edit Google Docs)
- openid + email + profile                  (for user login — web deployment)

Two flows:
1. CLI flow (desktop): `run_flow()` — opens browser, captures callback on
   localhost:8765. Used by `pokeboo auth` CLI command.
2. Web flow (deployed): `get_auth_url()` + `exchange_code()` — the frontend
   redirects to Google, Google redirects back to `/auth/google/callback`
   on the backend, which exchanges the code for tokens and logs the user in.
"""
from __future__ import annotations

from pathlib import Path
from threading import Lock

from loguru import logger

from ..config import Settings


# Scopes for Calendar + Docs + user identity (openid/email/profile for login)
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/documents",
    "openid",
    "email",
    "profile",
]


class GoogleAuth:
    """Manages OAuth credentials lifecycle.

    Usage (desktop CLI):
        auth = GoogleAuth(settings)
        if not auth.is_authenticated():
            auth.run_flow()  # opens browser, captures tokens
        creds = auth.get_credentials()  # auto-refreshes if expired

    Usage (web deployment):
        auth = GoogleAuth(settings)
        url = auth.get_auth_url(redirect_uri="https://backend.example.com/auth/google/callback")
        # ... frontend redirects to `url` ...
        # ... Google redirects back with ?code=xxx ...
        creds = auth.exchange_code(code, redirect_uri)
        user_info = auth.get_user_info(creds)
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._creds = None
        self._lock = Lock()
        self._load_cached_credentials()

    def is_configured(self) -> bool:
        """True if client_id and client_secret are present in settings."""
        return bool(self._settings.google_client_id and self._settings.google_client_secret)

    def is_authenticated(self) -> bool:
        """True if we have valid (or refreshable) credentials cached."""
        return self._creds is not None and (self._creds.valid or self._creds.refresh_token)

    def _load_cached_credentials(self) -> None:
        """Load cached tokens from disk if they exist."""
        try:
            from google.oauth2.credentials import Credentials
        except ImportError:
            logger.warning("google-auth-oauthlib not installed; Google integration disabled")
            return

        token_path = self._settings.google_token_path
        if not token_path.exists():
            return
        try:
            self._creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            logger.info(f"Loaded Google credentials from {token_path}")
        except Exception as e:
            logger.warning(f"Failed to load cached Google credentials: {e}")
            self._creds = None

    def run_flow(self, port: int = 8765) -> None:
        """Run the OAuth consent flow (CLI/desktop mode).

        Opens a browser, user grants access, we capture the auth code via
        a localhost callback, exchange it for tokens, and persist them.
        """
        if not self.is_configured():
            raise RuntimeError(
                "Google OAuth not configured. Set GOOGLE_CLIENT_ID and "
                "GOOGLE_CLIENT_SECRET in .env (see README for setup guide)."
            )
        from google_auth_oauthlib.flow import InstalledAppFlow

        client_config = {
            "installed": {
                "client_id": self._settings.google_client_id,
                "client_secret": self._settings.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        flow.redirect_uri = f"http://localhost:{port}"
        # local_server_launches browser, captures callback, returns creds
        self._creds = flow.run_local_server(port=port, access_type="offline", prompt="consent")
        self._save_credentials()
        logger.info("Google OAuth flow completed; tokens saved.")

    # --- Web OAuth flow (for deployed backend) ---

    def get_auth_url(self, redirect_uri: str, state: str = "") -> str:
        """Return the Google consent URL for the web flow.

        The frontend redirects the user to this URL. After consent, Google
        redirects back to `redirect_uri` with `?code=xxx&state=xxx`.

        Args:
            redirect_uri: The backend's callback URL (must be registered in
                Google Cloud Console as an authorized redirect URI). Example:
                "https://pokeboo-backend.onrender.com/auth/google/callback"
            state: Optional opaque state token (CSRF protection). The same
                value will be returned in the callback — verify it matches.

        Returns:
            The Google consent URL to redirect the user to.
        """
        if not self.is_configured():
            raise RuntimeError(
                "Google OAuth not configured. Set GOOGLE_CLIENT_ID and "
                "GOOGLE_CLIENT_SECRET in .env."
            )
        from google_auth_oauthlib.flow import Flow

        client_config = {
            "web": {
                "client_id": self._settings.google_client_id,
                "client_secret": self._settings.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        }
        flow = Flow.from_client_config(client_config, SCOPES, redirect_uri=redirect_uri)
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            state=state,
        )
        return auth_url

    def exchange_code(self, code: str, redirect_uri: str):
        """Exchange an authorization code for OAuth credentials (web flow).

        Called by the `/auth/google/callback` endpoint after Google redirects
        back with `?code=xxx`. Also persists the tokens to disk (same file as
        the CLI flow) so Calendar/Docs integrations can reuse them.

        Args:
            code: The authorization code from Google's callback.
            redirect_uri: Must match the redirect_uri used in `get_auth_url()`.

        Returns:
            google.oauth2.credentials.Credentials — includes access_token,
            refresh_token, id_token (with user identity).
        """
        if not self.is_configured():
            raise RuntimeError("Google OAuth not configured.")
        from google_auth_oauthlib.flow import Flow

        client_config = {
            "web": {
                "client_id": self._settings.google_client_id,
                "client_secret": self._settings.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        }
        flow = Flow.from_client_config(client_config, SCOPES, redirect_uri=redirect_uri)
        flow.fetch_token(code=code)
        creds = flow.credentials
        # Persist for Calendar/Docs integrations to reuse
        self._creds = creds
        self._save_credentials()
        logger.info("Google OAuth web flow completed; tokens saved.")
        return creds

    def get_user_info(self, creds) -> dict:
        """Fetch the user's profile info (email, name, avatar) from Google.

        Uses the ID token if present (no extra API call), otherwise fetches
        from the Google userinfo endpoint.
        """
        # Try ID token first (no API call needed)
        if creds.id_token:
            try:
                from google.oauth2 import id_token
                from google.auth.transport import requests
                info = id_token.verify_oauth2_token(
                    creds.id_token, requests.Request(),
                    self._settings.google_client_id,
                )
                return {
                    "google_id": info.get("sub"),
                    "email": info.get("email"),
                    "name": info.get("name"),
                    "picture": info.get("picture"),
                }
            except Exception as e:
                logger.warning(f"ID token verification failed, falling back to userinfo API: {e}")
        # Fallback: call the userinfo endpoint
        import httpx
        resp = httpx.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=10,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch user info: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        return {
            "google_id": data.get("id"),
            "email": data.get("email"),
            "name": data.get("name"),
            "picture": data.get("picture"),
        }

    def _save_credentials(self) -> None:
        if not self._creds:
            return
        token_path = self._settings.google_token_path
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(self._creds.to_json())
        logger.info(f"Saved Google credentials to {token_path}")

    def get_credentials(self):
        """Return valid credentials, refreshing if necessary.

        Raises if not authenticated — caller should call run_flow() first.
        """
        if not self._creds:
            raise RuntimeError("Not authenticated. Run `pokeboo auth` first.")
        with self._lock:
            if self._creds.expired and self._creds.refresh_token:
                from google.auth.transport.requests import Request
                logger.info("Refreshing expired Google access token...")
                self._creds.refresh(Request())
                self._save_credentials()
            return self._creds
