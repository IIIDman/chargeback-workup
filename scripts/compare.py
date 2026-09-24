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
PRICE_PER_MTOK = {"input_tokens": 4.0, "output_tokens": 20.0}  # claude-opus-5-5 list price, USD


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    expected = {k: v for k, v in json.loads((root / "expected.json").read_text()).items() if not k.startswith("_")}
    cases = load_cases()
    rows, action_ok, tier_ok, skipped = [], 0, 0, 0
    tiers: dict[str, int] = {"high": 0, "medium": 0, "needs_review": 0}
    tokens = {"input_tokens": 0, "output_tokens": 0}
    attempts_total = metered_total = 0
    for cid, exp in expected.items():
        if cid not in cases:
            rows.append((cid, "(in expected.json but not in cases.json)", "", "", ""))
            skipped += 1
            continue
        if not (ARTIFACTS / f"{cid}.json").exists():
            rows.append((cid, "(no cached result: run `uv run python run.py --all` first)", "", "", ""))
            skipped += 1
            continue
        try:
            r = run_case(cases[cid], allow_api=False)  # never calls the API; anything else is a real error
        except LookupError as e:
            rows.append((cid, f"(skipped: {e})", "", "", ""))
            skipped += 1
            continue
        for k in tokens:
            tokens[k] += int(r.usage.get(k) or 0)
        attempts_total += r.attempts
        metered_total += int(r.usage.get("attempts_metered") or 1)
        got = r.assessment.final_action.value
        acceptable = [exp["action"], *exp.get("also_acceptable", [])]
        a_ok = got in acceptable
        t_ok = RANK[r.assessment.tier] >= RANK[exp["tier"]]
        action_ok += a_ok
        tier_ok += t_ok
        tiers[r.assessment.tier] += 1
        revised = ""
        bits = []
        if "original" in exp:
            bits.append(f"action was {exp['original']}")
        if "original_tier" in exp:
            bits.append(f"tier was {exp['original_tier']}")
        if "original_also_acceptable" in exp:
            bits.append(f"also_acceptable was {exp['original_also_acceptable'] or 'empty'}")
        if bits:
            revised = f"  (key revised: {', '.join(bits)})"
        rows.append((
            cid,
            f"{'ok ' if a_ok else 'XX '}{got:<22} expected {exp['action']}"
            + (f" (or {', '.join(exp['also_acceptable'])})" if exp.get("also_acceptable") else "") + revised,
            f"{'ok ' if t_ok else 'XX '}{r.assessment.tier:<12} expected >= {exp['tier']}",
            f"{r.assessment.satisfied_count}/{r.assessment.required_count}",
            "; ".join(x for x in r.assessment.reasons[:2]),
        ))

    for cid, act, tier, cov, why in rows:
        print(f"{cid}  {act}")
        if tier:
            print(f"{'':14}{tier}   coverage {cov}")
            print(f"{'':14}{why}")
    n = len(expected)
    print(f"\naction agreement: {action_ok}/{n}   tier at least as cautious as expected: {tier_ok}/{n}"
          + (f"   ({skipped} case(s) skipped, counted as misses)" if skipped else ""))
    print(f"queue: {tiers['high']} high, {tiers['medium']} medium, {tiers['needs_review']} needs review")
    print("(a tool that marked everything needs_review would score 10/10 on caution and save nobody any time;"
          "\n the distribution is the counterweight to that metric)")
    cost = sum(tokens[k] / 1e6 * PRICE_PER_MTOK[k] for k in tokens)
    unmetered = attempts_total - metered_total
    print(f"tokens in the cache: {tokens['input_tokens']:,} in / {tokens['output_tokens']:,} out over "
          f"{metered_total} metered answer(s); ${cost:.2f} at ${PRICE_PER_MTOK['input_tokens']:.0f}/"
          f"${PRICE_PER_MTOK['output_tokens']:.0f} per MTok"
          + (f"; {unmetered} earlier attempt(s) were not metered, so the true cost is higher" if unmetered else ""))
    return 1 if skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
