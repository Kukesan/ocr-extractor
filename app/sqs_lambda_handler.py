"""Lambda entrypoint for an SQS event source mapping.

Same processing as the long-polling worker (app/worker.py), but Lambda owns
receiving and deleting messages. Returns a partial batch response, so the
event source mapping must have `ReportBatchItemFailures` enabled. Otherwise a
single failed record makes Lambda retry the whole batch.
"""
import asyncio
import logging
import os

from app.worker import handle_message_body

logger = logging.getLogger(__name__)
# Lambda's runtime installs its own root handler before our code runs, which
# turns logging.basicConfig in app.worker into a no-op. Set the level directly.
logging.getLogger().setLevel(os.getenv("LOG_LEVEL", "INFO"))

# One event loop for the life of the execution environment, reused across
# warm invocations.
_loop = asyncio.new_event_loop()


async def _process_batch(records: list[dict]) -> list[dict]:
    failures: list[dict] = []
    for record in records:
        message_id = record["messageId"]
        try:
            ok = await handle_message_body(record.get("body"))
        except Exception:
            logger.exception("Unhandled error processing SQS message %s", message_id)
            ok = False
        if not ok:
            logger.error("Reporting SQS message %s as failed for retry", message_id)
            failures.append({"itemIdentifier": message_id})
    return failures


def handler(event: dict, context) -> dict:
    records = event.get("Records", [])
    logger.info("Received %d SQS record(s)", len(records))
    failures = _loop.run_until_complete(_process_batch(records))
    return {"batchItemFailures": failures}
