"""Build-time step for the Lambda image: run both OCR engines once on a
generated image so every model they lazily download (docling layout/OCR,
PaddleOCR det/rec/cls) is baked into the image under $HOME.

Lambda's filesystem is read-only outside /tmp, so these downloads would fail
at runtime. Run with `python -m app.prefetch_models`; exits non-zero if either
engine fails, so a broken image never gets built.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from app.ocr_service import get_ocr_service


def _sample_image(path: Path) -> None:
    image = Image.new("RGB", (800, 200), "white")
    ImageDraw.Draw(image).text((40, 80), "Patient: John Doe  Hemoglobin 13.5 g/dL", fill="black")
    image.save(path)


async def main() -> int:
    service = get_ocr_service()
    with tempfile.TemporaryDirectory() as tmp_dir:
        sample = Path(tmp_dir) / "sample.png"
        _sample_image(sample)

        failed = False
        for name, run in (("docling", service._run_docling), ("paddleocr", service._run_paddleocr)):
            result = await run(sample)
            if result is None:
                print(f"{name} failed during model prefetch", file=sys.stderr)
                failed = True
            else:
                print(f"{name} ok: {result['text'][:80]!r}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
