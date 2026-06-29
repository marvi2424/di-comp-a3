# P4 Methodology — Dataset Run, Results & Integration Tests

**Scope (work plan P4):** push the whole devset through the deployed chain,
produce the three required result groups, and write integration tests for all
five functionalities. P4 owns the heavy compute and validates P2/P3 by test,
without touching Lambda internals (bugs go back as failing tests).

## Batch loader — `src/tools/load_devset.py`

The loader treats one review as one chain run, matching the assignment's
"chain starts when one review is added to an S3 bucket": each review from
`reviews_devset.json` is uploaded as its **own** object into the input bucket,
keyed `review-NNNNNN.json`. That key basename is exactly what the preprocessing
Lambda turns into the `reviewID`, so the loader can later look every review up.

All resource names (input bucket, both tables) are read from **SSM Parameter
Store**, never hardcoded — the same rule the Lambdas follow (CONTRACT.md §14).

Because the chain is fully asynchronous (S3 → EventBridge fan-out → DynamoDB
stream), the loader does not assume instant completion. It:

1. uploads all objects (small thread pool — the cost is the S3 round-trip, not
   CPU);
2. polls the reviews table until every item carries **both** `profane` and
   `sentiment` (i.e. both fan-out Lambdas have run);
3. waits for the customers table to **settle** (the ban Lambda trails the
   reviews because it runs off the table's stream);
4. aggregates and writes `src/results/results.json`.

A `--limit` flag allows a fast smoke run; `--skip-upload` re-aggregates an
already-populated stack without re-uploading.

## Reported numbers — `src/results/results.json`

Computed **only** from the devset (CONTRACT.md §13), all from a single full scan
of the **reviews table** — the complete, authoritative record of every processed
review:

- sentiment buckets (#positive / #neutral / #negative) and the profanity-failure
  count come straight from the per-review attributes;
- the **banned set** is derived from the per-reviewer profane counts (a reviewer
  with more than 3 impolite reviews is banned).

**Why the banned set is derived from the reviews table, not the customers
table:** the ban Lambda is the live, event-driven mechanism (and the integration
tests prove its logic — 3 impolite ⇒ not banned, 4 ⇒ banned). But when the whole
devset is pushed through at once, MiniStack drops a large fraction of
DynamoDB-stream events under the load, so the stream-fed customers table
undercounts (in our full run it captured ~838 of 3125 impolite reviews and
reported zero bans). Re-deriving the ban set from the complete reviews table is
robust to that load artifact and reproducible. On the devset this yields exactly
one banned user (`A320TMDV6KCFU`, 4 impolite reviews).

Corner-case reviews are never mixed in — they only ever run through the test
suite.

## Integration tests — `src/tests/test_integration.py`

Five tests, one per required functionality, each driving the **real deployed
chain** end to end (upload → wait → assert on stored state), with tiny synthetic
fixtures and unique uuid-based IDs so reruns never collide:

1. **Preprocessing** — tokens are lowercased, stop words removed, plurals
   lemmatized (`cats→cat`, `boxes→box`).
2. **Profanity** — an impolite review is flagged (`profane=true`, offending
   field recorded), a clean one is not.
3. **Sentiment** — positive / neutral / negative classified from representative
   reviews.
4. **Counting** — repeated impolite reviews increment the customer's
   `impoliteCount`.
5. **Ban edge case** — 3 impolite reviews keep `banned=false`; the 4th flips it
   to `true`.

## Corner cases — `src/test_reviews/corner_cases.json`

Shipped fixtures for the tricky paths (ban threshold both sides, clean user,
sarcasm override, 3-star neutral, empty text, profanity only in `summary`).
Excluded from the reported devset numbers.

They are also **verified automatically**: `test_corner_cases_behave_as_documented`
loads this file, drives every review through the deployed chain, and asserts the
documented outcomes (customerBANNED with 4 impolite ⇒ banned, customerEDGE with
exactly 3 ⇒ not banned, profanity in `summary` is caught, 3-star and sarcastic
5-star reviews ⇒ neutral). Fixed IDs in the file are prefixed with a per-run id
so reruns stay isolated.

> Run the test suite on a **fresh** MiniStack (`ministack` restart + `deploy.sh`),
> not on a stack that has just had the full devset pushed through it: after tens
> of thousands of reviews MiniStack's DynamoDB-stream delivery degrades, so the
> ban-dependent assertions can spuriously fail on an exhausted stack.
