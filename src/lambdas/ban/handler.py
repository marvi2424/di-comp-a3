"""Ban Lambda (DIC2026 A3, Group 36) — TEMPORARY non-crashing skeleton.

P1's skeleton defined ``def handler(...)`` while deploy.sh registers
``handler.lambda_handler``, so the function failed to initialize and the
DynamoDB stream retried the same batch forever, flooding MiniStack. This
restores a valid ``lambda_handler`` that simply acknowledges the stream batch
so the event source mapping stops retrying.

NOTE: placeholder only. P3 owns the real ban logic — count impolite reviews per
reviewer and set ``banned = true`` when the count exceeds 3 (CONTRACT.md §11).
"""


def lambda_handler(event, context):
    records = event.get("Records", []) if isinstance(event, dict) else []
    print("ban skeleton acknowledged {} stream record(s)".format(len(records)))
    return {"statusCode": 200}
