"""Build docs/QuakeNet_Project_Report.pdf from docs/PROJECT_REPORT.md.

Expands `{{include path}}` markers with the repository's configuration files so the report can never
drift from configs/. Requires pandoc and wkhtmltopdf (documentation build only).

    python -m tools.build_report
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def expand(md: str) -> str:
    def repl(m: re.Match) -> str:
        rel = m.group(1).strip()
        text = (ROOT / rel).read_text().rstrip()
        name = Path(rel).stem
        return f"## {name} configuration\n\n```\n{text}\n```\n"
    return re.sub(r"\{\{include (.+?)\}\}", repl, md)


def main() -> int:
    src = expand((DOCS / "PROJECT_REPORT.md").read_text())
    tmp_md = DOCS / "_report_expanded.md"
    tmp_html = DOCS / "_report.html"
    tmp_md.write_text(src)
    try:
        subprocess.run(["pandoc", str(tmp_md), "-f", "markdown+pipe_tables", "-t", "html5", "-s",
                        "--css", "report.css", "--metadata", "pagetitle=QuakeNet",
                        "--resource-path", str(DOCS), "-o", str(tmp_html)], check=True, cwd=DOCS)
        out = DOCS / "QuakeNet_Project_Report.pdf"
        subprocess.run(["wkhtmltopdf", "--quiet", "--enable-local-file-access", "--page-size", "A4",
                        "-T", "16", "-B", "18", "-L", "14", "-R", "14",
                        "--title", "QuakeNet — Project Report", str(tmp_html), str(out)], check=True, cwd=DOCS)
    finally:
        tmp_md.unlink(missing_ok=True)
        tmp_html.unlink(missing_ok=True)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
