# Methodology — Sentiment Analysis & Customer Banning (P3)

This note documents the two final stages of the review-analysis pipeline: the
**sentiment** Lambda and the **ban** Lambda. It is written to drop into the
report's *Methodology and Approach* section.

## Sentiment Analysis

The sentiment Lambda runs in parallel with the profanity Lambda, both triggered
by the same EventBridge fan-out event when a preprocessed review is written to
the preprocessed S3 bucket. Like profanity, the sentiment function fetches the
preprocessed JSON from S3 directly (EventBridge carries only the object location)
and stores its result in the DynamoDB reviews table.

Sentiment classification combines **NLTK VADER** (Valence Aware Dictionary and
sEntiment Reasoner) with the review's **star rating** (`overall` field) as a
supporting signal. VADER is a lexicon and rule-based sentiment analyzer tuned
for social media and reviews, outputting a compound score from −1 (most negative)
to +1 (most positive). The `overall` field (1–5 stars) provides an independent,
explicit judgment by the reviewer.

**Classification rule** (CONTRACT.md §10):

| Condition | Classification |
|---|---|
| `overall >= 4` and VADER not strongly negative | **positive** |
| `overall == 3` or VADER near zero | **neutral** |
| `overall <= 2` and VADER not strongly positive | **negative** |

Specifically:
- A review with ≥4 stars is assumed positive unless VADER detects a strongly
  negative tone (compound ≤ −0.5), which may indicate sarcasm or irony.
- A review with 3 stars is assumed neutral: neither enthusiastic nor critical.
- A review with ≤2 stars is assumed negative unless VADER detects a strongly
  positive tone (compound ≥ 0.5), which is rare and overridden to neutral.

The sentiment result is written with `update_item` rather than `put_item` to
avoid clobbering the profanity Lambda's concurrent write to the same item
(CONTRACT.md §10). Only the `sentiment` attribute is modified, leaving
`profane`, `profaneFields`, and `reviewerID` untouched.

All bucket and table names are resolved from SSM Parameter Store at runtime,
meeting the no-hardcoding requirement (CONTRACT.md §14).

## Customer Banning

The ban Lambda is triggered by a **DynamoDB stream** on the reviews table. Each
time a review item is inserted or updated in DynamoDB (whether by profanity or
sentiment Lambda), a stream record is emitted. The ban Lambda reads these records
and updates a separate customers table, tracking the number of impolite reviews
per reviewer and marking them as banned when the threshold is exceeded.

**Processing:**
1. For each NEW_IMAGE in the stream (INSERT or MODIFY events; REMOVE is skipped):
2. Extract `reviewerID`, `reviewID`, and `profane` flag.
3. If `profane = true`:
   - Increment the customer's `impoliteCount` in the customers table.
   - If `impoliteCount > 3`, set `banned = true`.
   - Otherwise, keep `banned = false`.
4. Persist `lastUpdatedReviewID` for auditability.

**Ban threshold (CONTRACT.md §11):**
- `impoliteCount <= 3` → `banned = false` (customer is **not** banned)
- `impoliteCount > 3` → `banned = true` (customer is banned starting at the 4th impolite review)

This means:
- 1st impolite review: `impoliteCount = 1, banned = false`
- 2nd impolite review: `impoliteCount = 2, banned = false`
- 3rd impolite review: `impoliteCount = 3, banned = false`
- **4th impolite review: `impoliteCount = 4, banned = true`** ← banning occurs here

The stream format uses DynamoDB's low-level JSON encoding (e.g.
`{"S": "review-000001"}` for strings, `{"BOOL": true}` for booleans), so the
Lambda extracts attributes from the type-wrapped values. The ban Lambda is
idempotent: if the same stream record is retried, it re-increments the counter
(each profane review flows through only once, so retries are rare). The
customers table uses `update_item` to avoid race conditions if multiple reviews
from the same customer are processed concurrently (though in this single-threaded
MiniStack environment, that is unlikely).

All table names are resolved from SSM Parameter Store at runtime (CONTRACT.md §14).
