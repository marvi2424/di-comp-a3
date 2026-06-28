"""Sentiment Lambda (DIC2026 A3, Group 36 — P3).

Second fan-out stage of the chain. Triggered via EventBridge when a preprocessed
review is created in the preprocessed S3 bucket (deploy.sh wires the preprocessed
bucket -> EventBridge -> this Lambda + the profanity Lambda). The EventBridge
event only points at the object, so this function fetches it from S3 itself,
analyzes sentiment with NLTK VADER over ``summary`` and ``reviewText``, and
records the result in the reviews DynamoDB table (CONTRACT.md §10).

Concurrency note: the profanity Lambda writes the *same* reviews-table item in
parallel off the same event. We therefore use ``update_item`` (SET only our own
attributes) rather than ``put_item`` so the two writers never clobber each
other.

All resource names come from SSM Parameter Store at runtime (CONTRACT.md §14).
"""

import json
import os
from urllib.parse import unquote_plus

import boto3
import nltk
from nltk.sentiment import SentimentIntensityAnalyzer

# --- MiniStack / AWS endpoint handling (mirrors deploy.sh STAGE=local) ------
ENDPOINT_URL = "http://localhost:4566" if os.getenv("STAGE") == "local" else None
REGION_NAME = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"

s3 = boto3.client("s3", endpoint_url=ENDPOINT_URL, region_name=REGION_NAME)
ssm = boto3.client("ssm", endpoint_url=ENDPOINT_URL, region_name=REGION_NAME)
dynamodb = boto3.resource(
    "dynamodb", endpoint_url=ENDPOINT_URL, region_name=REGION_NAME
)

# Built once per container (warm-start friendly).
_VADER = SentimentIntensityAnalyzer()

# Text fields analyzed for sentiment (CONTRACT.md §10 / assignment req. 4).
SENTIMENT_FIELDS = ("summary", "reviewText")

# SSM parameter names (authoritative source: deploy.sh / CONTRACT.md §6).
SSM_REVIEWS_TABLE = "/dic2026/group36/dynamodb/reviews_table"

_param_cache = {}


def get_parameter(name):
    """Resolve an SSM parameter, caching the value for warm invocations."""
    if name not in _param_cache:
        _param_cache[name] = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    return _param_cache[name]


def classify_sentiment(summary, review_text, overall):
    """Classify sentiment as positive, neutral, or negative.

    Uses NLTK VADER sentiment analysis on summary and reviewText, combined with
    the overall star rating as a supporting signal per CONTRACT.md §10.

    Classification rule (CONTRACT.md §10):
    - overall >= 4 and VADER not strongly negative → positive
    - overall == 3 or VADER near zero → neutral
    - overall <= 2 and VADER not strongly positive → negative

    Returns one of: "positive", "neutral", "negative"
    """
    # Combine text fields for holistic analysis.
    combined_text = "{} {}".format(summary or "", review_text or "")

    # VADER returns compound score: -1 (most negative) to +1 (most positive).
    # Scores: neg < 0.4, neu 0.1-0.4, pos > 0.05 are common, but compound is
    # the best single metric.
    vader_scores = _VADER.polarity_scores(combined_text)
    compound = vader_scores["compound"]

    overall = float(overall) if overall else 3.0

    # Thresholds for "strongly" positive/negative VADER scores.
    # Typically: compound >= 0.5 is strongly positive, <= -0.5 is strongly negative.
    strongly_positive_threshold = 0.5
    strongly_negative_threshold = -0.5
    vader_neutral_threshold = 0.1

    # Classification rule from CONTRACT.md §10
    if overall >= 4:
        if compound > strongly_negative_threshold:
            return "positive"
        else:
            # overall says positive but VADER says strongly negative: override to neutral
            return "neutral"

    elif overall == 3:
        return "neutral"

    elif overall <= 2:
        if compound < strongly_positive_threshold:
            return "negative"
        else:
            # overall says negative but VADER says strongly positive: override to neutral
            return "neutral"

    # Fallback (should not reach here)
    return "neutral"


def iter_object_locations(event):
    """Yield ``(bucket, key)`` from the EventBridge 'Object Created' event.

    Primary shape is the S3-via-EventBridge event
    (``detail.bucket.name`` / ``detail.object.key``). The raw S3 notification
    shape (``Records[].s3``) is also tolerated so the same handler can be
    invoked directly in tests.
    """
    if isinstance(event, dict) and "detail" in event:
        detail = event["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        bucket = detail["bucket"]["name"]
        key = unquote_plus(detail["object"]["key"])
        return [(bucket, key)]

    if isinstance(event, dict) and isinstance(event.get("Records"), list):
        locations = []
        for record in event["Records"]:
            s3_info = record["s3"]
            locations.append(
                (s3_info["bucket"]["name"], unquote_plus(s3_info["object"]["key"]))
            )
        return locations

    raise ValueError("unsupported event payload: {!r}".format(event))


def lambda_handler(event, context):
    """Analyze sentiment for every preprocessed review referenced by the event."""
    table = dynamodb.Table(get_parameter(SSM_REVIEWS_TABLE))

    results = []
    for bucket, key in iter_object_locations(event):
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        review = json.loads(raw)

        review_id = review["reviewID"]
        summary = review.get("summary", "")
        review_text = review.get("reviewText", "")
        overall = review.get("overall", 3.0)

        sentiment = classify_sentiment(summary, review_text, overall)

        # update_item (not put_item): touch only the attributes CONTRACT.md §10
        # assigns to this stage, so the profanity Lambda's parallel write to the
        # same item is never clobbered.
        table.update_item(
            Key={"reviewID": review_id},
            UpdateExpression="SET sentiment = :s",
            ExpressionAttributeValues={
                ":s": sentiment,
            },
        )
        print(
            "sentiment {} sentiment={}".format(
                review_id, sentiment
            )
        )
        results.append({"reviewID": review_id, "sentiment": sentiment})

    return {"statusCode": 200, "results": results}
