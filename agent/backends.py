"""Bedrock-backed implementations of the orchestration interfaces (design §13.7).

These adapt the gateway's `Backend` / `CodeRunner` / `CostMeter` protocols (defined
in agate.panel/agate.analyze) to real AWS calls, for use inside the AgentCore Runtime
container. They are thin I/O shims; all the logic they drive lives in the pure,
fakes-tested `agate` orchestration. Kept out of `agate/` so the pure suite stays
AWS-free.

The `tier` a roster member names is resolved to a concrete Bedrock model id via the
roster config the invocation carries (the institution pins which models sit in the
panel), so no product/model name is hard-coded here.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import boto3
from agate.analyze.schema import parse_invoke_result

# Model id is carried per call as the `tier` value (the roster maps a logical tier
# label to a concrete Bedrock model id at config time). The adapter treats `tier`
# as the model id to invoke — neutral, no hard-coded product names.

# Bedrock requestMetadata values must match [a-zA-Z0-9\s:_@$#=/+,.-]{1,256}.
_META_RE = re.compile(r"[^a-zA-Z0-9\s:_@$#=/+,.-]")


def _meta_value(v: object) -> str:
    """Sanitise a value to Bedrock's requestMetadata grammar (<=256, allowed chars)."""
    return _META_RE.sub("-", str(v))[:256]


class BedrockBackend:
    """Drives Bedrock Converse for review/adjudication/codegen/ask calls.

    `request_metadata` (optional) is attached to every Converse call so the Bedrock
    invocation log carries it — that's how the authoritative-spend meter attributes
    spend per tenant/user (#77). It's an attribution hint, not a security boundary:
    the credential's ABAC tenant tag remains the real fence.
    """

    def __init__(self, region: str, request_metadata: dict[str, str] | None = None):
        self._rt = boto3.client("bedrock-runtime", region_name=region)
        # Bedrock requestMetadata: keys/values are [a-zA-Z0-9_:./=+@ -]; sanitise.
        self._meta = {k: _meta_value(v) for k, v in (request_metadata or {}).items() if v}

    def converse(
        self, tier: str, system: str, prompt: str, max_tokens: int
    ) -> tuple[str, dict[str, int], Any]:
        kwargs: dict[str, Any] = {
            "modelId": tier,
            "system": [{"text": system}] if system else [],
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max_tokens},
        }
        if self._meta:
            kwargs["requestMetadata"] = self._meta
        resp = self._rt.converse(**kwargs)
        text = "".join(block.get("text", "") for block in resp["output"]["message"]["content"])
        usage = {
            "inputTokens": resp["usage"]["inputTokens"],
            "outputTokens": resp["usage"]["outputTokens"],
        }
        return text, usage, None


class CodeInterpreterRunner:
    """Runs code in the AgentCore Code Interpreter microVM (Analyze)."""

    def __init__(self, region: str, code_interpreter_id: str):
        self._client = boto3.client("bedrock-agentcore", region_name=region)
        self._id = code_interpreter_id

    def execute(self, code: str, *, language: str = "python"):
        t0 = time.monotonic()
        resp = self._client.invoke_code_interpreter(
            codeInterpreterIdentifier=self._id,
            name="executeCode",
            arguments={"language": language, "code": code},
        )
        # The data-plane returns an event stream; collect the result event.
        raw: dict[str, Any] = {}
        for event in resp.get("stream", []):
            if "result" in event:
                raw = {"result": event["result"]}
                break
        return parse_invoke_result(raw, elapsed_s=round(time.monotonic() - t0, 3))


# Cost metering is the authoritative `cost.CostMeter` (design §7.2/§13.6), wired in
# `agent/server.py`. It satisfies the same `add_llm`/`add_compute`/`total` protocol
# the orchestration calls, so no container-local meter is needed here.


def encode_payload(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj).encode("utf-8")


def decode_payload(blob: bytes | str) -> dict[str, Any]:
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8")
    return json.loads(blob) if blob else {}
