"""Classify Verovo's frozen C-01 close codes into the shared error families.

The seven families are a vocabulary shared by Askovo, Growovo and Verovo. They
carry no verdict, no database row and no booking status; each product still owns
its objects and decisions. `close.py` remains the single source of truth for the
codes themselves. This module only classifies them, and it refuses to import if
a code is added or removed without a family, so the two files cannot drift.
"""
from __future__ import annotations

from typing import Dict, Tuple

from .close import ERROR_CODES

FAMILIES: Tuple[str, ...] = (
    "NEEDS_USER_INPUT",
    "UNSUPPORTED_CAPABILITY",
    "INVALID_INPUT",
    "PERMISSION_DENIED",
    "SAFETY_BLOCKED",
    "STALE_OR_CONFLICT",
    "CONTRACT_ERROR",
)

CODE_TO_FAMILY: Dict[str, str] = {
    "INPUT_REQUIRED": "NEEDS_USER_INPUT",
    "REVIEW_REQUIRED": "NEEDS_USER_INPUT",
    "AUTH_REQUIRED": "PERMISSION_DENIED",
    "POLICY_DENIED": "PERMISSION_DENIED",
    "UNSUPPORTED_SCOPE": "UNSUPPORTED_CAPABILITY",
    "UNSUPPORTED_CAPABILITY": "UNSUPPORTED_CAPABILITY",
    "STALE_SOURCE": "STALE_OR_CONFLICT",
    "INDETERMINATE_EXTERNAL_ACTION": "STALE_OR_CONFLICT",
    "RATE_LIMITED": "SAFETY_BLOCKED",
    "ARTIFACT_PARTIAL": "CONTRACT_ERROR",
    "INTERNAL_ERROR": "CONTRACT_ERROR",
}

_DRIFT = set(ERROR_CODES) ^ set(CODE_TO_FAMILY)
if _DRIFT:
    raise RuntimeError(
        "close error codes and families drifted apart: %s" % ", ".join(sorted(_DRIFT))
    )

_GUIDANCE: Dict[str, Tuple[str, str, str]] = {
    "INPUT_REQUIRED": (
        "a required field or source file",
        "the action cannot start and nothing is written",
        "supply the named input and rerun the same command",
    ),
    "REVIEW_REQUIRED": (
        "a human decision on an open item",
        "the period stays open; a later step will not use an unreviewed result",
        "review the open item, then rerun the command that refused",
    ),
    "AUTH_REQUIRED": (
        "a caller identity the workspace accepts",
        "no protected command runs and no file is handed out",
        "use the buyer-controlled workspace credentials; do not retry unauthenticated",
    ),
    "POLICY_DENIED": (
        "permission for this action from the buyer organisation",
        "the action is refused even though the caller is known",
        "ask the buyer organisation to grant it; this is not a retryable error",
    ),
    "UNSUPPORTED_SCOPE": (
        "a scope this product covers",
        "no result is produced for the request",
        "narrow the request to the documented scope or keep it outside Verovo",
    ),
    "UNSUPPORTED_CAPABILITY": (
        "an environment capability (for example a cross-process lock)",
        "the command refuses rather than pretending the capability exists",
        "run it where the capability exists; do not treat the refusal as partial success",
    ),
    "STALE_SOURCE": (
        "the source that was actually reviewed",
        "the earlier review no longer applies and cannot be booked",
        "re-import and re-review the changed source; the old review is void",
    ),
    "INDETERMINATE_EXTERNAL_ACTION": (
        "confirmation that the external action did not already happen",
        "the result is unknown; repeating the action could duplicate it",
        "check the external side first, record the finding, then resume deliberately",
    ),
    "RATE_LIMITED": (
        "nothing; this is an external throttle",
        "the call was refused before any business change",
        "wait for the stated cooldown, reduce the request cadence (space calls, send "
        "smaller batches, avoid retrying inside the cooldown window), then retry the "
        "same command",
    ),
    "ARTIFACT_PARTIAL": (
        "a complete artefact set for this step",
        "downstream steps must not read a partial bundle",
        "rerun the producing step; report the partial set if it repeats",
    ),
    "INTERNAL_ERROR": (
        "nothing the user can supply",
        "the command failed inside the product contract",
        "keep the workspace as is and report it; retrying is not the fix",
    ),
}


def semantic_code(code: str) -> str:
    """Return the shared family for a C-01 close code; unknown codes are contract errors."""
    return CODE_TO_FAMILY.get(code, "CONTRACT_ERROR")


def describe(code: str) -> Dict[str, str]:
    """Return the four user-facing parts for a close code: what, impact, next action."""
    missing, impact, next_action = _GUIDANCE.get(
        code,
        (
            "nothing the user can supply",
            "the command failed outside the registered error contract",
            "keep the workspace as is and report it",
        ),
    )
    return {
        "code": code,
        "family": semantic_code(code),
        "missing": missing,
        "impact": impact,
        "next_action": next_action,
    }
