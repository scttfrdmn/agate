"""Collaborative rooms endpoint (#116, vision §7) — the live transport.

The social surface: humans AND agents as bounded participants in a scope-bounded space. This
Lambda is the EFFECT half — the pure room algebra lives in `agate.rooms` (N-way scope
INTERSECTION, attributed messages, transcript = SavedSession). Transport is **polling over an
IAM-authed Function URL** (NOT WebSocket — that bills per-connection-minute, breaking NO CLOCKS):
op-dispatched open/join/leave/post/events; the SPA polls `events?since=<cursor>`.

The load-bearing invariants, enforced HERE on every request:
  * Identity (tenant/scope/tier/subject) is derived from the VERIFIED IdP token, never the body.
  * The room's scope/tier are RE-DERIVED server-side from the members via `agate.rooms` on every
    mutation — a disjoint member is refused (fail-closed `RoomError` → 403), never widening the
    room to tenant-wide. A stored scope is never trusted.
  * Every message carries an `ActingAs` recovered from the verified session (unforgeable).
  * `post` is budget-gated under EVERY member's remaining budget (`evaluate_cascade` over
    `room_cascade_nodes`); reject names the breaching member; nothing is appended or debited.
  * The room object is read/written through a tenant-fenced role the handler ASSUMES with the
    verified `agate:` tags (the #130/#118 lesson — the writing principal carries the tags the
    `room_rw_policy` bucket fence enforces; the Lambda's own role only `sts:AssumeRole`s it).

Per-request Lambda, NO CLOCKS. Fails closed. Last-write-wins on the room object (a conditional
write is a follow-up; fine for a lab-meeting cadence).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from decimal import Decimal

import boto3
from agate.agent_record import from_json as agent_from_json
from agate.agentspec import parse_spec
from agate.delegate import delegate
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.room_record import (
    RoomRecordError,
    build_saved_room,
    room_object_key,
)
from agate.room_record import (
    from_json as room_from_json,
)
from agate.rooms import (
    Member,
    Room,
    RoomError,
    add_member,
    open_room,
    remove_member,
    room_cascade_nodes,
    room_message,
)
from agate.tags import ClaimsError, SessionTags, claims_to_tags, role_session_name
from cost.precall import evaluate_cascade
from meter import read_spend_item, scope_pk

REGION = os.environ.get("AGATE_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
DOCS_BUCKET = os.environ.get("AGATE_DOCS_BUCKET", "")
SPEND_TABLE = os.environ.get("AGATE_SPEND_TABLE", "")
BUDGET_TABLE = os.environ.get("AGATE_BUDGET_TABLE", "")
# The tenant-fenced role the handler assumes (with the verified tags) to read/write the room
# object — so the principal that touches S3 carries the tags `room_rw_policy` fences.
ROOM_ROLE_ARN = os.environ.get("AGATE_ROOM_ROLE_ARN", "")
# Worst-case token shape for pricing a room message's gated cost (a turn the message drives).
MSG_MODEL_ID = os.environ.get("AGATE_ROOM_MODEL_ID", "us.anthropic.claude-opus-4-1-20250805-v1:0")
MSG_INPUT_TOKENS = int(os.environ.get("AGATE_ROOM_INPUT_TOKENS", "4000"))
MSG_MAX_TOKENS = int(os.environ.get("AGATE_ROOM_MAX_TOKENS", "1024"))

_sts = boto3.client("sts", region_name=REGION)
_ddb = boto3.resource("dynamodb", region_name=REGION)


class RoomsToolError(ValueError):
    """A rooms request that cannot be served safely. Fail closed."""


def validate_idp_token(token: str) -> dict:
    if not token or not isinstance(token, str):
        raise RoomsToolError("missing idp_token")
    try:
        return verify_token(token, **config_from_env())
    except TokenError as exc:
        raise RoomsToolError(f"token verification failed: {exc}") from exc


def _identity(req: dict) -> tuple[SessionTags, str]:
    claims = validate_idp_token(req.get("idp_token", ""))
    try:
        tags = claims_to_tags(claims)
    except ClaimsError as exc:
        raise RoomsToolError(f"cannot scope session: {exc}") from exc
    subject = str(claims.get("sub") or claims.get("subject") or "agate-user")
    return tags, subject


def _period() -> str:
    """The current billing period `YYYY-MM` — stamped at the live edge (NO CLOCKS in the pure
    modules); mirrors meter.parse._period_from_timestamp's format."""
    return time.strftime("%Y-%m", time.gmtime())


def _assume_room_client(tags: SessionTags, subject: str):
    """Assume the tenant-fenced room role with the verified `agate:` tags → a scoped S3 client.
    The tags travel on the assumed session, so `room_rw_policy`'s `${aws:PrincipalTag/...}` fence
    binds the credential that reads/writes the room object (the #84/#130 discipline)."""
    if not ROOM_ROLE_ARN:
        raise RoomsToolError("AGATE_ROOM_ROLE_ARN not configured")
    sts_tags = tags.to_sts_tags()
    resp = _sts.assume_role(
        RoleArn=ROOM_ROLE_ARN,
        RoleSessionName=role_session_name(tags.tenant, subject),
        Tags=sts_tags,
        TransitiveTagKeys=[t["Key"] for t in sts_tags],
        DurationSeconds=900,
    )
    c = resp["Credentials"]
    return boto3.client(
        "s3",
        region_name=REGION,
        aws_access_key_id=c["AccessKeyId"],
        aws_secret_access_key=c["SecretAccessKey"],
        aws_session_token=c["SessionToken"],
    )


def _clean_nonce(raw: object) -> str:
    """A server-side room id from a client nonce + subject, sanitised to the key grammar so a
    client can't target another room's key (`_clean_id` strips `/`)."""
    from agate.budget import _clean_id

    n = _clean_id(str(raw or ""))
    return n


def _load_room(s3, tenant: str, room_id: str) -> tuple[Room, list[dict], str]:
    """Read + parse a room object, RE-DERIVING its scope/tier from members. Returns
    (room, messages, key). The read goes through the assumed (tenant-fenced) client, so a room
    in another tenant is denied by IAM; membership is checked by the caller."""
    key = room_object_key(tenant, room_id)
    try:
        body = s3.get_object(Bucket=DOCS_BUCKET, Key=key)["Body"].read()
    except Exception as exc:  # noqa: BLE001 — missing/forbidden object → not found, fail closed
        raise RoomsToolError(f"room not found: {room_id}") from exc
    saved = room_from_json(body.decode("utf-8"))
    # Re-derive scope/tier from the members (never trust the stored scope) by rebuilding the
    # room member-by-member through the pure algebra — a disjoint stored member would raise.
    members = saved.room.members
    if not members:
        raise RoomsToolError("room has no members")
    room = open_room(members[0].tags, room_id=saved.room.id, subject=members[0].subject)
    room = Room(  # preserve the first member's real kind (open_room assumes human)
        id=room.id, tenant=room.tenant, members=(members[0],), scope=room.scope, tier=room.tier
    )
    for m in members[1:]:
        room = add_member(room, m)
    return room, list(saved.messages), key


def _persist(s3, room: Room, messages: list[dict], room_id: str) -> str:
    """Write the room object under its tenant-rooted `_rooms/` key (the write fence confines
    this to the caller's own tenant). Returns the key."""
    key = room_object_key(room.tenant, room_id)
    s3.put_object(
        Bucket=DOCS_BUCKET,
        Key=key,
        Body=build_saved_room(room, messages).to_json().encode("utf-8"),
        ContentType="application/json",
    )
    return key


def _room_view(room: Room, messages: list[dict], *, since: int = 0) -> dict:
    """The client-facing view: members + derived scope/tier + messages after `since` + the
    next cursor (the log length). Never returns a credential."""
    return {
        "ok": True,
        "room": room.id,
        "scope": room.scope,
        "tier": room.tier,
        "members": [{"kind": m.kind, "subject": m.subject} for m in room.members],
        "messages": messages[since:],
        "cursor": len(messages),
    }


# --- ops --------------------------------------------------------------------


def op_open(req: dict, tags: SessionTags, subject: str) -> dict:
    room_id = _clean_nonce(req.get("nonce")) or _clean_nonce(subject + "-room")
    if not room_id:
        raise RoomsToolError("could not derive a room id")
    room = open_room(tags, room_id=room_id, subject=subject)
    s3 = _assume_room_client(tags, subject)
    _persist(s3, room, [], room_id)
    return _room_view(room, [])


def _agent_member(req: dict, joiner: SessionTags, s3) -> Member:
    """Build an AGENT member from a created-agent record the joiner names. The agent's
    credential is `delegate`d from the JOINER's tags (so agent ⊆ joiner ⊆ room) — never the
    agent's own stored scope, which could be broader than this joiner."""
    agent_name = str(req.get("agent") or "")
    if not agent_name:
        raise RoomsToolError("agent member requires an agent name")
    from agate.agent_record import agent_object_key

    key = agent_object_key(joiner.tenant, joiner.scope, agent_name)
    try:
        body = s3.get_object(Bucket=DOCS_BUCKET, Key=key)["Body"].read()
    except Exception as exc:  # noqa: BLE001
        raise RoomsToolError(f"agent not found in your scope: {agent_name}") from exc
    spec = parse_spec(agent_from_json(body.decode("utf-8")).spec)
    child = delegate(joiner, spec, subject=joiner.tenant)  # clamp the agent to the joiner
    agent_id = f"{joiner.tenant}/{spec.name}"
    return Member(kind="agent", subject=agent_id, tags=child)


def op_join(req: dict, tags: SessionTags, subject: str) -> dict:
    room_id = _clean_nonce(req.get("room"))
    if not room_id:
        raise RoomsToolError("missing room")
    s3 = _assume_room_client(tags, subject)
    room, messages, _ = _load_room(s3, tags.tenant, room_id)
    if req.get("agent"):
        member = _agent_member(req, tags, s3)
    else:
        member = Member(kind="human", subject=subject, tags=tags)
    room = add_member(room, member)  # re-derives scope/tier; RoomError on disjoint
    _persist(s3, room, messages, room_id)
    return _room_view(room, messages)


def op_leave(req: dict, tags: SessionTags, subject: str) -> dict:
    room_id = _clean_nonce(req.get("room"))
    if not room_id:
        raise RoomsToolError("missing room")
    s3 = _assume_room_client(tags, subject)
    room, messages, _ = _load_room(s3, tags.tenant, room_id)
    # A caller may remove ITSELF, or an AGENT member it shares the room with (an agent has no
    # session of its own to leave). A caller may NOT evict another HUMAN — that would be a
    # griefing/authorization gap (evicting peers, relaxing the room's collective ceiling). The
    # target defaults to the caller; a named target must be an agent member.
    target = str(req.get("member") or subject)
    if target != subject:
        tgt = next((m for m in room.members if m.subject == target), None)
        if tgt is None or tgt.kind != "agent":
            raise RoomsToolError("can only remove yourself or an agent member")
    room = remove_member(room, target)
    if not room.members:
        # Last member out — leave the (now empty) object; closing/transcript is a follow-up.
        _persist(s3, room, messages, room_id)
        return {"ok": True, "room": room_id, "closed": True}
    _persist(s3, room, messages, room_id)
    return _room_view(room, messages)


def _member_spend_lookup(tenant: str, period: str):
    """A `(Member) -> (spend, budget|None)` lookup over the live spend/budget tables, keyed by
    each member's subject (the per-member budget cascade, #116). Agent members key on their
    agent id; a missing budget row → no cap at that member (evaluate_cascade skips it)."""
    spend_tbl = _ddb.Table(SPEND_TABLE) if SPEND_TABLE else None
    budget_tbl = _ddb.Table(BUDGET_TABLE) if BUDGET_TABLE else None

    def lookup(member: Member) -> tuple[float, float | None]:
        spend = read_spend_item(spend_tbl, tenant, member.subject, period) if spend_tbl else 0.0
        budget: float | None = None
        if budget_tbl is not None:
            from meter import spend_key

            item = budget_tbl.get_item(Key={"pk": spend_key(tenant, member.subject, period)}).get(
                "Item"
            )
            if item and "budget_usd" in item:
                budget = float(item["budget_usd"])
        return spend, budget

    return lookup


def _debit(tenant: str, room: Room, period: str, cost: float) -> None:
    """Add `cost` USD to each member's scope spend row (the room debit). Mirrors the chokepoint
    Decimal-ADD upsert so the key format can't drift. Best-effort: a metering write must not
    fail an already-accepted message."""
    if not SPEND_TABLE or cost <= 0:
        return
    table = _ddb.Table(SPEND_TABLE)
    for m in room.members:
        with contextlib.suppress(Exception):  # best-effort debit; must not fail a served msg
            table.update_item(
                Key={"pk": scope_pk(tenant, m.subject, period)},
                UpdateExpression="ADD spend_usd :a",
                ExpressionAttributeValues={":a": Decimal(str(round(cost, 6)))},
            )


def op_post(req: dict, tags: SessionTags, subject: str) -> dict:
    room_id = _clean_nonce(req.get("room"))
    text = str(req.get("text") or "").strip()
    if not room_id or not text:
        raise RoomsToolError("missing room or text")
    s3 = _assume_room_client(tags, subject)
    room, messages, _ = _load_room(s3, tags.tenant, room_id)

    # The author is the calling member (matched by verified subject) — never a body field.
    member = next((m for m in room.members if m.subject == subject), None)
    if member is None:
        raise RoomsToolError("not a member of this room")

    # Budget gate: the message must fit under EVERY member's remaining budget.
    period = _period()
    nodes = room_cascade_nodes(room, _member_spend_lookup(tags.tenant, period))
    result = evaluate_cascade(
        model_id=MSG_MODEL_ID,
        input_tokens=MSG_INPUT_TOKENS,
        max_tokens=MSG_MAX_TOKENS,
        nodes=nodes,
    )
    if result.decision != "allow":
        return {
            "ok": False,
            "reason": f"over budget at {result.breaching_node!r}: {result.reason}",
        }

    msg = room_message(room, member, text=text)
    messages = [*messages, msg.to_dict()]
    _persist(s3, room, messages, room_id)
    _debit(tags.tenant, room, period, result.estimated_cost)
    return {"ok": True, "room": room_id, "cursor": len(messages), "message": msg.to_dict()}


def op_events(req: dict, tags: SessionTags, subject: str) -> dict:
    room_id = _clean_nonce(req.get("room"))
    if not room_id:
        raise RoomsToolError("missing room")
    since = req.get("since", 0)
    since = since if isinstance(since, int) and since >= 0 else 0
    s3 = _assume_room_client(tags, subject)
    room, messages, _ = _load_room(s3, tags.tenant, room_id)
    if not room.has_member(subject):
        raise RoomsToolError("not a member of this room")
    return _room_view(room, messages, since=since)


_OPS = {
    "open": op_open,
    "join": op_join,
    "leave": op_leave,
    "post": op_post,
    "events": op_events,
}


def process(req: dict) -> dict:
    if not DOCS_BUCKET:
        raise RoomsToolError("AGATE_DOCS_BUCKET not configured")
    tags, subject = _identity(req)
    op = req.get("op")
    fn = _OPS.get(op)
    if fn is None:
        raise RoomsToolError(f"unknown op: {op!r}")
    return fn(req, tags, subject)


def handler(event: dict, context: object) -> dict:
    """Function URL entry point. Fail-closed: a verification/scoping/room failure returns an
    error envelope, never a silent broad action."""
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8")
        req = json.loads(body) if isinstance(body, str) else body
        return _resp(200, process(req))
    except (RoomsToolError, RoomError, RoomRecordError) as exc:
        return _resp(403, {"error": "not_entitled", "detail": str(exc)})
    except Exception:  # noqa: BLE001 — last-resort fail-closed
        return _resp(500, {"error": "rooms_error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
