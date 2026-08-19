"""Standalone SQS worker: pulls S3 ObjectCreated notifications off a queue,
downloads the object, OCRs it, and writes the result into Supabase.

Runs as its own long-lived process/container (see README), separate from the
FastAPI app in app/main.py. Started with `python -m app.worker`.
"""
import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

import boto3
from dotenv import load_dotenv
from supabase import Client, create_client

from app.ocr_service import get_ocr_service

load_dotenv()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

AWS_REGION = os.environ["AWS_REGION"]
SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

# Long enough for docling/PaddleOCR to finish on a large PDF before the
# message becomes visible again to another consumer.
VISIBILITY_TIMEOUT_SECONDS = 600
POLL_WAIT_TIME_SECONDS = 20

sqs = boto3.client("sqs", region_name=AWS_REGION)
s3 = boto3.client("s3", region_name=AWS_REGION)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def _document_id_from_key(key: str) -> str | None:
    # Accepts either documents/{userId}/{documentId}/{filename}
    # or documents/{userId}/{documentId}.{ext} (documentId as the filename stem).
    parts = key.split("/")
    if parts[0] != "documents":
        return None
    if len(parts) == 4:
        return parts[2]
    if len(parts) == 3:
        return Path(parts[2]).stem
    return None


async def _process_record(record: dict) -> None:
    key = record["s3"]["object"]["key"]
    bucket = record["s3"]["bucket"]["name"]

    document_id = _document_id_from_key(key)
    if document_id is None:
        logger.error("Could not parse document id from key %s; skipping", key)
        return

    fd, tmp_path_str = tempfile.mkstemp(suffix=Path(key).suffix)
    os.close(fd)
    tmp_path = Path(tmp_path_str)

    try:
        await asyncio.to_thread(s3.download_file, bucket, key, str(tmp_path))
        result = await get_ocr_service().extract_text(tmp_path)

        if not result["text"]:
            logger.error(
                "OCR produced no text for document %s (engine=%s); marking failed",
                document_id,
                result["metadata"]["engine"],
            )
            supabase.table("medical_documents").update(
                {"processing_status": "failed"}
            ).eq("id", document_id).execute()
            return

        supabase.table("medical_documents").update(
            {
                "processing_status": "extracted",
                "ocr_text": result["text"],
            }
        ).eq("id", document_id).execute()

        logger.info(
            "Extracted text for document %s (%d chars, engine=%s)",
            document_id,
            len(result["text"]),
            result["metadata"]["engine"],
        )
    except Exception:
        logger.exception("Failed to process s3://%s/%s (document %s)", bucket, key, document_id)
        supabase.table("medical_documents").update(
            {"processing_status": "failed"}
        ).eq("id", document_id).execute()
        raise
    finally:
        tmp_path.unlink(missing_ok=True)


async def _handle_message(message: dict) -> bool:
    """Returns True if the message should be deleted from the queue."""
    try:
        body = json.loads(message["Body"])
    except (KeyError, json.JSONDecodeError):
        logger.exception("Malformed SQS message body, dropping: %s", message.get("Body"))
        return True

    records = [r for r in body.get("Records", []) if "s3" in r]
    if not records:
        logger.warning("SQS message had no S3 Records, dropping: %s", body)
        return True

    all_ok = True
    for record in records:
        if not record.get("eventName", "").startswith("ObjectCreated"):
            continue
        try:
            await _process_record(record)
        except Exception:
            all_ok = False
    return all_ok


async def run() -> None:
    logger.info("Worker started, polling %s", SQS_QUEUE_URL)
    while True:
        response = await asyncio.to_thread(
            sqs.receive_message,
            QueueUrl=SQS_QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=POLL_WAIT_TIME_SECONDS,
            VisibilityTimeout=VISIBILITY_TIMEOUT_SECONDS,
        )
        for message in response.get("Messages", []):
            if await _handle_message(message):
                await asyncio.to_thread(
                    sqs.delete_message,
                    QueueUrl=SQS_QUEUE_URL,
                    ReceiptHandle=message["ReceiptHandle"],
                )
            else:
                logger.error(
                    "Leaving message %s on queue for retry", message.get("MessageId")
                )


if __name__ == "__main__":
    asyncio.run(run())
