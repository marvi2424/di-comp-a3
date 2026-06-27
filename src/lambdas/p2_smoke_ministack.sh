#!/usr/bin/env bash
# P2 smoke test against a RUNNING MiniStack.
#
# Uploads one clean review (from the devset) and one crafted impolite review,
# then verifies the two P2 stages end-to-end:
#   * preprocessing  -> object appears in the preprocessed bucket
#   * profanity      -> row appears in the reviews DynamoDB table with the
#                       expected `profane` flag
#
# Run AFTER `bash src/deploy.sh` with MiniStack up. Resource names are read from
# SSM (never hardcoded). The only thing it writes is the two test objects in the
# input bucket; everything else is read-only.
#
#   bash src/lambdas/p2_smoke_ministack.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MINISTACK_ENDPOINT="${MINISTACK_ENDPOINT:-http://localhost:4566}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
AWS=(aws --endpoint-url="${MINISTACK_ENDPOINT}")

param() {
  "${AWS[@]}" ssm get-parameter --name "$1" --query 'Parameter.Value' --output text
}

INPUT_BUCKET="$(param /dic2026/group36/s3/input_bucket)"
PRE_BUCKET="$(param /dic2026/group36/s3/preprocessed_bucket)"
REVIEWS_TABLE="$(param /dic2026/group36/dynamodb/reviews_table)"
echo "input=${INPUT_BUCKET}  preprocessed=${PRE_BUCKET}  table=${REVIEWS_TABLE}"

# --- build two sample reviews (self-contained; no devset needed) -----------
CLEAN=/tmp/review-000001.json
DIRTY=/tmp/review-000002.json
cat >"${CLEAN}" <<'JSON'
{"reviewerID":"P2-SMOKE-CLEAN","asin":"X0000CLEAN","reviewerName":"smoke","helpful":[1,1],"reviewText":"This was a gift and we absolutely love it, works great and arrived on time.","overall":5.0,"summary":"Delightful and well made","unixReviewTime":0,"reviewTime":"01 1, 2020","category":"Smoke_Test"}
JSON
cat >"${DIRTY}" <<'JSON'
{"reviewerID":"P2-SMOKE-USER","asin":"X0000TEST","reviewerName":"smoke","helpful":[0,0],"reviewText":"you are an asshole and this is bullshit","overall":1.0,"summary":"good","unixReviewTime":0,"reviewTime":"01 1, 2020","category":"Smoke_Test"}
JSON

# --- upload (this kicks off the chain) ------------------------------------
"${AWS[@]}" s3 cp "${CLEAN}" "s3://${INPUT_BUCKET}/review-000001.json"
"${AWS[@]}" s3 cp "${DIRTY}" "s3://${INPUT_BUCKET}/review-000002.json"

# --- wait for the preprocessing output (poll up to ~30s) ------------------
wait_for_key() {
  local key="$1" i
  for i in $(seq 1 60); do
    if "${AWS[@]}" s3 ls "s3://${PRE_BUCKET}/${key}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

echo "waiting for preprocessing output..."
wait_for_key "preprocessed/review-000001.json" || {
  echo "FAIL: review-000001 was not preprocessed (check Lambda logs)"; exit 1; }
wait_for_key "preprocessed/review-000002.json" || {
  echo "FAIL: review-000002 was not preprocessed (check Lambda logs)"; exit 1; }
echo "preprocessing OK — objects present under ${PRE_BUCKET}/preprocessed/"
echo "--- preprocessed/review-000001.json ---"
"${AWS[@]}" s3 cp "s3://${PRE_BUCKET}/preprocessed/review-000001.json" -

# --- wait for the profanity output in DynamoDB ----------------------------
get_profane() {
  "${AWS[@]}" dynamodb get-item \
    --table-name "${REVIEWS_TABLE}" \
    --key "{\"reviewID\":{\"S\":\"$1\"}}" \
    --query 'Item.profane.BOOL' --output text 2>/dev/null
}

wait_for_profane() {
  local rid="$1" i v
  for i in $(seq 1 60); do
    v="$(get_profane "$rid")"
    if [[ "$v" == "true" || "$v" == "false" ]]; then
      echo "$v"; return 0
    fi
    sleep 1
  done
  echo "MISSING"; return 1
}

echo "waiting for profanity output in DynamoDB..."
CLEAN_P="$(wait_for_profane review-000001)" || true
DIRTY_P="$(wait_for_profane review-000002)" || true
echo "review-000001 profane=${CLEAN_P}  (expect false)"
echo "review-000002 profane=${DIRTY_P}  (expect true)"

# --- verdict --------------------------------------------------------------
if [[ "${CLEAN_P}" == "false" && "${DIRTY_P}" == "true" ]]; then
  echo "P2 SMOKE TEST PASSED"
else
  echo "P2 SMOKE TEST FAILED (inspect the Lambda logs in the MiniStack console)"
  exit 1
fi
