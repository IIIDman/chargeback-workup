"""Deterministic pre-checks: everything that can be decided from the case metadata and the rule
without reading a document. Computed in code, shown to the model as facts, and shown to the analyst.

The point is not to replace the LLM but to take the things it should never get wrong (a non-representable
code, a postcode mismatch, a date ordering) out of its hands.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .rules import ReasonCode
from .schema import Case


@dataclass
class PreChecks:
    representable: bool
    forced_action: str | None  # "accept_liability" when the code cannot be represented
    postcode_match: bool | None  # None when shipping postcode is absent (digital goods / services)
    avs: str  # human-readable
    cvv: str
    three_ds: str
    days_to_chargeback: int
    avs_and_cvv_failed: bool = False
    amount_mismatch: bool = False
    flags: list[str] = field(default_factory=list)  # things worth the analyst's attention

    def as_lines(self) -> list[str]:
        lines = [
            f"Representable under the rule: {'yes' if self.representable else 'NO (forced accept_liability)'}",
            f"AVS: {self.avs}; CVV: {self.cvv}; 3DS: {self.three_ds}",
            f"Shipping vs billing postcode: {_pc(self.postcode_match)}",
            f"Chargeback raised {self.days_to_chargeback} days after the transaction",
        ]
        lines += [f"Flag: {f}" for f in self.flags]
        return lines


def _pc(match: bool | None) -> str:
    if match is None:
        return "no shipping postcode on file (digital goods or service)"
    return "match" if match else "MISMATCH"


AVS = {"Y": "Y (full address match)", "A": "A (address matched, postcode did not)", "N": "N (no match)", None: "not run"}
CVV = {"M": "M (match)", "N": "N (no match)", None: "not run"}


def _norm_pc(pc: str | None) -> str | None:
    return pc.replace(" ", "").upper() if pc else None


def run_prechecks(case: Case, rule: ReasonCode) -> PreChecks:
    t = case.transaction
    representable = rule.logic != "non_representable"
    forced = None if representable else "accept_liability"

    ship, bill = _norm_pc(t.shipping_address_postcode), _norm_pc(t.billing_address_postcode)
    postcode_match = None if ship is None else (ship == bill)

    txn_day = datetime.fromisoformat(t.transaction_date.replace("Z", "+00:00")).date()
    cb_day = datetime.fromisoformat(case.chargeback_date.replace("Z", "+00:00")).date()  # date or timestamp
    days = (cb_day - txn_day).days

    flags: list[str] = []
    if not representable:
        flags.append(f"{rule.scheme.title()} {rule.code} is non-representable: evidence does not change the outcome")
    if postcode_match is False:
        flags.append(
            f"shipping postcode {t.shipping_address_postcode} differs from billing postcode "
            f"{t.billing_address_postcode}; delivery-to-cardholder-address requirements are at risk"
        )
    avs_and_cvv_failed = t.avs_result == "N" and t.cvv_result == "N"
    if avs_and_cvv_failed:
        flags.append("both AVS and CVV failed on the disputed transaction")
    if t.three_ds_status == "not_attempted":
        flags.append("3DS was not attempted")
    if days < 0:
        flags.append("chargeback date precedes transaction date: data problem")
    amount_mismatch = (case.chargeback_amount.value, case.chargeback_amount.currency) != (t.amount.value, t.amount.currency)
    if amount_mismatch:
        flags.append(
            f"chargeback amount {case.chargeback_amount.value} {case.chargeback_amount.currency} differs from "
            f"transaction amount {t.amount.value} {t.amount.currency}"
        )

    return PreChecks(
        representable=representable,
        forced_action=forced,
        postcode_match=postcode_match,
        avs=AVS.get(t.avs_result, t.avs_result or "not run"),
        cvv=CVV.get(t.cvv_result, t.cvv_result or "not run"),
        three_ds=t.three_ds_status,
        days_to_chargeback=days,
        avs_and_cvv_failed=avs_and_cvv_failed,
        amount_mismatch=amount_mismatch,
        flags=flags,
    )
