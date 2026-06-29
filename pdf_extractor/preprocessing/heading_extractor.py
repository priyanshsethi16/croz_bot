import re
from typing import List, Dict
from extractors.base import ExtractionResult


class HeadingExtractor:
    """Extract headings from OCR markdown with support for multi-line titles."""

    MAX_CONTINUATION_LINES = 2
    MAX_CONTINUATION_WORDS = 8

    TABLE_KEYWORDS = {
        "SPECIFICATIONS",
        "ORDERING INFORMATION",
        "ORDERING",
        "FEATURES",
        "UTILITY",
        "APPLICATIONS",
        "MAINTENANCE",
        "WARNING",
        "CAUTION",
        "NOTE",
        "NOTES",
        "TECHNICAL DATA",
        "WORKING TEMPERATURE RANGE",
        "CAT NO",
        "BOX QTY",
        "HEAD WEIGHT",
        "OVERALL LENGTH",
        "DIMENSIONS",
    }

    def is_heading_continuation(self, line: str) -> bool:
        """
        Decide whether a line is likely a continuation of the previous heading.
        """

        line = line.strip()

        if not line:
            return False

        # another markdown heading
        if line.startswith("#"):
            return False

        # bullets
        if line.startswith(("-", "*", "•")):
            return False

        # numbered list
        if re.match(r"^\d+[\.\)]", line):
            return False

        # long sentence -> body paragraph
        if len(line.split()) > self.MAX_CONTINUATION_WORDS:
            return False

        # sentence
        if line.endswith("."):
            return False

        # urls
        if "http" in line.lower() or "www." in line.lower():
            return False

        # table separator
        if "|" in line:
            return False

        # markdown table
        if "---" in line:
            return False

        # markdown image
        if "![" in line:
            return False

        # common section names
        if line.upper() in self.TABLE_KEYWORDS:
            return False

        # common caption words
        caption_keywords = [
            "STEEL LOCKING PLATE",
            "SPRING STEEL BARS",
            "ERGONOMIC RUBBER GRIP HANDLE",
            "HEAD",
            "HANDLE",
        ]

        if line.upper() in caption_keywords:
            return False

        # mostly numeric
        alpha = sum(c.isalpha() for c in line)
        digit = sum(c.isdigit() for c in line)

        if digit > alpha:
            return False

        return True

    def merge_heading(self, heading: str, start_index: int, lines: List[str]):
        """
        Merge continuation lines following a markdown heading.
        """

        merged = heading
        consumed = []

        for offset in range(1, self.MAX_CONTINUATION_LINES + 1):

            idx = start_index + offset

            if idx >= len(lines):
                break

            candidate = lines[idx].strip()

            if self.is_heading_continuation(candidate):
                merged += " " + candidate
                consumed.append(idx)
            else:
                break

        return merged, consumed

    def extract_headings(self, text: str) -> List[Dict[str, str]]:
        headings = []

        lines = text.split("\n")

        for idx, raw_line in enumerate(lines):

            line = raw_line.strip()

            if not line:
                continue

            # -----------------------------
            # Markdown Heading
            # -----------------------------
            match = re.match(r"^(#{1,6})\s+(.+)$", line)

            if match:

                level = len(match.group(1))
                heading = match.group(2).strip()

                merged_heading, continuation = self.merge_heading(
                    heading,
                    idx,
                    lines,
                )

                headings.append(
                    {
                        "level": level,
                        "text": merged_heading,
                        "original_text": heading,
                        "line_number": idx + 1,
                        "continuation_lines": [i + 1 for i in continuation],
                    }
                )

                continue

            # -----------------------------
            # ALL CAPS headings
            # -----------------------------
            if (
                len(line) >= 10
                and re.fullmatch(r"[A-Z0-9&°/\-\s]+", line)
            ):

                if any(
                    skip in line
                    for skip in [
                        "PSI",
                        "BAR",
                        "MM",
                        "KG",
                        "LB",
                        "WWW",
                        "HTTP",
                    ]
                ):
                    continue

                headings.append(
                    {
                        "level": 1,
                        "text": line,
                        "original_text": line,
                        "line_number": idx + 1,
                        "continuation_lines": [],
                    }
                )

        return headings

    def format_headings(self, headings: List[Dict[str, str]]) -> str:

        output = []

        for h in headings:

            indent = "  " * (h["level"] - 1)

            output.append(
                f"{indent}{'#'*h['level']} {h['text']} (line {h['line_number']})"
            )

        return "\n".join(output)

    def save_headings(self, headings: List[Dict[str, str]], output_path: str):

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(self.format_headings(headings))

    def extract_and_save(
        self,
        result: ExtractionResult,
        output_path: str,
    ) -> List[Dict[str, str]]:

        headings = self.extract_headings(result.markdown)
        self.save_headings(headings, output_path)

        return headings