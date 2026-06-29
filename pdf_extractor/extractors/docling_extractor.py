from extractors.base import BaseExtractor, ExtractionResult


class DoclingExtractor(BaseExtractor):
    def extract(self, pdf_path: str) -> ExtractionResult:
        try:
            from docling.document_converter import DocumentConverter

            converter = DocumentConverter()
            result    = converter.convert(pdf_path)
            doc       = result.document

            # Build per-page markdown by filtering elements per page
            pages_dict: dict = {}
            for element, _level in doc.iterate_items():
                prov = getattr(element, "prov", None) or []
                page_no = int(prov[0].page_no) if prov else 1
                idx = page_no - 1
                pages_dict.setdefault(idx, [])
                text = getattr(element, "text", None)
                if text:
                    pages_dict[idx].append(text.strip())

            pages = [
                {"index": idx, "markdown": "\n\n".join(lines), "images": []}
                for idx, lines in sorted(pages_dict.items())
            ]
            if not pages:
                pages = [{"index": 0, "markdown": doc.export_to_markdown(), "images": []}]

            markdown = doc.export_to_markdown()
            return ExtractionResult(
                engine="docling",
                text=markdown,
                markdown=markdown,
                json_data={"pages": pages},
                success=True,
            )

        except Exception as ex:
            return ExtractionResult(engine="docling", success=False, error=str(ex))
