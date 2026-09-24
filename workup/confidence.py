"""Confidence tier for the analyst queue, computed from checkable signals rather than the model's own opinion.

Tier is one of high / medium / needs_review, always with the list of reasons that produced it. The signal
families, in the order they are applied:

1. the rule itself: a non-representable code fixes the action, whatever the model proposed
2. coverage: how many requirements are effectively satisfied against what the rule's logic needs, and
   whether the recommended action agrees with that count (including partials, conceding a partly-met case,
   and asking for evidence when most of the file is absent)
3. verification: downgrades, quotes not found, support that rests only on an image
4. transaction pre-checks, counted only where they cut against the recommendation
5. the rationale mentioning something the workup also asks the merchant for
6. the model's self-reported confidence, as a minor input
7. leftover validation problems: a workup that still failed structural validation after the retry

Before any of that, requirements the transaction record answers by itself (AVS/CVV and 3DS under
Mastercard 4837 and 4863) are checked against the record: a satisfied that the record contradicts is
downgraded to missing, so a merchant document cannot talk the tool out of a failed AVS.

Three principles worth stating, because two of them were wrong in the first version:

- **Direction-neutral.** accept_liability means the merchant eats the loss and request_more_evidence costs
  days; they are decisions too. Escalation does not only apply to represent.
- **Whatever code cannot check, a human must.** A requirement supported only by an image is not
  text-verifiable, so it goes to needs_review rather than being quietly accepted.
- **A transaction fact only counts against the recommendation it undermines.** A failed AVS makes a
  represent doubtful; on an accept_liability it is the reason the recommendation is right, not a warning.

The queue is sorted by tier so that confident cases take a minute and doubtful ones get attention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .checks import PreChecks
from .rules import ReasonCode
from .schema import Action, Status, Workup
from .verify import RequirementVerification, rationale_conflicts

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
    """Return (rule_met, satisfied, required, applicable) using effective (post-verification) statuses.

    Iterates over the rule's own requirement ids: one the model did not return counts as missing, and an
    id the rule does not have is ignored, so a truncated or padded answer cannot inflate coverage.
    """
    if rule.logic == "non_representable":
        return False, 0, 0, 0
    by_id = {v.requirement_id: v.effective_status for v in verifications}
    statuses = [by_id.get(r.id, Status.missing) for r in rule.requirements]
    applicable = [s for s in statuses if s is not Status.not_applicable]
    satisfied = sum(1 for s in applicable if s is Status.satisfied)
    if rule.logic == "all":
        required = len(applicable)
        return (required > 0 and satisfied == required), satisfied, required, required
    required = rule.required_count
    return satisfied >= required, satisfied, required, len(applicable)


def apply_transaction_facts(rule: ReasonCode, prechecks: PreChecks,
                            verifications: list[RequirementVerification]) -> list[int]:
    """Downgrade a satisfied requirement that the transaction record contradicts. Returns the ids changed."""
    by_id = {r.id: r for r in rule.requirements}
    changed: list[int] = []
    for v in verifications:
        req = by_id.get(v.requirement_id)
        if req is None or req.fact is None or v.effective_status is not Status.satisfied:
            continue
        if req.fact == "avs_cvv":
            ok = prechecks.avs_full_match and prechecks.cvv_match
            seen = f"AVS {prechecks.avs}, CVV {prechecks.cvv}"
        else:
            ok = prechecks.three_ds_ok
            seen = f"3DS {prechecks.three_ds}"
        if not ok:
            v.effective_status = Status.missing
            v.downgraded = True
            v.note = f"downgraded satisfied -> missing: the transaction record says {seen}"
            changed.append(v.requirement_id)
    return changed


def assess(rule: ReasonCode, prechecks: PreChecks, workup: Workup,
           verifications: list[RequirementVerification],
           validation_problems: list[str] | None = None,
           known_identifiers: set[str] | None = None) -> Assessment:
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
        if validation_problems:
            add("needs_review", f"workup failed structural validation after retry: {'; '.join(validation_problems)}")
        a.tier = max(sev, key=_RANK.get, default="high")
        return a

    # 1b. the transaction record outranks the model on the requirements it answers itself
    apply_transaction_facts(rule, prechecks, verifications)

    # 2. coverage vs recommendation
    a.rule_met, a.satisfied_count, a.required_count, a.applicable_count = coverage(rule, verifications)
    cov = f"{a.satisfied_count} of {a.required_count} required requirement(s) satisfied"
    if workup.recommended_action is Action.represent and not a.rule_met:
        add("needs_review", f"model recommends represent but the rule is not met: {cov}")
    elif a.rule_met and workup.recommended_action is not Action.represent:
        add("medium", f"requirements appear met ({cov}) but model recommends {workup.recommended_action.value}; read its caveats")
    else:
        a.reasons.append(cov)

    known_ids = {r.id for r in rule.requirements}
    returned = [v for v in verifications if v.requirement_id in known_ids]
    missing_ids = sorted(known_ids - {v.requirement_id for v in returned})
    if missing_ids:
        add("needs_review", f"requirement(s) {missing_ids} were not assessed by the model; counted as missing")
    na = [v.requirement_id for v in returned if v.effective_status is Status.not_applicable]
    if na and rule.logic == "all":
        by_id = {r.id: r for r in rule.requirements}
        unconditional = [i for i in na if not by_id[i].conditional]
        if unconditional:
            # Under an ALL rule, an N/A shrinks the bar. Fine when the rule text states a condition
            # ("For services: ..."); a second look when it does not.
            add("medium", f"requirement(s) {unconditional} marked not applicable although the rule states no "
                          f"condition for them; confirm before relying on the count")
        else:
            a.reasons.append(f"requirement(s) {na} marked not applicable by the model and excluded from the count; confirm")

    partials = [v.requirement_id for v in returned if v.effective_status is Status.partial]
    satisfied_or_partial = [v.requirement_id for v in returned
                            if v.effective_status in (Status.satisfied, Status.partial)]
    if partials and workup.recommended_action is Action.represent:
        if a.rule_met and rule.logic != "all":
            a.reasons.append(f"requirement(s) {partials} partial, not needed: the rule is already met by satisfied ones")
        else:
            add("needs_review", f"represent recommended with partial requirement(s): {partials}")
    # Conceding a case that partly works is a decision too: the merchant eats the loss.
    if workup.recommended_action is Action.accept_liability and satisfied_or_partial:
        add("needs_review", f"accept_liability although requirement(s) {satisfied_or_partial} are met or partly met")
    # Asking costs the analyst a follow-up and the merchant goodwill. When most of the file is absent
    # rather than incomplete, the request is unlikely to close the gap and accepting may be the faster call.
    missing = [v.requirement_id for v in returned if v.effective_status is Status.missing] + missing_ids
    if (workup.recommended_action is Action.request_more_evidence
            and a.applicable_count and len(missing) > a.applicable_count / 2):
        add("medium", f"request_more_evidence, but {len(missing)} of {a.applicable_count} applicable "
                      f"requirement(s) are missing entirely: the merchant would have to supply most of the file")

    # 3. verification outcomes
    for v in returned:
        if v.downgraded:
            add("needs_review", f"requirement {v.requirement_id}: {v.note}")
        elif v.unverified_count:
            add("medium", f"requirement {v.requirement_id}: {v.unverified_count} cited quote(s) not found in the document text")
        # An image cannot be checked against extracted text, so a human has to look at it.
        if v.image_only and v.effective_status in (Status.satisfied, Status.partial):
            add("needs_review", f"requirement {v.requirement_id} rests on an image only and cannot be text-verified; open it")

    # 4. transaction pre-check facts, only where they cut AGAINST the recommendation
    if workup.recommended_action is Action.represent:
        escalate: Tier | None = "needs_review"
    elif workup.recommended_action is Action.request_more_evidence:
        escalate = "medium"  # the requested evidence may not be able to fix a fatal fact
    else:
        escalate = None  # accept_liability: these facts support the recommendation
    if escalate:
        if prechecks.postcode_match is False:
            add(escalate, "shipping and billing postcodes differ; check the delivery-address requirement by hand")
        if prechecks.avs_and_cvv_failed:
            add(escalate, "AVS and CVV both failed on the disputed transaction")
        if prechecks.amount_mismatch:
            add(escalate, "chargeback amount differs from the transaction amount")

    # 5. the rationale is the text that gets filed: it must not assert evidence we do not hold
    for conflict in rationale_conflicts(workup, ignore=known_identifiers):
        add("medium", conflict)

    # 6. model self-report, minor: low on its own, medium only when something else is already shaky
    if workup.model_confidence == "low":
        add("medium", "model reports low confidence")
    elif workup.model_confidence == "medium" and partials:
        add("medium", f"model reports medium confidence and requirement(s) {partials} are partial")

    # 7. structural problems the retry did not fix are never silently accepted
    if validation_problems:
        add("needs_review", f"workup failed structural validation after retry: {'; '.join(validation_problems)}")

    a.tier = max(sev, key=_RANK.get, default="high")
    return a
