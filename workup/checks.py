"""Deterministic pre-checks: everything that can be decided from the case metadata and the rule
without reading a document. Computed in code, shown to the model as facts, and shown to the analyst.

The point is not to replace the LLM but to take the things it should never get wrong (a non-representable
code, a postcode mismatch, a date ordering) out of its hands.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .rules import ReasonCode
from .schema import Case


@dataclass
class PreChecks:
    representable: bool
    forced_action: str | None  # "accept_liability" when the code cannot be represented
    postcode_match: bool | None  # None when either postcode is absent (digital goods / services / not captured)
    avs: str  # human-readable
    cvv: str
    three_ds: str
    days_to_chargeback: int
    avs_and_cvv_failed: bool = False
    amount_mismatch: bool = False
    # The record's own answer to requirements that ask for it (Mastercard 4837 r1/r2, 4863 r2).
    avs_full_match: bool = False
    cvv_match: bool = False
    three_ds_ok: bool = False
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
    """Postcodes compare after NFKC, with every kind of whitespace and hyphen removed, upper-cased."""
    if not pc:
        return None
    return re.sub(r"[\s-]", "", unicodedata.normalize("NFKC", pc)).upper() or None


def _day(stamp: str):
    """Calendar day in UTC, whether the value is a date or an offset timestamp."""
    dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.date()


def run_prechecks(case: Case, rule: ReasonCode) -> PreChecks:
    t = case.transaction
    representable = rule.logic != "non_representable"
    forced = None if representable else "accept_liability"

    ship, bill = _norm_pc(t.shipping_address_postcode), _norm_pc(t.billing_address_postcode)
    postcode_match = None if (ship is None or bill is None) else (ship == bill)

    days = (_day(case.chargeback_date) - _day(t.transaction_date)).days

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
    amount_mismatch = ((round(case.chargeback_amount.value, 2), case.chargeback_amount.currency.upper())
                       != (round(t.amount.value, 2), t.amount.currency.upper()))
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
        avs_full_match=t.avs_result == "Y",
        cvv_match=t.cvv_result == "M",
        three_ds_ok=t.three_ds_status in ("authenticated", "frictionless"),
        flags=flags,
    )
