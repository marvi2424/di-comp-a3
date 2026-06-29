"""End-to-end integration tests (DIC2026 A3, Group 36 — P4).

Each test drives the *deployed* serverless chain on MiniStack exactly the way a
real review would: it uploads a raw review JSON into the input S3 bucket and
then waits for the asynchronous chain (preprocessing -> EventBridge fan-out ->
profanity + sentiment -> reviews table -> stream -> ban -> customers table) to
produce its effect, asserting on the stored state.

Covers the five required functionalities (CONTRACT.md §12):

  1. preprocessing (tokenize, stop-word removal, lemmatize),
  2. profanity check flags impolite reviews (and clears clean ones),
  3. sentiment classification (positive / neutral / negative),
  4. impolite reviews counted per customer in DynamoDB,
  5. customer banned only AFTER more than 3 impolite reviews (3 -> not banned,
     4 -> banned).

Prerequisites (see instructions / work plan P4):
  * MiniStack running on the endpoint below,
  * ``bash src/deploy.sh`` already run (all four Lambdas + resources exist).

Run from the repo root:
    pytest src/tests/test_integration.py -v

Fixtures are tiny and synthetic — the full ``reviews_devset.json`` numbers are
produced separately by ``src/tools/load_devset.py``, never here. Every test
mints unique reviewIDs / reviewerIDs (uuid) so reruns and parallel writes never
collide with earlier state.
"""

import json
import os
import time
import typing
import uuid

import boto3
import pytest

if typing.TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_ssm import SSMClient
    from mypy_boto3_lambda import LambdaClient
    from mypy_boto3_dynamodb import DynamoDBServiceResource

ENDPOINT_URL = os.getenv("MINISTACK_ENDPOINT", "http://localhost:4566")

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

s3: "S3Client" = boto3.client("s3", endpoint_url=ENDPOINT_URL)
ssm: "SSMClient" = boto3.client("ssm", endpoint_url=ENDPOINT_URL)
awslambda: "LambdaClient" = boto3.client("lambda", endpoint_url=ENDPOINT_URL)
dynamodb: "DynamoDBServiceResource" = boto3.resource(
    "dynamodb", endpoint_url=ENDPOINT_URL
)

# SSM parameter names — single source of truth is deploy.sh / CONTRACT.md §6.
SSM_INPUT_BUCKET = "/dic2026/group36/s3/input_bucket"
SSM_PREPROCESSED_BUCKET = "/dic2026/group36/s3/preprocessed_bucket"
SSM_REVIEWS_TABLE = "/dic2026/group36/dynamodb/reviews_table"
SSM_CUSTOMERS_TABLE = "/dic2026/group36/dynamodb/customers_table"

LAMBDA_NAMES = (
    "group36-preprocessing-lambda",
    "group36-profanity-lambda",
    "group36-sentiment-lambda",
    "group36-ban-lambda",
)

# How long to wait for the async chain before giving up on a single assertion.
CHAIN_TIMEOUT = float(os.getenv("CHAIN_TIMEOUT", "90"))
POLL_INTERVAL = 2.0


# --- Fixtures --------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _wait_for_lambdas():
    """Ensure all four Lambdas are deployed and active before any test runs."""
    waiter = awslambda.get_waiter("function_active")
    for name in LAMBDA_NAMES:
        waiter.wait(FunctionName=name)


@pytest.fixture(scope="session")
def names():
    """Resolve all bucket/table names from SSM (no hardcoding — CONTRACT.md §14)."""

    def value(param):
        return ssm.get_parameter(Name=param)["Parameter"]["Value"]

    return {
        "input_bucket": value(SSM_INPUT_BUCKET),
        "preprocessed_bucket": value(SSM_PREPROCESSED_BUCKET),
        "reviews_table": value(SSM_REVIEWS_TABLE),
        "customers_table": value(SSM_CUSTOMERS_TABLE),
    }


# --- Helpers ---------------------------------------------------------------
def _make_review(review_id, reviewer_id, summary, review_text, overall):
    """Build a raw review object in the devset shape (CONTRACT.md §3)."""
    return {
        "reviewID": review_id,
        "reviewerID": reviewer_id,
        "asin": "TEST000000",
        "reviewerName": "Integration Test",
        "helpful": [0, 0],
        "summary": summary,
        "reviewText": review_text,
        "overall": overall,
        "unixReviewTime": 1259798400,
        "reviewTime": "01 1, 2020",
        "category": "Test_Category",
    }


def _upload(bucket, review):
    """Upload one raw review to the input bucket; reviewID becomes the object key."""
    review_id = review["reviewID"]
    s3.put_object(
        Bucket=bucket,
        Key="{}.json".format(review_id),
        Body=json.dumps(review).encode("utf-8"),
        ContentType="application/json",
    )
    return review_id


def _wait_for_review_item(table, review_id, require=("profane", "sentiment")):
    """Poll the reviews table until the item exists with all required attributes.

    Returns the item. Raises ``AssertionError`` on timeout so the test fails with
    a clear message instead of hanging.
    """
    deadline = time.time() + CHAIN_TIMEOUT
    last = None
    while time.time() < deadline:
        last = table.get_item(Key={"reviewID": review_id}).get("Item")
        if last and all(attr in last for attr in require):
            return last
        time.sleep(POLL_INTERVAL)
    raise AssertionError(
        "review {} not fully processed within {}s (got: {})".format(
            review_id, CHAIN_TIMEOUT, last
        )
    )


def _wait_for_preprocessed(bucket, review_id):
    """Poll the preprocessed bucket for ``preprocessed/{reviewID}.json``."""
    key = "preprocessed/{}.json".format(review_id)
    deadline = time.time() + CHAIN_TIMEOUT
    while time.time() < deadline:
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            return json.loads(obj["Body"].read())
        except s3.exceptions.NoSuchKey:
            time.sleep(POLL_INTERVAL)
    raise AssertionError(
        "preprocessed object {} not produced within {}s".format(key, CHAIN_TIMEOUT)
    )


def _wait_for_customer(table, reviewer_id, expected_count):
    """Poll the customers table until ``impoliteCount`` reaches ``expected_count``."""
    deadline = time.time() + CHAIN_TIMEOUT
    last = None
    while time.time() < deadline:
        last = table.get_item(Key={"reviewerID": reviewer_id}).get("Item")
        if last and int(last.get("impoliteCount", 0)) >= expected_count:
            return last
        time.sleep(POLL_INTERVAL)
    raise AssertionError(
        "customer {} did not reach impoliteCount={} within {}s (got: {})".format(
            reviewer_id, expected_count, CHAIN_TIMEOUT, last
        )
    )


def _unique(prefix):
    return "{}-{}".format(prefix, uuid.uuid4().hex[:10])


# Mild profanity used purely to exercise the profanityfilter word list. Kept in
# one place so the intent is obvious and the rest of the file stays readable.
PROFANE_TEXT = "This product is shit and the seller is an asshole."
CLEAN_TEXT = "This product is wonderful and the seller was very helpful."


# --- 1. Preprocessing ------------------------------------------------------
def test_preprocessing_tokenizes_removes_stopwords_and_lemmatizes(names):
    """Functionality 1: tokenize + stop-word removal + lemmatization."""
    review_id = _unique("review-prep")
    review = _make_review(
        review_id,
        _unique("cust-prep"),
        summary="The cats are running",
        review_text="They were playing with the boxes over there",
        overall=5.0,
    )
    _upload(names["input_bucket"], review)

    out = _wait_for_preprocessed(names["preprocessed_bucket"], review_id)

    combined = out["combinedProcessedText"]
    # Lowercased tokens only.
    assert all(tok == tok.lower() for tok in combined)
    # Stop words ("the", "are", "they", "were", "with", "over", "there") removed.
    for stop in ("the", "are", "they", "were", "with", "over", "there"):
        assert stop not in combined
    # Lemmatization: plural nouns collapse to singular ("cats" -> "cat",
    # "boxes" -> "box"); the originals must be gone.
    assert "cat" in combined and "cats" not in combined
    assert "box" in combined and "boxes" not in combined
    # Content words survive.
    assert "running" in combined or "run" in combined
    assert "playing" in combined or "play" in combined


# --- 2. Profanity ----------------------------------------------------------
def test_profanity_flags_impolite_and_clears_clean(names):
    """Functionality 2: profanity check flags impolite, not clean, reviews."""
    table = dynamodb.Table(names["reviews_table"])

    impolite_id = _upload(
        names["input_bucket"],
        _make_review(
            _unique("review-prof-bad"),
            _unique("cust-prof-bad"),
            summary="Awful",
            review_text=PROFANE_TEXT,
            overall=1.0,
        ),
    )
    clean_id = _upload(
        names["input_bucket"],
        _make_review(
            _unique("review-prof-ok"),
            _unique("cust-prof-ok"),
            summary="Lovely",
            review_text=CLEAN_TEXT,
            overall=5.0,
        ),
    )

    impolite_item = _wait_for_review_item(table, impolite_id)
    clean_item = _wait_for_review_item(table, clean_id)

    assert impolite_item["profane"] is True
    assert "reviewText" in impolite_item.get("profaneFields", [])
    assert clean_item["profane"] is False
    assert clean_item.get("profaneFields", []) == []


# --- 3. Sentiment ----------------------------------------------------------
@pytest.mark.parametrize(
    "summary,review_text,overall,expected",
    [
        ("Great!", "Excellent product, I love it", 5.0, "positive"),
        ("OK", "Average quality, nothing special", 3.0, "neutral"),
        ("Terrible", "Complete waste of money, broke instantly", 1.0, "negative"),
    ],
)
def test_sentiment_classification(names, summary, review_text, overall, expected):
    """Functionality 3: sentiment classified as positive / neutral / negative."""
    table = dynamodb.Table(names["reviews_table"])
    review_id = _upload(
        names["input_bucket"],
        _make_review(
            _unique("review-sent"),
            _unique("cust-sent"),
            summary=summary,
            review_text=review_text,
            overall=overall,
        ),
    )

    item = _wait_for_review_item(table, review_id)
    assert item["sentiment"] == expected


# --- 4. Per-customer impolite counting -------------------------------------
def test_impolite_reviews_counted_per_customer(names):
    """Functionality 4: each impolite review increments the customer's count."""
    reviews_table = dynamodb.Table(names["reviews_table"])
    customers_table = dynamodb.Table(names["customers_table"])
    reviewer_id = _unique("cust-count")

    for n in range(2):
        rid = _upload(
            names["input_bucket"],
            _make_review(
                _unique("review-count"),
                reviewer_id,
                summary="Bad",
                review_text=PROFANE_TEXT,
                overall=1.0,
            ),
        )
        # Make sure the review is stored before uploading the next one, so the
        # ban Lambda processes the stream events one at a time.
        _wait_for_review_item(reviews_table, rid)

    customer = _wait_for_customer(customers_table, reviewer_id, expected_count=2)
    assert int(customer["impoliteCount"]) == 2
    assert customer["banned"] is False


# --- 5. Ban threshold (the critical edge case) -----------------------------
def test_customer_banned_only_after_more_than_three_impolite(names):
    """Functionality 5: 3 impolite reviews -> NOT banned, 4th -> banned."""
    reviews_table = dynamodb.Table(names["reviews_table"])
    customers_table = dynamodb.Table(names["customers_table"])
    reviewer_id = _unique("cust-ban")

    def add_impolite():
        rid = _upload(
            names["input_bucket"],
            _make_review(
                _unique("review-ban"),
                reviewer_id,
                summary="Bad",
                review_text=PROFANE_TEXT,
                overall=1.0,
            ),
        )
        _wait_for_review_item(reviews_table, rid)

    # Three impolite reviews: must NOT be banned (impoliteCount == 3).
    for _ in range(3):
        add_impolite()
    customer = _wait_for_customer(customers_table, reviewer_id, expected_count=3)
    assert int(customer["impoliteCount"]) == 3
    assert customer["banned"] is False, "3 impolite reviews must NOT ban"

    # Fourth impolite review: now banned (impoliteCount == 4).
    add_impolite()
    customer = _wait_for_customer(customers_table, reviewer_id, expected_count=4)
    assert int(customer["impoliteCount"]) == 4
    assert customer["banned"] is True, "4th impolite review MUST ban"


# --- 6. Shipped corner cases, checked automatically ------------------------
def _load_corner_cases():
    """Load the shipped corner-case reviews (src/test_reviews/corner_cases.json)."""
    path = os.path.join(
        os.path.dirname(__file__), os.pardir, "test_reviews", "corner_cases.json"
    )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_corner_cases_behave_as_documented(names):
    """Drive every review in corner_cases.json through the chain and assert the
    documented outcomes — so the shipped corner cases are verified automatically,
    not merely included in the archive.

    The file uses fixed reviewerIDs (customerBANNED, customerEDGE, ...). We prefix
    them with a per-run id so reruns stay isolated (otherwise the ban counts would
    accumulate across runs); the prefix is identical for every review, so the
    per-customer grouping the cases rely on is preserved. Reviews are uploaded
    sequentially, waiting for each, so the ban stream is processed in order.
    """
    reviews_table = dynamodb.Table(names["reviews_table"])
    customers_table = dynamodb.Table(names["customers_table"])
    run = _unique("cc")

    uploaded = []  # (original reviewerID, minted reviewID)
    for i, case in enumerate(_load_corner_cases()):
        review = {k: v for k, v in case.items() if not k.startswith("_")}
        original_reviewer = review["reviewerID"]
        review_id = "{}-r{:03d}".format(run, i)
        review["reviewID"] = review_id
        review["reviewerID"] = "{}-{}".format(run, original_reviewer)
        _upload(names["input_bucket"], review)
        _wait_for_review_item(reviews_table, review_id)
        uploaded.append((original_reviewer, review_id))

    def review_id_of(original_reviewer):
        return next(rid for orig, rid in uploaded if orig == original_reviewer)

    def review_item(review_id):
        return reviews_table.get_item(Key={"reviewID": review_id}).get("Item")

    def customer_key(original_reviewer):
        return "{}-{}".format(run, original_reviewer)

    # Ban threshold, both sides: 4 impolite -> banned, exactly 3 -> NOT banned.
    banned = _wait_for_customer(customers_table, customer_key("customerBANNED"), 4)
    assert int(banned["impoliteCount"]) == 4
    assert banned["banned"] is True, "customerBANNED (4 impolite) must be banned"

    edge = _wait_for_customer(customers_table, customer_key("customerEDGE"), 3)
    assert int(edge["impoliteCount"]) == 3
    assert edge["banned"] is False, "customerEDGE (exactly 3) must NOT be banned"

    # Clean customer never gets an impolite count.
    clean = customers_table.get_item(
        Key={"reviewerID": customer_key("customerCLEAN")}
    ).get("Item")
    assert clean is None or int(clean.get("impoliteCount", 0)) == 0

    # Profanity that appears only in the summary field is still caught.
    summary_item = review_item(review_id_of("customerSUMMARY"))
    assert summary_item["profane"] is True
    assert "summary" in summary_item.get("profaneFields", [])

    # Sentiment overrides: a 3-star review and a 5-star sarcastic review -> neutral.
    assert review_item(review_id_of("customerNEUTRAL"))["sentiment"] == "neutral"
    assert review_item(review_id_of("customerSARCASM"))["sentiment"] == "neutral"
