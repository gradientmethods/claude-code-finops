import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shared import pricing  # noqa: E402


def test_exact_match():
    assert pricing.rates_for("claude-sonnet-5")["input"] == 3.00


def test_bedrock_model_id_substring_match():
    r = pricing.rates_for("us.anthropic.claude-sonnet-5-20250929-v1:0")
    assert r["input"] == 3.00
    r = pricing.rates_for("us.anthropic.claude-opus-4-8-20251115-v1:0")
    assert r["input"] == 15.00


def test_longest_key_wins():
    # claude-sonnet-4-5 must not match claude-sonnet-4's entry
    r = pricing.rates_for("anthropic.claude-sonnet-4-5-20250929-v1:0")
    assert r == pricing.DEFAULT_PRICING["claude-sonnet-4-5"]


def test_unknown_model_falls_back():
    assert pricing.rates_for("some-new-model")["input"] == 3.00
    assert pricing.rates_for(None)["input"] == 3.00


def test_estimate_cost():
    # 1M input + 1M output on sonnet = 3 + 15
    cost = pricing.estimate_cost_usd(
        "claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert abs(cost - 18.0) < 1e-9


def test_cache_write_premium():
    cost = pricing.estimate_cost_usd(
        "claude-sonnet-5", cache_write_tokens=1_000_000)
    assert abs(cost - 3.75) < 1e-9  # 125% of $3


def test_cache_savings():
    saved = pricing.cache_savings_usd("claude-sonnet-5", 1_000_000)
    assert abs(saved - 2.70) < 1e-9  # 90% of $3


def test_pricing_override_env(monkeypatch):
    monkeypatch.setenv("PRICING_JSON",
                       '{"claude-sonnet-5": {"input": 2.5, "output": 12.5}}')
    table = pricing.load_pricing()
    assert pricing.rates_for("claude-sonnet-5", table)["input"] == 2.5
    # non-overridden entries survive
    assert pricing.rates_for("claude-haiku-4-5", table)["input"] == 1.00


def test_bad_override_ignored(monkeypatch):
    monkeypatch.setenv("PRICING_JSON", "{not json")
    assert pricing.load_pricing() == pricing.DEFAULT_PRICING
