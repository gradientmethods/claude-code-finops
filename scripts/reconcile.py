#!/usr/bin/env python3
"""Reconcile telemetry-derived spend against actual AWS billing.

Compares three numbers per day:
  1. cost_usd      -- what Claude Code itself reported (cost.usage metric)
  2. est_cost_usd  -- tokens x pricing table (this project's estimate)
  3. billed        -- Amazon Bedrock unblended cost from Cost Explorer

Usage:
  python scripts/reconcile.py --table <RollupTableName> [--days 14]
      [--region us-east-1] [--tag-key team --tag-value platform]

Cost Explorer data lags ~24h, so today's row is expected to under-report.
Requires ce:GetCostAndUsage and dynamodb:Query permissions.

Tip: for per-team billing (not just telemetry) attribution, route each
team through its own Bedrock application inference profile and tag it;
see docs/bedrock-cost-attribution.md.
"""

import argparse
from datetime import date, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key


def telemetry_by_day(table_name, days, region):
    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    out = {}
    today = date.today()
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        resp = table.query(
            KeyConditionExpression=Key("pk").eq(f"D#{d}"))
        for item in resp.get("Items", []):
            if item.get("sk") == "TOTAL":
                out[d] = {
                    "reported": float(item.get("cost_usd", Decimal(0))),
                    "estimated": float(item.get("est_cost_usd", Decimal(0))),
                }
    return out


def billed_by_day(days, region, tag_key=None, tag_value=None):
    ce = boto3.client("ce", region_name="us-east-1")  # CE is us-east-1 only
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=days + 1)
    flt = {"Dimensions": {"Key": "SERVICE",
                          "Values": ["Amazon Bedrock"]}}
    if tag_key:
        flt = {"And": [flt, {"Tags": {"Key": tag_key,
                                      "Values": [tag_value or ""]}}]}
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        Filter=flt,
    )
    out = {}
    for row in resp.get("ResultsByTime", []):
        d = row["TimePeriod"]["Start"]
        out[d] = float(row["Total"]["UnblendedCost"]["Amount"])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table", required=True,
                    help="RollupTable name (see stack resources)")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--region", default=None)
    ap.add_argument("--tag-key", default=None,
                    help="Optional cost allocation tag to filter billing")
    ap.add_argument("--tag-value", default=None)
    args = ap.parse_args()

    telemetry = telemetry_by_day(args.table, args.days, args.region)
    billed = billed_by_day(args.days, args.region, args.tag_key,
                           args.tag_value)

    print(f"{'date':<12}{'reported':>12}{'estimated':>12}"
          f"{'billed':>12}{'est vs billed':>15}")
    print("-" * 63)
    t_rep = t_est = t_bill = 0.0
    for d in sorted(set(telemetry) | set(billed)):
        rep = telemetry.get(d, {}).get("reported", 0.0)
        est = telemetry.get(d, {}).get("estimated", 0.0)
        bil = billed.get(d, 0.0)
        t_rep, t_est, t_bill = t_rep + rep, t_est + est, t_bill + bil
        drift = f"{(est - bil) / bil * 100:+.1f}%" if bil > 0.005 else "-"
        print(f"{d:<12}{rep:>12.2f}{est:>12.2f}{bil:>12.2f}{drift:>15}")
    print("-" * 63)
    drift = (f"{(t_est - t_bill) / t_bill * 100:+.1f}%"
             if t_bill > 0.005 else "-")
    print(f"{'TOTAL':<12}{t_rep:>12.2f}{t_est:>12.2f}{t_bill:>12.2f}"
          f"{drift:>15}")
    print("\nNotes: Cost Explorer lags ~24h; 'billed' includes ALL Bedrock "
          "usage in the account\nunless filtered by tag. Consistent drift "
          "usually means the pricing table needs an update\n(PRICING_JSON) "
          "or non-Claude-Code Bedrock workloads share the account.")


if __name__ == "__main__":
    main()
