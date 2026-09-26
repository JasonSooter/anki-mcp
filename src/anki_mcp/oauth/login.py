"""The login page that guards the OAuth flow.

This is internet-facing once Funnel is on, and it is the only thing between
the public and the collection. So it is deliberately boring: one password, one
TOTP code, constant-time comparison, rate limiting, replay protection, and no
information in the error messages about which factor was wrong.
"""

from __future__ import annotations

import hmac
import logging
import time

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .. import telemetry
from .provider import AnkiOAuthProvider
from .store import Store
from .totp import verify as verify_totp

log = logging.getLogger(__name__)

# Rate limiting. A TOTP code is 6 digits, so an attacker who already has the
# password gets a 1-in-a-million shot per attempt; capping attempts per window
# keeps that from becoming feasible by volume.
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 900

_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>anki-mcp</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.5 system-ui, sans-serif; display: grid; place-items: center;
         min-height: 100vh; margin: 0; background: Canvas; color: CanvasText; }}
  form {{ width: min(22rem, 90vw); display: grid; gap: .75rem; }}
  h1 {{ font-size: 1.1rem; margin: 0 0 .5rem; }}
  input {{ font: inherit; padding: .6rem .7rem; border: 1px solid GrayText;
           border-radius: .4rem; background: Field; color: FieldText; }}
  button {{ font: inherit; padding: .6rem; border: 0; border-radius: .4rem;
            background: Highlight; color: HighlightText; cursor: pointer; }}
  .err {{ color: #b3261e; font-size: .9rem; margin: 0; }}
</style>
<form method="post" action="{action}">
  <h1>Authorize access to your Anki collection</h1>
  {error}
  <input type="password" name="password" placeholder="Password" required autofocus
         autocomplete="current-password">
  <input type="text" name="totp" placeholder="6-digit code" required
         inputmode="numeric" pattern="[0-9 ]*" autocomplete="one-time-code">
  <button type="submit">Authorize</button>
</form>
"""


def _render(action: str, error: str | None = None) -> HTMLResponse:
    return HTMLResponse(
        _PAGE.format(
            action=action,
            error=f'<p class="err">{error}</p>' if error else "",
        ),
        # Never let a browser or proxy cache the login form.
        headers={"Cache-Control": "no-store"},
    )


def _client_ip(request: Request) -> str:
    """Best guess at the caller, for rate limiting.

    Behind Tailscale Funnel a proxy sits in front, so the socket address is not
    the caller and X-Forwarded-For has to be consulted. The LAST entry is used,
    not the first: a caller can send their own X-Forwarded-For header, and a
    proxy that appends rather than overwrites would leave that attacker-chosen
    value leading the list.Each forged value would then get its own rate-limit
    bucket, which defeats the limiter entirely. The final entry is the one the
    nearest trusted proxy added.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


def register_routes(
    mcp: object,
    provider: AnkiOAuthProvider,
    store: Store,
    password: str,
    totp_secret: bytes,
    login_path: str = "/login",
) -> None:
    """Attach GET/POST handlers for the login page to the MCP server."""

    @mcp.custom_route(login_path, methods=["GET"])  # type: ignore[attr-defined]
    async def login_form(request: Request) -> Response:
        login_id = request.query_params.get("login_id", "")
        if not login_id or store.take_pending(login_id) is None:
            return HTMLResponse(
                "<p>This login link has expired. Start again from Claude.</p>",
                status_code=400,
            )
        return _render(f"{login_path}?login_id={login_id}")

    @mcp.custom_route(login_path, methods=["POST"])  # type: ignore[attr-defined]
    async def login_submit(request: Request) -> Response:
        login_id = request.query_params.get("login_id", "")
        action = f"{login_path}?login_id={login_id}"
        ip = _client_ip(request)

        if store.failure_count(ip, FAILURE_WINDOW_SECONDS) >= MAX_FAILURES:
            log.warning("login rate-limited for %s", ip)
            telemetry.record_login(success=False, client_ip=ip, reason="rate_limited")
            response = _render(action, "Too many attempts. Try again later.")
            response.status_code = 429
            return response
        if store.take_pending(login_id) is None:
            return HTMLResponse(
                "<p>This login link has expired. Start again from Claude.</p>",
                status_code=400,
            )

        form = await request.form()
        submitted_password = str(form.get("password", ""))
        submitted_totp = str(form.get("totp", ""))

        password_ok = hmac.compare_digest(submitted_password, password)
        counter = verify_totp(totp_secret, submitted_totp)
        # Evaluate both factors before branching so the response time does not
        # reveal that the password alone was wrong.
        totp_ok = counter is not None and counter > store.last_totp_counter()

        if not (password_ok and totp_ok):
            failures = store.record_failure(ip, FAILURE_WINDOW_SECONDS)
            log.warning(
                "failed login from %s (%d/%d in window)", ip, failures, MAX_FAILURES
            )
            telemetry.record_login(
                success=False, client_ip=ip, reason="bad_credentials"
            )
            # One message for every failure mode: a distinct "bad code" reply
            # would confirm the password to an attacker.
            return _render(action, "Incorrect password or code.")

        assert counter is not None
        store.set_totp_counter(counter)
        store.clear_failures(ip)

        redirect_url = provider.complete_login(login_id)
        if redirect_url is None:
            return HTMLResponse(
                "<p>This login link has expired. Start again from Claude.</p>",
                status_code=400,
            )
        log.info("successful login from %s; redirecting to client", ip)
        telemetry.record_login(success=True, client_ip=ip)
        return RedirectResponse(redirect_url, status_code=302)
