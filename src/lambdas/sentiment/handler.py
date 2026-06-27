"""Sentiment Lambda (DIC2026 A3, Group 36) — TEMPORARY non-crashing skeleton.

P1's skeleton defined ``def handler(...)`` while deploy.sh registers
``handler.lambda_handler``, so every EventBridge invocation crashed on init.
This restores a valid ``lambda_handler`` that returns successfully.

NOTE: placeholder only. P3 owns the real sentiment logic — classify each review
as positive / neutral / negative with NLTK VADER over summary + reviewText,
factoring in ``overall`` (CONTRACT.md §10).
"""


def lambda_handler(event, context):
    print("sentiment skeleton invoked")
    return {"statusCode": 200}
