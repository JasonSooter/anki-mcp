"""Translation from anki's exceptions to messages a client can act on.

anki's backend exceptions carry their detail in the Rust layer and mostly
render as an empty ``DBError()`` / ``NotFoundError()`` from Python -- verified
against anki 26.8.1. So we never surface the original text; we supply our own.
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError as _SDKToolError

from anki.errors import (
    DBError,
    NotFoundError,
    NetworkError,
    SearchError,
    SyncError,
)


class ToolError(_SDKToolError):
    """An error meant to be read by the model calling the tool.

    Messages should say what went wrong *and* what to do about it, since the
    caller is an LLM that will try to recover on its own.

    Subclassing the SDK's ToolError is load-bearing, not decoration: the SDK
    treats that type as an *anticipated* failure and passes the message through
    to the client, while any other exception is treated as a crash and reported
    as a bare "Error executing tool <name>". Raising a plain Exception here
    would silently throw away every message in this module.
    """


class CollectionLockedError(RuntimeError):
    """The collection file is already open by another process."""


def describe_open_failure(path: str, exc: BaseException) -> str:
    if isinstance(exc, DBError):
        return (
            f"Could not open the Anki collection at {path}: the database is "
            "locked or corrupt. Anki allows exactly one writer. Check that no "
            "second anki-mcp container is running against this same directory, "
            "and that no stale process is holding the file. This server will "
            "not retry -- it exits so the problem is visible."
        )
    return f"Could not open the Anki collection at {path}: {exc!r}"


def translate(exc: BaseException) -> ToolError | None:
    """Map an anki exception to a ToolError, or None if we don't recognise it.

    Unrecognised exceptions are deliberately left to propagate rather than
    being flattened into a vague message.
    """
    if isinstance(exc, SearchError):
        return ToolError(
            "That is not valid Anki search syntax. Check for unbalanced "
            "parentheses or quotes. Examples that work: 'deck:German', "
            "'tag:noun -is:suspended', '\"exact phrase\"', 'front:der*'."
        )
    if isinstance(exc, NotFoundError):
        return ToolError(
            "No such note or card in the collection. It may have been deleted; "
            "use search_notes to find a current note id."
        )
    if isinstance(exc, NetworkError):
        return ToolError(
            "Could not reach AnkiWeb. This is a network problem on the server, "
            "not a problem with the collection; the local changes are safe and "
            "the sync can be retried."
        )
    if isinstance(exc, SyncError):
        return ToolError(
            "AnkiWeb rejected the sync. The usual causes are wrong credentials "
            "or an expired session key -- delete the cached hkey file and set "
            "ANKIWEB_USERNAME/ANKIWEB_PASSWORD to log in again. Note that "
            "AnkiWeb accounts with 2FA enabled cannot be used here."
        )
    if isinstance(exc, DBError):
        return ToolError(
            "The collection database reported an error. If this persists, "
            "check the server logs -- the collection may need to be closed and "
            "reopened."
        )
    return None
