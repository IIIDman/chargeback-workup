# Challenge Dataset - README

This zip contains everything you need to build and test your
representment workup tool.

## Contents

.

├── README.md \# this file

├── reason_codes.md \# simplified compelling-evidence rules

├── cases.json \# 10 synthetic chargeback cases

└── documents/ \# 19 PDF/PNG merchant evidence files



All data is synthetic. Names, addresses, card numbers, BINs, tracking
numbers, and merchants are fabricated. Any resemblance to real entities
is coincidental.

## cases.json schema

cases.json is a JSON array of 10 case objects. Each case has the
following shape:

{

"case_id": "CB-2025-0001", // unique string identifier

"scheme": "visa", // "visa" \| "mastercard"

"reason_code": "13.1", // matches a code in reason_codes.md

"reason_code_label": "Merchandise / Services Not Received",

"chargeback_date": "2025-04-18", // ISO date the chargeback was raised

"chargeback_amount": {

"value": 189.99,

"currency": "GBP" // ISO 4217

},

"transaction": {

"transaction_id": "txn_8821AB",

"merchant_name": "NorthernThread Apparel",

"merchant_mcc": "5651", // 4-digit Merchant Category Code

"transaction_date": "2025-03-22T14:31:08Z",

"amount": { "value": 189.99, "currency": "GBP" },

"card_bin_country": "GB", // ISO 3166-1 alpha-2

"avs_result": "Y", // "Y" full match, "N" no match, "A" partial, null if
not run

"cvv_result": "M", // "M" match, "N" no match, null if not run

"three_ds_status": "authenticated", // "authenticated" \| "attempted" \|
"frictionless" \| "not_attempted"

"ip_address": "82.39.114.22", // string or null

"device_fingerprint": "fp_b7d2e9c4a1", // string or null

"billing_address_postcode": "SW4 7QR",

"shipping_address_postcode": "SW4 7QR" // may be null for digital goods
/ services

},

"issuer_narrative": "Free-text reason from the issuer...",

"merchant_evidence_documents": \[

"CB-2025-0001_delivery_confirmation.pdf",

"CB-2025-0001_carrier_tracking.pdf"

\]

}



Filenames in merchant_evidence_documents correspond to files in
documents/. Mix of PDFs and PNGs.

## reason_codes.md

Contains simplified compelling-evidence requirements for each of the 15
reason codes used in the dataset (9 Visa, 6 Mastercard). For each code,
the file lists what a merchant must provide for a defensible
representment.

**Important:** these rules are simplified for the take-home. They are
not a substitute for real Visa VCR or Mastercard Chargeback Guide
documentation.

## Cases at a glance

| **\#** | **Case ID** | **Scheme** | **Code** | **Merchant** | **Spoiler-free hint** |
|----|----|----|----|----|----|
| 1 | CB-2025-0001 | Visa | 13.1 | NorthernThread Apparel | Straightforward - sanity check |
| 2 | CB-2025-0002 | Visa | 13.1 | LumaTech Electronics | Check the addresses carefully |
| 3 | CB-2025-0003 | Mastercard | 4837 | Velo Continental Rentals | How many of the four requirements need to be met? |
| 4 | CB-2025-0004 | Mastercard | 4837 | QuickStream Digital | The merchant's evidence sounds confident |
| 5 | CB-2025-0005 | Visa | 12.6.1 | Roastline Coffee Co | Two charges, same amount, same day |
| 6 | CB-2025-0006 | Visa | 13.3 | HavenHome Furnishings | What can the photo actually tell you? |
| 7 | CB-2025-0007 | Mastercard | 4855 | TerraFreight Logistics | The evidence is in there somewhere |
| 8 | CB-2025-0008 | Visa | 13.2 | FluxFit Online | What's the difference between policy and proof? |
| 9 | CB-2025-0009 | Mastercard | 4859 | Hôtel Marais Saint-Paul | Read the policy and the log together |
| 10 | CB-2025-0010 | Visa | 10.5 | Brightline Beauty | Read the reason code rules carefully |

## Gotchas

A few cases are deliberately tricky. Without spoiling them all, here are
two examples to set expectations:

1.  **One case has merchant evidence that reads as authoritative and
    confident - internal scoring, neat tables, a "we are confident this
    is legitimate" conclusion - but does not actually meet any of the
    compelling-evidence requirements for the reason code raised.** If
    your tool simply trusts confident-sounding evidence, this is the
    case it will get wrong. The right answer is accept_liability. The
    interesting bit is *how* your tool's output makes it easy for the
    analyst to see that.

2.  **One case's compelling evidence is real, but buried inside a
    multi-page document containing mostly unrelated content.** A naive
    prompt that puts the whole document into context and asks "did the
    merchant prove delivery?" should work - but might come back
    uncertain or miss it. A more thoughtful approach (chunking, targeted
    extraction, citation back to a specific page/line) will do better.
    We're not requiring any specific approach - but we will read your
    output and assess whether an analyst could confidently act on it.

Other cases have address mismatches, partial requirement fulfillment,
missing 3DS, or reason codes that aren't representable at all. How you
surface these to the analyst is what we're evaluating.

## Currency, date, address notes

- Dates are ISO 8601, mostly UTC. Hotel and FR transactions use local
  context where it matters.

- Amounts are positive numbers; currency is ISO 4217.

- UK postcodes follow standard format; FR postcodes are 5-digit; DE
  postcodes are 5-digit.

- avs_result follows simplified Visa/Mastercard AVS coding: Y = full
  address match, A = address match but postcode no, N = no match, null =
  not checked.

## Running it

This dataset is framework-agnostic. Load cases.json however you like,
iterate cases, fetch document content from documents/ by filename, and
feed it to your LLM however suits your design.

If you want a smoke test that everything's wired up:

python -c "import json; cases = json.load(open('cases.json'));
print(f'{len(cases)} cases loaded'); print(cases\[0\]\['case_id'\],
cases\[0\]\['reason_code'\])"



Expected output:

10 cases loaded

CB-2025-0001 13.1


