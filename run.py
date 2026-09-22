"""CLI: run the workup for one case or all cases and print it as markdown.

  uv run python run.py CB-2025-0001
  uv run python run.py --all
  uv run python run.py CB-2025-0004 --recompute     # ignore the cached response
  uv run python run.py CB-2025-0001 --dry-run       # show the prompt, make no API call
"""
from __future__ import annotations

import json

import typer
from dotenv import load_dotenv

load_dotenv()  # before workup imports: WORKUP_MODEL is read at import time

from workup.checks import run_prechecks
from workup.docs import load_case_documents
from workup.llm import SYSTEM_PROMPT, build_user_content
from workup.pipeline import DOCS_DIR, CaseResult, load_cases, run_case
from workup.rules import get_rule, rule_as_text
from workup.schema import Status

app = typer.Typer(add_completion=False)

ICON = {Status.satisfied: "[x]", Status.partial: "[~]", Status.missing: "[ ]", Status.not_applicable: "[-]"}


def render(r: CaseResult) -> str:
    c, w = r.case, r.workup
    t = c.transaction
    a = r.assessment
    ver = {v.requirement_id: v for v in r.verifications}
    out = [
        f"# {c.case_id}  {c.scheme.title()} {c.reason_code} {c.reason_code_label}",
        f"{t.merchant_name} · {c.chargeback_amount.value} {c.chargeback_amount.currency} · "
        f"txn {t.transaction_date[:10]} · chargeback {c.chargeback_date}",
        f"source: {'cache' if r.from_cache else 'api'}, attempts: {r.attempts}"
        + (f", validation problems: {r.validation_problems}" if r.validation_problems else ""),
        "",
        f"## Confidence: {a.tier.upper()}   ->  final action: {a.final_action.value}"
        + ("  (overridden by rule)" if a.action_overridden else ""),
        *[f"- {x}" for x in a.reasons],
        "",
        "## Pre-checks",
        *[f"- {line}" for line in r.prechecks.as_lines()],
        "",
        "## Rule",
        w.reason_code_summary,
        "",
        "## Evidence assessment",
    ]
    for ra in w.requirements:
        req = next(x for x in r.rule.requirements if x.id == ra.requirement_id)
        v = ver[ra.requirement_id]
        status_txt = v.effective_status.value + (f" (model said {ra.status.value})" if v.downgraded else "")
        out.append(f"{ICON[v.effective_status]} {ra.requirement_id}. {req.text}")
        out.append(f"    {status_txt}: {ra.reasoning}")
        if v.note:
            out.append(f"    ! {v.note}")
        for pc in v.pointers:
            mark = {True: "verified", False: "NOT FOUND", None: "image"}[pc.verified]
            out.append(f'    -> [{mark}] {pc.document} p.{pc.page}: "{pc.quote}"')
    out += [
        "",
        "## Rationale",
        w.rationale,
        "",
        f"## Recommended action: {w.recommended_action.value}",
        w.action_justification,
    ]
    if w.evidence_to_request:
        out += ["", "Ask the merchant for:", *[f"- {e}" for e in w.evidence_to_request]]
    if w.caveats:
        out += ["", "Caveats:", *[f"- {e}" for e in w.caveats]]
    out += ["", f"model confidence: {w.model_confidence}", ""]
    return "\n".join(out)


@app.command()
def main(
    case_id: str = typer.Argument(None, help="e.g. CB-2025-0001"),
    all_cases: bool = typer.Option(False, "--all", help="run every case in data/cases.json"),
    recompute: bool = typer.Option(False, "--recompute", help="ignore cached responses"),
    dry_run: bool = typer.Option(False, "--dry-run", help="print the prompt and exit without calling the API"),
):
    cases = load_cases()
    ids = list(cases) if all_cases else [case_id]
    if not all_cases and case_id not in cases:
        raise typer.BadParameter(f"unknown case {case_id!r}; known: {list(cases)}")

    for cid in ids:
        case = cases[cid]
        if dry_run:
            rule = get_rule(case.scheme, case.reason_code)
            pre = run_prechecks(case, rule)
            docs = load_case_documents(case.merchant_evidence_documents, DOCS_DIR)
            content = build_user_content(case, rule, pre, docs)
            typer.echo("=== SYSTEM ===\n" + SYSTEM_PROMPT)
            typer.echo("=== USER ===")
            for block in content:
                if block["type"] == "text":
                    typer.echo(block["text"])
                else:
                    typer.echo(f"[image block: {block['source']['media_type']}, {len(block['source']['data'])} b64 chars]")
            typer.echo(f"\n(rule text used: {len(rule_as_text(rule))} chars; docs: {[d.name for d in docs]})")
            continue
        result = run_case(case, recompute=recompute)
        typer.echo(render(result))
        if not result.from_cache:
            typer.echo(f"usage: {json.dumps(result.usage)}")
        typer.echo("-" * 80)


if __name__ == "__main__":
    app()
