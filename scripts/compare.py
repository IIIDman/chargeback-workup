"""Compare the tool's output against expected.json (my hand-made expectations).

  uv run python scripts/compare.py

Reads cached artifacts only; never calls the API. Prints one row per case and a short summary.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from workup.pipeline import ARTIFACTS, load_cases, run_case  # noqa: E402

RANK = {"high": 0, "medium": 1, "needs_review": 2}


def main() -> int:
    expected = {k: v for k, v in json.loads(Path("expected.json").read_text()).items() if not k.startswith("_")}
    cases = load_cases()
    rows, action_ok, tier_ok = [], 0, 0
    for cid, exp in expected.items():
        if not (ARTIFACTS / f"{cid}.json").exists():
            rows.append((cid, "(no cached result: run `uv run python run.py --all` first)", "", "", ""))
            continue
        r = run_case(cases[cid])  # cache hit; a fingerprint mismatch would call the API, so run --all first
        got = r.assessment.final_action.value
        acceptable = [exp["action"], *exp.get("also_acceptable", [])]
        a_ok = got in acceptable
        t_ok = RANK[r.assessment.tier] >= RANK[exp["tier"]]
        action_ok += a_ok
        tier_ok += t_ok
        rows.append((
            cid,
            f"{'ok ' if a_ok else 'XX '}{got:<22} expected {exp['action']}"
            + (f" (or {', '.join(exp['also_acceptable'])})" if exp.get("also_acceptable") else ""),
            f"{'ok ' if t_ok else 'XX '}{r.assessment.tier:<12} expected >= {exp['tier']}",
            f"{r.assessment.satisfied_count}/{r.assessment.required_count}",
            "; ".join(x for x in r.assessment.reasons[:2]),
        ))

    for cid, act, tier, cov, why in rows:
        print(f"{cid}  {act}")
        if tier:
            print(f"{'':14}{tier}   coverage {cov}")
            print(f"{'':14}{why}")
    n = sum(1 for r in rows if r[2])
    print(f"\naction agreement: {action_ok}/{n}   tier at least as cautious as expected: {tier_ok}/{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
