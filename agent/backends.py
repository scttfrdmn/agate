"""Bedrock-backed implementations of the orchestration interfaces (design §13.7).

These adapt the gateway's `Backend` / `CodeRunner` / `CostMeter` protocols (defined
in agg.panel/agg.analyze) to real AWS calls, for use inside the AgentCore Runtime
container. They are thin I/O shims; all the logic they drive lives in the pure,
fakes-tested `agg` orchestration. Kept out of `agg/` so the pure suite stays
AWS-free.

The `tier` a roster member names is resolved to a concrete Bedrock model id via the
roster config the invocation carries (the institution pins which models sit in the
panel), so no product/model name is hard-coded here.
"""

from __future__ import annotations

import json
import time
from typing import Any

import boto3
from agg.analyze.schema import parse_invoke_result

# Model id is carried per call as the `tier` value (the roster maps a logical tier
# label to a concrete Bedrock model id at config time). The adapter treats `tier`
# as the model id to invoke — neutral, no hard-coded product names.


class BedrockBackend:
    """Drives Bedrock Converse for review/adjudication/codegen/ask calls."""

    def __init__(self, region: str):
        self._rt = boto3.client("bedrock-runtime", region_name=region)

    def converse(
        self, tier: str, system: str, prompt: str, max_tokens: int
    ) -> tuple[str, dict[str, int], Any]:
        resp = self._rt.converse(
            modelId=tier,
            system=[{"text": system}] if system else [],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens},
        )
        text = "".join(
            block.get("text", "")
            for block in resp["output"]["message"]["content"]
        )
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


class RunMeter:
    """Thread-safe cost meter. Uses the shared CostMeter pricing when wired; here a
    minimal running total is kept so the container always emits coherent cost events
    even before the full pricing engine (Phase 5 of design §12) is attached."""

    def __init__(self):
        self._total = 0.0
        self.rows: list[dict[str, Any]] = []
        import threading

        self._lock = threading.Lock()

    @property
    def total(self) -> float:
        return self._total

    def add_llm(self, label: str, tier: str, model_label: str, usage: dict[str, int]) -> float:
        # Placeholder per-token estimate until the authoritative pricing engine is
        # wired; the SPA shows this as a non-authoritative live figure (design §7.2).
        cost = (usage.get("inputTokens", 0) + usage.get("outputTokens", 0)) * 1e-6
        with self._lock:
            self._total += cost
            self.rows.append({"label": label, "kind": "llm", "cost": round(cost, 6)})
        return cost

    def add_compute(self, label: str, seconds: float) -> float:
        cost = seconds * 1e-4
        with self._lock:
            self._total += cost
            self.rows.append({"label": label, "kind": "compute", "cost": round(cost, 6)})
        return cost


def encode_payload(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj).encode("utf-8")


def decode_payload(blob: bytes | str) -> dict[str, Any]:
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8")
    return json.loads(blob) if blob else {}
