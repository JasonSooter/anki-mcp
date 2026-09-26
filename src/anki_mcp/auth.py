"""Bearer-token authentication, shaped so OAuth can replace it later.

The MCP SDK's ``TokenVerifier`` protocol is a single async method:

    async def verify_token(token: str) -> AccessToken | None

That protocol *is* the seam. ``server.py`` hands whatever verifier it is given
to ``MCPServer(token_verifier=...)`` and knows nothing else about auth; tool
code never sees a token at all. Swapping to OAuth means constructing a
different verifier here -- no tool signature changes.
"""

from __future__ import annotations

import hmac

from mcp.server.auth.provider import AccessToken, TokenVerifier

from . import telemetry

# Scope name required of every caller. With a static token it is a formality,
# but declaring it now means an OAuth provider issuing scoped tokens later
# slots in without changing how the server is constructed.
ANKI_SCOPE = "anki"


class StaticBearerVerifier(TokenVerifier):
    """Verifies a single shared secret from the environment.

    The comparison is constant-time: a naive ``==`` leaks the length of the
    matching prefix through timing, which over enough requests recovers the
    token one byte at a time.
    """

    def __init__(self, token: str, *, client_id: str = "anki-mcp-static") -> None:
        self._token = token
        self._client_id = client_id

    async def verify_token(self, token: str) -> AccessToken | None:
        # compare_digest needs equal-length byte strings to be meaningful;
        # it handles unequal lengths safely but returns False immediately.
        if not hmac.compare_digest(token, self._token):
            # The one place a failed credential is visible. Logged without any
            # part of the token itself -- this endpoint may be public, and the
            # log is shipped off-box.
            telemetry.record_auth_failure("bearer token mismatch")
            return None
        return AccessToken(
            token=token,
            client_id=self._client_id,
            scopes=[ANKI_SCOPE],
        )
