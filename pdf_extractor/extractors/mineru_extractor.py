import os
import json
from pathlib import Path
from extractors.base import BaseExtractor, ExtractionResult

DEFAULT_CONFIG = {
    "bucket_info": {
        "bucket-name-1": ["ak", "sk", "endpoint"],
        "bucket-name-2": ["ak", "sk", "endpoint"]
    },
    "temp-output-dir": "/tmp/mineru_output",
    "models-dir": str(Path.home() / "magic-pdf-models"),
    "device-mode": "cpu",
    "layout-config": {
        "model": "doclayout_yolo"
    },
    "formula-config": {
        "mfd_model": "yolo_v8_mfd",
        "mfr_model": "unimernet_small",
        "enable": False
    },
    "table-config": {
        "model": "rapid_table",
        "enable": False,
        "max_time": 400
    }
}


def ensure_config():
    config_path = Path.home() / "magic-pdf.json"
    if not config_path.exists():
        with open(config_path, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)


class MinerUExtractor(BaseExtractor):
    def extract(self, pdf_path: str) -> ExtractionResult:
        try:
            ensure_config()

            from magic_pdf.data.data_reader_writer import FileBasedDataWriter, FileBasedDataReader
            from magic_pdf.data.dataset import PymuDocDataset
            from magic_pdf.model.doc_analyze_by_custom_model import doc_analyze
            from magic_pdf.config.enums import SupportedPdfParseMethod

            name = os.path.splitext(os.path.basename(pdf_path))[0]
            output_dir = "/tmp/mineru_output"
            os.makedirs(output_dir, exist_ok=True)

            pdf_bytes = FileBasedDataReader("").read(pdf_path)
            ds = PymuDocDataset(pdf_bytes)
            model_list = doc_analyze(ds, ocr=False)

            if ds.classify() == SupportedPdfParseMethod.OCR:
                pipe = ds.apply(lambda d: d.apply_ocr_model(model_list))
            else:
                pipe = ds.apply(lambda d: d.apply_layout_model(model_list))

            pipe.dump_markdown(FileBasedDataWriter(output_dir), name, output_dir)

            with open(os.path.join(output_dir, f"{name}.md"), "r", encoding="utf-8") as f:
                markdown = f.read()

            return ExtractionResult(engine="mineru", text=markdown, markdown=markdown, success=True)

        except Exception as ex:
            return ExtractionResult(engine="mineru", success=False, error=str(ex))
