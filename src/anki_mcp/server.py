"""Server assembly: which tools exist, and who is allowed to call them."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import ANKI_SCOPE
from .collection import CollectionWorker
from .config import AuthMode, Config, Profile
from .oauth.login import register_routes as register_login_routes
from .oauth.provider import AnkiOAuthProvider
from .oauth.store import Store
from .oauth.totp import normalise_secret
from .tools import read, write

log = logging.getLogger(__name__)


def build(
    config: Config, worker: CollectionWorker, verifier: TokenVerifier | None
) -> tuple[MCPServer, Starlette]:
    """Construct the MCP server and its Streamable HTTP app.

    The two auth modes plug into the same seam. In bearer mode the SDK is given
    a TokenVerifier; in oauth mode it is given a full authorization-server
    provider and derives the verifier itself. Tool code is identical either way
    -- it never sees a token.
    """
    oauth_provider: AnkiOAuthProvider | None = None
    store: Store | None = None

    if config.auth_mode is AuthMode.OAUTH:
        store = Store(config.oauth_db_path)
        oauth_provider = AnkiOAuthProvider(store)
        auth_settings = AuthSettings(
            issuer_url=config.public_url,
            resource_server_url=config.public_url,
            required_scopes=[ANKI_SCOPE],
            # Claude registers itself when you add the connector; without
            # dynamic registration you would have to pre-provision a client id
            # and paste it into the connector dialog.
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[ANKI_SCOPE],
                default_scopes=[ANKI_SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
    else:
        auth_settings = AuthSettings(
            issuer_url=config.public_url,
            resource_server_url=config.public_url,
            required_scopes=[ANKI_SCOPE],
        )

    mcp = MCPServer(
        name="anki",
        title="Anki Collection",
        version="0.1.0",
        instructions=_instructions(config.profile),
        token_verifier=verifier if oauth_provider is None else None,
        auth_server_provider=oauth_provider,
        # Enabling auth here is what makes the SDK reject unauthenticated
        # requests before they ever reach a tool.
        auth=auth_settings,
    )

    if oauth_provider is not None:
        assert store is not None and config.login_password and config.totp_secret
        register_login_routes(
            mcp,
            oauth_provider,
            store,
            config.login_password,
            normalise_secret(config.totp_secret),
        )

    read.register(mcp, worker, config)
    write.register_add_note(mcp, worker, config)
    if config.profile is Profile.FULL:
        write.register(mcp, worker, config)
    log.info("tool profile: %s", config.profile.value)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        """Liveness probe. Unauthenticated, and deliberately contentless.

        It reports whether the collection is open -- which is the failure this
        container actually has -- and nothing about what is in it.
        """
        healthy = worker.is_open
        body: dict[str, Any] = {
            "status": "ok" if healthy else "collection not open",
            "profile": config.profile.value,
            "auth_mode": config.auth_mode.value,
            "collection_open": healthy,
        }
        return JSONResponse(body, status_code=200 if healthy else 503)

    app = mcp.streamable_http_app(host=config.host)
    return mcp, app


def _instructions(profile: Profile) -> str:
    base = (
        "Read and write an Anki flashcard collection. Deck and note-type names "
        "are case-sensitive; call list_decks and list_note_types to discover "
        "the exact names before adding notes. Searches use Anki's own search "
        "syntax."
    )
    if profile is Profile.PUBLIC:
        return base + (
            " This endpoint is read-only apart from add_note: notes cannot be "
            "edited and changes cannot be pushed to AnkiWeb from here."
        )
    return base + (
        " Changes are saved locally but are not pushed to AnkiWeb until "
        "sync_to_ankiweb is called explicitly."
    )
