"""Split policy markdown into retrievable chunks.

Policies are already written in short, self-contained `##` sections, so the
section is the natural retrieval unit — splitting on a fixed token count would
cut a refund window away from the sentence that qualifies it. Oversized sections
fall back to paragraph packing with overlap.
"""

import re
from dataclasses import dataclass

HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$", re.MULTILINE)

MAX_CHUNK_CHARS = 1400
OVERLAP_CHARS = 180


@dataclass
class Chunk:
    index: int
    heading: str | None
    content: str

    @property
    def token_estimate(self) -> int:
        """Rough char/4 heuristic — used for display and budgeting, not billing."""
        return max(1, len(self.content) // 4)


def _split_long_section(heading: str | None, body: str) -> list[str]:
    """Pack paragraphs up to the size limit, repeating a tail slice as overlap."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    parts: list[str] = []
    current = ""

    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= MAX_CHUNK_CHARS or not current:
            current = candidate
            continue
        parts.append(current)
        # Carry the tail of the previous chunk so a rule split across the
        # boundary is still retrievable from either side.
        overlap = current[-OVERLAP_CHARS:]
        current = f"{overlap}\n\n{para}"

    if current:
        parts.append(current)
    return parts


def chunk_markdown(body: str, doc_title: str) -> list[Chunk]:
    """Chunk a policy body, prefixing each chunk with its document and section."""
    matches = list(HEADING_RE.finditer(body))
    sections: list[tuple[str | None, str]] = []

    if not matches or matches[0].start() > 0:
        preamble = body[: matches[0].start()] if matches else body
        if preamble.strip():
            sections.append((None, preamble.strip()))

    for i, match in enumerate(matches):
        heading = match.group(2).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        section_body = body[start:end].strip()
        if section_body:
            sections.append((heading, section_body))

    chunks: list[Chunk] = []
    for heading, section_body in sections:
        for part in _split_long_section(heading, section_body):
            # The title/section prefix gives the embedding topical anchoring and
            # lets a retrieved chunk be cited without a join back to the parent.
            prefix = f"{doc_title} — {heading}\n\n" if heading else f"{doc_title}\n\n"
            chunks.append(Chunk(index=len(chunks), heading=heading, content=prefix + part))

    return chunks
