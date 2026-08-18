import logging
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse

from app.ocr_service import get_ocr_service

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = FastAPI(title="ocr-extraction-service")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    suffix = os.path.splitext(file.filename or "")[1]
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)

    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(await file.read())

        result = await get_ocr_service().extract_text(Path(tmp_path))
        return JSONResponse(content=result)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            logger.exception("failed to remove temp file %s", tmp_path)
