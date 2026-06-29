"""MinerU Cloud API extractor — uploads PDF, polls until done, returns markdown."""
import os
import time
import zipfile
import io
import requests
from extractors.base import BaseExtractor, ExtractionResult

_BASE_V4       = "https://mineru.net/api/v4"
_POLL_INTERVAL = 5    # seconds between status checks
_TIMEOUT       = 300  # max seconds to wait


def _headers():
    token = os.getenv("MINERU_API_KEY")
    if not token:
        raise ValueError("MINERU_API_KEY missing in .env")
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


def _upload_and_get_task_id(pdf_path: str) -> str:
    filename = os.path.basename(pdf_path)

    # Use VLM model (95-96% accuracy, balanced)
    # Valid options: "vlm" (balanced, recommended), "auto" (API decides)
    # Note: "ocr" mentioned in docs but currently rejected by API
    model_version = os.getenv("MINERU_MODEL_VERSION", "vlm")
    
    # Step 1: Get presigned upload URL
    res = requests.post(
        f"{_BASE_V4}/file-urls/batch",
        headers=_headers(),
        json={"files": [{"name": filename}], "model_version": model_version},
    )
    res.raise_for_status()
    data = res.json()
    if data["code"] != 0:
        raise RuntimeError(f"MinerU upload URL failed: {data['msg']}")

    batch_id   = data["data"]["batch_id"]
    upload_url = data["data"]["file_urls"][0]

    # Step 2: Upload file (no Content-Type header per MinerU docs)
    with open(pdf_path, "rb") as f:
        up = requests.put(upload_url, data=f)
    if up.status_code != 200:
        raise RuntimeError(f"MinerU file upload failed: {up.status_code}")

    return batch_id


def _poll_batch(batch_id: str) -> str:
    """Poll batch status until done, return full_zip_url."""
    deadline = time.time() + _TIMEOUT
    while time.time() < deadline:
        res = requests.get(
            f"{_BASE_V4}/extract-results/batch/{batch_id}",
            headers=_headers(),
        )
        res.raise_for_status()
        data = res.json().get("data", {})
        tasks = data.get("extract_result", [])

        if not tasks:
            time.sleep(_POLL_INTERVAL)
            continue

        task = tasks[0]
        state = task.get("state")

        if state == "done":
            return task["full_zip_url"]
        elif state == "failed":
            raise RuntimeError(f"MinerU extraction failed: {task.get('err_msg')}")

        time.sleep(_POLL_INTERVAL)

    raise TimeoutError(f"MinerU extraction timed out after {_TIMEOUT}s")


def _extract_markdown_from_zip(zip_url: str) -> str:
    """Download zip and extract full.md content."""
    res = requests.get(zip_url)
    res.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        for name in z.namelist():
            if name.endswith("full.md"):
                return z.read(name).decode("utf-8")
    raise RuntimeError("full.md not found in MinerU result zip")


def _build_result(engine: str, markdown: str) -> ExtractionResult:
    # Convert markdown to HTML
    try:
        import markdown as md_lib
        html = md_lib.markdown(markdown, extensions=['tables', 'fenced_code'])
    except ImportError:
        html = ""
    
    pages = [{"index": i, "markdown": p.strip(), "images": []}
             for i, p in enumerate(markdown.split("\n\n\n")) if p.strip()]
    return ExtractionResult(
        engine=engine,
        text=markdown,
        markdown=markdown,
        html=html,
        json_data={"pages": pages},
        success=True,
    )


class MinerUExtractor(BaseExtractor):
    """MinerU Cloud API — VLM model, no local GPU needed."""

    def extract(self, pdf_path: str) -> ExtractionResult:
        try:
            print("   📤 Uploading to MinerU...", end="", flush=True)
            batch_id = _upload_and_get_task_id(pdf_path)
            print(f" batch_id={batch_id}")

            print("   ⏳ Waiting for MinerU to process...", end="", flush=True)
            zip_url = _poll_batch(batch_id)
            print(" done")

            markdown = _extract_markdown_from_zip(zip_url)
            return _build_result("mineru", markdown)
        except Exception as ex:
            import traceback
            traceback.print_exc()
            return ExtractionResult(engine="mineru", success=False, error=str(ex))
