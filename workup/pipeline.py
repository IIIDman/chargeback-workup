"""Run one case end to end: load -> rule -> pre-checks -> documents -> LLM -> verify quotes -> confidence tier.

Only the LLM response is cached; verification and the confidence tier are recomputed on every run because
they are deterministic and cheap, so a change to the code layer applies immediately without new API calls.

Every LLM response is cached under artifacts/<case_id>.json. The cache key includes a hash of the prompt
inputs, so a change to the system prompt, the rule text or the documents invalidates it automatically.
The cache is committed to the repo, which lets the tool run with no API key.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .checks import PreChecks, run_prechecks
from .confidence import Assessment, assess
from .docs import Document, load_case_documents
from .llm import (
    CALL_PARAMS,
    MODEL,
    SYSTEM_PROMPT,
    LLMResult,
    build_user_content,
    request_workup,
)
from .rules import ReasonCode, get_rule, rule_as_text
from .schema import Case, Workup
from .verify import RequirementVerification, verify_workup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS_DIR = DATA / "documents"
ARTIFACTS = ROOT / "artifacts"


@dataclass
class CaseResult:
    case: Case
    rule: ReasonCode
    prechecks: PreChecks
    documents: list[Document]
    workup: Workup
    verifications: list[RequirementVerification]
    assessment: Assessment
    usage: dict
    from_cache: bool
    attempts: int
    validation_problems: list[str]
    fingerprint: str = ""


def load_cases(path: Path = DATA / "cases.json") -> dict[str, Case]:
    raw = json.loads(path.read_text())
    cases = [Case.model_validate(c) for c in raw]
    return {c.case_id: c for c in cases}


def prompt_fingerprint(case: Case, rule: ReasonCode, prechecks: PreChecks, documents: list[Document]) -> str:
    """Hash of everything that shapes the answer: model id, call parameters, system prompt, the output schema
    (field order and descriptions are part of the prompt the model sees) and the full user content, images
    included via their base64 payload. Any change to any of these makes the cached answer stale."""
    content = build_user_content(case, rule, prechecks, documents)
    canonical = json.dumps({
        "model": MODEL, "params": CALL_PARAMS, "system": SYSTEM_PROMPT,
        "schema": Workup.model_json_schema(), "content": content,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def run_case(case: Case, recompute: bool = False, allow_api: bool = True) -> CaseResult:
    """allow_api=False guarantees no network call: used by the UI and the compare script, so that
    opening the app can never spend money, and a stale cache is reported instead of silently refreshed."""
    rule = get_rule(case.scheme, case.reason_code)
    prechecks = run_prechecks(case, rule)
    documents = load_case_documents(case.merchant_evidence_documents, DOCS_DIR)
    fp = prompt_fingerprint(case, rule, prechecks, documents)

    ARTIFACTS.mkdir(exist_ok=True)
    cache_path = ARTIFACTS / f"{case.case_id}.json"
    why = "no cached workup"
    if cache_path.exists() and not recompute:
        cached = _read_cache(cache_path)
        if cached is None:
            why = "unreadable cached workup"  # half-written or hand-edited file: a miss, not a crash
        elif cached.get("fingerprint") != fp:
            why = "stale cached workup (the prompt, schema or model changed since it was produced)"
        else:
            try:
                workup = Workup.model_validate(cached["workup"])
            except ValidationError:
                workup = None  # schema moved on; treat as a cache miss
                why = "cached workup no longer fits the schema"
            if workup is not None:
                return _finish(case, rule, prechecks, documents, workup,
                               usage=cached.get("usage", {}), from_cache=True,
                               attempts=cached.get("attempts", 1), problems=cached.get("validation_problems", []),
                               fingerprint=fp)

    if not allow_api:
        raise LookupError(f"{why} for {case.case_id} (fingerprint {fp}). Run `uv run python run.py --all` to refresh.")
    result: LLMResult = request_workup(case, rule, prechecks, documents)
    _write_atomic(cache_path, json.dumps({
        "case_id": case.case_id,
        "fingerprint": fp,
        "model": result.raw_response.get("model"),
        "workup": result.workup.model_dump(mode="json"),
        "usage": result.usage,
        "usage_per_attempt": result.usage_per_attempt,
        "attempts": result.attempts,
        "validation_problems": result.validation_problems,
        "problems_per_attempt": result.problems_per_attempt,
        "rule_text": rule_as_text(rule),
        "prechecks": prechecks.as_lines(),
        "raw_response": result.raw_response,
    }, indent=2, ensure_ascii=False))

    return _finish(case, rule, prechecks, documents, result.workup, usage=result.usage, from_cache=False,
                   attempts=result.attempts, problems=result.validation_problems, fingerprint=fp)


def _read_cache(path: Path) -> dict | None:
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return cached if isinstance(cached, dict) and "workup" in cached else None


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file and rename, so an interrupted run never leaves a half-written artifact."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _finish(case: Case, rule: ReasonCode, prechecks: PreChecks, documents: list[Document], workup: Workup,
            *, usage: dict, from_cache: bool, attempts: int, problems: list[str], fingerprint: str = "") -> CaseResult:
    verifications = verify_workup(workup, documents)
    known = {case.case_id, case.transaction.transaction_id, case.transaction.merchant_name}
    assessment = assess(rule, prechecks, workup, verifications, validation_problems=problems, known_identifiers=known)
    return CaseResult(
        case=case, rule=rule, prechecks=prechecks, documents=documents, workup=workup,
        verifications=verifications, assessment=assessment,
        usage=usage, from_cache=from_cache, attempts=attempts, validation_problems=problems,
        fingerprint=fingerprint,
    )
