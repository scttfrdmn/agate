"""Tier -> entitled-model map and affiliation -> tier derivation.

THE SINGLE SOURCE OF TRUTH for "which models may a session invoke" (design §13.2).
Both the broker Lambda (to derive `agate:tier`) and the generated IAM model-access
policy (to scope `bedrock:Converse*`) read from this table. Do NOT express the
tier->model map as inline branches anywhere else — generate it from here.

Pure data + pure functions. No AWS, no boto3, no I/O.
"""

from __future__ import annotations

from typing import Literal

# --- Affiliation (eduPerson) ------------------------------------------------
# Normalised set we recognise. eduPersonAffiliation also defines "member",
# "affiliate", "alum", "library-walk-in"; we map the ones that carry entitlement
# and treat everything else as the least-privileged default.
Affiliation = Literal["student", "faculty", "staff", "researcher"]
AFFILIATIONS: tuple[Affiliation, ...] = ("student", "faculty", "staff", "researcher")

# --- Tier -------------------------------------------------------------------
Tier = Literal["oss", "mid", "frontier"]
TIERS: tuple[Tier, ...] = ("oss", "mid", "frontier")

# Affiliation -> tier (design §5). A grant-tagged researcher reaches frontier;
# faculty/staff reach mid; students get the open-weight floor. Unknown/least
# affiliation falls back to the oss floor (least privilege).
AFFILIATION_TIER: dict[Affiliation, Tier] = {
    "student": "oss",
    "staff": "mid",
    "faculty": "mid",
    "researcher": "frontier",
}
DEFAULT_TIER: Tier = "oss"

# Tiers are cumulative: a higher tier may invoke everything below it.
TIER_RANK: dict[Tier, int] = {"oss": 0, "mid": 1, "frontier": 2}

# --- Model entitlement ------------------------------------------------------
# Bedrock model identifiers per tier, LEAST-privilege first. These are the
# on-demand foundation-model / inference-profile IDs used with Converse.
# Cumulative: an entitlement for a tier includes all lower tiers (see
# models_for_tier). Verify exact IDs against the Bedrock console for the target
# region before deploy — IDs drift and not every model is in every region.
# IDs verified present + correctly-typed in us-east-1 (live, 2026-06-12). The
# open-weight gpt-oss/gemma line are on-demand FOUNDATION MODELS (bare id ->
# foundation-model ARN). Anthropic Claude 4.x are INFERENCE-PROFILE-only models, so
# they are listed with the `us.` cross-region profile prefix, which
# foundation_model_arn() routes to an inference-profile ARN. Mixing the two ARN
# shapes is deliberate and required: a bare Claude id fails Converse with a
# ValidationException (surfaced live during the Phase 1 deploy).
TIER_MODELS: dict[Tier, tuple[str, ...]] = {
    # Rung-0 open-weight line (design §2.5): gpt-oss, Gemma — on-demand FMs.
    "oss": (
        "openai.gpt-oss-20b-1:0",
        "openai.gpt-oss-120b-1:0",
        "google.gemma-3-12b-it",
        "google.gemma-3-4b-it",
    ),
    # Mid: solid general models below the frontier price point (inference profiles).
    "mid": (
        "us.anthropic.claude-3-5-haiku-20241022-v1:0",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    ),
    # Frontier: the most capable (inference profiles).
    "frontier": (
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-opus-4-1-20250805-v1:0",
    ),
}


def derive_tier(affiliation: Affiliation | str | None, *, grant: bool = False) -> Tier:
    """Derive the model tier from affiliation (+ optional grant flag).

    A grant-tagged principal is promoted to frontier regardless of base
    affiliation (design §5: "researcher (grant-tagged) -> frontier"). Unknown
    affiliation falls back to the least-privileged tier.
    """
    if grant:
        return "frontier"
    if affiliation in AFFILIATION_TIER:
        return AFFILIATION_TIER[affiliation]  # type: ignore[index]
    return DEFAULT_TIER


def models_for_tier(tier: Tier) -> list[str]:
    """All model IDs a session at `tier` may invoke (cumulative, lower tiers included)."""
    rank = TIER_RANK[tier]
    out: list[str] = []
    for t in TIERS:
        if TIER_RANK[t] <= rank:
            out.extend(TIER_MODELS[t])
    return out


_PROFILE_PREFIXES = ("us", "eu", "apac")


def _is_inference_profile(model_id: str) -> bool:
    return model_id.split(".", 1)[0] in _PROFILE_PREFIXES


def foundation_model_arn(model_id: str, region: str = "*", account: str = "") -> str:
    """Build the primary Bedrock resource ARN for a model id.

    Foundation models are account-less (`arn:aws:bedrock:{region}::foundation-model/{id}`);
    inference-profile ids (prefixed `us.`/`eu.`/`apac.`) live under the account.
    Region defaults to a wildcard so one generated policy works across the regions
    an institution enables; pin it at deploy time if you need to constrain region.
    """
    if _is_inference_profile(model_id):
        return f"arn:aws:bedrock:{region}:{account}:inference-profile/{model_id}"
    return f"arn:aws:bedrock:{region}::foundation-model/{model_id}"


def model_resource_arns(model_id: str, region: str = "*", account: str = "") -> list[str]:
    """ALL resource ARNs an InvokeModel call against `model_id` needs.

    Invoking a cross-region inference profile (e.g. `us.anthropic.claude-...`)
    requires `bedrock:InvokeModel` on BOTH the profile ARN AND the underlying
    foundation-model ARN it routes to — and a cross-region profile may route to any
    of its member regions, so the foundation-model ARN uses a region wildcard.
    Verified live (2026-06-12): granting only the profile ARN yields AccessDenied
    on `foundation-model/<bare id>`. A plain foundation model needs just its own ARN.
    """
    if not _is_inference_profile(model_id):
        return [foundation_model_arn(model_id, region, account)]
    bare_id = model_id.split(".", 1)[1]  # strip the us./eu./apac. prefix
    return [
        foundation_model_arn(model_id, region, account),  # the profile ARN
        # The underlying FM, reachable in any of the profile's member regions.
        f"arn:aws:bedrock:*::foundation-model/{bare_id}",
    ]


def model_arns_for_tier(tier: Tier, region: str = "*", account: str = "") -> list[str]:
    """Resource ARNs for every model entitled at `tier` — the IAM policy input.

    Flattens each model to all ARNs its invocation needs (profile + underlying FM
    for inference profiles), de-duplicated while preserving order.
    """
    seen: dict[str, None] = {}
    for m in models_for_tier(tier):
        for arn in model_resource_arns(m, region, account):
            seen.setdefault(arn, None)
    return list(seen)
