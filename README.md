# chargeback-workup

Takes a chargeback case (reason code, transaction metadata, issuer narrative, merchant evidence files) and
produces an analyst-ready representment workup: the rule in plain English, each compelling-evidence
requirement marked satisfied / partial / missing with a pointer to the document, page and verbatim quote,
a draft rationale, and a recommended action.

Status: work in progress. Full run instructions, design notes and limitations will land here.

## Run

```bash
cp .env.example .env            # add ANTHROPIC_API_KEY
uv sync
uv run python run.py CB-2025-0001
uv run python run.py --all
uv run pytest
```
