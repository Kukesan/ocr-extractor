# ocr-extraction-service

Standalone microservice that accepts a document (PDF or image) and returns extracted
text as JSON. It does OCR only — no auth, no field-extraction/analysis logic. Intended
to be called over HTTP by a separate backend.

The image also ships a second entrypoint, `app/worker.py`, an SQS-driven worker that
downloads S3 documents and writes OCR results into Supabase. It's a separate process
from the HTTP API — see [SQS worker](#sqs-worker) below — and is the only part of this
repo that touches S3 or a database.

## API

- `POST /extract` — multipart/form-data upload, field name `file`. Returns:
  ```json
  {
    "text": "...",
    "metadata": {
      "pages": [{"page_number": 1, "confidence": 0.85}],
      "overall_confidence": 0.85,
      "document_type": "other",
      "engine": "docling"
    }
  }
  ```
- `GET /health` — returns `200 OK` for use as the ECS/ALB health check.

## Extraction pipeline

1. Try [docling](https://github.com/DS4SD/docling)'s `DocumentConverter.convert()`,
   exported to markdown via `export_to_markdown()`. Runs in a thread
   (`asyncio.to_thread`) since docling is synchronous. Reported as
   `engine="docling"`, `overall_confidence=0.85`.
2. If docling raises, fall back to PaddleOCR (`use_angle_cls=True, lang="en"`),
   also via `asyncio.to_thread`. Per-line `(text, confidence)` pairs are joined
   with `\n` and confidences averaged. Reported as `engine="paddleocr"`.
3. If both engines raise, the failure is logged and the endpoint returns
   `{"text": "", "metadata": {"pages": [], "overall_confidence": 0.0, "document_type": "other", "engine": "none"}}`
   rather than raising an error.

`document_type` is always `"other"` — this service does no document classification;
it's a placeholder field kept for output-shape compatibility with the calling backend.

Both OCR engines are lazy singletons held on a single `OCRService` instance (via
`get_ocr_service()`) and reused across requests, since instantiating them is expensive.

### Why `paddleocr` is pinned below 3.0.0

PaddleOCR's Python API changed incompatibly between v2 and v3: v3 renamed/removed the
`cls` kwarg on `.ocr()` (raises `TypeError`) and changed the shape of the returned
result objects. This code targets the v2 API
(`PaddleOCR(use_angle_cls=True, lang="en")`, `.ocr(path, cls=True)`,
`[[box, (text, confidence)], ...]` results), so `requirements.txt` pins
`paddleocr>=2.8.1,<3.0.0`. Don't relax that upper bound without updating
`_run_paddleocr` in `app/ocr_service.py` to match the v3 API. `paddlepaddle` (the
actual inference engine) is also a required — but not automatically pulled in —
dependency of `paddleocr`, so it's listed explicitly, and is itself pinned to
`<3.0.0`: PaddlePaddle 3.x's oneDNN/PIR execution backend has known regressions
running the legacy v2-exported inference models PaddleOCR 2.x ships, which
surfaces as `OneDnnContext does not have the input Filter` / `fused_conv2d`
errors at inference time — pip installing an unpinned, latest `paddlepaddle`
alongside the pinned `paddleocr<3.0.0` reproduces this.

### Why docling has `torch.compile()` disabled

`app/ocr_service.py` sets `DOCLING_INFERENCE_COMPILE_TORCH_MODELS=false` before
importing docling. By default docling's object-detection layout model calls
`torch.compile()`, which needs a C/C++ compiler (`cl.exe` on Windows, `gcc`/`g++`
on Linux) to JIT the CPU kernels. Neither runtime image installs one (only
`libgl1`/`libglib2.0-0`/`libgomp1`, needed for OpenCV), so left at its default this
fails with `InvalidCxxCompiler: Compiler ... is not found` and docling falls
through to the PaddleOCR fallback (or, if that also fails, to `engine="none"`
with empty text).

## Running locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Docker

```bash
docker build -t ocr-extraction-service .
docker run -p 8080:8080 ocr-extraction-service
```

## SQS worker

`app/worker.py` long-polls an SQS queue fed by S3 `ObjectCreated` notifications
(bucket → SQS, or bucket → SNS → SQS as long as the message body is the standard
S3 event JSON with a top-level `Records` array). For each record it:

1. Parses the document id out of the object key, expected in one of two forms:
   `documents/{userId}/{documentId}/{filename}` or
   `documents/{userId}/{documentId}.{ext}` (document id as the filename stem).
2. Downloads the object from S3 to a temp file.
3. Runs it through the same `OCRService` used by `/extract` (in-process, no HTTP call).
4. Updates the `medical_documents` row where `id = {documentId}`:
   `processing_status = "extracted"`, `ocr_text = <extracted text>`.
5. Calls `POST {BACKEND_API_URL}/api/v1/documents/{documentId}/extract` with header
   `X-API-Key: {INTERNAL_SERVICE_API_KEY}` to kick off field-extraction on the backend.
   This call is best-effort: a failure here is logged but does not fail the SQS
   message, since OCR already succeeded and `ocr_text` is already persisted.
6. Deletes the SQS message on success. On failure, the row is set to
   `processing_status = "failed"`, the exception is logged, and the message is left
   on the queue to retry (route a redrive policy / DLQ at the queue level for
   poison messages).

Non-`ObjectCreated` events (e.g. deletes) and messages without a parseable `Records`/S3
payload are skipped or dropped rather than retried.

### Running the worker

Copy `.env.example` to `.env` and fill in real values — `worker.py` loads it
automatically via `python-dotenv` (`.env` is gitignored, never commit real
credentials into it):

```bash
cp .env.example .env
python -m app.worker
```

```bash
docker run \
  -e AWS_REGION=... \
  -e SQS_QUEUE_URL=... \
  -e SUPABASE_URL=... \
  -e SUPABASE_SERVICE_ROLE_KEY=... \
  -e BACKEND_API_URL=... \
  -e INTERNAL_SERVICE_API_KEY=... \
  ocr-extraction-service python -m app.worker
```

Required environment variables:

| Variable | Purpose |
| --- | --- |
| `AWS_REGION` | Region for the SQS and S3 clients |
| `SQS_QUEUE_URL` | Queue URL to poll |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Service-role key (bypasses RLS) used to update `medical_documents` |
| `BACKEND_API_URL` | Base URL of the backend service (e.g. `https://api.example.com`); if unset, the post-extraction notification is skipped with a warning |
| `INTERNAL_SERVICE_API_KEY` | Shared secret sent as `X-API-Key` when calling the backend's extract endpoint |

AWS credentials are picked up from the default boto3 chain (e.g. the ECS task role) —
no AWS keys are read from env vars directly.

On ECS/Fargate, run this as its own service (or task definition command override
pointing at the same image), separate from the HTTP API service, since it's a
long-running poller rather than a request handler.

## Deployment notes (AWS ECS / Fargate)

- **Resource sizing**: docling and PaddleOCR load large models into memory and are
  CPU-heavy, with a slow cold start (model download/initialization on first use).
  Size the ECS task definition at **minimum 2 vCPU / 4 GB memory**. Under-provisioning
  will manifest as OOM kills or requests timing out during model init.
- **No scale-to-zero**: keep the ECS service's minimum task count at **1** at all
  times. Because the OCR engines are initialized lazily on first request and are slow
  to construct, scaling to zero (or aggressive scale-in) causes the *first* request
  after a cold start to pay the full model-load cost, which can exceed typical ALB/
  client timeouts. A warm task avoids this.
- This service is stateless and holds no persistent data — horizontal scaling beyond
  the minimum warm task is just a matter of adding more tasks behind the ALB target
  group.
