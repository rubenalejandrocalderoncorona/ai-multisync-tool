"""Chunking for the two Qdrant collections (documentation prose and source code)."""
from __future__ import annotations

import hashlib
import re


def chunk_markdown(content: str, max_chars: int = 1200) -> list[dict]:
    """Split Markdown on headings, then pack paragraphs up to max_chars."""
    body = re.sub(r"^---\n[\s\S]*?\n---\n", "", content, count=1)
    sections = [s for s in re.split(r"(?m)^(?=#{1,4}\s)", body) if s.strip()]
    chunks: list[dict] = []
    for section in sections:
        m = re.search(r"(?m)^#{1,4}\s+(.+)$", section)
        heading = m.group(1) if m else ""
        buf = ""
        for para in re.split(r"\n{2,}", section):
            if buf and len(buf) + len(para) > max_chars:
                chunks.append({"heading": heading, "text": buf.strip()})
                buf = ""
            buf += ("\n\n" if buf else "") + para
        if buf.strip():
            chunks.append({"heading": heading, "text": buf.strip()})
    return chunks


def point_id(repo: str, file_path: str, idx: int) -> str:
    """Deterministic UUID-shaped id so re-indexing a chunk upserts instead of duplicating."""
    h = hashlib.sha1(f"{repo}\0{file_path}\0{idx}".encode("utf-8")).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def chunk_code(file_path: str, content: str, max_lines: int = 80, overlap: int = 10, max_chars: int = 3200) -> list[dict]:
    """Split source code into overlapping line windows, preferring to cut at blank lines so functions stay together.
    Each chunk is prefixed with its location so a retrieved chunk is self-describing."""
    lines = content.split("\n")
    chunks: list[dict] = []
    start = 0
    while start < len(lines):
        end = min(start + max_lines, len(lines))
        if end < len(lines):
            # look back up to 15 lines for a blank line to cut on
            for i in range(end, max(start + max_lines - 15, start + 1), -1):
                if not lines[i - 1].strip():
                    end = i
                    break
        text = "\n".join(lines[start:end])
        if len(text) > max_chars:
            text = f"{text[:max_chars]}\n[truncated]"
        if text.strip():
            chunks.append({"start": start + 1, "end": end, "text": f"FILE {file_path} lines {start + 1}-{end}\n{text}"})
        if end >= len(lines):
            break
        start = max(end - overlap, start + 1)
    return chunks
