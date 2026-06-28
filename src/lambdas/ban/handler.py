"""Ban Lambda (DIC2026 A3, Group 36 — P3).

Final stage of the chain. Triggered by DynamoDB stream events from the reviews
table. For each new/updated review item with profane=true, increments the
reviewer's impolite count in the customers table and sets ``banned = true``
when the count exceeds 3 (CONTRACT.md §11).

Ban rule (CONTRACT.md §11):
- impoliteCount <= 3 → banned = false (customer *not* banned)
- impoliteCount > 3 → banned = true (customer is banned on the 4th impolite review)

All resource names come from SSM Parameter Store at runtime (CONTRACT.md §14).
"""

import os

import boto3

# --- MiniStack / AWS endpoint handling (mirrors deploy.sh STAGE=local) ------
ENDPOINT_URL = "http://localhost:4566" if os.getenv("STAGE") == "local" else None
REGION_NAME = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"

ssm = boto3.client("ssm", endpoint_url=ENDPOINT_URL, region_name=REGION_NAME)
dynamodb = boto3.resource(
    "dynamodb", endpoint_url=ENDPOINT_URL, region_name=REGION_NAME
)

# SSM parameter names (authoritative source: deploy.sh / CONTRACT.md §6).
SSM_CUSTOMERS_TABLE = "/dic2026/group36/dynamodb/customers_table"

_param_cache = {}


def get_parameter(name):
    """Resolve an SSM parameter, caching the value for warm invocations."""
    if name not in _param_cache:
        _param_cache[name] = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    return _param_cache[name]


def iter_stream_records(event):
    """Yield DynamoDB stream records from the trigger event.

    The event structure is:
    {
      "Records": [
        {
          "eventName": "INSERT" | "MODIFY" | "REMOVE",
          "dynamodb": {
            "Keys": {"reviewID": {"S": "review-000001"}},
            "NewImage": {...full new item...},
            "OldImage": {...full old item (on MODIFY)...}
          }
        },
        ...
      ]
    }
    """
    if isinstance(event, dict) and isinstance(event.get("Records"), list):
        return event["Records"]
    return []


def lambda_handler(event, context):
    """Process DynamoDB stream records and update customer ban status."""
    customers_table = dynamodb.Table(get_parameter(SSM_CUSTOMERS_TABLE))

    processed = []
    for record in iter_stream_records(event):
        # Skip DELETE events; we only care about new/updated reviews.
        event_name = record.get("eventName")
        if event_name not in ("INSERT", "MODIFY"):
            continue

        # Extract the new item from the stream.
        new_image = record.get("dynamodb", {}).get("NewImage", {})
        if not new_image:
            continue

        # DynamoDB stream format uses low-level JSON with type descriptors.
        # E.g., {"S": "review-000001"} for strings, {"BOOL": true} for booleans.
        review_id = new_image.get("reviewID", {}).get("S")
        reviewer_id = new_image.get("reviewerID", {}).get("S")
        profane = new_image.get("profane", {}).get("BOOL")

        # Only count this review if it is profane and we have both IDs.
        if not profane or not reviewer_id or not review_id:
            print(
                "ban: skipping review {} (profane={}, reviewer={})".format(
                    review_id, profane, reviewer_id
                )
            )
            continue

        print("ban: processing profane review {} for reviewer {}".format(
            review_id, reviewer_id
        ))

        # Get or initialize the customer record. Use get_item to check existence;
        # if not found, initialize impoliteCount to 0 and banned to false.
        try:
            existing = customers_table.get_item(Key={"reviewerID": reviewer_id})
            impolite_count = int(
                existing.get("Item", {}).get("impoliteCount", 0)
            )
        except (KeyError, ValueError, TypeError):
            impolite_count = 0

        # Increment the impolite count.
        impolite_count += 1

        # Determine ban status: banned only if impoliteCount > 3.
        banned = impolite_count > 3

        # Update the customer record in DynamoDB.
        customers_table.update_item(
            Key={"reviewerID": reviewer_id},
            UpdateExpression=(
                "SET impoliteCount = :ic, banned = :b, lastUpdatedReviewID = :rid"
            ),
            ExpressionAttributeValues={
                ":ic": impolite_count,
                ":b": banned,
                ":rid": review_id,
            },
        )
        print(
            "ban: updated reviewer {} impoliteCount={} banned={}".format(
                reviewer_id, impolite_count, banned
            )
        )
        processed.append({
            "reviewerID": reviewer_id,
            "impoliteCount": impolite_count,
            "banned": banned,
            "lastUpdatedReviewID": review_id,
        })

    return {"statusCode": 200, "processed": processed}
