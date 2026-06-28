# ADDITIONAL NOTES. USE AT OWN RISK - DELETE ONCE P4 IS DONE

# P3 Handoff Document for P4 (Integration Tests & Results)

**Date:** 2026-06-28  
**From:** P3 (Sentiment & Ban implementation)  
**To:** P4 (Integration tests & dataset results)

---

## Summary

P3 has completed the sentiment analysis and customer banning Lambdas. Both functions are fully implemented, offline-tested, and ready for integration testing with P1 & P2.

---

## Sentiment Lambda Handoff

### Handler Location
- **File:** `src/lambdas/sentiment/handler.py`
- **Function:** `handler.lambda_handler(event, context)`
- **Dependencies:** `nltk==3.8.1` (VADER sentiment analyzer)

### Input Contract
Accepts EventBridge events with object location:
```json
{
  "detail": {
    "bucket": {"name": "group36-reviews-preprocessed"},
    "object": {"key": "preprocessed/review-000001.json"}
  }
}
```

### Output Contract
Writes to DynamoDB reviews table:
- **Table:** Retrieved from SSM `/dic2026/group36/dynamodb/reviews_table`
- **Key:** `reviewID` (same key as profanity Lambda writes to)
- **Attributes written:**
  - `sentiment`: One of `"positive"`, `"neutral"`, `"negative"`
  - Returns HTTP 200 on success

### Output Format Example
```json
{
  "statusCode": 200,
  "results": [
    {
      "reviewID": "review-000001",
      "sentiment": "positive"
    }
  ]
}
```

### Sentiment Classification Rules

| Condition | Result |
|-----------|--------|
| `overall >= 4` AND compound VADER score > −0.5 | `positive` |
| `overall >= 4` AND compound VADER score ≤ −0.5 | `neutral` (sarcasm override) |
| `overall == 3` | `neutral` (always) |
| `overall <= 2` AND compound VADER score < 0.5 | `negative` |
| `overall <= 2` AND compound VADER score >= 0.5 | `neutral` (rare positive override) |

### Testing Guidance for P4

**Test cases to verify:**

1. **5-star review with positive text** → expect `sentiment = "positive"`
   - Fixture: `overall=5, summary="Great!", reviewText="Excellent product"`

2. **5-star review with sarcasm** → expect `sentiment = "neutral"` (override)
   - Fixture: `overall=5, summary="Wonderful", reviewText="Yeah right, broken immediately"`

3. **3-star review** → expect `sentiment = "neutral"` (always)
   - Fixture: `overall=3, summary="OK", reviewText="Average quality"`

4. **1-star review with negative text** → expect `sentiment = "negative"`
   - Fixture: `overall=1, summary="Terrible", reviewText="Waste of money"`

5. **1-star review with enthusiastic text** (rare) → expect `sentiment = "neutral"` (override)
   - Fixture: `overall=1, summary="Bad", reviewText="Best worst purchase ever!"`

### Known Behavior

- Empty text fields → defaults to sentiment based on `overall` star rating
- VADER is English-only → ensure test reviews are in English
- Classification is deterministic → same review always produces same sentiment
- Uses `update_item` for concurrent safety → safe to run with profanity Lambda

---

## Ban Lambda Handoff

### Handler Location
- **File:** `src/lambdas/ban/handler.py`
- **Function:** `handler.lambda_handler(event, context)`
- **Dependencies:** boto3 (provided by runtime)

### Input Contract
Accepts DynamoDB stream events from the reviews table:
```json
{
  "Records": [
    {
      "eventName": "INSERT" or "MODIFY",
      "dynamodb": {
        "NewImage": {
          "reviewID": {"S": "review-000001"},
          "reviewerID": {"S": "A2VNYWOPJ13AFP"},
          "profane": {"BOOL": true}
        }
      }
    }
  ]
}
```

### Output Contract
Updates DynamoDB customers table:
- **Table:** Retrieved from SSM `/dic2026/group36/dynamodb/customers_table`
- **Key:** `reviewerID`
- **Attributes written:**
  - `impoliteCount`: Integer (incremented for each profane review)
  - `banned`: Boolean (`true` if `impoliteCount > 3`, else `false`)
  - `lastUpdatedReviewID`: String (audit trail)
  - Returns HTTP 200 on success

### Output Format Example
```json
{
  "statusCode": 200,
  "processed": [
    {
      "reviewerID": "A2VNYWOPJ13AFP",
      "impoliteCount": 4,
      "banned": true,
      "lastUpdatedReviewID": "review-000004"
    }
  ]
}
```

### Ban Threshold Logic

**Rule:** `banned = (impoliteCount > 3)`

This means:
- Impolite reviews 1–3: `banned = false` (customer NOT banned)
- Impolite review 4+: `banned = true` (customer IS banned)

**Critical edge case to test:**
- A customer with exactly **3 impolite reviews must NOT be banned**
- A customer with **4 impolite reviews MUST be banned**

### Testing Guidance for P4

**Test cases to verify:**

1. **First impolite review** → `impoliteCount=1, banned=false`
   - Fixture: One profane review from customerA

2. **After 3 impolite reviews** → `impoliteCount=3, banned=false` (EDGE CASE!)
   - Fixture: Three profane reviews from customerB
   - **Critical:** Verify `banned != true` after 3rd

3. **After 4 impolite reviews** → `impoliteCount=4, banned=true` (BANNING POINT!)
   - Fixture: Four profane reviews from customerC
   - **Critical:** Verify `banned == true` on 4th

4. **Multiple customers** → Each tracked independently
   - Fixture: customerD with 2 impolite, customerE with 5 impolite
   - Verify: customerD banned=false, customerE banned=true

5. **Non-impolite reviews** → Do NOT increment count
   - Fixture: Five reviews (all clean/non-profane) from customerF
   - Verify: customerF has NO entry in customers table (or impoliteCount=0)

### Known Behavior

- Only processes INSERT/MODIFY events (REMOVE is skipped)
- Ignores reviews where `profane != true`
- Idempotent: retried events increment again (design intended for exactly-once DynamoDB streams)
- DynamoDB stream format uses low-level type descriptors: `{"S": "..."}`, `{"BOOL": ...}`
- Creates customers table entry on first impolite review; initializes count to 1

---

## Integration Points to Verify

### 1. Event Chain Connectivity

```
S3 Input Bucket (new object)
  ↓ (S3 notification)
  → Preprocessing Lambda ✓ (P2)
    ↓ (writes to preprocessed bucket)
    → S3 Preprocessed Bucket
      ↓ (EventBridge)
      → [Profanity Lambda ✓ (P2) + Sentiment Lambda ✓ (P3)]
        ↓ (both write to reviews table)
        → DynamoDB Reviews Table
          ↓ (stream)
          → Ban Lambda ✓ (P3)
            ↓ (writes to customers table)
            → DynamoDB Customers Table
```

Verify:
- [ ] Sentiment Lambda receives EventBridge events (check CloudWatch logs)
- [ ] Sentiment writes to reviews table successfully
- [ ] Ban Lambda receives stream events from reviews table
- [ ] Ban writes to customers table successfully
- [ ] No Lambda errors in CloudWatch logs

### 2. Data Flow

Verify these fields flow through correctly:

- **From preprocessing to sentiment:** `reviewID`, `reviewerID`, `summary`, `reviewText`, `overall`
- **From profanity/sentiment to ban:** `reviewID`, `reviewerID`, `profane` flag
- **In customers table:** `reviewerID` (primary key), `impoliteCount`, `banned`

### 3. Concurrency Safety

- [ ] Profanity and sentiment Lambdas both write same `reviewID` item simultaneously
- [ ] Verify both complete without one overwriting the other (both fields present in final item)
- [ ] Use `update_item` ensures separate attributes don't conflict

---

## Deployment Checklist for P4

Before running integration tests:

1. [ ] Fresh MiniStack running (check `ministack start`)
2. [ ] Run `bash src/deploy.sh` (creates all resources)
3. [ ] Verify all SSM parameters created: `/dic2026/group36/*`
4. [ ] Verify DynamoDB tables exist: `group36-reviews-table`, `group36-customers-table`
5. [ ] Verify S3 buckets exist: `group36-reviews-input`, `group36-reviews-preprocessed`, `group36-reviews-results`
6. [ ] Verify all four Lambdas deployed and callable
7. [ ] Verify EventBridge rule created: `group36-preprocessed-review-rule`
8. [ ] Verify DynamoDB stream attached to reviews table
9. [ ] Verify event source mapping for ban Lambda on the stream

---

## Offline Validation Results

Both P3 Lambdas have been offline-tested:

```
Sentiment Classification Logic Tests: 8/8 passed ✓
Ban Threshold Tests: 5/5 passed ✓
```

See `src/lambdas/p3_offline_check.py` for details.

---

## Remaining Work (P4 & Beyond)

- **P4:**
  - [ ] Write integration tests using fixtures (5 test cases minimum)
  - [ ] Run full `reviews_devset.json` through pipeline
  - [ ] Aggregate results: #positive, #neutral, #negative, #failed_profanity, banned_users
  - [ ] Save results to `src/results/results.json`
  - [ ] Verify `pytest` passes all 5 functionalities

- **P5:**
  - [ ] Compile report.pdf with P3_methodology.md section
  - [ ] Verify report includes architecture diagram
  - [ ] Write instructions.pdf for deployment and testing
  - [ ] Assemble final zip and submit

---

## Files for Reference

- Sentiment handler: `src/lambdas/sentiment/handler.py`
- Ban handler: `src/lambdas/ban/handler.py`
- Sentiment requirements: `src/lambdas/sentiment/requirements.txt`
- Ban requirements: `src/lambdas/ban/requirements.txt`
- Methodology note: `src/lambdas/P3_methodology.md`
- Implementation summary: `src/lambdas/P3_IMPLEMENTATION_SUMMARY.md`
- Offline test script: `src/lambdas/p3_offline_check.py`
- CONTRACT.md (full spec): `src/CONTRACT.md`

---

## Questions or Issues?

If integration tests fail, check:
1. CloudWatch logs for Lambda errors
2. DynamoDB items to verify write success
3. EventBridge rule pattern and targets
4. SSM parameter values (verify names/values match expected)
5. Stream event format (verify NEW_IMAGE contains expected fields)

**Threshold debugging:** If ban logic doesn't work, print the raw stream record in ban Lambda to verify DynamoDB format.

---

**Status:** ✅ P3 COMPLETE - Ready for P4 integration testing
