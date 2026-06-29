# Corner-case test reviews

`corner_cases.json` is a small, hand-crafted set of reviews that exercise the
tricky paths of the chain:

| Customer | What it checks |
|---|---|
| `customerBANNED` | 4 impolite reviews → must end up `banned = true` |
| `customerEDGE` | exactly 3 impolite reviews → must stay `banned = false` (the edge case) |
| `customerCLEAN` | only clean reviews → no impolite count |
| `customerSARCASM` | 5 stars but negative text → sentiment neutral override |
| `customerNEUTRAL` | exactly 3 stars → always neutral |
| `customerEMPTY` | empty text → sentiment falls back to `overall` |
| `customerSUMMARY` | profane word only in `summary` |

Each object carries a `_case` field documenting its intent; the Lambdas ignore
unknown fields, so it is harmless in the chain.

**These reviews are for testing only.** They are **excluded** from the reported
devset numbers in `src/results/results.json` (CONTRACT.md §13). To replay them
manually against a deployed stack:

```bash
python3 - <<'PY'
import json, boto3
s3 = boto3.client("s3", endpoint_url="http://localhost:4566")
ssm = boto3.client("ssm", endpoint_url="http://localhost:4566")
bucket = ssm.get_parameter(Name="/dic2026/group36/s3/input_bucket")["Parameter"]["Value"]
for i, r in enumerate(json.load(open("src/test_reviews/corner_cases.json")), 1):
    s3.put_object(Bucket=bucket, Key=f"corner-{i:04d}.json", Body=json.dumps(r).encode())
print("uploaded corner cases")
PY
```
