# CONTRACT.md

**DIC2026 – Assignment 3**
**Group:** 36
**Project:** Event-Driven Serverless Review Analysis Application
**Environment:** MiniStack

***

## Main Goal

Analyze customer reviews using an event-driven serverless pipeline. The system preprocesses text, checks for profanity, performs sentiment analysis, counts impolite reviews per customer, and bans customers who exceed 3 impolite reviews.

***

## 1. System Overview

The application processes customer reviews using an event-driven serverless architecture. The chain starts when one review JSON object is uploaded to an S3 input bucket. The preprocessing Lambda reads the review, normalizes the text, and writes the processed result to a second S3 bucket. That bucket event triggers both the profanity Lambda and the sentiment Lambda through EventBridge. Both Lambdas store their results in DynamoDB. A DynamoDB stream then triggers the ban Lambda, which increments the customer's impolite count and bans them if the count exceeds 3.

***

## 2. Architecture Flow

```
S3 input bucket
  → group36-preprocessing-lambda
    → S3 preprocessed bucket
      → EventBridge fan-out
        → group36-profanity-lambda   → DynamoDB reviews table
        → group36-sentiment-lambda   → DynamoDB reviews table
                                          → DynamoDB Stream
                                            → group36-ban-lambda
                                              → DynamoDB customers table
```

***

## 3. Dataset Fields

The application uses review records from `reviews_devset.json`.

**Expected input JSON shape:**

```json
{
  "reviewerID": "A2VNYWOPJ13AFP",
  "asin": "0981850006",
  "reviewerName": "Amazon Customer",
  "helpful": [6, 7],
  "reviewText": "Review text here",
  "overall": 5.0,
  "summary": "Review summary here",
  "unixReviewTime": 1259798400,
  "reviewTime": "12 3, 2009",
  "category": "Patio_Lawn_and_Garde"
}
```

**Field mapping:**

| Field | Purpose |
|---|---|
| `reviewerID` | Customer/user ID for counting impolite reviews and banning |
| `summary` | Used for preprocessing, profanity check, and sentiment analysis |
| `reviewText` | Used for preprocessing, profanity check, and sentiment analysis |
| `overall` | Used as a supporting signal for sentiment analysis |
| `asin` | Product ID — kept as metadata |
| `category` | Product category — kept as metadata |

> **Note:** The customer identifier for this project is `reviewerID`.

***

## 4. S3 Buckets

All bucket names **must** be retrieved from SSM Parameter Store. No bucket name may be hardcoded inside a Lambda function.

| Bucket Name | Purpose | SSM Parameter |
|---|---|---|
| `group36-reviews-input` | Stores raw review JSON files | `/dic2026/group36/s3/input_bucket` |
| `group36-reviews-preprocessed` | Stores preprocessed review JSON files | `/dic2026/group36/s3/preprocessed_bucket` |
| `group36-reviews-results` | Stores optional output/result files | `/dic2026/group36/s3/results_bucket` |

***

## 5. DynamoDB Tables

All table names **must** be retrieved from SSM Parameter Store. No table name may be hardcoded inside a Lambda function.

### 5.1 Reviews Table

| Property | Value |
|---|---|
| Table name | `group36-reviews-table` |
| SSM parameter | `/dic2026/group36/dynamodb/reviews_table` |
| Purpose | Stores per-review processing results (profanity + sentiment output) |
| Primary key | `reviewID` (String) |
| DynamoDB stream | Enabled with `NEW_AND_OLD_IMAGES` |

**Recommended item shape:**

```json
{
  "reviewID": "review-000001",
  "reviewerID": "A2VNYWOPJ13AFP",
  "asin": "0981850006",
  "summary": "Review summary here",
  "reviewText": "Original review text here",
  "overall": 5.0,
  "preprocessedText": ["token1", "token2"],
  "profane": false,
  "profaneFields": [],
  "sentiment": "positive",
  "status": "processed"
}
```

### 5.2 Customers Table

| Property | Value |
|---|---|
| Table name | `group36-customers-table` |
| SSM parameter | `/dic2026/group36/dynamodb/customers_table` |
| Purpose | Stores customer-level impolite review count and ban status |
| Primary key | `reviewerID` (String) |

**Recommended item shape:**

```json
{
  "reviewerID": "A2VNYWOPJ13AFP",
  "impoliteCount": 4,
  "banned": true,
  "lastUpdatedReviewID": "review-000004"
}
```

**Ban rule:**

- `impoliteCount <= 3` → `banned = false`
- `impoliteCount > 3` → `banned = true`

This means: 3 impolite reviews = **not banned**, 4 impolite reviews = **banned**.

***

## 6. SSM Parameter Names

All of the following parameters must be created by `src/deploy.sh`.

| Parameter | Value |
|---|---|
| `/dic2026/group36/s3/input_bucket` | `group36-reviews-input` |
| `/dic2026/group36/s3/preprocessed_bucket` | `group36-reviews-preprocessed` |
| `/dic2026/group36/s3/results_bucket` | `group36-reviews-results` |
| `/dic2026/group36/dynamodb/reviews_table` | `group36-reviews-table` |
| `/dic2026/group36/dynamodb/customers_table` | `group36-customers-table` |
| `/dic2026/group36/eventbridge/preprocessed_rule` | `group36-preprocessed-review-rule` |
| `/dic2026/group36/config/ban_threshold` | `3` |

***

## 7. Lambda Functions

The system uses four Lambda functions.

| Lambda | Trigger | Input | Output |
|---|---|---|---|
| `group36-preprocessing-lambda` | S3 object created in input bucket | Raw review JSON | Preprocessed review JSON written to preprocessed bucket |
| `group36-profanity-lambda` | EventBridge event from preprocessed bucket | Preprocessed review JSON | Profanity result stored in reviews table |
| `group36-sentiment-lambda` | EventBridge event from preprocessed bucket | Preprocessed review JSON | Sentiment result stored in reviews table |
| `group36-ban-lambda` | DynamoDB stream from reviews table | New/updated review item | Customer impolite count and ban status in customers table |

***

## 8. Preprocessing Lambda Contract

| Property | Value |
|---|---|
| Folder | `src/lambdas/preprocessing/` |
| Handler | `handler.lambda_handler` |
| Trigger | S3 object created event from input bucket |

**Processing steps:**
1. Read `summary`, `reviewText`, and `overall` from the raw JSON
2. Tokenize `summary` and `reviewText`
3. Remove stop words (NLTK)
4. Lemmatize tokens (NLTK)
5. Keep all original metadata fields
6. Write the processed JSON to the preprocessed S3 bucket

**Output JSON shape:**

```json
{
  "reviewID": "review-000001",
  "reviewerID": "A2VNYWOPJ13AFP",
  "asin": "0981850006",
  "summary": "Original summary",
  "reviewText": "Original review text",
  "overall": 5.0,
  "processedSummaryTokens": ["token1", "token2"],
  "processedReviewTokens": ["token3", "token4"],
  "combinedProcessedText": ["token1", "token2", "token3", "token4"],
  "category": "Patio_Lawn_and_Garde"
}
```

**Output S3 key format:** `preprocessed/{reviewID}.json`

> **Note:** NLTK data must be bundled into the Lambda ZIP. Total unzipped size must stay within the 250 MB limit.

***

## 9. Profanity Lambda Contract

| Property | Value |
|---|---|
| Folder | `src/lambdas/profanity/` |
| Handler | `handler.lambda_handler` |
| Trigger | EventBridge event when a preprocessed review is created in the preprocessed S3 bucket |

**Processing steps:**
1. Read `summary` and `reviewText` from the preprocessed JSON
2. Run `profanityfilter` on both fields
3. Set `profane = true` if bad words are found in either field
4. Record which fields triggered the flag in `profaneFields`
5. Store the result in the DynamoDB reviews table

**Output fields added to reviews table:**

```json
{
  "reviewID": "review-000001",
  "reviewerID": "A2VNYWOPJ13AFP",
  "profane": true,
  "profaneFields": ["reviewText"]
}
```

***

## 10. Sentiment Lambda Contract

| Property | Value |
|---|---|
| Folder | `src/lambdas/sentiment/` |
| Handler | `handler.lambda_handler` |
| Trigger | EventBridge event when a preprocessed review is created in the preprocessed S3 bucket |

**Processing steps:**
1. Read `summary`, `reviewText`, and `overall` from the preprocessed JSON
2. Analyze text sentiment using NLTK VADER on `summary` and `reviewText`
3. Use `overall` star rating as a supporting signal
4. Classify the review as `positive`, `neutral`, or `negative`
5. Store the result in the DynamoDB reviews table

**Output fields added to reviews table:**

```json
{
  "reviewID": "review-000001",
  "reviewerID": "A2VNYWOPJ13AFP",
  "sentiment": "positive"
}
```

**Recommended classification rule:**

| Condition | Label |
|---|---|
| `overall >= 4` and VADER score is not strongly negative | `positive` |
| `overall == 3` or VADER score is near zero | `neutral` |
| `overall <= 2` and VADER score is not strongly positive | `negative` |

***

## 11. Ban Lambda Contract

| Property | Value |
|---|---|
| Folder | `src/lambdas/ban/` |
| Handler | `handler.lambda_handler` |
| Trigger | DynamoDB stream from reviews table |

**Processing steps:**
1. Read `reviewerID` and `profane` from the incoming stream event
2. If `profane = true`, increment `impoliteCount` for that reviewer in the customers table
3. If `impoliteCount > 3`, set `banned = true`
4. If `impoliteCount <= 3`, keep `banned = false`
5. Update `lastUpdatedReviewID` to the current review

**Output stored in customers table:**

```json
{
  "reviewerID": "A2VNYWOPJ13AFP",
  "impoliteCount": 4,
  "banned": true,
  "lastUpdatedReviewID": "review-000004"
}
```

> **Stream safety rule:** Count a review only when `profane` changes from missing/false to true. Ignore sentiment-only updates and repeated updates where `profane` was already true.

> **Edge case to test:** A customer with exactly 3 impolite reviews must **not** be banned. Ban only triggers on the 4th.

***

## 12. Integration Test Requirements

Tests must be stored in `src/tests/test_integration.py` (extend the existing file). Each test should use small fixtures — **not** the full devset.

The five required functionalities to test:

1. Preprocessing works correctly (tokenize, stop-word removal, lemmatize)
2. Profanity check correctly flags impolite reviews
3. Sentiment analysis classifies correctly
4. Impolite reviews are counted per customer in DynamoDB
5. Customer is banned only after more than 3 impolite reviews (not at 3)

**Corner-case reviews** (e.g. a user with 4+ impolite reviews) must be stored in `src/test_reviews/corner_cases.json` and must **not** be included in the final reported devset numbers.

***

## 13. Dataset Result Rules

Final reported results must be calculated **only** from `reviews_devset.json`. No corner-case or test reviews may be mixed in.

**Required output numbers:**
- Number of positive reviews
- Number of neutral reviews
- Number of negative reviews
- Number of reviews that failed the profanity check
- List of banned users (if any)

**Output file:** `src/results/results.json`

**Recommended shape:**

```json
{
  "positive_reviews": 0,
  "neutral_reviews": 0,
  "negative_reviews": 0,
  "failed_profanity_reviews": 0,
  "banned_users": []
}
```

***

## 14. Development Rules

- Do **not** hardcode bucket names or table names inside any Lambda function
- All bucket and table names must be read from SSM Parameter Store at runtime
- All resources must be recreatable by running `src/deploy.sh` from scratch
- MiniStack is ephemeral — the deploy script must assume nothing exists beforehand
- The application must be fully event-driven (no polling)
- Corner-case/test reviews are allowed only for testing purposes
- Final reported numbers must only come from `reviews_devset.json`

***

## 15. Handoff Notes

**P1 provides to the team:**
- This contract (`CONTRACT.md`)
- `src/deploy.sh` — creates all buckets, tables, SSM params, Lambdas, S3 notifications, and EventBridge rules
- S3 bucket schema and DynamoDB table schema
- SSM parameter list
- Deployable Lambda skeleton code (reads names from SSM, no logic yet)
- `architecture.png` — diagram of the full chain

**P2 uses this contract to implement:**
- `src/lambdas/preprocessing/handler.py`
- `src/lambdas/profanity/handler.py`

**P3 uses this contract to implement:**
- `src/lambdas/sentiment/handler.py`
- `src/lambdas/ban/handler.py`

**P4 uses this contract to implement:**
- `src/tools/load_devset.py` — batch loader for `reviews_devset.json`
- `src/tests/test_integration.py` — integration tests for all 5 functionalities
- `src/results/results.json` — final result numbers from the devset run

**P5 uses this contract to write:**
- `report.pdf` — 5 sections, ≤8 pages, 11pt, one column, with architecture diagram
- `instructions.pdf` — how to deploy and run the app from scratch
- Final submission ZIP: `<groupID>_DIC2026_Assignment_3.zip`
