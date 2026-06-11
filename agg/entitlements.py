"""Tier -> entitled-model map and affiliation -> tier derivation.

THE SINGLE SOURCE OF TRUTH for "which models may a session invoke" (design §13.2).
Both the broker Lambda (to derive `agg:tier`) and the generated IAM model-access
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
TIER_MODELS: dict[Tier, tuple[str, ...]] = {
    # Rung-0 open-weight line (design §2.5): gpt-oss, Gemma, etc.
    "oss": (
        "openai.gpt-oss-20b-1:0",
        "openai.gpt-oss-120b-1:0",
        "google.gemma-2-9b-it-1:0",
        "mistral.mistral-7b-instruct-v0:2",
    ),
    # Mid: solid general models below the frontier price point.
    "mid": (
        "anthropic.claude-3-5-haiku-20241022-v1:0",
        "mistral.mistral-large-2407-v1:0",
        "meta.llama3-3-70b-instruct-v1:0",
    ),
    # Frontier: the most capable (and most expensive) models.
    "frontier": (
        "anthropic.claude-sonnet-4-5-20250929-v1:0",
        "anthropic.claude-opus-4-1-20250805-v1:0",
        "openai.gpt-5-2025-08-07",
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


def foundation_model_arn(model_id: str, region: str = "*", account: str = "") -> str:
    """Build the Bedrock resource ARN for a model id.

    Foundation models are account-less (`arn:aws:bedrock:{region}::foundation-model/{id}`);
    inference-profile ids (prefixed `us.`/`eu.`/`apac.`) live under the account.
    Region defaults to a wildcard so one generated policy works across the regions
    an institution enables; pin it at deploy time if you need to constrain region.
    """
    if model_id.split(".", 1)[0] in ("us", "eu", "apac"):
        return f"arn:aws:bedrock:{region}:{account}:inference-profile/{model_id}"
    return f"arn:aws:bedrock:{region}::foundation-model/{model_id}"


def model_arns_for_tier(tier: Tier, region: str = "*", account: str = "") -> list[str]:
    """Resource ARNs for every model entitled at `tier` — the IAM policy input."""
    return [foundation_model_arn(m, region, account) for m in models_for_tier(tier)]
