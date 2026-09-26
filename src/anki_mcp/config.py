"""Environment-driven configuration.

Secrets (bearer token, AnkiWeb password) come from the environment only --
never from the repo's committed .env, which by its own comment holds
non-secret ids and the timezone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class AuthMode(str, Enum):
    """How callers authenticate.

    BEARER -- one static token in an env var. Fine on the tailnet, where the
              network is already the perimeter.
    OAUTH  -- full OAuth 2.1 authorization server with a password + TOTP login.
              Required for Claude custom connectors (and therefore for the
              mobile apps), and the only defensible option on a public
              Tailscale Funnel endpoint.
    """

    BEARER = "bearer"
    OAUTH = "oauth"


class Profile(str, Enum):
    """Which set of tools this process exposes.

    FULL   -- every tool; the tailnet-only endpoint.
    PUBLIC -- read-only tools plus add_note; safe(r) to put behind Funnel.
    """

    FULL = "full"
    PUBLIC = "public"


# A short token is worse than no token, because it invites a guess against an
# endpoint that will happily be brute-forced. 32 chars of urandom-ish entropy.
MIN_TOKEN_LENGTH = 32

# Shorter than the bearer token because a human types this one, and it is
# backed by a second factor. Still long enough to survive online guessing at
# the rate limiter's ceiling.
MIN_PASSWORD_LENGTH = 12


class ConfigError(RuntimeError):
    """Raised at startup for a config problem that must stop the process."""


@dataclass(frozen=True)
class Config:
    collection_path: Path
    state_dir: Path
    profile: Profile
    auth_mode: AuthMode
    bearer_token: str | None
    login_password: str | None
    totp_secret: str | None
    host: str
    port: int
    public_url: str
    environment: str
    image_provider: str
    google_cse_key: str | None
    google_cse_engine_id: str | None
    pixabay_key: str | None
    tts_key: str | None
    tts_voice: str
    tts_provider: str
    prefer_tts: bool
    ankiweb_username: str | None
    ankiweb_password: str | None
    ankiweb_endpoint: str | None

    @property
    def oauth_db_path(self) -> Path:
        """OAuth clients and tokens. Must persist, or the connector breaks on
        every restart and has to be re-added from a desktop."""
        return self.state_dir / "oauth.db"

    @property
    def hkey_path(self) -> Path:
        """Where the AnkiWeb session key is cached after the first login."""
        return self.state_dir / "ankiweb_hkey"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if value else None


def load() -> Config:
    """Read config from the environment, or raise ConfigError.

    Deliberately strict: it is better to refuse to boot than to come up
    unauthenticated or pointed at the wrong file.
    """
    data_dir = Path(_env("ANKI_MCP_DATA_DIR", "/data") or "/data")
    state_dir = Path(_env("ANKI_MCP_STATE_DIR", "/config") or "/config")

    raw_profile = (_env("ANKI_MCP_PROFILE", "full") or "full").lower()
    try:
        profile = Profile(raw_profile)
    except ValueError:
        valid = ", ".join(p.value for p in Profile)
        raise ConfigError(
            f"ANKI_MCP_PROFILE={raw_profile!r} is not valid. Use one of: {valid}"
        ) from None

    raw_mode = (_env("ANKI_MCP_AUTH_MODE", "bearer") or "bearer").lower()
    try:
        auth_mode = AuthMode(raw_mode)
    except ValueError:
        valid = ", ".join(m.value for m in AuthMode)
        raise ConfigError(
            f"ANKI_MCP_AUTH_MODE={raw_mode!r} is not valid. Use one of: {valid}"
        ) from None

    token = _env("ANKI_MCP_BEARER_TOKEN")
    login_password = _env("ANKI_MCP_LOGIN_PASSWORD")
    totp_secret = _env("ANKI_MCP_TOTP_SECRET")

    if auth_mode is AuthMode.BEARER:
        if not token:
            raise ConfigError(
                "ANKI_MCP_BEARER_TOKEN is not set. This server refuses to start "
                "without auth -- it would expose the collection to anyone who can "
                "reach the port. Generate one with: openssl rand -hex 32"
            )
        if len(token) < MIN_TOKEN_LENGTH:
            raise ConfigError(
                f"ANKI_MCP_BEARER_TOKEN is only {len(token)} characters; at least "
                f"{MIN_TOKEN_LENGTH} are required. Generate one with: openssl rand -hex 32"
            )
    else:
        missing = [
            name
            for name, value in (
                ("ANKI_MCP_LOGIN_PASSWORD", login_password),
                ("ANKI_MCP_TOTP_SECRET", totp_secret),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"ANKI_MCP_AUTH_MODE=oauth requires {' and '.join(missing)}. "
                "The login page is the only thing protecting a public endpoint, "
                "so the server will not start without both factors configured."
            )
        if login_password and len(login_password) < MIN_PASSWORD_LENGTH:
            raise ConfigError(
                f"ANKI_MCP_LOGIN_PASSWORD is only {len(login_password)} characters; "
                f"at least {MIN_PASSWORD_LENGTH} are required for an "
                "internet-facing login page."
            )
        if not _env("ANKI_MCP_PUBLIC_URL"):
            raise ConfigError(
                "ANKI_MCP_AUTH_MODE=oauth requires ANKI_MCP_PUBLIC_URL -- OAuth "
                "redirect URLs and discovery metadata must carry the externally "
                "reachable https:// address, e.g. "
                "https://<your-host>.<your-tailnet>.ts.net"
            )

    host = _env("ANKI_MCP_HOST", "0.0.0.0") or "0.0.0.0"
    port = int(_env("ANKI_MCP_PORT", "8770") or "8770")
    # The URL clients actually reach this server on. It only feeds the OAuth
    # protected-resource metadata the MCP SDK publishes, so the default is fine
    # for a tailnet; set it explicitly if you put the server behind Funnel.
    public_url = _env("ANKI_MCP_PUBLIC_URL") or f"http://localhost:{port}"
    # Tags every telemetry record; the dashboard's $env variable filters on it.
    environment = _env("ANKI_MCP_ENVIRONMENT", "production") or "production"
    # 'auto' = Openverse first for relevance, Wikimedia as a fallback. Wikimedia
    # alone returns scanned documents for everyday phrases, which is worse than
    # no picture on a card that carries meaning through the image.
    image_provider = (_env("ANKI_MCP_IMAGE_PROVIDER", "auto") or "auto").lower()

    password = _env("ANKIWEB_PASSWORD")
    username = _env("ANKIWEB_USERNAME")

    return Config(
        collection_path=data_dir / "collection.anki2",
        state_dir=state_dir,
        profile=profile,
        auth_mode=auth_mode,
        bearer_token=token,
        login_password=login_password,
        totp_secret=totp_secret,
        host=host,
        port=port,
        public_url=public_url,
        environment=environment,
        image_provider=image_provider,
        google_cse_key=_env("GOOGLE_CSE_API_KEY"),
        google_cse_engine_id=_env("GOOGLE_CSE_ENGINE_ID"),
        pixabay_key=_env("PIXABAY_API_KEY"),
        # 'google' or 'elevenlabs'. Google's de-DE voices are native German
        # and free; ElevenLabs' German voices are library voices, which the API
        # only serves to paid plans.
        tts_provider=(_env("ANKI_MCP_TTS_PROVIDER", "google") or "google").lower(),
        # Synthesise even where Wiktionary has a recording, for one consistent
        # voice across the deck instead of a mix of volunteer recordings.
        prefer_tts=(_env("ANKI_MCP_PREFER_TTS", "false") or "false").lower()
        in ("1", "true", "yes"),
        # Deliberately NO fallback to GOOGLE_CSE_API_KEY. A Custom Search key
        # is usually not enabled for Text-to-Speech, so borrowing it produced a
        # 403 that looked like a TTS bug when the user had only ever configured
        # image search.
        tts_key=(
            _env("GOOGLE_TTS_API_KEY")
            if (_env("ANKI_MCP_TTS_PROVIDER", "google") or "google").lower() == "google"
            else _env("ELEVENLABS_API_KEY")
        ),
        # No default voice id: every service's default is an English speaker,
        # and reading German with an English accent is the opposite of what a
        # pronunciation card is for. Pick a German voice explicitly.
        tts_voice=(
            _env("ANKI_MCP_TTS_VOICE", "de-DE-Neural2-F") or "de-DE-Neural2-F"
            if (_env("ANKI_MCP_TTS_PROVIDER", "google") or "google").lower() == "google"
            else _env("ELEVENLABS_VOICE_ID", "") or ""
        ),
        ankiweb_username=username,
        ankiweb_password=password,
        ankiweb_endpoint=_env("ANKIWEB_ENDPOINT"),
    )
