"""Model pricing for cost estimation and cache-savings math.

Prices are USD per million tokens and reflect Amazon Bedrock on-demand
list pricing for Anthropic models. Override at deploy time via the
PRICING_JSON environment variable (same shape as DEFAULT_PRICING) when
prices change or when you have negotiated rates -- the point of this
table is estimation and reconciliation, not billing.

Cache pricing model (Anthropic/Bedrock):
  - cacheRead tokens are billed at 10% of the input rate
  - cacheCreation (cache write) tokens are billed at 125% of the input rate
"""

import json
import os

CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_MULTIPLIER = 1.25

# USD per 1M tokens: (input, output)
DEFAULT_PRICING = {
    "claude-fable-5": {"input": 20.00, "output": 100.00},
    "claude-opus-4-8": {"input": 15.00, "output": 75.00},
    "claude-opus-4-5": {"input": 15.00, "output": 75.00},
    "claude-opus-4-1": {"input": 15.00, "output": 75.00},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.00},
}

_FALLBACK = {"input": 3.00, "output": 15.00}


def load_pricing():
    override = os.environ.get("PRICING_JSON")
    if override:
        try:
            table = dict(DEFAULT_PRICING)
            table.update(json.loads(override))
            return table
        except (ValueError, TypeError):
            pass
    return DEFAULT_PRICING


def rates_for(model, pricing=None):
    """Match a model ID (which may be a full Bedrock ID like
    us.anthropic.claude-sonnet-5-20250929-v1:0) to a pricing entry."""
    pricing = pricing or load_pricing()
    if not model:
        return _FALLBACK
    if model in pricing:
        return pricing[model]
    normalized = model.lower()
    # Longest key first so claude-sonnet-4-5 wins over claude-sonnet-4
    for key in sorted(pricing, key=len, reverse=True):
        if key in normalized:
            return pricing[key]
    return _FALLBACK


def estimate_cost_usd(model, input_tokens=0, output_tokens=0,
                      cache_read_tokens=0, cache_write_tokens=0, pricing=None):
    """Estimated Bedrock cost for a bundle of token counts."""
    r = rates_for(model, pricing)
    per_tok_in = r["input"] / 1_000_000
    per_tok_out = r["output"] / 1_000_000
    return (
        input_tokens * per_tok_in
        + output_tokens * per_tok_out
        + cache_read_tokens * per_tok_in * CACHE_READ_MULTIPLIER
        + cache_write_tokens * per_tok_in * CACHE_WRITE_MULTIPLIER
    )


def cache_savings_usd(model, cache_read_tokens, pricing=None):
    """What the cacheRead tokens would have cost as fresh input tokens,
    minus what they actually cost at the cache-read rate."""
    r = rates_for(model, pricing)
    per_tok_in = r["input"] / 1_000_000
    return cache_read_tokens * per_tok_in * (1 - CACHE_READ_MULTIPLIER)
