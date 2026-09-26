"""AnkiWeb credentials.

AnkiWeb has no app tokens -- ``sync_login`` trades a username and password for
an ``hkey`` session key. So the password is read from the environment at most
once, on the first sync ever performed; the hkey is then cached in the state
directory and used from then on. After that first run the password env var can
be removed entirely.

Accounts with two-factor authentication enabled cannot be used: there is no
interactive step available here to satisfy the second factor.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from anki.collection import Collection
from anki.sync_pb2 import SyncAuth

from .config import Config

log = logging.getLogger(__name__)

# The hkey is a long-lived credential for the AnkiWeb account. Owner-only.
_HKEY_MODE = 0o600


class SyncCredentialsError(RuntimeError):
    """No usable AnkiWeb credentials are available."""


def _read_cached(config: Config) -> SyncAuth | None:
    path = config.hkey_path
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning("cached AnkiWeb key at %s is unreadable; ignoring it", path)
        return None
    hkey = data.get("hkey")
    if not hkey:
        return None
    return SyncAuth(hkey=hkey, endpoint=data.get("endpoint") or None)


def store_hkey(config: Config, hkey: str, endpoint: str | None) -> None:
    """Cache the session key so later syncs don't need the password."""
    path = config.hkey_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"hkey": hkey, "endpoint": endpoint})
    # Create with restrictive permissions rather than chmod-ing afterwards,
    # which would leave a window where the key is world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _HKEY_MODE)
    with os.fdopen(fd, "w") as handle:
        handle.write(payload)
    os.chmod(path, _HKEY_MODE)


def resolve_auth(config: Config, col: Collection) -> SyncAuth:
    """Return usable AnkiWeb credentials, logging in if we have none cached.

    Must be called on the collection's owning thread: ``sync_login`` goes
    through the same Rust backend as everything else.
    """
    cached = _read_cached(config)
    if cached is not None:
        return cached

    if not (config.ankiweb_username and config.ankiweb_password):
        raise SyncCredentialsError(
            "No AnkiWeb credentials. There is no cached session key at "
            f"{config.hkey_path}, so ANKIWEB_USERNAME and ANKIWEB_PASSWORD must "
            "be set for the first sync. They can be removed once the key is "
            "cached. Accounts with 2FA enabled are not supported."
        )

    auth = col.sync_login(
        config.ankiweb_username,
        config.ankiweb_password,
        config.ankiweb_endpoint,
    )
    store_hkey(config, auth.hkey, auth.endpoint or None)
    log.info("logged in to AnkiWeb; session key cached at %s", config.hkey_path)
    return auth
