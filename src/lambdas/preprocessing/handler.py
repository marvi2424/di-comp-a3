"""Preprocessing Lambda (DIC2026 A3, Group 36 — P2).

Entry point of the review-analysis chain. Triggered by an object created in the
input S3 bucket (raw S3 notification, *not* EventBridge). For each uploaded
review JSON it:

  1. reads ``summary``, ``reviewText`` and ``overall`` (keeps all other metadata),
  2. tokenizes both text fields with NLTK,
  3. removes English stop words,
  4. lemmatizes the remaining tokens (WordNet),
  5. writes the preprocessed JSON to the preprocessed S3 bucket under
     ``preprocessed/{reviewID}.json`` (see CONTRACT.md §8).

All bucket names are resolved from SSM Parameter Store at runtime — nothing is
hardcoded (CONTRACT.md §4 / §14). NLTK corpora are bundled next to this file in
``nltk_data/`` (deploy.sh copies that directory into the deployment package);
the path is registered below so NLTK never tries to download at runtime.
"""

import hashlib
import json
import os
from urllib.parse import unquote_plus

import boto3
import nltk
from nltk.corpus import stopwords
from nltk.stem import WordNetLemmatizer
from nltk.tokenize import word_tokenize

# --- MiniStack / AWS endpoint handling -------------------------------------
# deploy.sh deploys the Lambda with the environment variable STAGE=local, which
# is how the tutorial decides to talk to MiniStack instead of real AWS. Mirror
# that exactly (the original skeleton used a different mechanism — STAGE is what
# actually gets set at deploy time).
ENDPOINT_URL = "http://localhost:4566" if os.getenv("STAGE") == "local" else None

s3 = boto3.client("s3", endpoint_url=ENDPOINT_URL)
ssm = boto3.client("ssm", endpoint_url=ENDPOINT_URL)

# --- NLTK data: use the bundled corpora, never download at runtime ----------
_BUNDLED_NLTK_DATA = os.path.join(os.path.dirname(__file__), "nltk_data")
if _BUNDLED_NLTK_DATA not in nltk.data.path:
    nltk.data.path.insert(0, _BUNDLED_NLTK_DATA)

# Built once per container (warm-start friendly).
_LEMMATIZER = WordNetLemmatizer()
_STOPWORDS = set(stopwords.words("english"))

# SSM parameter names (authoritative source: deploy.sh / CONTRACT.md §6).
SSM_PREPROCESSED_BUCKET = "/dic2026/group36/s3/preprocessed_bucket"

# Cache resolved parameter values across warm invocations.
_param_cache = {}


def get_parameter(name):
    """Resolve an SSM parameter, caching the value for warm invocations."""
    if name not in _param_cache:
        _param_cache[name] = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    return _param_cache[name]


def preprocess_text(text):
    """Tokenize -> lowercase -> drop non-alpha & stop words -> lemmatize.

    Lemmatization uses WordNet's default (noun) part of speech. POS tagging is
    intentionally skipped: it would add another corpus to the bundle for an
    accuracy gain the assignment explicitly does not grade ("the scope ... is on
    the design ... rather than ... accuracy"). Returns a list of tokens.
    """
    if not text:
        return []

    tokens = []
    for raw in word_tokenize(text):
        token = raw.lower()
        if not token.isalpha():  # drop punctuation / numerics
            continue
        if token in _STOPWORDS:
            continue
        tokens.append(_LEMMATIZER.lemmatize(token))
    return tokens


def derive_review_id(record, key):
    """Determine the per-review primary key (CONTRACT.md §8 / reviews-table key).

    The raw devset records carry no ``reviewID`` and the reviews DynamoDB table
    uses ``reviewID`` as its hash key, so one must be minted here. Resolution
    order (robust to however the loader uploads objects):

      1. an explicit ``reviewID`` field in the JSON, if present;
      2. otherwise the input object key's basename without ``.json``
         (e.g. ``review-000001.json`` -> ``review-000001``);
      3. otherwise a deterministic fallback from identifying fields.
    """
    explicit = record.get("reviewID")
    if explicit:
        return str(explicit)

    base = os.path.basename(key)
    if base.lower().endswith(".json"):
        base = base[: -len(".json")]
    if base:
        return base

    # Last-resort id, deterministic across runs (hashlib, not the salted
    # built-in hash()). Only reached if both a reviewID field and the object
    # key are absent, which should not happen for a real S3 upload.
    seed = "{}|{}|{}".format(
        record.get("reviewerID", ""),
        record.get("asin", ""),
        record.get("unixReviewTime", ""),
    )
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return "review-" + digest


def build_output(record, review_id):
    """Assemble the preprocessed document per CONTRACT.md §8.

    Starts from a copy of the original record so every original metadata field
    is preserved (step 5: "keep all original metadata fields"), then adds the
    review id and the three token lists. Consumers (P3/P4) read by field name,
    so the extra preserved fields are harmless.
    """
    summary = record.get("summary", "") or ""
    review_text = record.get("reviewText", "") or ""

    summary_tokens = preprocess_text(summary)
    review_tokens = preprocess_text(review_text)

    output = dict(record)
    output["reviewID"] = review_id
    output["processedSummaryTokens"] = summary_tokens
    output["processedReviewTokens"] = review_tokens
    output["combinedProcessedText"] = summary_tokens + review_tokens
    return output


def iter_s3_records(event):
    """Yield S3 notification records, tolerating the shapes MiniStack sends.

    The input bucket is wired with a *raw* Lambda notification (deploy.sh), so
    the event is the classic ``{"Records": [{"s3": {...}}]}`` shape. S3 also
    emits a test event when the notification is first configured — skip it.
    """
    if isinstance(event, dict):
        if event.get("Event") == "s3:TestEvent":
            return []
        if isinstance(event.get("Records"), list):
            return event["Records"]
        if "s3" in event:
            return [event]
    if isinstance(event, list):
        return event
    raise ValueError("unsupported S3 event payload: {!r}".format(event))


def lambda_handler(event, context):
    """Process every review object referenced by the triggering S3 event."""
    dest_bucket = get_parameter(SSM_PREPROCESSED_BUCKET)

    processed = []
    for record in iter_s3_records(event):
        src_bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])

        raw = s3.get_object(Bucket=src_bucket, Key=key)["Body"].read()
        review = json.loads(raw)

        review_id = derive_review_id(review, key)
        output = build_output(review, review_id)

        out_key = "preprocessed/{}.json".format(review_id)
        s3.put_object(
            Bucket=dest_bucket,
            Key=out_key,
            Body=json.dumps(output).encode("utf-8"),
            ContentType="application/json",
        )
        print("preprocessed {} -> s3://{}/{}".format(key, dest_bucket, out_key))
        processed.append(out_key)

    return {"statusCode": 200, "processed": processed}
