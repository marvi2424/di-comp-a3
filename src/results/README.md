# Results

`results.json` holds the **final reported numbers**, computed **only** from
`reviews_devset.json` (CONTRACT.md §13). It is **generated**, never hand-edited:

```bash
# MiniStack up + `bash src/deploy.sh` already run:
python3 src/tools/load_devset.py
```

This uploads every devset review through the deployed chain, waits for it to
finish, and writes `results.json` here with the shape:

```json
{
  "positive_reviews": 0,
  "neutral_reviews": 0,
  "negative_reviews": 0,
  "failed_profanity_reviews": 0,
  "banned_users": [],
  "total_reviews_scanned": 0
}
```

Corner-case / test reviews (`src/test_reviews/`) are **never** included in these
numbers — they run against tests only.
