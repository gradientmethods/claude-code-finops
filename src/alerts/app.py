"""Scheduled spend guard: budget thresholds + spike detection.

Runs hourly (EventBridge). Two checks against the rollup table:

1. Budget: if month-to-date total spend crosses MONTHLY_BUDGET_USD (or a
   per-team budget from BUDGETS_JSON, e.g. {"platform": 500}), notify.
2. Spike: if any user's spend today exceeds SPIKE_MULTIPLIER x their
   trailing 7-day daily average (and is above SPIKE_MIN_USD), notify.
   This is the "an agent looped all night" alarm.

Notifications go to an SNS topic; deduplicated per day via marker items
so the hourly schedule doesn't re-send the same alert.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ.get("TABLE_NAME", "")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
MONTHLY_BUDGET_USD = float(os.environ.get("MONTHLY_BUDGET_USD", "0") or 0)
BUDGETS_JSON = os.environ.get("BUDGETS_JSON", "")
SPIKE_MULTIPLIER = float(os.environ.get("SPIKE_MULTIPLIER", "4"))
SPIKE_MIN_USD = float(os.environ.get("SPIKE_MIN_USD", "25"))

_dynamodb = None
_sns = None


def table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _dynamodb


def sns():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    return _sns


def _num(item, field):
    v = item.get(field, 0)
    return float(v) if isinstance(v, Decimal) else (v or 0)


def _cost(item):
    return _num(item, "cost_usd") or _num(item, "est_cost_usd")


def _day_items(date):
    resp = table().query(KeyConditionExpression=Key("pk").eq(f"D#{date}"))
    return resp.get("Items", [])


def month_to_date(today):
    """(total_spend, {team: spend}) for the current calendar month."""
    first = today.replace(day=1)
    total, teams = 0.0, {}
    d = first
    while d <= today:
        for item in _day_items(d.isoformat()):
            sk = item.get("sk", "")
            if sk == "TOTAL":
                total += _cost(item)
            elif sk.startswith("T#"):
                teams[sk[2:]] = teams.get(sk[2:], 0.0) + _cost(item)
        d += timedelta(days=1)
    return total, teams


def user_spend_by_day(dates):
    """{user: {date: spend}} across the given dates."""
    out = {}
    for date in dates:
        for item in _day_items(date):
            sk = item.get("sk", "")
            if sk.startswith("U#"):
                out.setdefault(sk[2:], {})[date] = _cost(item)
    return out


def check_budgets(today):
    alerts = []
    total, teams = month_to_date(today)
    if MONTHLY_BUDGET_USD and total >= MONTHLY_BUDGET_USD:
        alerts.append({
            "key": f"budget-total-{today.strftime('%Y-%m')}",
            "subject": "Claude Code spend: monthly budget reached",
            "message": (f"Month-to-date Claude Code spend is "
                        f"${total:,.2f}, at or above the configured "
                        f"budget of ${MONTHLY_BUDGET_USD:,.2f}."),
        })
    if BUDGETS_JSON:
        try:
            budgets = json.loads(BUDGETS_JSON)
        except ValueError:
            budgets = {}
        for team, budget in budgets.items():
            spend = teams.get(team, 0.0)
            if spend >= float(budget):
                alerts.append({
                    "key": f"budget-{team}-{today.strftime('%Y-%m')}",
                    "subject": f"Claude Code spend: team '{team}' over budget",
                    "message": (f"Team '{team}' month-to-date spend is "
                                f"${spend:,.2f}, at or above its budget "
                                f"of ${float(budget):,.2f}."),
                })
    return alerts


def check_spikes(today):
    dates = [(today - timedelta(days=i)).isoformat() for i in range(8)]
    today_iso = dates[0]
    spend = user_spend_by_day(dates)
    alerts = []
    for user, by_day in spend.items():
        today_spend = by_day.get(today_iso, 0.0)
        if today_spend < SPIKE_MIN_USD:
            continue
        history = [by_day.get(d, 0.0) for d in dates[1:]]
        baseline = sum(history) / 7.0
        if baseline == 0 or today_spend >= baseline * SPIKE_MULTIPLIER:
            alerts.append({
                "key": f"spike-{user}-{today_iso}",
                "subject": f"Claude Code spend spike: {user}",
                "message": (
                    f"{user} has spent ${today_spend:,.2f} today, vs a "
                    f"7-day daily average of ${baseline:,.2f}. This can "
                    f"indicate a runaway agent loop or an unusually "
                    f"heavy workload worth a look."),
            })
    return alerts


def already_sent(key):
    resp = table().get_item(Key={"pk": "ALERT", "sk": key})
    return "Item" in resp


def mark_sent(key):
    import time
    table().put_item(Item={
        "pk": "ALERT", "sk": key,
        "expires_at": int(time.time()) + 40 * 86400,
    })


def handler(event, context):
    today = datetime.now(timezone.utc).date()
    sent = 0
    for alert in check_budgets(today) + check_spikes(today):
        if already_sent(alert["key"]):
            continue
        if TOPIC_ARN:
            sns().publish(TopicArn=TOPIC_ARN, Subject=alert["subject"][:100],
                          Message=alert["message"])
        mark_sent(alert["key"])
        sent += 1
    return {"alerts_sent": sent}
