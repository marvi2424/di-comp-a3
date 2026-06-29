"""Devset batch loader & result aggregator (DIC2026 A3, Group 36 — P4).

This is the "heavy compute" step. It pushes the official ``reviews_devset.json``
through the deployed serverless chain and produces the final reported numbers
(CONTRACT.md §13 / work plan P4):

  1. read every review from ``reviews_devset.json`` (JSON-Lines, one object per
     line; a plain JSON array is also tolerated),
  2. upload each review as its *own* object into the input S3 bucket — the
     preprocessing Lambda fires per object, so one upload == one chain run
     (assignment requirement: "chain starts when one review is added"),
  3. wait until every review has been processed by *both* the profanity and the
     sentiment Lambda (the reviews table item carries both attributes),
  4. aggregate over the devset only: #positive / #neutral / #negative,
     #reviews failing the profanity check, and the list of banned users,
  5. write the numbers to ``src/results/results.json``.

Resource names are never hardcoded — like the Lambdas, the loader resolves the
input bucket and both DynamoDB table names from SSM Parameter Store (CONTRACT.md
§6 / §14), so it keeps working if P1 ever renames a resource.

The reviewID convention matches the preprocessing Lambda's ``derive_review_id``:
the input object key's basename without ``.json`` becomes the reviewID. Uploading
``review-000001.json`` therefore yields reviewID ``review-000001``.

Typical usage (MiniStack up, ``bash src/deploy.sh`` already run):

    # full devset run -> writes src/results/results.json
    python3 src/tools/load_devset.py

    # quick smoke run on the first 200 reviews (does NOT overwrite results.json)
    python3 src/tools/load_devset.py --limit 200 --output /tmp/sample.json

    # only re-aggregate from a table that is already populated (no upload)
    python3 src/tools/load_devset.py --skip-upload
"""

import argparse
import concurrent.futures
import json
import os
import sys
import time
from decimal import Decimal

import boto3

# --- Defaults --------------------------------------------------------------
# localhost:4566 is the MiniStack endpoint used everywhere else in this repo
# (deploy.sh, the Lambdas, the tutorial). Overridable for the LBD proxy.
DEFAULT_ENDPOINT = os.getenv("MINISTACK_ENDPOINT", "http://localhost:4566")

# SSM parameter names — single source of truth is deploy.sh / CONTRACT.md §6.
SSM_INPUT_BUCKET = "/dic2026/group36/s3/input_bucket"
SSM_REVIEWS_TABLE = "/dic2026/group36/dynamodb/reviews_table"
SSM_CUSTOMERS_TABLE = "/dic2026/group36/dynamodb/customers_table"

# Repo paths (this file lives in src/tools/).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_THIS_DIR)
DEFAULT_DATA = os.path.join(_SRC_DIR, "data", "reviews_devset.json")
DEFAULT_OUTPUT = os.path.join(_SRC_DIR, "results", "results.json")

VALID_SENTIMENTS = ("positive", "neutral", "negative")


# --- Dataset reading -------------------------------------------------------
def read_reviews(path, limit=None):
    """Yield review dicts from ``path`` (JSON-Lines first, JSON-array fallback).

    The official devset ships as JSON-Lines (~78k objects, one per line). Some
    exports wrap the same records in a single JSON array, so we fall back to
    that if the first non-empty line is not parseable on its own.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            "dataset not found: {}\nPlace reviews_devset.json there "
            "(see src/README.md).".format(path)
        )

    reviews = []
    with open(path, "r", encoding="utf-8") as fh:
        first = fh.readline()
        fh.seek(0)
        # JSON-Lines means each line is its own JSON *object*. A single-line JSON
        # array would also parse here, so distinguish by type: a list on the
        # first line is the whole-array form, not JSON-Lines.
        try:
            is_json_lines = not isinstance(json.loads(first), list)
        except json.JSONDecodeError:
            is_json_lines = False

        if is_json_lines:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    reviews.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        "invalid JSON on line {} of {}: {}".format(line_no, path, exc)
                    )
                if limit is not None and len(reviews) >= limit:
                    break
        else:
            data = json.load(fh)
            if not isinstance(data, list):
                raise ValueError(
                    "{} is neither JSON-Lines nor a JSON array".format(path)
                )
            reviews = data if limit is None else data[:limit]

    return reviews


def review_id_for(index):
    """Stable, zero-padded reviewID for the Nth uploaded review (0-based)."""
    return "review-{:06d}".format(index + 1)


# --- Upload ----------------------------------------------------------------
def upload_reviews(s3, bucket, reviews, workers=16, progress_every=500):
    """Upload each review as its own object; return the list of reviewIDs.

    Uploads run on a small thread pool because the chain itself is async — the
    bottleneck is the round-trip per ``put_object``, not CPU. The object key is
    ``{reviewID}.json`` so the preprocessing Lambda derives the same reviewID
    we track here.
    """
    review_ids = [review_id_for(i) for i in range(len(reviews))]

    def _put(item):
        review_id, review = item
        s3.put_object(
            Bucket=bucket,
            Key="{}.json".format(review_id),
            Body=json.dumps(review).encode("utf-8"),
            ContentType="application/json",
        )
        return review_id

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(_put, zip(review_ids, reviews)):
            done += 1
            if done % progress_every == 0:
                print("  uploaded {}/{}".format(done, len(reviews)))
    print("  uploaded {}/{} (done)".format(done, len(reviews)))
    return review_ids


def upload_in_batches(
    s3,
    bucket,
    reviews,
    dynamodb_client,
    table_name,
    batch_size,
    workers,
    poll_interval,
    batch_timeout,
):
    """Upload reviews in throttled batches with backpressure.

    MiniStack spawns a thread per async S3->Lambda event delivery. Firing all
    ~78k uploads at once exhausts the (shared cluster) host's threads and memory
    ("RuntimeError: can't start new thread" / "MemoryError"). So we upload a
    small batch, wait until the chain has fully processed everything sent so far
    (both the profanity and sentiment writers present), and only then send the
    next batch. That keeps the in-flight work bounded to roughly one batch.

    Returns the number of reviews uploaded.
    """
    review_ids = [review_id_for(i) for i in range(len(reviews))]
    total = len(reviews)
    uploaded = 0

    def _put(item):
        review_id, review = item
        s3.put_object(
            Bucket=bucket,
            Key="{}.json".format(review_id),
            Body=json.dumps(review).encode("utf-8"),
            ContentType="application/json",
        )

    for start in range(0, total, batch_size):
        ids = review_ids[start : start + batch_size]
        chunk = reviews[start : start + batch_size]

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_put, zip(ids, chunk)))
        uploaded += len(ids)

        # Backpressure: do not send the next batch until the chain has caught up
        # with everything uploaded so far.
        deadline = time.time() + batch_timeout
        processed = count_fully_processed(dynamodb_client, table_name)
        while processed < uploaded and time.time() < deadline:
            time.sleep(poll_interval)
            processed = count_fully_processed(dynamodb_client, table_name)

        print(
            "  uploaded {}/{}, processed {}".format(uploaded, total, processed),
            flush=True,
        )
        if processed < uploaded:
            print(
                "  WARNING: batch did not fully drain within {}s "
                "(processed {} of {} uploaded) — continuing".format(
                    batch_timeout, processed, uploaded
                ),
                flush=True,
            )

    return uploaded


# --- Waiting for the async chain -------------------------------------------
def count_fully_processed(dynamodb_client, table_name):
    """Count reviews-table items that carry BOTH ``profane`` and ``sentiment``.

    An item appears after the first of the two parallel writers (profanity /
    sentiment) runs, so we cannot just count items — we require both attributes
    to be present, which means the review has cleared the whole fan-out.
    """
    total = 0
    kwargs = {
        "TableName": table_name,
        "Select": "COUNT",
        "FilterExpression": "attribute_exists(profane) AND attribute_exists(sentiment)",
    }
    while True:
        resp = dynamodb_client.scan(**kwargs)
        total += resp["Count"]
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return total


def wait_for_processing(dynamodb_client, table_name, expected, timeout, interval):
    """Poll until ``expected`` reviews are fully processed (or timeout).

    Returns the final processed count. A timeout is not fatal — the caller still
    aggregates whatever made it through, but warns that the numbers are partial.
    """
    deadline = time.time() + timeout
    processed = 0
    while time.time() < deadline:
        processed = count_fully_processed(dynamodb_client, table_name)
        print(
            "  processed {}/{} reviews...".format(processed, expected),
            flush=True,
        )
        if processed >= expected:
            return processed
        time.sleep(interval)
    return processed


def wait_for_customers_to_settle(dynamodb_client, table_name, interval, max_wait):
    """Wait for the customers table (fed by the async DynamoDB stream) to settle.

    The ban Lambda runs off the reviews-table stream *after* the reviews land,
    so its writes trail the processing count. We consider the table settled once
    the item count stops changing across two consecutive polls.
    """
    deadline = time.time() + max_wait
    previous = -1
    while time.time() < deadline:
        count = _scan_count(dynamodb_client, table_name)
        if count == previous:
            return count
        previous = count
        time.sleep(interval)
    return previous


def _scan_count(dynamodb_client, table_name):
    """Total item count of a table (paginated COUNT scan)."""
    total = 0
    kwargs = {"TableName": table_name, "Select": "COUNT"}
    while True:
        resp = dynamodb_client.scan(**kwargs)
        total += resp["Count"]
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return total


# --- Aggregation -----------------------------------------------------------
# Ban rule (CONTRACT.md §11): a reviewer is banned with MORE THAN this many
# impolite reviews (so 3 -> not banned, 4 -> banned).
BAN_THRESHOLD = 3


def aggregate_reviews(reviews_table):
    """Tally sentiment, profanity, and per-reviewer impolite counts in one scan.

    Everything is derived from the **reviews table** — the complete, authoritative
    record of every processed review (the loader waits until all reviews carry
    both writers' attributes before aggregating).

    The banned set is computed here from the per-reviewer profane counts rather
    than read from the stream-fed customers table on purpose: when tens of
    thousands of reviews are pushed through at once, MiniStack drops some
    DynamoDB-stream events, so the ban Lambda undercounts. Deriving the result
    from the full reviews table is robust to that load artifact and reproducible.
    The ban Lambda stays the live, event-driven mechanism (verified by the
    integration tests); this is the reporting ground truth.
    """
    counts = {
        "positive_reviews": 0,
        "neutral_reviews": 0,
        "negative_reviews": 0,
        "failed_profanity_reviews": 0,
    }
    impolite_per_reviewer = {}
    scanned = 0
    for item in _scan_items(reviews_table):
        scanned += 1
        sentiment = item.get("sentiment")
        if sentiment in VALID_SENTIMENTS:
            counts["{}_reviews".format(sentiment)] += 1
        if _as_bool(item.get("profane")):
            counts["failed_profanity_reviews"] += 1
            reviewer = item.get("reviewerID")
            if reviewer:
                impolite_per_reviewer[reviewer] = (
                    impolite_per_reviewer.get(reviewer, 0) + 1
                )
    counts["_reviews_scanned"] = scanned
    counts["_impolite_per_reviewer"] = impolite_per_reviewer
    return counts


def banned_users_from_counts(impolite_per_reviewer, threshold=BAN_THRESHOLD):
    """Reviewers with strictly more than ``threshold`` impolite reviews."""
    return sorted(
        reviewer
        for reviewer, count in impolite_per_reviewer.items()
        if count > threshold
    )


def _scan_items(table):
    """Yield every item of a boto3 Table resource, following pagination."""
    kwargs = {}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            yield item
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last


def _as_bool(value):
    """Coerce a DynamoDB-stored truthy value to a Python bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return value != 0
    if isinstance(value, str):
        return value.lower() == "true"
    return False


# --- Orchestration ---------------------------------------------------------
def build_results(reviews_table, customers_table=None):
    """Assemble the results.json payload (CONTRACT.md §13 shape).

    ``customers_table`` is accepted for backward compatibility but no longer used
    for the reported numbers — the banned set is derived from the reviews table
    (see ``aggregate_reviews``).
    """
    review_counts = aggregate_reviews(reviews_table)
    banned_users = banned_users_from_counts(review_counts["_impolite_per_reviewer"])
    return {
        "positive_reviews": review_counts["positive_reviews"],
        "neutral_reviews": review_counts["neutral_reviews"],
        "negative_reviews": review_counts["negative_reviews"],
        "failed_profanity_reviews": review_counts["failed_profanity_reviews"],
        "banned_users": banned_users,
        "total_reviews_scanned": review_counts["_reviews_scanned"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data", default=DEFAULT_DATA, help="path to reviews_devset.json"
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT, help="results.json output path"
    )
    parser.add_argument(
        "--endpoint", default=DEFAULT_ENDPOINT, help="MiniStack endpoint URL"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="process only the first N reviews"
    )
    parser.add_argument(
        "--workers", type=int, default=4, help="parallel S3 upload workers"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="reviews uploaded per batch before waiting for the chain to drain "
        "them (keeps MiniStack's in-flight thread count bounded)",
    )
    parser.add_argument(
        "--batch-timeout",
        type=float,
        default=600.0,
        help="max seconds to wait for one batch to drain before moving on",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=3.0,
        help="seconds between progress polls",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3600.0,
        help="max seconds for the final wait after all batches are uploaded",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="do not upload; only aggregate an already-populated table",
    )
    args = parser.parse_args(argv)

    region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

    s3 = boto3.client("s3", endpoint_url=args.endpoint, region_name=region)
    ssm = boto3.client("ssm", endpoint_url=args.endpoint, region_name=region)
    ddb_client = boto3.client(
        "dynamodb", endpoint_url=args.endpoint, region_name=region
    )
    ddb = boto3.resource("dynamodb", endpoint_url=args.endpoint, region_name=region)

    def ssm_value(name):
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]

    input_bucket = ssm_value(SSM_INPUT_BUCKET)
    reviews_table_name = ssm_value(SSM_REVIEWS_TABLE)
    customers_table_name = ssm_value(SSM_CUSTOMERS_TABLE)
    reviews_table = ddb.Table(reviews_table_name)
    customers_table = ddb.Table(customers_table_name)

    print("Endpoint:        {}".format(args.endpoint))
    print("Input bucket:    {}".format(input_bucket))
    print("Reviews table:   {}".format(reviews_table_name))
    print("Customers table: {}".format(customers_table_name))

    if not args.skip_upload:
        print("\nReading dataset: {}".format(args.data), flush=True)
        reviews = read_reviews(args.data, limit=args.limit)
        print("Loaded {} reviews.".format(len(reviews)), flush=True)
        if not reviews:
            print("No reviews to process — aborting.", file=sys.stderr)
            return 1

        print(
            "\nUploading to s3://{} in batches of {} (backpressure on) ...".format(
                input_bucket, args.batch_size
            ),
            flush=True,
        )
        upload_in_batches(
            s3,
            input_bucket,
            reviews,
            ddb_client,
            reviews_table_name,
            batch_size=args.batch_size,
            workers=args.workers,
            poll_interval=args.poll_interval,
            batch_timeout=args.batch_timeout,
        )

        print("\nFinal wait for the chain to finish all reviews...", flush=True)
        processed = wait_for_processing(
            ddb_client,
            reviews_table_name,
            expected=len(reviews),
            timeout=args.timeout,
            interval=args.poll_interval,
        )
        if processed < len(reviews):
            print(
                "WARNING: only {}/{} reviews finished before timeout — "
                "results are PARTIAL.".format(processed, len(reviews)),
                file=sys.stderr,
            )

        print("\nWaiting for the ban Lambda (DynamoDB stream) to settle...")
        wait_for_customers_to_settle(
            ddb_client,
            customers_table_name,
            interval=args.poll_interval,
            max_wait=args.timeout,
        )

    print("\nAggregating results...")
    results = build_results(reviews_table, customers_table)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
        fh.write("\n")

    print("\n=== RESULTS (devset only) ===")
    print(json.dumps(results, indent=2))
    print("\nWritten to {}".format(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
