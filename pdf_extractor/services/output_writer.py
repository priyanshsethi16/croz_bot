import json
import os
from extractors.base import ExtractionResult


class OutputWriter:
    def __init__(self, output_dir: str = "output"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def save(self, result: ExtractionResult) -> dict:
        name = result.engine
        md_path = os.path.join(self.output_dir, f"{name}.md")
        txt_path = os.path.join(self.output_dir, f"{name}.txt")
        html_path = os.path.join(self.output_dir, f"{name}.html")
        json_path = os.path.join(self.output_dir, f"{name}.json")

        with open(md_path, "w", encoding="utf-8") as f:
            f.write(result.markdown)

        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(result.text)

        if result.html:
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(result.html)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result.model_dump(), f, indent=2, ensure_ascii=False)

        files = {"markdown": md_path, "text": txt_path, "json": json_path}
        if result.html:
            files["html"] = html_path
        return files
