"""Simplified compelling-evidence rules, one entry per reason code in the dataset.

Source: data/reason_codes.txt (the take-home's simplified rules). Encoded by hand as data so the
rest of the tool can reason about them in code: which requirements exist, and how many must be met.

Logic values:
  all               every requirement must be satisfied
  any_two           at least two requirements satisfied
  any_one           at least one requirement satisfied
  non_representable the code cannot be represented; recommend accept_liability regardless of evidence
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Logic = Literal["all", "any_two", "any_one", "non_representable"]


@dataclass(frozen=True)
class Requirement:
    id: int
    text: str


@dataclass(frozen=True)
class ReasonCode:
    scheme: Literal["visa", "mastercard"]
    code: str
    label: str
    issuer_claim: str
    logic: Logic
    requirements: tuple[Requirement, ...]
    note: str | None = None

    @property
    def key(self) -> str:
        return f"{self.scheme}:{self.code}"

    @property
    def required_count(self) -> int:
        """How many requirements must be satisfied for a defensible representment."""
        if self.logic == "all":
            return len(self.requirements)
        if self.logic == "any_two":
            return 2
        if self.logic == "any_one":
            return 1
        return 0

    def logic_sentence(self) -> str:
        if self.logic == "all":
            return f"ALL {len(self.requirements)} requirements must be satisfied."
        if self.logic == "any_two":
            return f"Any TWO of the {len(self.requirements)} requirements must be satisfied."
        if self.logic == "any_one":
            return f"Any ONE of the {len(self.requirements)} requirements is sufficient."
        return "This reason code is not representable under the simplified rules."


def _req(*texts: str) -> tuple[Requirement, ...]:
    return tuple(Requirement(i + 1, t) for i, t in enumerate(texts))


REASON_CODES: dict[str, ReasonCode] = {
    rc.key: rc
    for rc in [
        # ---------------------------------------------------------------- Visa
        ReasonCode(
            "visa", "10.4", "Other Fraud, Card Absent Environment",
            "Cardholder denies authorising a card-not-present transaction.",
            "any_two",
            _req(
                "Evidence the cardholder used the same card and same shipping address in two prior undisputed "
                "transactions with this merchant, completed more than 120 days but less than 365 days before the "
                "disputed transaction",
                "Evidence the cardholder is in possession of and using the merchandise (e.g. signed-in account "
                "activity post-delivery, social media post tagging the merchant)",
                "For digital goods: device fingerprint, IP address, geolocation, and customer account login "
                "matching prior undisputed transactions",
                "Proof of delivery to the cardholder's verified billing address (not just shipping address) with "
                "signature confirmation",
            ),
        ),
        ReasonCode(
            "visa", "10.5", "Visa Fraud Monitoring Program",
            "Transaction flagged under Visa's fraud monitoring program.",
            "non_representable",
            (),
            note="This reason code generally cannot be represented. Recommend accept_liability unless the merchant "
                 "can prove the transaction was miscoded by the issuer.",
        ),
        ReasonCode(
            "visa", "12.5", "Incorrect Amount",
            "The amount charged does not match the amount the cardholder authorised.",
            "all",
            _req(
                "The signed receipt, terms of service, or order confirmation showing the amount the cardholder agreed to",
                "Documentation showing the amount charged matches that agreed amount",
                "If a tip, gratuity, or adjustment was added, evidence the cardholder authorised it",
            ),
        ),
        ReasonCode(
            "visa", "12.6.1", "Duplicate Processing",
            "The same transaction was processed more than once.",
            "all",
            _req(
                "Evidence the two transactions are for two separate purchases (e.g. different order IDs, different "
                "items, different services rendered)",
                "Documentation of each purchase event (separate invoices, separate delivery confirmations, separate "
                "service dates)",
                "Transaction timestamps and authorisation codes for each charge",
            ),
        ),
        ReasonCode(
            "visa", "13.1", "Merchandise / Services Not Received",
            "Cardholder paid but never received the goods or services.",
            "all",
            _req(
                "Proof of delivery: tracking number, carrier name, and confirmation of delivery to the cardholder's address",
                "For services: evidence the service was rendered on or before the expected date (booking confirmation, "
                "attendance log, access logs)",
                "Date of delivery / service rendered is on or before the chargeback date",
                "The delivery address materially matches the address provided by the cardholder at purchase",
            ),
        ),
        ReasonCode(
            "visa", "13.2", "Cancelled Recurring Transaction",
            "Cardholder cancelled a recurring subscription but was still charged.",
            "all",
            _req(
                "Terms of service disclosing the recurring billing arrangement and the cancellation method",
                "Evidence the cardholder was notified of the upcoming charge (typically 7+ days in advance) for "
                "transactions over a defined threshold",
                "No record of the cardholder having submitted a cancellation request prior to the billing date",
                "Evidence of the cardholder's original opt-in to the recurring arrangement",
            ),
        ),
        ReasonCode(
            "visa", "13.3", "Not as Described or Defective Merchandise",
            "Cardholder received the goods but they are materially not as described or defective.",
            "all",
            _req(
                "The merchant's published description of the item the cardholder purchased",
                "Evidence the item delivered matches that description (photos, specs, serial number match)",
                "Evidence the merchant offered a return/refund route and the cardholder did not use it, OR evidence "
                "the cardholder used and retained the merchandise after raising the complaint",
            ),
        ),
        ReasonCode(
            "visa", "13.6", "Credit Not Processed",
            "The merchant agreed to a refund but never processed it.",
            "any_one",
            _req(
                "Evidence that a refund was processed (refund transaction ID, date, amount)",
                "Evidence that no refund was ever agreed (merchant's refund policy and absence of any refund "
                "commitment in cardholder communications)",
            ),
        ),
        ReasonCode(
            "visa", "13.7", "Cancelled Merchandise / Services",
            "Cardholder cancelled the purchase per the merchant's policy but was charged.",
            "all",
            _req(
                "The merchant's cancellation policy as displayed at point of sale",
                "Evidence the cardholder agreed to that policy (e.g. checkbox click record, signed terms)",
                "Evidence the cardholder either did not cancel within the policy window, or cancelled outside the "
                "refundable period",
            ),
        ),
        # ---------------------------------------------------------- Mastercard
        ReasonCode(
            "mastercard", "4837", "No Cardholder Authorisation",
            "Cardholder denies authorising the transaction (card-not-present fraud equivalent).",
            "any_two",
            _req(
                "AVS match (full address) AND CVV match on the disputed transaction",
                "3D Secure authentication completed successfully (Mastercard SecureCode / Identity Check)",
                "Two prior undisputed transactions from the same cardholder with this merchant in the past 12 months, "
                "with matching billing details",
                "Proof of delivery to the cardholder's billing address with signature",
            ),
        ),
        ReasonCode(
            "mastercard", "4853", "Cardholder Dispute (Goods / Services Not Provided)",
            "Goods or services were not provided as agreed.",
            "all",
            _req(
                "Proof of delivery or service provision (tracking, confirmation, access log)",
                "Evidence the goods or services materially match what was advertised",
                "Either: no contact from the cardholder attempting to resolve the issue before the chargeback, OR "
                "documentation showing the merchant attempted resolution and the cardholder refused",
            ),
        ),
        ReasonCode(
            "mastercard", "4855", "Goods / Services Not Provided",
            "Paid for goods or services that were never delivered or rendered.",
            "all",
            _req(
                "Proof of delivery (tracking + carrier confirmation) or proof of service rendered (access logs, "
                "attendance, completed booking)",
                "Date of delivery / service is before the chargeback date",
                "Delivery address matches the cardholder's records",
            ),
        ),
        ReasonCode(
            "mastercard", "4863", "Cardholder Does Not Recognise - Potential Fraud",
            "Cardholder does not recognise the transaction (may not be fraud, could be a confusing descriptor).",
            "any_one",
            _req(
                "Evidence the merchant's billing descriptor matches the merchant name the cardholder would recognise",
                "AVS + CVV match on the disputed transaction",
                "Prior undisputed transactions from the same cardholder with this merchant",
                "Cardholder's IP / device / account login matching prior undisputed sessions",
            ),
        ),
        ReasonCode(
            "mastercard", "4859", "No-Show / Addendum",
            "Cardholder was charged a no-show fee, late cancellation fee, or addendum charge that they dispute.",
            "all",
            _req(
                "Evidence of the cardholder's original reservation or booking",
                "The merchant's no-show / cancellation policy as disclosed at booking",
                "Evidence the cardholder either failed to show or cancelled outside the policy window",
                "Evidence the fee charged matches the policy disclosed",
            ),
        ),
        ReasonCode(
            "mastercard", "4870", "Chip Liability Shift",
            "Counterfeit card used at a non-chip-enabled terminal (card-present only).",
            "non_representable",
            (),
            note="Card-present reason code. For a card-not-present acquiring exercise expect accept_liability; flag "
                 "the merchant for terminal upgrade.",
        ),
    ]
}


def get_rule(scheme: str, code: str) -> ReasonCode:
    try:
        return REASON_CODES[f"{scheme}:{code}"]
    except KeyError:
        raise KeyError(f"unknown reason code {scheme} {code}; known: {sorted(REASON_CODES)}") from None


def rule_as_text(rc: ReasonCode) -> str:
    """Human-readable block used in the LLM prompt and the UI."""
    lines = [
        f"{rc.scheme.title()} {rc.code} - {rc.label}",
        f"Issuer claim: {rc.issuer_claim}",
        f"Logic: {rc.logic_sentence()}",
    ]
    if rc.note:
        lines.append(f"Note: {rc.note}")
    if rc.requirements:
        lines.append("Compelling evidence requirements:")
        lines += [f"  {r.id}. {r.text}" for r in rc.requirements]
    return "\n".join(lines)
