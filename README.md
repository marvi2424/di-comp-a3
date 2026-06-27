# DIC2026 Assignment 3 — Group 36

Event-driven serverless review analysis on MiniStack (a local AWS emulator). A review
uploaded to S3 is processed by a chain of four Lambdas and stored in DynamoDB. All
resource names are resolved from SSM Parameter Store at runtime — nothing is hardcoded.

## Pipeline

```
S3 input bucket
  → preprocessing            tokenize / stop-word removal / lemmatize (NLTK)
  → S3 preprocessed bucket
  → EventBridge fan-out:
      → profanity            profanityfilter             → reviews table
      → sentiment            VADER (stub)                → reviews table
  → reviews-table stream
      → ban                  count impolite per reviewer (stub) → customers table
```

## Status

| Component | State |
|---|---|
| Infra — `deploy.sh`, SSM, S3/EventBridge/stream wiring (P1) | deploys & wires the whole chain; only the preprocessing→profanity path is exercised so far |
| preprocessing Lambda (P2) | done |
| profanity Lambda (P2) | done |
| sentiment Lambda (P3) | stub — returns 200, no logic yet |
| ban Lambda (P3) | stub — acks stream batch, no logic yet |
| Dataset run + tests (P4), report (P5) | pending |

preprocessing + profanity are verified end-to-end on MiniStack (both local Docker and
the TU Wien cluster).

## Layout

```
src/
  deploy.sh            provision buckets/tables/SSM, deploy + wire all 4 Lambdas
  event-pattern.json   EventBridge rule (preprocessed bucket → fan-out)
  CONTRACT.md          I/O shapes, SSM keys, table schemas (the interface contract)
  lambdas/
    preprocessing/     handler, requirements, fetch_nltk_data.sh, nltk_data/ (bundled)
    profanity/         handler, requirements
    sentiment/  ban/   P3 stubs
  data/                reviews_devset.json — obtain separately (see src/README.md)
```

## Run

Requires MiniStack on `:4566`, the `aws` CLI, and Python 3 (Docker optional — see Notes).

```bash
# once: fetch NLTK corpora (bundled into the preprocessing package)
cd src/lambdas/preprocessing && ./fetch_nltk_data.sh && cd -

bash src/deploy.sh   # provisions resources and deploys all Lambdas to MiniStack
```

After deploy, the chain runs whenever a review JSON is uploaded to the input bucket.
Inspect results in DynamoDB:

```bash
aws --endpoint-url=http://localhost:4566 dynamodb scan --table-name group36-reviews-table
```

## Notes

- `deploy.sh` builds each Lambda's dependencies in a `python:3.12-alpine` container
  when Docker is present (musl wheels, matching MiniStack's container worker) and
  falls back to a host `pip install` otherwise (correct when MiniStack runs Lambdas on
  the host runtime, e.g. the cluster). Either path produces a package that loads.
- `nltk_data/` (~86 MB) and `reviews_devset.json` (~57 MB) are gitignored; both must be
  present at deploy time and included in the submission ZIP.
- The dataset has no review ID, so preprocessing mints `reviewID` from the S3 object
  key (`review-000001.json` → `review-000001`). Name uploaded objects `<reviewID>.json`.
