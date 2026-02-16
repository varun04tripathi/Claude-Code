#!/usr/bin/env python3
"""Convert Microsoft Word (.docx) files to Markdown format.

Usage:
    python docx_to_markdown.py input.docx                  # outputs input.md
    python docx_to_markdown.py input.docx -o output.md     # outputs output.md
    python docx_to_markdown.py docs/                       # converts all .docx in directory
    python docx_to_markdown.py docs/ -o converted/         # converts all .docx, output to converted/
"""

import argparse
import os
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.table import Table


def extract_image_info(paragraph):
    """Check if a paragraph contains an inline image."""
    inline_shapes = paragraph._element.findall(
        ".//" + qn("wp:inline") + "//" + qn("a:blip")
    )
    blip_fills = paragraph._element.findall(
        ".//" + qn("wp:anchor") + "//" + qn("a:blip")
    )
    return len(inline_shapes) + len(blip_fills) > 0


def get_hyperlinks(paragraph):
    """Extract hyperlinks from a paragraph."""
    hyperlinks = {}
    rels = paragraph.part.rels

    for hyperlink in paragraph._element.findall(qn("w:hyperlink")):
        r_id = hyperlink.get(qn("r:id"))
        if r_id and r_id in rels:
            url = rels[r_id].target_ref
            text_parts = []
            for run_elem in hyperlink.findall(qn("w:r")):
                for text_elem in run_elem.findall(qn("w:t")):
                    if text_elem.text:
                        text_parts.append(text_elem.text)
            link_text = "".join(text_parts)
            if link_text:
                hyperlinks[link_text] = url

    return hyperlinks


def run_to_markdown(run):
    """Convert a single run of text to markdown with formatting."""
    text = run.text
    if not text:
        return ""

    # Apply formatting
    if run.bold and run.italic:
        text = f"***{text}***"
    elif run.bold:
        text = f"**{text}**"
    elif run.italic:
        text = f"*{text}*"

    if run.underline:
        text = f"<u>{text}</u>"

    if run.font.strike:
        text = f"~~{text}~~"

    if run.font.superscript:
        text = f"<sup>{text}</sup>"

    if run.font.subscript:
        text = f"<sub>{text}</sub>"

    if run.font.cs_bold or (run.font.name and "code" in run.font.name.lower()):
        # Heuristic: monospace fonts often indicate inline code
        pass

    return text


def paragraph_to_markdown(paragraph, list_state):
    """Convert a single paragraph to markdown.

    Args:
        paragraph: A python-docx Paragraph object.
        list_state: Dict tracking current list numbering.

    Returns:
        A string of markdown text for this paragraph.
    """
    style_name = paragraph.style.name.lower() if paragraph.style else ""

    # Handle headings
    if style_name.startswith("heading"):
        try:
            level = int(style_name.replace("heading", "").strip())
            level = min(level, 6)
        except ValueError:
            level = 1
        text = get_paragraph_text_with_formatting(paragraph)
        return f"{'#' * level} {text}"

    # Handle title
    if style_name == "title":
        text = get_paragraph_text_with_formatting(paragraph)
        return f"# {text}"

    # Handle subtitle
    if style_name == "subtitle":
        text = get_paragraph_text_with_formatting(paragraph)
        return f"## {text}"

    # Handle list items
    numpr = paragraph._element.find(qn("w:pPr"))
    is_list = False
    is_ordered = False
    indent_level = 0

    if numpr is not None:
        num_elem = numpr.find(qn("w:numPr"))
        if num_elem is not None:
            is_list = True
            ilvl = num_elem.find(qn("w:ilvl"))
            if ilvl is not None:
                indent_level = int(ilvl.get(qn("w:val"), "0"))

            num_id_elem = num_elem.find(qn("w:numId"))
            if num_id_elem is not None:
                num_id = num_id_elem.get(qn("w:val"), "0")
                # Try to detect ordered vs unordered from style name
                if "list number" in style_name or "numbered" in style_name:
                    is_ordered = True
                elif "list bullet" in style_name or "bullet" in style_name:
                    is_ordered = False
                else:
                    # Fallback heuristic: try to detect from the numbering
                    is_ordered = "number" in style_name or "ordered" in style_name

    if is_list or "list" in style_name:
        text = get_paragraph_text_with_formatting(paragraph)
        indent = "  " * indent_level

        if is_ordered or "list number" in style_name:
            key = f"ordered_{indent_level}"
            list_state[key] = list_state.get(key, 0) + 1
            return f"{indent}{list_state[key]}. {text}"
        else:
            return f"{indent}- {text}"
    else:
        # Reset list counters when we leave a list
        list_state.clear()

    # Handle block quotes
    if "quote" in style_name or "block" in style_name:
        text = get_paragraph_text_with_formatting(paragraph)
        lines = text.split("\n")
        return "\n".join(f"> {line}" for line in lines)

    # Handle code blocks (No Spacing or styles with "code" in name)
    if "code" in style_name:
        return f"    {paragraph.text}"

    # Handle images
    if extract_image_info(paragraph):
        return "![image](image)"

    # Default: regular paragraph
    text = get_paragraph_text_with_formatting(paragraph)
    return text


def get_paragraph_text_with_formatting(paragraph):
    """Get paragraph text with inline markdown formatting and hyperlinks."""
    hyperlinks = get_hyperlinks(paragraph)
    parts = []

    for child in paragraph._element:
        if child.tag == qn("w:r"):
            # Regular run
            from docx.text.run import Run

            run = Run(child, paragraph)
            parts.append(run_to_markdown(run))
        elif child.tag == qn("w:hyperlink"):
            # Hyperlink
            text_parts = []
            for run_elem in child.findall(qn("w:r")):
                for text_elem in run_elem.findall(qn("w:t")):
                    if text_elem.text:
                        text_parts.append(text_elem.text)
            link_text = "".join(text_parts)
            if link_text in hyperlinks:
                parts.append(f"[{link_text}]({hyperlinks[link_text]})")
            else:
                parts.append(link_text)

    return "".join(parts)


def table_to_markdown(table):
    """Convert a Word table to a markdown table.

    Args:
        table: A python-docx Table object.

    Returns:
        A string of markdown representing the table.
    """
    rows = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            # Get cell text, replace newlines with <br>
            cell_text = cell.text.strip().replace("\n", "<br>")
            # Escape pipe characters in cell content
            cell_text = cell_text.replace("|", "\\|")
            cells.append(cell_text)
        rows.append(cells)

    if not rows:
        return ""

    lines = []
    # Header row
    lines.append("| " + " | ".join(rows[0]) + " |")
    # Separator
    lines.append("| " + " | ".join("---" for _ in rows[0]) + " |")
    # Data rows
    for row in rows[1:]:
        # Pad row if it has fewer cells than header
        while len(row) < len(rows[0]):
            row.append("")
        lines.append("| " + " | ".join(row[: len(rows[0])]) + " |")

    return "\n".join(lines)


def convert_docx_to_markdown(docx_path):
    """Convert a .docx file to markdown text.

    Args:
        docx_path: Path to the .docx file.

    Returns:
        A string containing the markdown conversion of the document.
    """
    doc = Document(docx_path)
    markdown_parts = []
    list_state = {}

    # Iterate through the document body elements in order
    for element in doc.element.body:
        if element.tag == qn("w:p"):
            # Paragraph
            from docx.text.paragraph import Paragraph

            paragraph = Paragraph(element, doc)
            md = paragraph_to_markdown(paragraph, list_state)
            markdown_parts.append(md)
        elif element.tag == qn("w:tbl"):
            # Table
            table = Table(element, doc)
            md = table_to_markdown(table)
            markdown_parts.append(md)
            list_state.clear()

    # Join parts and clean up excessive blank lines
    result = "\n\n".join(part for part in markdown_parts if part is not None)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip() + "\n"


def get_output_path(input_path, output_arg):
    """Determine the output file path.

    Args:
        input_path: Path to the input .docx file.
        output_arg: User-specified output path (file or directory), or None.

    Returns:
        A Path object for the output .md file.
    """
    input_path = Path(input_path)
    md_name = input_path.stem + ".md"

    if output_arg is None:
        return input_path.parent / md_name

    output = Path(output_arg)
    if output.is_dir() or output_arg.endswith(os.sep):
        output.mkdir(parents=True, exist_ok=True)
        return output / md_name

    return output


def convert_file(docx_path, output_path):
    """Convert a single .docx file and write the markdown output.

    Args:
        docx_path: Path to the input .docx file.
        output_path: Path to the output .md file.
    """
    print(f"Converting: {docx_path} -> {output_path}")
    markdown = convert_docx_to_markdown(docx_path)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(markdown)

    print(f"  Done. ({len(markdown)} characters written)")


def main():
    parser = argparse.ArgumentParser(
        description="Convert Microsoft Word (.docx) files to Markdown format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s document.docx                    Convert a single file (outputs document.md)
  %(prog)s document.docx -o result.md       Convert with custom output name
  %(prog)s docs/                             Convert all .docx files in a directory
  %(prog)s docs/ -o output/                  Convert all .docx, write to output directory
""",
    )
    parser.add_argument(
        "input",
        help="Input .docx file or directory containing .docx files",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output .md file or directory (default: same location as input)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)

    if not input_path.exists():
        print(f"Error: '{input_path}' does not exist.", file=sys.stderr)
        sys.exit(1)

    if input_path.is_dir():
        docx_files = sorted(input_path.glob("*.docx"))
        if not docx_files:
            print(f"No .docx files found in '{input_path}'.", file=sys.stderr)
            sys.exit(1)

        print(f"Found {len(docx_files)} .docx file(s) in '{input_path}'")
        for docx_file in docx_files:
            output = get_output_path(docx_file, args.output)
            convert_file(docx_file, output)
    else:
        if not input_path.suffix.lower() == ".docx":
            print(
                f"Warning: '{input_path}' does not have a .docx extension.",
                file=sys.stderr,
            )
        output = get_output_path(input_path, args.output)
        convert_file(input_path, output)

    print("\nConversion complete.")


if __name__ == "__main__":
    main()
