"""OTLP/HTTP ingest endpoint for Claude Code telemetry.

POST /v1/metrics  -- OTLP ExportMetricsServiceRequest (http/json)
POST /v1/logs     -- accepted and archived raw (no rollups yet)

Auth: Authorization: Bearer <INGEST_TOKEN>, matching what clients set in
OTEL_EXPORTER_OTLP_HEADERS.

Pipeline per request:
  1. Archive the raw payload to S3 via Kinesis Firehose (Athena layer).
  2. Parse datapoints, convert any cumulative series to deltas using
     per-series state in DynamoDB, and ADD the increments to the daily
     rollup items the dashboard reads.
"""

import base64
import gzip
import hmac
import json
import os
import time
from decimal import Decimal

import boto3

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared import otlp, rollup  # noqa: E402
from shared.pricing import load_pricing  # noqa: E402

TABLE_NAME = os.environ.get("TABLE_NAME", "")
FIREHOSE_NAME = os.environ.get("FIREHOSE_NAME", "")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
SERIES_TTL_DAYS = int(os.environ.get("SERIES_TTL_DAYS", "3"))

_dynamodb = None
_firehose = None


def _table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _dynamodb


def _firehose_client():
    global _firehose
    if _firehose is None:
        _firehose = boto3.client("firehose")
    return _firehose


def _response(status, body=""):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body) if not isinstance(body, str) else body,
    }


def _authorized(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    expected = f"Bearer {INGEST_TOKEN}"
    return bool(INGEST_TOKEN) and hmac.compare_digest(auth, expected)


def _decode_body(event):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(body)
    else:
        raw = body.encode("utf-8")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if headers.get("content-encoding", "").lower() == "gzip":
        raw = gzip.decompress(raw)
    return raw


def resolve_cumulative_delta(point, table=None):
    """Convert a cumulative counter observation to a delta by atomically
    swapping the stored last value for this series."""
    table = table or _table()
    key = otlp.series_key(point)
    new_value = Decimal(str(point["value"]))
    ttl = int(time.time()) + SERIES_TTL_DAYS * 86400
    resp = table.update_item(
        Key={"pk": f"SERIES#{key}", "sk": "STATE"},
        UpdateExpression="SET last_value = :v, expires_at = :t",
        ExpressionAttributeValues={":v": new_value, ":t": ttl},
        ReturnValues="UPDATED_OLD",
    )
    old = (resp.get("Attributes") or {}).get("last_value")
    if old is None:
        return float(new_value)  # first observation of this series
    delta = float(new_value) - float(old)
    # Counter reset (new session reusing key, or client restart)
    return float(new_value) if delta < 0 else delta


def apply_increments(buckets, table=None):
    table = table or _table()
    for (pk, sk), fields in buckets.items():
        names, values, adds = {}, {}, []
        for i, (field, delta) in enumerate(fields.items()):
            names[f"#f{i}"] = field
            values[f":v{i}"] = Decimal(str(round(delta, 8)))
            adds.append(f"#f{i} :v{i}")
        table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression="ADD " + ", ".join(adds),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )


def archive_raw(raw_bytes, signal):
    if not FIREHOSE_NAME:
        return
    record = json.dumps({
        "received_at": int(time.time()),
        "signal": signal,
        "payload": json.loads(raw_bytes),
    }, separators=(",", ":")) + "\n"
    _firehose_client().put_record(
        DeliveryStreamName=FIREHOSE_NAME,
        Record={"Data": record.encode("utf-8")},
    )


def handler(event, context):
    if not _authorized(event):
        return _response(401, {"error": "unauthorized"})

    path = (event.get("rawPath") or
            event.get("requestContext", {}).get("http", {}).get("path", ""))
    try:
        raw = _decode_body(event)
        payload = json.loads(raw)
    except (ValueError, OSError):
        return _response(400, {"error": "invalid payload"})

    if path.endswith("/v1/logs"):
        try:
            archive_raw(raw, "logs")
        except Exception:  # archive is best-effort for logs
            pass
        return _response(200, {"partialSuccess": {}})

    try:
        archive_raw(raw, "metrics")
    except Exception:
        pass  # rollups still proceed if the archive stream hiccups

    points = otlp.parse_export_request(payload)
    buckets = rollup.points_to_increments(
        points,
        resolve_delta=resolve_cumulative_delta,
        pricing_table=load_pricing(),
    )
    apply_increments(buckets)
    return _response(200, {"partialSuccess": {}})
