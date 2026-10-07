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
   `processing_status = "ocr_completed"`, `ocr_text = <extracted text>`,
   `ocr_metadata = <engine/confidence metadata>`.
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

## Deploying to AWS Lambda (SQS trigger)

`Dockerfile.lambda` builds a Lambda container image whose entrypoint is
`app.sqs_lambda_handler.handler`. Lambda polls the queue itself through an event
source mapping and calls the handler with batches of messages. Each message goes
through the same `handle_message_body` the polling worker uses. The handler returns
a partial batch response (`batchItemFailures`), so only failed messages go back on
the queue. Lambda deletes the successful ones.

The image bakes all OCR models in at build time (`python -m app.prefetch_models`),
because Lambda's filesystem is read-only outside `/tmp` and the engines would
otherwise try to download models into `~` on first use. If either engine fails
during that step, the build fails.

### 1. Build and push to ECR

Lambda only accepts single-platform Docker v2 manifests, so build for
`linux/amd64` with provenance attestations disabled:

```bash
AWS_REGION=ap-south-1
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REPO=ocr-extractor
IMAGE_URI=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO:latest

aws ecr create-repository --repository-name $REPO --region $AWS_REGION  # first time only
aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

docker build --platform linux/amd64 --provenance=false -f Dockerfile.lambda -t $IMAGE_URI .
docker push $IMAGE_URI
```

### 2. Execution role

Create a role that `lambda.amazonaws.com` can assume. Attach the managed policy
`AWSLambdaBasicExecutionRole` (for CloudWatch Logs), plus:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
      "Resource": "arn:aws:sqs:<region>:<account-id>:<queue-name>"
    },
    {
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::<bucket-name>/documents/*"
    }
  ]
}
```

If the queue uses a customer-managed KMS key, the role also needs `kms:Decrypt` on that key.

### 3. Create the function

```bash
aws lambda create-function \
  --function-name ocr-extractor \
  --package-type Image \
  --code ImageUri=$IMAGE_URI \
  --role arn:aws:iam::$ACCOUNT_ID:role/<ocr-extractor-role> \
  --architectures x86_64 \
  --memory-size 10240 \
  --timeout 900 \
  --ephemeral-storage Size=2048 \
  --environment "Variables={SUPABASE_URL=...,SUPABASE_SERVICE_ROLE_KEY=...,BACKEND_API_URL=...,INTERNAL_SERVICE_API_KEY=...,LOG_LEVEL=INFO}" \
  --region $AWS_REGION
```

- **Memory**: Lambda allocates CPU in proportion to memory. At 10240 MB you get
  6 vCPU, and docling/PaddleOCR are CPU-bound, so lower memory means much slower
  OCR. 6144 MB should be the lowest you try.
- **Timeout**: 900 s (15 min) is the Lambda maximum. A document that takes longer
  than that to OCR can't be processed on Lambda.
- **Ephemeral storage**: `/tmp` holds the downloaded document. Raise the 512 MB
  default if you expect large PDFs.
- Don't set `AWS_REGION` (Lambda sets it) or AWS access keys (the execution role
  provides credentials). `SQS_QUEUE_URL` isn't needed either, because the event
  source mapping owns the queue.
- Cold starts are slow. The first invocation in a new execution environment imports
  torch/docling and loads the models, which can take tens of seconds. Warm
  invocations reuse the loaded engines.

To ship a new image later:

```bash
aws lambda update-function-code --function-name ocr-extractor --image-uri $IMAGE_URI
```

### 4. Configure the queue and connect it

On the SQS queue:

- Set the **visibility timeout to at least 6× the function timeout** (5400 s for a
  900 s timeout). AWS recommends this so a message doesn't reappear while Lambda is
  still retrying or throttled.
- Add a **dead-letter queue** with a redrive `maxReceiveCount` (e.g. 3). Without one,
  a document that keeps failing OCR is retried until the message expires.

Then create the event source mapping:

```bash
aws lambda create-event-source-mapping \
  --function-name ocr-extractor \
  --event-source-arn arn:aws:sqs:$AWS_REGION:$ACCOUNT_ID:<queue-name> \
  --batch-size 1 \
  --function-response-types ReportBatchItemFailures \
  --scaling-config MaximumConcurrency=5 \
  --region $AWS_REGION
```

- `--batch-size 1` gives each document its own invocation, with the full 15 minutes
  to itself.
- `ReportBatchItemFailures` is **required**. The handler signals failures through
  `batchItemFailures` and doesn't raise.
- `MaximumConcurrency` limits how many documents are OCR'd in parallel. That bounds
  the cost and the load on Supabase and the backend. The minimum allowed value is 2.

The S3 bucket → SQS notification is the same one the polling worker uses (see
[SQS worker](#sqs-worker)). Run the polling worker or the Lambda against a queue,
not both.

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
