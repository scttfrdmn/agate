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
    # ORDERED BY ASCENDING CAPABILITY within the tier: the router treats index 0 as the weakest/
    # thrifty default and index -1 as the "best" model (`select_model`, #263). gpt-oss-120b is the
    # strongest open-weight coder, so it's last; the small Gemma is first.
    "oss": (
        "google.gemma-3-4b-it",
        "google.gemma-3-12b-it",
        "openai.gpt-oss-20b-1:0",
        "openai.gpt-oss-120b-1:0",
    ),
    # Mid: solid general models below the frontier price point (inference profiles).
    "mid": (
        "us.anthropic.claude-3-5-haiku-20241022-v1:0",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    ),
    # Frontier: the most capable (inference profiles), weakest→strongest.
    "frontier": (
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-opus-4-1-20250805-v1:0",
    ),
}


# Models excluded from AUTO selection but still PINNABLE (#247 follow-up). The tiniest open-weight
# models are entitled and can be chosen deliberately, but they deflect on ordinary questions, so
# Auto never picks them as a default — a deflecting answer is a false economy (the user re-asks and
# pays twice). This is a quality FLOOR on auto-routing, not an entitlement change.
AUTO_EXCLUDED_MODELS: frozenset[str] = frozenset(
    {
        "google.gemma-3-4b-it",
        "google.gemma-3-12b-it",
    }
)


def auto_candidates(tier: Tier) -> list[str]:
    """Entitled models Auto may SELECT — `models_for_tier` minus the toy models that deflect
    (AUTO_EXCLUDED_MODELS). Falls back to the full entitled set if exclusion would leave nothing
    (fail-safe: never return empty). The excluded models remain pinnable via models_for_tier."""
    full = models_for_tier(tier)
    usable = [m for m in full if m not in AUTO_EXCLUDED_MODELS]
    return usable or full


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


def tier_for_model(model_id: str) -> Tier | None:
    """The tier a concrete model id belongs to (reverse of TIER_MODELS), or None.

    Used by the cost engine to price an unlisted model id at its TIER's rate rather
    than the cheapest fallback — `models_for_tier` is the forward map; this is the
    inverse. Returns None for an id not in the table (caller decides the fallback).
    """
    for tier, ids in TIER_MODELS.items():
        if model_id in ids:
            return tier
    return None


def supports_vision(model_id: str) -> bool:
    """Whether a model can accept image input (the Canvas result→prompt image leg, #244).

    Anthropic Claude 3+/4/5 are vision-capable; the open-weight rung (gpt-oss, Gemma text
    variants) is text-only. Conservative substring match on the id — an unknown id is treated as
    NOT vision-capable (fail closed: don't attach an image a model may reject). Pure."""
    mid = model_id.lower()
    return "claude" in mid  # entitled Claude ids are all multimodal; oss/gemma are text-only


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
