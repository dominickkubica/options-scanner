"""Build a PDF from a Markdown document in docs/, so the Markdown stays the source.

    venv\\Scripts\\python.exe scripts\\build_doc.py docs\\design\\project-map.md
    -> dist\\project-map.pdf

The PDF is a build output, never edited and never committed: change the Markdown and
rebuild. Converts the subset of Markdown the docs use (headings, paragraphs, tables,
lists, fenced code, images, inline code, bold, italic) without a dependency, then prints
the page with headless Edge, which every Windows machine here has.
"""

from __future__ import annotations

import html
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EDGE_PATHS = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]

CSS = """
@page { size: Letter; margin: 18mm 16mm; }
body { font-family: "Segoe UI", Helvetica, Arial, sans-serif; font-size: 10.5pt;
       line-height: 1.45; color: #1f2328; }
h1 { font-size: 20pt; margin: 0 0 10pt; }
h2 { font-size: 14pt; margin: 18pt 0 6pt; border-bottom: 1px solid #d0d7de;
     padding-bottom: 3pt; break-after: avoid; }
h3 { font-size: 11.5pt; margin: 12pt 0 4pt; break-after: avoid; }
p, ul, ol { margin: 0 0 7pt; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0 10pt; font-size: 9.5pt;
        break-inside: avoid; }
th, td { border: 1px solid #d0d7de; padding: 3pt 5pt; text-align: left;
         vertical-align: top; }
th { background: #f3f4f6; }
code { font-family: Consolas, monospace; font-size: 9pt; background: #f3f4f6;
       padding: 0 2pt; border-radius: 2pt; }
pre { background: #f3f4f6; padding: 6pt 8pt; border-radius: 4pt; font-size: 9pt;
      white-space: pre-wrap; break-inside: avoid; }
pre code { background: none; padding: 0; }
img { width: 100%; margin: 4pt 0 8pt; }
"""


def inline(text: str) -> str:
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
            continue
        escaped = html.escape(part)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        out.append(re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"<em>\1</em>", escaped))
    return "".join(out)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _table(lines: list[str], index: int, out: list[str]) -> int:
    rows = []
    while index < len(lines) and lines[index].startswith("|"):
        rows.append(_cells(lines[index]))
        index += 1
    head, body = rows[0], rows[2:]
    table = "<table>"
    if any(head):
        table += "<tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr>"
    for row in body:
        table += "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>"
    out.append(table + "</table>")
    return index


def _list(lines: list[str], index: int, out: list[str]) -> int:
    ordered = bool(re.match(r"\d+\. ", lines[index]))
    items: list[str] = []
    while index < len(lines) and lines[index].strip():
        current = lines[index]
        if re.match(r"(- |\d+\. )", current):
            items.append(re.sub(r"^(- |\d+\. )", "", current))
        else:
            items[-1] += " " + current.strip()
        index += 1
    tag = "ol" if ordered else "ul"
    out.append(f"<{tag}>" + "".join(f"<li>{inline(i)}</li>" for i in items) + f"</{tag}>")
    return index


def to_html(markdown: str, base: Path) -> str:
    lines = markdown.splitlines()
    out, index = [], 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
        elif line.startswith("```"):
            end = index + 1
            while not lines[end].startswith("```"):
                end += 1
            code = html.escape("\n".join(lines[index + 1 : end]))
            out.append(f"<pre><code>{code}</code></pre>")
            index = end + 1
        elif match := re.match(r"(#{1,3}) (.*)", line):
            level = len(match.group(1))
            out.append(f"<h{level}>{inline(match.group(2))}</h{level}>")
            index += 1
        elif match := re.match(r"!\[(.*)\]\((.*)\)", line):
            src = (base / match.group(2)).resolve().as_uri()
            out.append(f'<img src="{src}" alt="{html.escape(match.group(1))}">')
            index += 1
        elif line.startswith("|"):
            index = _table(lines, index, out)
        elif re.match(r"(- |\d+\. )", line):
            index = _list(lines, index, out)
        else:
            paragraph = []
            while (
                index < len(lines)
                and lines[index].strip()
                and not re.match(r"(#|\||```|- |\d+\. |!\[)", lines[index])
            ):
                paragraph.append(lines[index].strip())
                index += 1
            out.append(f"<p>{inline(' '.join(paragraph))}</p>")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    source = (REPO / argv[0]).resolve() if not Path(argv[0]).is_absolute() else Path(argv[0])
    edge = next((path for path in EDGE_PATHS if path.exists()), None)
    if edge is None:
        print("Microsoft Edge not found; it prints the PDF.")
        return 1
    body = to_html(source.read_text(encoding="utf-8"), source.parent)
    title = html.escape(source.stem)
    page = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )
    target = REPO / "dist" / f"{source.stem}.pdf"
    target.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        page_path = Path(folder) / f"{source.stem}.html"
        page_path.write_text(page, encoding="utf-8")
        subprocess.run(
            [
                str(edge),
                # The old headless mode exits 0 without writing anything.
                "--headless=new",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--user-data-dir={Path(folder) / 'profile'}",
                f"--print-to-pdf={target}",
                page_path.as_uri(),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        # Edge's launcher returns before its renderer has written the file, and leaving
        # this block deletes the page it is still printing.
        deadline = time.monotonic() + 60
        while not target.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        time.sleep(1)
    if not target.exists():
        print("Edge did not write the PDF.")
        return 1
    print(f"Built {target.relative_to(REPO)} from {source.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
