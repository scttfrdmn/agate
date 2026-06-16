"""AG-UI — the event-stream governor (#119 slice, vision §8.6).

AG-UI is the open streaming protocol for agent state/events to a UI — it replaces agate's
bespoke event protocol, and "the event stream is still scope-tagged and attributed, so a live
UI shows only what the credential authorized." Per §0.1, agenkit owns the wire (the
SSE/WebSocket transport + the SPA reducer); **agate owns the authority under it** — that is
this module, the last #119 slice.

agate's job: the stream carries only what the session's credential authorized. `govern_event`
stamps every event with its scope + the #137 `ActingAs` (so the UI + the audit see WHO emitted
it on WHOSE authority) and **DROPS** any event whose explicit scope the session doesn't
contain — the same `delegate._contains` containment the #106 delegation + #116 rooms use,
tenant-fenced. An event with no explicit scope is in-scope by construction (the session's own
run, whose data was already fenced by retrieval #84) — it's stamped with the session scope and
kept. So a cross-scope pane (e.g. a room peer's sub-scope a member shouldn't see) is filtered
before it ever reaches the wire.

`governed_emit` wraps any existing `Emit` sink (`agate.contracts.Emit`) so the governor sits at
the SINGLE emit choke point — every orchestration (router/panel/analyze/dispatch) inherits it
unchanged, no rewrite of the scattered `emit({...})` call sites.

PURE and AWS-free: an emit-time filter over the session's own scope. It adds NO new authority
(the data was already fenced at retrieval); it makes "only in-scope, attributed events stream"
provable. The `acting_as` comes from the verified session (never a client field); the scope
from the session tags. agenkit streams + renders; agate decides which events may stream.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agate.delegate import _contains
from agate.identity import ActingAs
from agate.tags import SessionTags


def _event_scope_ok(event_scope: str, tags: SessionTags) -> bool:
    """Is an event's EXPLICIT scope tag within the session's reach? Reuses `_contains` (the
    #106 containment): the session scope must contain the event scope (ancestor-or-self). A
    `..`/garbled event scope, or one the session doesn't contain, is out of scope (fail-closed,
    handled by the caller dropping it). An empty event scope is treated as 'no explicit tag'
    by the caller, not here."""
    # `_contains` already strips `/` and is path-segment-wise; a `..` segment makes the event
    # scope not equal-or-under the session scope, so it won't be contained -> dropped.
    if ".." in event_scope.split("/"):
        return False
    return _contains(tags.scope, event_scope)


def govern_event(
    event: dict[str, Any], *, tags: SessionTags, acting_as: ActingAs | None = None
) -> dict[str, Any] | None:
    """Govern ONE run event for the AG-UI stream. Returns a NEW stamped dict (the original is
    never mutated), or None if the event is OUT OF SCOPE and must be dropped.

    * An event carrying an explicit `scope` the session does NOT contain (or a different
      tenant via that scope, or a `..`-garbled scope) → dropped (the headline filter).
    * An event with no explicit `scope` is in-scope by construction → kept, stamped with the
      session's own scope.
    * Every kept event is stamped with `scope` (the resolved in-scope value) + `actingAs`
      (the #137 attribution dict, if a record is supplied) — so the stream is attributed.
    """
    explicit = event.get("scope")
    if explicit is not None and explicit != "":
        event_scope = str(explicit)
        if not _event_scope_ok(event_scope, tags):
            return None  # out of scope — never streamed
        resolved_scope = _contains_value(tags.scope, event_scope)
    else:
        resolved_scope = tags.scope  # in-scope by construction

    stamped = dict(event)  # copy — never mutate the caller's event
    stamped["scope"] = resolved_scope
    if acting_as is not None:
        stamped["actingAs"] = acting_as.to_dict()
    return stamped


def _contains_value(session_scope: str, event_scope: str) -> str:
    """The resolved scope to stamp for a contained event: the event's own (narrower) scope,
    path-normalised. (The containment was already checked; this just returns the deeper node
    so the stamp records the precise scope, not the session's broader one.)"""
    return event_scope.strip("/")


def governed_emit(
    emit: Callable[[dict[str, Any]], None],
    *,
    tags: SessionTags,
    acting_as: ActingAs | None = None,
) -> Callable[[dict[str, Any]], None]:
    """Wrap an `Emit` sink so every event passes through `govern_event` before reaching it: a
    kept event is stamped + forwarded; an out-of-scope event is silently dropped (it never
    streams — the audit is its absence). Returns a new sink with the SAME `Emit` signature, so
    it wires in at the ONE choke point (e.g. `emit = governed_emit(events.append, tags=…)`)
    and every orchestration's scattered `emit({...})` calls inherit the governance unchanged."""

    def _emit(event: dict[str, Any]) -> None:
        stamped = govern_event(event, tags=tags, acting_as=acting_as)
        if stamped is not None:
            emit(stamped)

    return _emit
