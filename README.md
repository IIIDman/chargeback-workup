# Chargeback representment workup

A disputes analyst gets a chargeback, decides in about ninety seconds, and then spends eighteen minutes
re-reading scheme rules, opening PDFs to check one date, and writing up why. This tool does the eighteen
minutes: for each case it lays out **the rule, the evidence, and the gap**, drafts the representment
rationale, and recommends represent / accept liability / request more evidence. The analyst decides.

```
case ─┬─ scheme rule (encoded)      ─┐
      ├─ deterministic pre-checks    ├─ one LLM call ─ structured workup ─ quote verification ─ confidence tier ─ UI
      └─ evidence, page by page     ─┘
```

## Run it

Needs [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`). Python 3.12 is
fetched by uv if absent. No API key is needed for anything below: the ten workups are committed under
`artifacts/` and every command serves from them.

```bash
uv sync
uv run pytest                          # tests, no network
uv run python scripts/compare.py       # tool output vs my hand-written expectations
uv run python run.py CB-2025-0001      # one case as markdown, from cache
uv run streamlit run app.py            # the analyst UI
```

To regenerate the workups (or run a new case) you need a key:

```bash
cp .env.example .env                   # then set ANTHROPIC_API_KEY
uv run python run.py --all             # all ten, about $0.70 on claude-opus-5-5
uv run python run.py CB-2025-0004 --recompute   # one case, ignoring its cache
uv run python run.py CB-2025-0004 --dry-run     # print the exact prompt, make no call
```

`WORKUP_MODEL` in `.env` overrides the model. The model id is part of the cache key, so switching models
never replays another model's answers.

## What it produces

For every requirement of the reason code: a status (satisfied / partial / missing / not applicable), a
pointer to the document, the page and a **verbatim quote**, and the reasoning. Then a draft rationale, the
recommended action, what to ask the merchant for, and caveats. Every case carries a confidence tier with
the reasons that produced it.

On the ten provided cases: **46 pointers, 40 verified verbatim against the extracted text, 0 not found**,
6 pointing at images (not text-verifiable by design). Queue: 6 high, 2 medium, 2 needs review. Cost: the
cached answers add up to $0.61 at list price (`scripts/compare.py` prints the figure from the stored token
counts); the two cases that needed a second attempt were metered on the final answer only in this run, so
a full run is nearer $0.70. Every answer is metered from now on.

## Design decisions

**Split the job between code and the model.** The model reads documents; code owns everything that can be
decided without reading. The rule logic (`all` / `any two` / `any one` / non-representable) is encoded as
data in `workup/rules.py`, and `workup/checks.py` computes AVS/CVV/3DS, postcode match, date ordering and
amount agreement from the transaction record. So the outcome for Visa 10.5 is fixed by code whatever the
documents say (today the model is still called; skipping the call for rule-only codes is the first
production change), and a merchant document cannot talk the tool out of a failed AVS: for the requirements
the record answers by itself (AVS/CVV and 3DS under Mastercard 4837 and 4863), a satisfied that the record
contradicts is downgraded to missing before the tier is computed.

**Extract the text myself rather than posting PDFs to the model.** `workup/docs.py` pulls each page with
`pdfplumber`, and the page arrives in the prompt inside `<document name= page= of=>`. That buys three
things: I know exactly what the model saw, page-level pointers come free, and every quote can be checked
against the same text afterwards. The two PNGs go in as image blocks. Cost: a scanned PDF would need an
OCR step in `extract_pages`; there are none here (every page returns text), so I did not build or run one.

**The output schema is the guardrail.** `Workup` in `workup/schema.py` is passed to the API as the required
response format, so statuses and actions are enums rather than prose. What a JSON schema cannot express is
checked afterwards in `validate_pointers`: the document must be one of this case's files, the page must
exist in it, a satisfied requirement must carry a pointer. A failure sends the model its own answer plus the
list of problems for one corrective round-trip. On the committed run that fired on two of ten cases: one
answer carried a requirement id the rule does not have, the other returned one requirement of three and an
empty request list under request_more_evidence. Both validated on the second attempt. If problems survive
the retry they are kept and the case is marked needs review rather than trusted. An answer that does not
parse at all is retried once from scratch and then raised as an error, never cached: one run produced JSON
that degenerated into repeated asterisks mid-string, which is what that path is for.

**Field order is the order the model writes in.** The rationale sits after the recommended action and the
list of evidence still needed, so the filed text is written after the model has said what is missing. In
the first version it came straight after the requirement statuses, and on one case it asserted a
proof-of-delivery image that the same response asked the merchant for. I moved the field and, in the same
change, added a prompt rule against exactly that; I did not test them separately, so I cannot say which one
did the work. The reordering is the part that does not depend on the model following instructions.

**Confidence is computed, not asked for.** The model's own confidence is one weak input among several in
`workup/confidence.py`. The tier comes from things that can be checked: does the count of satisfied
requirements meet the rule's logic, were the cited quotes actually on the cited pages, does a transaction
fact cut against the recommendation, does a requirement rest on an image that code cannot read, how big is
the gap when more evidence is requested (asking is cheap for the tool and expensive for the analyst and the
merchant), and does the filed rationale mention something the same workup asks the merchant for. Two rules
took a wrong turn first and are worth stating:

- *Direction-neutral.* Accepting liability means the merchant eats the loss; it is a decision too, so
  conceding a case where requirements are partly met is `needs_review` in its own right.
- *But a signal only counts against the recommendation it undermines.* A failed AVS makes a represent
  doubtful; on an accept-liability it is the reason the recommendation is right, so it is not a warning.

Every tier is shown with its reasons. `needs_review` never appears without a sentence saying why.

**The tool downgrades rather than discards.** A satisfied requirement whose quotes cannot be found in the
document text becomes partial with a note, because a failed match is usually the extractor, not the model.
The analyst sees "quote NOT found" next to the quote and decides. The match itself is strict where it has
to be: on word boundaries (ORD-551 must not match inside ORD-5512), with punctuation turned into a space
rather than deleted (12.50 must not match inside 1,250.00), and a two-word fragment without a digit does
not count as evidence at all.

## For the analyst

`app.py` sorts the queue by tier, hardest first, then by amount. A case opens as rule / evidence / gap, with
the requirement checklist below it. Every pointer expands to show the quote and whether it was verified.
Every status has an override, the rationale is editable, and the action can be changed. Approving writes a
record to `artifacts/decisions.jsonl` that keeps both what the tool proposed and what the analyst chose,
which is the data you would want later to see where the tool is wrong.

The UI never calls the API. A stale cache is reported, not silently refreshed.

## Checking myself

`expected.json` holds an answer I wrote by hand for each case, after reading every document and before
running the tool on all ten. `scripts/compare.py` prints agreement on the action and whether the tool was
at least as cautious as I was, plus the tier distribution, because "mark everything needs review" would
score perfectly on caution while saving nobody any time.

First run: agreement on eight of ten. Of the two disagreements, one is a fair judgement call (whether a
freight company's own manifest counts as carrier confirmation when it is also the carrier), which I added
as an acceptable alternative. On the other the tool was right and I was wrong: it noticed the hotel charge
is dated 26 April while the booking says the rate was charged on 12 March, which makes a duplicate charge
possible. I also lowered the attention level I expected on one case after seeing that the output was a
clean, actionable request list. All three edits keep the original value in `expected.json` with the
reason, and `compare.py` prints `(key revised: ...)` on those rows.

Current run: ten of ten on action, nine of ten on caution. The miss is a case where the tool says high on
a request for evidence with one of four requirements satisfied, and I would have wanted medium. I left the
tier rules alone rather than tune them to one case; the row is printed as a miss.

Before submitting I went through the code layer with adversarial inputs rather than the ten cases, and fixed
what that found. Quote matching: digits could merge across punctuation, a quote could match inside a longer
word or identifier, a generic two-word fragment counted as evidence, and a word hyphenated across a line
break could not be found. Pre-checks: a missing billing postcode read as a mismatch, a non-breaking space or
a hyphen in a postcode broke the comparison, the currency check was case-sensitive, and the day count used
the local rather than the UTC calendar day. Tiering: the transaction record did not outrank the model on the
AVS/3DS requirements, a not-applicable on an unconditional requirement under an ALL rule was not raised, a
partial beyond an already met any-two rule was escalated for nothing, and a non-representable code skipped
the check for leftover validation problems. Robustness: a half-written artifact crashed the CLI and the UI
instead of counting as a cache miss, `--all` exited 0 with failures, the UI kept serving old artifacts
after a regeneration and fell over on one damaged log line, and the decision record did not keep the tool's
own status next to the analyst's override. None of these changed a tier on the ten cases; each has a test
in `tests/test_review_fixes.py`.

## Limitations

- Scanned documents would need OCR; every PDF here has a text layer.
- The rules are the take-home's simplified ones, not Visa VCR or the Mastercard Chargeback Guide: no time
  limits, thresholds or exclusions.
- The rationale-consistency check matches identifier-shaped tokens and over-fires when a reference is used
  as context in a request; it raises the tier to medium and tells the analyst what to look at.
- Ten cases is not an evaluation. It is enough to catch design errors, which it did, and not enough to
  quote an accuracy number. Four of the confidence rules were added or changed after seeing where the tool
  and I disagreed, which is how a small set should be used, and also why a number from it means little.
- The UI keeps an analyst's edits only until they switch case; approving records them.
- No auth, no deployment, no concurrency, single-user decision log.

## If this were going into production

Build the eval set first: a few hundred closed cases with the outcome the scheme actually gave, which is
the only ground truth that matters. Then choose the model against it rather than by reputation, and expect
a cascade: rule-only cases need no model at all, routine ones can run on a cheaper model, and only the
doubtful ones need the expensive one. Cache the system prompt and the rule text, batch the overnight queue
at half price, and track the override rate from `decisions.jsonl` per reason code, because the requirements
an analyst keeps correcting are where the prompt or the encoded rules are wrong.
