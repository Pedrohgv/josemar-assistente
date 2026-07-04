# Note Export PDF Playbook

## Status

- State: active
- Source: vault-gateway
- Mode: Hermes vault-gateway flow

## Route Mapping

- Route: `note.export_pdf`
- Payload: `path` (required relative path, must end with `.md`), `output_path` (optional relative path, must end with `.pdf`; defaults to source stem `.pdf` next to source)

## Purpose

- Convert any markdown note in the vault to a PDF file.
- Strip YAML frontmatter from the rendered content before producing the PDF.
- Render Markdown structures (headings, paragraphs, lists, tables, code blocks, blockquotes, links, horizontal rules) into a viewer-like PDF using Python-Markdown and PyMuPDF.
- Keep the PDF inside the vault (and the configured allowed roots).

## When to Use

- The user asks to export, render, or convert a note to PDF.
- The user wants a shareable, read-only artifact of a markdown note.
- The user wants a printable snapshot of a note without frontmatter noise.

## Decision Flow

1. **Validate the source path.** Must be a safe relative path ending with `.md`. Reject absolute paths, parent traversal, and non-`.md` sources.
2. **Resolve the output path.**
   - Omitted: write `<source-stem>.pdf` next to the source.
   - Provided: must be a safe relative path ending with `.pdf`. Reject absolute paths, parent traversal, and non-`.pdf` extensions.
3. **Read and strip frontmatter.** YAML frontmatter is removed from the rendered content; the body is converted from Markdown to HTML.
4. **Render the PDF.** Uses Python-Markdown (with `extra`, `tables`, `fenced_code`, `sane_lists` extensions) to convert the body to styled HTML, then renders it to a multi-page PDF via PyMuPDF Story + DocumentWriter. If Python-Markdown or PyMuPDF is not installed (or the Story/DocumentWriter API is unavailable), the route returns a `validation_error` explaining the missing dependency instead of crashing.
5. **Write the PDF** to the resolved output path and log the operation in `Meta/vault-gateway-log.md`.

## Output Expectations

After execution, return:
- `path` (relative source path)
- `output_path` (relative PDF path)
- `bytes_written` (number)
- `summary` (human-readable summary string)

## Safety

- Never write outside the vault root / allowed roots.
- Reject absolute or traversal paths for both source and output.
- Reject non-`.md` source and non-`.pdf` output.
- Reject missing source files.
- Do not install dependencies at runtime; surface a clear error if PyMuPDF is unavailable.

## Compatibility

- This route does not modify the source note; it only reads it and writes a sibling PDF.
- Frontmatter stripping is limited to a leading `---\n...\n---\n` YAML block; bodies without frontmatter are rendered verbatim.
- Markdown rendering covers common viewer structures (headings, paragraphs, lists, tables, fenced code, blockquotes, links, horizontal rules) via Python-Markdown extensions. Complex or non-standard Markdown may render with reduced fidelity.