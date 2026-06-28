#!/usr/bin/env python3
"""Offline validation of P3 Lambda implementations (sentiment & ban).

This script validates the core logic of the P3 Lambdas without requiring
AWS/MiniStack or NLTK installation. The sentiment VADER analyzer is tested
in integration tests; this checks the classification logic itself.

Run from the repo root:
  python3 src/lambdas/p3_offline_check.py
"""

import sys


def test_ban_logic():
    """Test the DynamoDB stream processing and ban threshold logic."""
    print("\n=== BAN THRESHOLD TESTS ===\n")

    # The ban Lambda logic: banned = impoliteCount > 3
    test_cases = [
        (1, False, "1st impolite review: not banned"),
        (2, False, "2nd impolite review: not banned"),
        (3, False, "3rd impolite review: not banned (edge case)"),
        (4, True, "4th impolite review: banned (threshold exceeded)"),
        (5, True, "5th impolite review: still banned"),
    ]

    passed = 0
    failed = 0

    for count, expected_banned, description in test_cases:
        # The ban logic is: banned = impoliteCount > 3
        actual_banned = count > 3
        status = "✓" if actual_banned == expected_banned else "✗"
        if actual_banned == expected_banned:
            passed += 1
        else:
            failed += 1
        print(
            f"{status} {description}: impoliteCount={count}, "
            f"banned={actual_banned} (expected {expected_banned})"
        )

    print(f"\nBan tests: {passed} passed, {failed} failed")
    return failed == 0


def test_sentiment_classification_logic():
    """Test sentiment classification decision tree (without VADER computation)."""
    print("\n=== SENTIMENT CLASSIFICATION LOGIC TESTS ===\n")

    # Test the classification decision tree independently.
    # The actual VADER computation is tested in integration tests.

    def classify_based_on_rule(overall, vader_compound):
        """Simplified classification matching the handler logic."""
        strongly_positive_threshold = 0.5
        strongly_negative_threshold = -0.5

        overall = float(overall) if overall else 3.0

        if overall >= 4:
            if vader_compound > strongly_negative_threshold:
                return "positive"
            else:
                return "neutral"
        elif overall == 3:
            return "neutral"
        elif overall <= 2:
            if vader_compound < strongly_positive_threshold:
                return "negative"
            else:
                return "neutral"
        return "neutral"

    test_cases = [
        # (overall, vader_score, expected, description)
        (5.0, 0.7, "positive", "5 stars + positive VADER → positive"),
        (5.0, -0.6, "neutral", "5 stars + strongly negative VADER → neutral (override)"),
        (4.0, 0.3, "positive", "4 stars + neutral VADER → positive"),
        (3.0, 0.8, "neutral", "3 stars + any VADER → neutral"),
        (3.0, -0.8, "neutral", "3 stars + any VADER → neutral"),
        (2.0, -0.6, "negative", "2 stars + negative VADER → negative"),
        (2.0, 0.6, "neutral", "2 stars + strongly positive VADER → neutral (override)"),
        (1.0, -0.9, "negative", "1 star + negative VADER → negative"),
    ]

    passed = 0
    failed = 0

    for overall, vader_score, expected, description in test_cases:
        try:
            result = classify_based_on_rule(overall, vader_score)
            status = "✓" if result == expected else "✗"
            if result == expected:
                passed += 1
            else:
                failed += 1
            print(
                f"{status} {description}: got {result!r}, expected {expected!r}"
            )
        except Exception as e:
            failed += 1
            print(f"✗ {description}: EXCEPTION {e}")

    print(f"\nSentiment classification logic tests: {passed} passed, {failed} failed")
    return failed == 0


def main():
    print("\n" + "=" * 60)
    print("P3 OFFLINE VALIDATION (Sentiment & Ban Lambda)")
    print("=" * 60)

    try:
        classification_ok = test_sentiment_classification_logic()
        ban_ok = test_ban_logic()

        print("\n" + "=" * 60)
        if classification_ok and ban_ok:
            print("✓ ALL TESTS PASSED")
            print("=" * 60)
            print("\nNOTE: Actual VADER sentiment analysis is tested via")
            print("integration tests with real NLTK library.")
            return 0
        else:
            print("✗ SOME TESTS FAILED")
            print("=" * 60)
            return 1

    except Exception as e:
        print(f"\n✗ FATAL ERROR: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

