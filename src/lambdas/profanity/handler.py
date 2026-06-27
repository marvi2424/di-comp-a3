"""Profanity Lambda (DIC2026 A3, Group 36 — P2).

Second stage of the chain. Triggered via EventBridge when a preprocessed review
is created in the preprocessed S3 bucket (deploy.sh wires the preprocessed
bucket -> EventBridge -> this Lambda + the sentiment Lambda). The EventBridge
event only points at the object, so this function fetches it from S3 itself,
runs ``profanityfilter`` over ``summary`` and ``reviewText``, and records the
result in the reviews DynamoDB table (CONTRACT.md §9).

Concurrency note: the sentiment Lambda writes the *same* reviews-table item in
parallel off the same event. We therefore use ``update_item`` (SET only our own
attributes) rather than ``put_item`` so the two writers never clobber each
other. ``reviewerID`` is written here so the ban Lambda can read it off the
DynamoDB stream (CONTRACT.md §11).

All resource names come from SSM Parameter Store at runtime (CONTRACT.md §14).
"""

import json
import os
from urllib.parse import unquote_plus

import boto3
from profanityfilter import ProfanityFilter

# --- MiniStack / AWS endpoint handling (mirrors deploy.sh STAGE=local) ------
ENDPOINT_URL = "http://localhost:4566" if os.getenv("STAGE") == "local" else None

s3 = boto3.client("s3", endpoint_url=ENDPOINT_URL)
ssm = boto3.client("ssm", endpoint_url=ENDPOINT_URL)
dynamodb = boto3.resource("dynamodb", endpoint_url=ENDPOINT_URL)

# Built once per container (warm-start friendly).
_PF = ProfanityFilter()

# Text fields checked for profanity (CONTRACT.md §9 / assignment req. 4).
PROFANITY_FIELDS = ("summary", "reviewText")

# SSM parameter names (authoritative source: deploy.sh / CONTRACT.md §6).
SSM_REVIEWS_TABLE = "/dic2026/group36/dynamodb/reviews_table"

_param_cache = {}


def get_parameter(name):
    """Resolve an SSM parameter, caching the value for warm invocations."""
    if name not in _param_cache:
        _param_cache[name] = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    return _param_cache[name]


def check_profanity(review):
    """Return ``(profane, profane_fields)`` for a preprocessed review.

    A field is impolite when ``profanityfilter`` finds it not clean. The review
    is profane if *any* checked field is impolite; ``profane_fields`` lists which
    ones triggered (CONTRACT.md §9).
    """
    profane_fields = []
    for field in PROFANITY_FIELDS:
        text = review.get(field) or ""
        if text and not _PF.is_clean(text):
            profane_fields.append(field)
    return (len(profane_fields) > 0, profane_fields)


def iter_object_locations(event):
    """Yield ``(bucket, key)`` from the EventBridge 'Object Created' event.

    Primary shape is the S3-via-EventBridge event
    (``detail.bucket.name`` / ``detail.object.key``). The raw S3 notification
    shape (``Records[].s3``) is also tolerated so the same handler can be
    invoked directly in tests.
    """
    if isinstance(event, dict) and "detail" in event:
        detail = event["detail"]
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
    """Flag profanity for every preprocessed review referenced by the event."""
    table = dynamodb.Table(get_parameter(SSM_REVIEWS_TABLE))

    results = []
    for bucket, key in iter_object_locations(event):
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        review = json.loads(raw)

        review_id = review["reviewID"]
        reviewer_id = review.get("reviewerID")
        profane, profane_fields = check_profanity(review)

        # update_item (not put_item): touch only the attributes CONTRACT.md §9
        # assigns to this stage, so the sentiment Lambda's parallel write to the
        # same item is never clobbered. reviewerID is persisted so the ban
        # Lambda can read it off the DynamoDB stream.
        table.update_item(
            Key={"reviewID": review_id},
            UpdateExpression="SET profane = :p, profaneFields = :pf, reviewerID = :r",
            ExpressionAttributeValues={
                ":p": profane,
                ":pf": profane_fields,
                ":r": reviewer_id,
            },
        )
        print(
            "profanity {} profane={} fields={}".format(
                review_id, profane, profane_fields
            )
        )
        results.append({"reviewID": review_id, "profane": profane})

    return {"statusCode": 200, "results": results}
