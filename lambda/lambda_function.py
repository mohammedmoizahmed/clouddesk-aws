import os
import urllib.parse
from datetime import datetime, timezone

import boto3

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table("clouddesk-attachments")

ALLOWED = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".txt"}
MAX_BYTES = 5 * 1024 * 1024  # 5 MB


def lambda_handler(event, context):
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        size = record["s3"]["object"].get("size", 0)

        ext = os.path.splitext(key)[1].lower()
        ticket_id = key.split("/")[0] if "/" in key else "unknown"

        status = "accepted"
        reason = ""
        if ext not in ALLOWED:
            status = "rejected"
            reason = f"File type {ext or 'none'} not allowed"
        elif size > MAX_BYTES:
            status = "rejected"
            reason = "File larger than 5 MB"

        table.put_item(Item={
            "file_key": key,
            "ticket_id": ticket_id,
            "size_bytes": size,
            "status": status,
            "reason": reason,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        })

        if status == "rejected":
            s3.delete_object(Bucket=bucket, Key=key)

        print(f"{key}: {status} {reason}")

    return {"processed": len(event["Records"])}
