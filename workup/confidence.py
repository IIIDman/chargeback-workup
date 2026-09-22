"""Confidence tier for the analyst queue, computed from checkable signals rather than the model's own opinion.

Tier is one of high / medium / needs_review, always with the list of reasons that produced it. The signals:

- rule coverage: how many requirements are effectively satisfied vs how many the rule needs
- agreement between the model's recommended action and that coverage
- quote verification results (downgrades, unverified pointers, image-only support)
- deterministic pre-check flags that contradict a represent recommendation (postcode mismatch, AVS/CVV fail)
- non-representable codes, where the action is forced by rule and the evidence is irrelevant
- the model's self-reported confidence, as a minor input

The queue is sorted by tier so that confident cases take a minute and doubtful ones get attention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .checks import PreChecks
from .rules import ReasonCode
from .schema import Action, Status, Workup
from .verify import RequirementVerification

Tier = Literal["high", "medium", "needs_review"]
_RANK = {"high": 0, "medium": 1, "needs_review": 2}


@dataclass
class Assessment:
    tier: Tier
    reasons: list[str] = field(default_factory=list)
    final_action: Action = Action.accept_liability
    action_overridden: bool = False  # rule forced a different action than the model proposed
    rule_met: bool = False
    satisfied_count: int = 0
    required_count: int = 0
    applicable_count: int = 0


def coverage(rule: ReasonCode, verifications: list[RequirementVerification]) -> tuple[bool, int, int, int]:
    """Return (rule_met, satisfied, required, applicable) using effective (post-verification) statuses."""
    statuses = {v.requirement_id: v.effective_status for v in verifications}
    applicable = [s for s in statuses.values() if s is not Status.not_applicable]
    satisfied = sum(1 for s in applicable if s is Status.satisfied)
    if rule.logic == "non_representable":
        return False, 0, 0, 0
    if rule.logic == "all":
        required = len(applicable)
        return (len(applicable) > 0 and satisfied == required), satisfied, required, len(applicable)
    required = rule.required_count
    return satisfied >= required, satisfied, required, len(applicable)


def assess(rule: ReasonCode, prechecks: PreChecks, workup: Workup,
           verifications: list[RequirementVerification]) -> Assessment:
    a = Assessment(tier="high", final_action=workup.recommended_action)
    sev: list[Tier] = []

    def add(tier: Tier, reason: str) -> None:
        sev.append(tier)
        a.reasons.append(reason)

    # 1. non-representable: the rule decides, evidence is irrelevant
    if rule.logic == "non_representable":
        a.final_action = Action.accept_liability
        a.reasons.append(f"{rule.scheme.title()} {rule.code} is non-representable under the rule; evidence not assessed")
        if workup.recommended_action is not Action.accept_liability:
            a.action_overridden = True
            add("medium", f"model proposed {workup.recommended_action.value}; overridden to accept_liability by rule")
        a.tier = max(sev, key=_RANK.get, default="high")
        return a

    # 2. coverage vs recommendation
    a.rule_met, a.satisfied_count, a.required_count, a.applicable_count = coverage(rule, verifications)
    cov = f"{a.satisfied_count} of {a.required_count} required requirement(s) satisfied"
    if workup.recommended_action is Action.represent and not a.rule_met:
        add("needs_review", f"model recommends represent but the rule is not met: {cov}")
    elif a.rule_met and workup.recommended_action is not Action.represent:
        add("medium", f"requirements appear met ({cov}) but model recommends {workup.recommended_action.value}; read its caveats")
    else:
        a.reasons.append(cov)

    partials = [v.requirement_id for v in verifications if v.effective_status is Status.partial]
    if partials and workup.recommended_action is Action.represent:
        add("needs_review", f"represent recommended with partial requirement(s): {partials}")

    # 3. verification outcomes
    for v in verifications:
        if v.downgraded:
            add("needs_review", f"requirement {v.requirement_id}: {v.note}")
        elif v.unverified_count:
            add("medium", f"requirement {v.requirement_id}: {v.unverified_count} cited quote(s) not found in the document text")
        if v.image_only and v.effective_status in (Status.satisfied, Status.partial):
            add("medium", f"requirement {v.requirement_id} rests on an image only; view it before filing")

    # 4. pre-check contradictions
    if workup.recommended_action is Action.represent:
        if prechecks.postcode_match is False:
            add("needs_review", "shipping and billing postcodes differ; check the delivery-address requirement by hand")
        if any("AVS and CVV failed" in f for f in prechecks.flags):
            add("needs_review", "AVS and CVV both failed on the disputed transaction")

    # 5. model self-report, minor
    if workup.model_confidence == "low":
        add("medium", "model reports low confidence")

    a.tier = max(sev, key=_RANK.get, default="high")
    return a
