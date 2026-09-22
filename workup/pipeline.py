"""Run one case end to end: load -> rule -> pre-checks -> documents -> LLM -> (later: verify, confidence).

Every LLM response is cached under artifacts/<case_id>.json. The cache key includes a hash of the prompt
inputs, so a change to the system prompt, the rule text or the documents invalidates it automatically.
The cache is committed to the repo, which lets the tool run with no API key.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .checks import PreChecks, run_prechecks
from .docs import Document, load_case_documents
from .llm import SYSTEM_PROMPT, LLMResult, build_user_content, request_workup
from .rules import ReasonCode, get_rule, rule_as_text
from .schema import Case, Workup

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
    usage: dict
    from_cache: bool
    attempts: int
    validation_problems: list[str]


def load_cases(path: Path = DATA / "cases.json") -> dict[str, Case]:
    raw = json.loads(path.read_text())
    cases = [Case.model_validate(c) for c in raw]
    return {c.case_id: c for c in cases}


def prompt_fingerprint(case: Case, rule: ReasonCode, prechecks: PreChecks, documents: list[Document]) -> str:
    """Hash of everything the model sees. Image bytes are included via their base64 payload."""
    content = build_user_content(case, rule, prechecks, documents)
    canonical = json.dumps({"system": SYSTEM_PROMPT, "content": content}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def run_case(case: Case, recompute: bool = False) -> CaseResult:
    rule = get_rule(case.scheme, case.reason_code)
    prechecks = run_prechecks(case, rule)
    documents = load_case_documents(case.merchant_evidence_documents, DOCS_DIR)
    fp = prompt_fingerprint(case, rule, prechecks, documents)

    ARTIFACTS.mkdir(exist_ok=True)
    cache_path = ARTIFACTS / f"{case.case_id}.json"
    if cache_path.exists() and not recompute:
        cached = json.loads(cache_path.read_text())
        if cached.get("fingerprint") == fp:
            return CaseResult(
                case=case, rule=rule, prechecks=prechecks, documents=documents,
                workup=Workup.model_validate(cached["workup"]),
                usage=cached.get("usage", {}), from_cache=True,
                attempts=cached.get("attempts", 1), validation_problems=cached.get("validation_problems", []),
            )

    result: LLMResult = request_workup(case, rule, prechecks, documents)
    cache_path.write_text(json.dumps({
        "case_id": case.case_id,
        "fingerprint": fp,
        "model": result.raw_response.get("model"),
        "workup": result.workup.model_dump(mode="json"),
        "usage": result.usage,
        "attempts": result.attempts,
        "validation_problems": result.validation_problems,
        "rule_text": rule_as_text(rule),
        "prechecks": prechecks.as_lines(),
        "raw_response": result.raw_response,
    }, indent=2, ensure_ascii=False))

    return CaseResult(
        case=case, rule=rule, prechecks=prechecks, documents=documents,
        workup=result.workup, usage=result.usage, from_cache=False,
        attempts=result.attempts, validation_problems=result.validation_problems,
    )
