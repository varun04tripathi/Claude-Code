# Word to Markdown Converter

Convert Microsoft Word (`.docx`) files to Markdown format.

## Features

- **Headings** — Converts Word heading styles (Heading 1–6, Title, Subtitle) to `#` markdown headings
- **Inline formatting** — Bold, italic, bold-italic, underline, strikethrough, superscript, subscript
- **Lists** — Ordered and unordered lists with nested indent support
- **Tables** — Full table conversion with header row and alignment separator
- **Hyperlinks** — Preserves links as `[text](url)`
- **Block quotes** — Quote-styled paragraphs become `>` blocks
- **Images** — Detects inline images (placeholder syntax)

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Convert a single file (outputs document.md alongside the input)
python docx_to_markdown.py document.docx

# Convert with a custom output path
python docx_to_markdown.py document.docx -o result.md

# Convert all .docx files in a directory
python docx_to_markdown.py docs/

# Convert all .docx files, writing output to a separate directory
python docx_to_markdown.py docs/ -o converted/
```

## How It Works

1. Parses the `.docx` XML structure using `python-docx`
2. Iterates through body elements (paragraphs, tables) in document order
3. Maps Word styles and formatting to their Markdown equivalents
4. Writes the resulting Markdown to a `.md` file
