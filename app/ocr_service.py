from pathlib import Path

import asyncio
import logging

from docling.document_converter import DocumentConverter
from paddleocr import PaddleOCR

logger = logging.getLogger(__name__)


class OCRService:
    def __init__(self) -> None:
        self._docling_converter = None
        self._paddle_ocr = None

    async def extract_text(self, file_path: Path) -> dict:
        docling_result = await self._run_docling(file_path)
        if docling_result is not None:
            return docling_result

        paddle_result = await self._run_paddleocr(file_path)
        if paddle_result is not None:
            return paddle_result

        logger.error("All OCR engines failed for %s; returning empty text", file_path)
        return {
            "text": "",
            "metadata": {
                "pages": [],
                "overall_confidence": 0.0,
                "document_type": "other",
                "engine": "none",
            },
        }

    async def _run_docling(self, file_path: Path) -> dict | None:
        try:
            if self._docling_converter is None:
                self._docling_converter = await asyncio.to_thread(DocumentConverter)
            result = await asyncio.to_thread(
                self._docling_converter.convert,
                str(file_path)
            )
            text = result.document.export_to_markdown()
            return {
                "text": text,
                "metadata": {
                    "pages": [{"page_number": 1, "confidence": 0.85}],
                    "overall_confidence": 0.85,
                    "document_type": "other",
                    "engine": "docling",
                },
            }
        except Exception:
            logger.exception("Docling OCR failed for %s", file_path)
            return None

    async def _run_paddleocr(self, file_path: Path) -> dict | None:
        try:
            if self._paddle_ocr is None:
                self._paddle_ocr = await asyncio.to_thread(PaddleOCR, use_angle_cls=True, lang="en")
            result = await asyncio.to_thread(self._paddle_ocr.ocr, str(file_path), cls=True)
            lines: list[str] = []
            confidences: list[float] = []
            for page in result:
                for item in page:
                    if not item or len(item) < 2:
                        continue
                    txt = item[1][0]
                    conf = float(item[1][1])
                    lines.append(txt)
                    confidences.append(conf)
            overall = sum(confidences) / len(confidences) if confidences else 0.0
            return {
                "text": "\n".join(lines),
                "metadata": {
                    "pages": [{"page_number": 1, "confidence": overall}],
                    "overall_confidence": overall,
                    "document_type": "other",
                    "engine": "paddleocr",
                },
            }
        except Exception:
            logger.exception("PaddleOCR failed for %s", file_path)
            return None


_ocr_service: OCRService | None = None


def get_ocr_service() -> OCRService:
    global _ocr_service
    if _ocr_service is None:
        _ocr_service = OCRService()
    return _ocr_service
