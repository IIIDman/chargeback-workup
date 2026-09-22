"""Analyst UI: the queue on the left, one case workup on the right.

Written for someone working 80 cases a day, so the layout answers two questions on sight:
what does this case need from me, and where is the evidence. Everything the tool decided can be
overridden, and approving a case writes a decision record that captures what the analyst changed.

Run:  uv run streamlit run app.py
Reads cached results from artifacts/, so it never calls the API.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from workup.pipeline import ARTIFACTS, CaseResult, load_cases, run_case  # noqa: E402
from workup.schema import Action, Status  # noqa: E402

DECISIONS = ARTIFACTS / "decisions.jsonl"
TIER_RANK = {"needs_review": 0, "medium": 1, "high": 2}
TIER_BADGE = {"needs_review": "🔴 needs review", "medium": "🟡 medium", "high": "🟢 high"}
STATUS_BADGE = {
    Status.satisfied: "✅ satisfied",
    Status.partial: "🟠 partial",
    Status.missing: "❌ missing",
    Status.not_applicable: "➖ n/a",
}
VERIFY_BADGE = {True: "✅ quote verified in document", False: "⚠️ quote NOT found in that document",
                None: "🖼 image, not text-verifiable"}
ACTIONS = [a.value for a in Action]

st.set_page_config(page_title="Representment workups", layout="wide", initial_sidebar_state="expanded")


# --------------------------------------------------------------------------------- data


@st.cache_resource(show_spinner="Loading workups...")
def load_results() -> dict[str, CaseResult]:
    """Cached model output only; verification and tiering are recomputed on load."""
    out, stale = {}, []
    for case_id, case in load_cases().items():
        if not (ARTIFACTS / f"{case_id}.json").exists():
            continue
        try:
            out[case_id] = run_case(case, allow_api=False)  # the UI never calls the API
        except LookupError:
            stale.append(case_id)
    if stale:
        st.warning(f"Cached workups are stale for {', '.join(stale)} (the prompt changed since they were "
                   "produced). Run `uv run python run.py --all` to refresh.", icon="⚠️")
    return out


def decisions_by_case() -> dict[str, dict]:
    if not DECISIONS.exists():
        return {}
    rows = {}
    for line in DECISIONS.read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            rows[d["case_id"]] = d  # last write wins
    return rows


results = load_results()
if not results:
    st.error("No cached workups in artifacts/. Run `uv run python run.py --all` first.")
    st.stop()
decided = decisions_by_case()

order = sorted(results, key=lambda cid: (TIER_RANK[results[cid].assessment.tier],
                                         -results[cid].case.chargeback_amount.value))

# ------------------------------------------------------------------------------- queue


with st.sidebar:
    st.markdown("### Queue")
    open_count = sum(1 for cid in order if cid not in decided)
    st.caption(f"{open_count} open of {len(order)} · hardest first")
    if "selected" not in st.session_state:
        st.session_state.selected = order[0]
    for cid in order:
        r = results[cid]
        a, c = r.assessment, r.case
        mark = "✔︎ " if cid in decided else ""
        label = (f"{mark}{TIER_BADGE[a.tier].split()[0]} {cid}\n\n"
                 f"{c.scheme[:4].title()} {c.reason_code} · {c.chargeback_amount.value:,.0f} "
                 f"{c.chargeback_amount.currency} · {a.final_action.value.replace('_', ' ')}")
        if st.button(label, key=f"q_{cid}", use_container_width=True,
                     type="primary" if cid == st.session_state.selected else "secondary"):
            st.session_state.selected = cid
            st.rerun()
    st.divider()
    st.caption(f"Decisions recorded: {len(decided)}")
    if decided and st.button("Export decisions as JSON", use_container_width=True):
        st.download_button("Download", json.dumps(list(decided.values()), indent=2),
                           "decisions.json", "application/json", use_container_width=True)

# -------------------------------------------------------------------------- case header

r = results[st.session_state.selected]
case, w, a, rule = r.case, r.workup, r.assessment, r.rule
t = case.transaction
ver = {v.requirement_id: v for v in r.verifications}
prior = decided.get(case.case_id)

head_l, head_r = st.columns([3, 1])
with head_l:
    st.markdown(f"## {case.case_id} · {case.scheme.title()} {case.reason_code} {case.reason_code_label}")
    st.markdown(f"**{t.merchant_name}** · {case.chargeback_amount.value:,.2f} {case.chargeback_amount.currency} "
                f"· MCC {t.merchant_mcc} · transaction {t.transaction_date[:10]} · chargeback {case.chargeback_date}")
with head_r:
    st.markdown(f"### {TIER_BADGE[a.tier]}")
    st.markdown(f"**proposed: {a.final_action.value.replace('_', ' ')}**")
    if a.action_overridden:
        st.caption("forced by the scheme rule")
    if prior:
        st.success(f"decided: {prior['final_action']}", icon="✔️")

with st.container(border=True):
    st.markdown("**Why this tier**")
    for reason in a.reasons:
        st.markdown(f"- {reason}")

# ------------------------------------------------------------- rule / evidence / gap

rule_col, ev_col, gap_col = st.columns(3)
with rule_col:
    st.markdown("#### The rule")
    st.caption(f"{rule.logic_sentence()}  Coverage: {a.satisfied_count} of {a.required_count}.")
    st.write(w.reason_code_summary)
with ev_col:
    st.markdown("#### The evidence")
    if r.documents:
        for d in r.documents:
            st.markdown(f"- `{d.name}` · {d.page_count} page(s) · {d.kind}")
    else:
        st.markdown("_The merchant supplied no documents._")
    st.caption("Issuer says:")
    st.caption(f"_{case.issuer_narrative}_")
with gap_col:
    st.markdown("#### The gap")
    if w.caveats:
        for cav in w.caveats:
            st.warning(cav, icon="⚠️")
    else:
        st.info("No caveats raised.")

# ------------------------------------------------------------------------- checklist

st.markdown("#### Requirement checklist")
if not rule.requirements:
    st.info(f"{rule.scheme.title()} {rule.code} is not representable under the simplified rules, so the "
            "merchant's evidence was not assessed. " + (rule.note or ""))
overrides: dict[int, str] = {}
for ra in w.requirements:
    req = next(x for x in rule.requirements if x.id == ra.requirement_id)
    v = ver[ra.requirement_id]
    default = v.effective_status.value
    saved = (prior or {}).get("requirement_overrides", {}).get(str(ra.requirement_id))
    with st.container(border=True):
        left, right = st.columns([5, 1])
        with left:
            st.markdown(f"**{ra.requirement_id}. {req.text}**")
            st.markdown(f"{STATUS_BADGE[v.effective_status]} — {ra.reasoning}")
            if v.downgraded:
                st.warning(v.note, icon="⬇️")
            elif v.note:
                st.info(v.note, icon="ℹ️")
            for pc in v.pointers:
                with st.expander(f"{VERIFY_BADGE[pc.verified]} · {pc.document} · page {pc.page}"):
                    st.code(pc.quote, language=None)
                    if pc.note:
                        st.caption(pc.note)
        with right:
            opts = [s.value for s in Status]
            choice = st.selectbox("analyst", opts, index=opts.index(saved or default),
                                  key=f"ov_{case.case_id}_{ra.requirement_id}", label_visibility="collapsed")
            if choice != default:
                overrides[ra.requirement_id] = choice
                st.caption("overridden")

# ------------------------------------------------------------- rationale and decision

st.markdown("#### Decision")
dec_l, dec_r = st.columns([2, 1])
with dec_l:
    rationale = st.text_area("Representment rationale (edit before filing)",
                             (prior or {}).get("rationale", w.rationale), height=180,
                             key=f"rat_{case.case_id}")
    st.caption(f"{len(rationale.split())} words · the model's justification: {w.action_justification}")
with dec_r:
    action = st.selectbox("Action", ACTIONS,
                          index=ACTIONS.index((prior or {}).get("final_action", a.final_action.value)),
                          key=f"act_{case.case_id}")
    ask_default = "\n".join((prior or {}).get("evidence_to_request", w.evidence_to_request))
    ask = ""
    if action == Action.request_more_evidence.value:
        ask = st.text_area("Ask the merchant for", ask_default or "", height=120, key=f"ask_{case.case_id}")
    note = st.text_input("Note (optional)", (prior or {}).get("note", ""), key=f"note_{case.case_id}")

    changed = (action != a.final_action.value) or bool(overrides) or rationale.strip() != w.rationale.strip()
    if changed:
        st.caption("⚠️ differs from the proposal; the difference is recorded")
    if st.button("Approve and record", type="primary", use_container_width=True, key=f"ok_{case.case_id}"):
        DECISIONS.parent.mkdir(exist_ok=True)
        with DECISIONS.open("a") as f:
            f.write(json.dumps({
                "case_id": case.case_id,
                "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tool_action": a.final_action.value,
                "final_action": action,
                "action_changed": action != a.final_action.value,
                "tool_tier": a.tier,
                "requirement_overrides": {str(k): v for k, v in overrides.items()},
                "rationale": rationale,
                "rationale_edited": rationale.strip() != w.rationale.strip(),
                "evidence_to_request": [x for x in ask.splitlines() if x.strip()],
                "note": note,
            }, ensure_ascii=False) + "\n")
        st.cache_resource.clear()
        nxt = [c for c in order if c != case.case_id and c not in decided]
        st.session_state.selected = nxt[0] if nxt else case.case_id
        st.rerun()

with st.expander("Transaction record and deterministic pre-checks"):
    pre_l, pre_r = st.columns(2)
    with pre_l:
        for line in r.prechecks.as_lines():
            st.markdown(f"- {line}")
    with pre_r:
        st.json(t.model_dump(), expanded=False)
    st.caption(f"source: {'cache' if r.from_cache else 'api'} · attempts: {r.attempts}"
               + (f" · validation problems: {r.validation_problems}" if r.validation_problems else ""))
