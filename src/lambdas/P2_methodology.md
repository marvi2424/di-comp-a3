# Methodology — Preprocessing & Profanity (P2)

This note documents the two entry stages of the review-analysis pipeline: the
**preprocessing** Lambda and the **profanity** Lambda. It is written to drop
into the report's *Methodology and Approach* section.

## Preprocessing

The preprocessing Lambda is the entry point of the chain. It is triggered by an
*object-created* notification on the input S3 bucket, so each uploaded review is
processed independently as it arrives — the system never polls or batches at this
stage, satisfying the event-driven requirement. The bucket-to-Lambda wiring is a
direct S3 notification, so the function receives the classic
`Records[].s3.{bucket,object}` event shape and skips the `s3:TestEvent` that S3
emits when the notification is first configured.

For each review the function reads `summary` and `reviewText` (and preserves
`overall` together with all remaining metadata such as `reviewerID`, `asin`, and
`category`), then normalizes both text fields with a three-step **NLTK** pipeline:

1. **Tokenization** with NLTK's `word_tokenize` (Punkt model). Tokens are
   lowercased and reduced to alphabetic tokens only, which removes punctuation
   and numeric noise.
2. **Stop-word removal** using NLTK's English stop-word list, eliminating
   high-frequency function words that carry little analytic signal.
3. **Lemmatization** with the WordNet lemmatizer, mapping inflected forms to
   their base form (e.g. *dogs → dog*).

Lemmatization uses WordNet's default (noun) part of speech rather than running a
POS tagger first. This is a deliberate trade-off: POS-aware lemmatization would
require bundling an additional tagger corpus for an accuracy gain the assignment
explicitly does not grade ("the scope … is on the design … rather than …
accuracy"). The cost is minor and visible only on some verb forms (e.g.
*running* is not reduced to *run*, and the lemmatizer occasionally over-trims a
short token such as *us → u*).

The output is the original record augmented with three token lists —
`processedSummaryTokens`, `processedReviewTokens`, and their concatenation
`combinedProcessedText` — and a `reviewID`, then written to the preprocessed S3
bucket at `preprocessed/{reviewID}.json` (CONTRACT.md §8). Because the raw Amazon
review records contain no native review identifier yet the downstream DynamoDB
reviews table is keyed by `reviewID`, the function mints one: it prefers an
explicit `reviewID` field if present, otherwise derives it from the input object
key (`review-000001.json → review-000001`), giving a stable, human-readable key
that the loader controls.

**Runtime data.** NLTK corpora (Punkt, stop words, WordNet, OMW) are pre-fetched
and bundled inside the deployment package (`nltk_data/`, ~86 MB unzipped, well
under the 250 MB limit) and registered on `nltk.data.path` at import time, so the
function never attempts a network download — important because MiniStack Lambdas
have no outbound connectivity.

## Profanity

The profanity Lambda runs immediately after preprocessing. The preprocessed
bucket is configured to emit events to **EventBridge**, and an EventBridge rule
fans the single *Object Created* event out to both the profanity and sentiment
Lambdas in parallel. The EventBridge event carries only the object's location
(`detail.bucket.name` / `detail.object.key`), so the function fetches the
preprocessed JSON from S3 itself.

Profanity is detected with the **`profanityfilter`** library applied to the
`summary` and `reviewText` fields. A review is flagged `profane = true` if either
field is not clean, and the offending field names are recorded in
`profaneFields` (CONTRACT.md §9). The check runs on the original text rather than
the lemmatized tokens, since the bad-word list matches surface forms.

The result is written to the DynamoDB reviews table with **`update_item`** rather
than `put_item`. This is essential because the sentiment Lambda writes the *same*
item (keyed by the same `reviewID`) concurrently off the same fan-out event; an
`update_item` that sets only this stage's attributes (`profane`, `profaneFields`,
`reviewerID`) guarantees the two parallel writers never overwrite each other's
results. Persisting `reviewerID` here also lets the downstream ban Lambda read it
directly from the DynamoDB stream.

All bucket and table names used by both Lambdas are resolved from SSM Parameter
Store at runtime (resolved values are cached across warm invocations); no
resource name is hardcoded, meeting the assignment's parameter-store requirement.
