# ── TEST-ONLY: dev verification, NOT a submission deliverable — exclude from the ZIP. ──
"""Offline verification for the P2 Lambdas (preprocessing + profanity).

MiniStack is not reachable from this environment, so instead of a live deploy
this script exercises the pure handler logic with boto3/SSM mocked, using real
records from reviews_devset.json plus a hand-made impolite review, and asserts
the output matches CONTRACT.md §8/§9.

Run with the project venv (nltk==3.8.1 + profanityfilter installed):
    python src/lambdas/p2_offline_check.py

This is a throwaway P2 check — the graded integration tests live in
src/tests/test_integration.py (owned by P4).
"""

import importlib.util
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEVSET = os.path.join(HERE, "..", "data", "reviews_devset.json")


# --- minimal boto3 fakes ----------------------------------------------------
class FakeS3:
    def __init__(self):
        self.objects = {}
        self.puts = []

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.puts.append((Bucket, Key, Body))


class FakeSSM:
    def __init__(self, params):
        self.params = params

    def get_parameter(self, Name):
        return {"Parameter": {"Value": self.params[Name]}}


class FakeTable:
    def __init__(self):
        self.updates = []

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


class FakeDynamoResource:
    def __init__(self, table):
        self._table = table

    def Table(self, name):
        return self._table


class FakeEvents:
    def __init__(self):
        self.entries = []

    def put_events(self, Entries):
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0}


def install_fake_boto3(s3=None, ssm=None, dynamo=None, events=None):
    """Patch boto3.client/resource before a handler module is imported."""
    import boto3

    events = events or FakeEvents()

    def fake_client(service, **kwargs):
        return {"s3": s3, "ssm": ssm, "events": events}[service]

    def fake_resource(service, **kwargs):
        return {"dynamodb": dynamo}[service]

    boto3.client = fake_client
    boto3.resource = fake_resource
    return events


def load_handler(name, path):
    """Import a handler module fresh (so module-level clients pick up fakes)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def read_devset_records(n):
    records = []
    with open(DEVSET, "r") as fh:
        for _ in range(n):
            records.append(json.loads(fh.readline()))
    return records


# --- tests ------------------------------------------------------------------
def test_preprocessing():
    os.environ.pop("STAGE", None)
    record = read_devset_records(1)[0]

    s3 = FakeS3()
    ssm = FakeSSM({"/dic2026/group36/s3/preprocessed_bucket": "preprocessed-bucket"})
    s3.objects[("input-bucket", "review-000001.json")] = json.dumps(record).encode()
    events = install_fake_boto3(s3=s3, ssm=ssm)

    pre = load_handler("pre_handler", os.path.join(HERE, "preprocessing", "handler.py"))

    event = {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": "input-bucket"},
                    "object": {"key": "review-000001.json"},
                }
            }
        ]
    }
    resp = pre.lambda_handler(event, None)
    assert resp["statusCode"] == 200, resp

    assert len(s3.puts) == 1, "expected exactly one preprocessed object"
    bucket, key, body = s3.puts[0]
    assert bucket == "preprocessed-bucket"
    assert key == "preprocessed/review-000001.json", key  # CONTRACT.md §8 key

    out = json.loads(body)
    for field in (
        "reviewID",
        "reviewerID",
        "asin",
        "summary",
        "reviewText",
        "overall",
        "processedSummaryTokens",
        "processedReviewTokens",
        "combinedProcessedText",
        "category",
    ):
        assert field in out, "missing §8 field: " + field
    assert out["reviewID"] == "review-000001"
    assert out["reviewerID"] == record["reviewerID"]  # metadata preserved
    assert out["overall"] == record["overall"]
    assert isinstance(out["processedSummaryTokens"], list)
    assert (
        out["combinedProcessedText"]
        == out["processedSummaryTokens"] + out["processedReviewTokens"]
    )

    # Pipeline properties: lowercased, no stop words, lemmatized.
    toks = out["combinedProcessedText"]
    assert all(t == t.lower() for t in toks), "tokens must be lowercased"
    assert "the" not in toks and "and" not in toks, "stop words not removed"

    # Targeted lemmatization check on controlled input.
    controlled = pre.preprocess_text("The dogs were running through bushes")
    assert "dog" in controlled, controlled  # plural -> singular (noun lemma)
    assert "the" not in controlled and "were" not in controlled, controlled

    # Preprocessing must publish the EventBridge "Object Created" event itself
    # (MiniStack doesn't auto-emit it for Lambda-written objects).
    assert len(events.entries) == 1, events.entries
    entry = events.entries[0]
    assert entry["Source"] == "aws.s3"
    assert entry["DetailType"] == "Object Created"
    detail = json.loads(entry["Detail"])
    assert detail["bucket"]["name"] == "preprocessed-bucket"
    assert detail["object"]["key"] == "preprocessed/review-000001.json"

    print("  preprocessing OK — key={} tokens(sample)={}".format(key, toks[:8]))
    print("  eventbridge emit OK — {}".format(detail))
    return out


def test_profanity():
    os.environ.pop("STAGE", None)

    clean = {
        "reviewID": "review-000010",
        "reviewerID": "U-clean",
        "summary": "Delish",
        "reviewText": "Have a wonderful nice day, great product.",
    }
    dirty = {
        "reviewID": "review-000011",
        "reviewerID": "U-dirty",
        "summary": "good",
        "reviewText": "you are an asshole and this is bullshit",
    }

    s3 = FakeS3()
    ssm = FakeSSM({"/dic2026/group36/dynamodb/reviews_table": "reviews-table"})
    table = FakeTable()
    s3.objects[("pre", "preprocessed/review-000010.json")] = json.dumps(clean).encode()
    s3.objects[("pre", "preprocessed/review-000011.json")] = json.dumps(dirty).encode()
    install_fake_boto3(s3=s3, ssm=ssm, dynamo=FakeDynamoResource(table))

    prof = load_handler("prof_handler", os.path.join(HERE, "profanity", "handler.py"))

    def event_for(key):
        return {"detail": {"bucket": {"name": "pre"}, "object": {"key": key}}}

    prof.lambda_handler(event_for("preprocessed/review-000010.json"), None)
    prof.lambda_handler(event_for("preprocessed/review-000011.json"), None)

    assert len(table.updates) == 2
    clean_u, dirty_u = table.updates

    assert clean_u["Key"] == {"reviewID": "review-000010"}
    assert clean_u["ExpressionAttributeValues"][":p"] is False
    assert clean_u["ExpressionAttributeValues"][":pf"] == []
    assert clean_u["ExpressionAttributeValues"][":r"] == "U-clean"

    assert dirty_u["Key"] == {"reviewID": "review-000011"}
    assert dirty_u["ExpressionAttributeValues"][":p"] is True
    assert "reviewText" in dirty_u["ExpressionAttributeValues"][":pf"]
    # uses update_item (SET only §9 attributes), never put_item
    assert dirty_u["UpdateExpression"].startswith("SET profane = :p")
    assert ":s" not in dirty_u["ExpressionAttributeValues"]  # no stray status

    print(
        "  profanity OK — clean profane=False, dirty profane=True fields={}".format(
            dirty_u["ExpressionAttributeValues"][":pf"]
        )
    )


def test_review_id_robustness():
    """reviewID resolution order: explicit field > object-key basename."""
    pre = sys.modules["pre_handler"]
    assert pre.derive_review_id({"reviewID": "rid-X"}, "whatever.json") == "rid-X"
    assert pre.derive_review_id({}, "path/to/review-000042.json") == "review-000042"
    assert pre.derive_review_id({}, "review-7") == "review-7"
    minted = pre.derive_review_id(
        {"reviewerID": "A", "asin": "B", "unixReviewTime": 1}, ""
    )
    assert minted.startswith("review-"), minted
    print("  reviewID robustness OK")


if __name__ == "__main__":
    print("P2 offline verification (boto3/SSM mocked)")
    test_preprocessing()
    test_profanity()
    test_review_id_robustness()
    print("ALL P2 OFFLINE CHECKS PASSED")
