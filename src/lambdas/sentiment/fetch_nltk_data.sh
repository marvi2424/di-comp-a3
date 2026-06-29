#!/usr/bin/env bash
# Populate ./nltk_data with the corpus the sentiment Lambda needs.
#
# deploy.sh copies this nltk_data/ directory into the Lambda deployment package,
# and handler.py registers it on nltk.data.path, so the function never downloads
# anything at runtime (MiniStack Lambdas have no outbound network).
#
# Run once from this directory after installing requirements.txt:
#     pip install -r requirements.txt
#     ./fetch_nltk_data.sh
#
# Corpus:
#   vader_lexicon - the VADER sentiment lexicon used by SentimentIntensityAnalyzer
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${SCRIPT_DIR}/nltk_data"

# The corpus is DATA, not a pip package, so nltk must be importable to run its
# downloader. Install it here if it isn't already (no manual step needed).
python3 -c "import nltk" 2>/dev/null || python3 -m pip install --user "nltk==3.8.1"

python3 -m nltk.downloader -d "${DEST}" vader_lexicon

echo "NLTK data ready in ${DEST} ($(du -sh "${DEST}" | cut -f1) unzipped)"
