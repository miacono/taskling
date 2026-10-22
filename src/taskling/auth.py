"""OAuth 2.0 for installed apps (loopback redirect + PKCE), standard library only."""

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from . import config

SCOPE = "https://www.googleapis.com/auth/tasks"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Serializes token refreshes between the GUI and worker threads.
_lock = threading.Lock()


class AuthError(Exception):
    pass


def _client() -> dict:
    data = config.read_json(config.CLIENT_SECRET_FILE)
    if data is None:
        raise AuthError(f"missing OAuth client file {config.CLIENT_SECRET_FILE} (see the README)")
    if "installed" not in data:
        raise AuthError("client_secret.json must belong to an OAuth client of type 'Desktop app'")
    return data["installed"]


def _post_token(params: dict) -> dict:
    request = urllib.request.Request(
        TOKEN_URL, data=urllib.parse.urlencode(params).encode(), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e)
        except ValueError:
            detail = {}
        reason = detail.get("error_description") or detail.get("error") or str(e)
        raise AuthError(f"token request failed: {reason}") from None
    except urllib.error.URLError as e:
        raise AuthError(f"network error: {e.reason}") from None


def _save_token(token: dict, refresh_token: str | None = None) -> dict:
    stored = {
        "access_token": token["access_token"],
        # Refresh responses do not repeat the refresh token, so keep the old one.
        "refresh_token": token.get("refresh_token") or refresh_token,
        "expires_at": time.time() + token.get("expires_in", 3600),
    }
    config.write_private_json(config.TOKEN_FILE, stored)
    return stored


def is_authorized() -> bool:
    token = config.read_json(config.TOKEN_FILE)
    return bool(token and token.get("refresh_token"))


def authorize(timeout: float = 300) -> None:
    """Sign in through the browser and store the resulting tokens."""
    client = _client()
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [None])[0] != state:
                self.send_error(400, "Invalid state")
                return
            result.update({key: values[0] for key, values in query.items()})
            message = (
                "Taskling is signed in, you can close this tab."
                if "code" in result
                else "Sign-in failed, you can close this tab."
            )
            body = message.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    })
    print(f"Opening the browser to sign in. If it does not open, visit:\n{url}", flush=True)
    webbrowser.open(url)

    deadline = time.monotonic() + timeout
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "code" not in result:
        raise AuthError(f"sign-in failed: {result.get('error', 'timed out')}")

    token = _post_token({
        "code": result["code"],
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    })
    with _lock:
        _save_token(token)


def access_token(force_refresh: bool = False) -> str:
    """Return a valid access token, refreshing it when it is about to expire."""
    with _lock:
        token = config.read_json(config.TOKEN_FILE)
        if not token or not token.get("refresh_token"):
            raise AuthError("not signed in: run 'taskling auth'")
        if force_refresh or token["expires_at"] - 60 < time.time():
            client = _client()
            try:
                fresh = _post_token({
                    "client_id": client["client_id"],
                    "client_secret": client["client_secret"],
                    "refresh_token": token["refresh_token"],
                    "grant_type": "refresh_token",
                })
            except AuthError as e:
                raise AuthError(f"{e} (run 'taskling auth' to sign in again)") from None
            token = _save_token(fresh, token["refresh_token"])
        return token["access_token"]
