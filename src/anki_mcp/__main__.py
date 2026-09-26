"""Entrypoint.

Order matters: config is validated, then the collection is opened, and only
then does the server start listening. A failure in either of the first two
steps exits non-zero -- this server never comes up half-working, because a
container that answers health checks while unable to reach the collection is
worse than one that visibly restarts.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from . import telemetry
from .auth import StaticBearerVerifier
from .collection import CollectionWorker
from .config import AuthMode, ConfigError, load
from .oauth.totp import InvalidTOTPSecret
from .errors import CollectionLockedError
from .server import build

log = logging.getLogger("anki_mcp")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    try:
        config = load()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2

    worker = CollectionWorker(config.collection_path)
    try:
        worker.open()
    except CollectionLockedError as exc:
        log.error("%s", exc)
        return 3

    # Everything from here on runs with the collection open, so it all sits
    # inside one try/finally. A failure in build() -- an unusable TOTP secret,
    # say -- previously escaped past the finally, skipping worker.close() and
    # printing a stack trace instead of a startup error with an exit code.
    try:
        # Telemetry after config (it needs the environment name) but before the
        # server starts, so startup problems are shipped too. A missing OTLP
        # endpoint disables it silently -- the collection must stay servable
        # whether or not Grafana is reachable.
        telemetry.setup(
            service_name="anki-mcp",
            environment=config.environment,
            version="0.1.0",
        )
        telemetry.emit(
            telemetry.EVENT_STARTUP,
            profile=config.profile.value,
            auth_mode=config.auth_mode.value,
            port=config.port,
        )
        _emit_collection_stats(worker)

        # In oauth mode the SDK builds its own verifier from the provider.
        verifier = (
            StaticBearerVerifier(config.bearer_token)
            if config.auth_mode is AuthMode.BEARER and config.bearer_token
            else None
        )
        try:
            _, app = build(config, worker, verifier)
        except (ConfigError, InvalidTOTPSecret) as exc:
            log.error("configuration error: %s", exc)
            return 2

        log.info(
            "serving MCP on http://%s:%s/mcp (profile=%s, auth=%s)",
            config.host,
            config.port,
            config.profile.value,
            config.auth_mode.value,
        )
        uvicorn.run(app, host=config.host, port=config.port, log_level="info")
    finally:
        telemetry.shutdown()
        worker.close()
    return 0


def _emit_collection_stats(worker: CollectionWorker) -> None:
    """One gauge-style event at startup, backing the 'how big is it' panels.

    Emitted once rather than on a timer: these are cheap SQL counts, but the
    worker thread is the same one serving tool calls, so a background poller
    would contend with real work for no real benefit.
    """
    from anki.collection import Collection

    def op(col: Collection) -> dict[str, int]:
        tree = col.sched.deck_due_tree()
        return {
            "notes": col.note_count(),
            "cards": col.card_count(),
            "decks": len(col.decks.all_names_and_ids()),
            "note_types": len(col.models.all_names_and_ids()),
            "due_new": tree.new_count,
            "due_learning": tree.learn_count,
            "due_review": tree.review_count,
        }

    try:
        telemetry.emit(telemetry.EVENT_COLLECTION_STATS, **worker.run_sync(op))
    except Exception:
        # Stats are nice to have; never let them stop the server booting.
        log.exception("could not emit collection stats")


if __name__ == "__main__":
    sys.exit(main())
