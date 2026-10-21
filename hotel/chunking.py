"""Structure-aware chunking for RAG documents.

A chunk is a span [start, end) of the stored document text, so a chunk's text
is always an exact slice: quotes found in a chunk map back to the document,
and nothing is paraphrased or re-joined. Chunks follow the document's shape:

  * Markdown ATX headings open sections; each chunk carries its heading path
    ("Guide > Install > Windows"), which is prepended when indexing, so a
    chunk that says "it" still knows what it is about (cheap contextual
    retrieval, no model needed);
  * paragraphs are packed up to `target` chars, never past `hard`;
  * fenced code blocks are never split unless they alone exceed `hard`;
  * an oversized paragraph splits at sentence ends, then at whitespace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
FENCE = re.compile(r"^[ \t]*(```|~~~)")
SENTENCE_END = re.compile(r"(?<=[.!?…。])\s+")


@dataclass
class Chunk:
    start: int
    end: int
    heading: str  # "A > B > C", "" before the first heading


def _blocks(text: str):
    """Yield (start, end, kind, heading_path) for headings, paragraphs and fences."""
    path: list[tuple[int, str]] = []
    pos, i, lines = 0, 0, text.splitlines(keepends=True)
    para_start = None

    def heading_str():
        return " > ".join(t for _, t in path)

    while i < len(lines):
        ln = lines[i]
        stripped = ln.strip()
        if FENCE.match(ln):
            if para_start is not None:
                yield para_start, pos, "para", heading_str()
                para_start = None
            start, mark = pos, FENCE.match(ln).group(1)
            pos += len(ln)
            i += 1
            while i < len(lines):
                pos += len(lines[i])
                i += 1
                if lines[i - 1].strip().startswith(mark):
                    break
            yield start, pos, "fence", heading_str()
            continue
        m = HEADING.match(stripped) if not ln.startswith((" ", "\t")) else None
        if m or not stripped:
            if para_start is not None:
                yield para_start, pos, "para", heading_str()
                para_start = None
            if m:
                level = len(m.group(1))
                path = [(lv, t) for lv, t in path if lv < level] + [(level, m.group(2).strip())]
                yield pos, pos + len(ln), "heading", heading_str()
        elif para_start is None:
            para_start = pos
        pos += len(ln)
        i += 1
    if para_start is not None:
        yield para_start, pos, "para", heading_str()


def _split_long(text: str, start: int, end: int, hard: int) -> list[tuple[int, int]]:
    """Split [start, end) at sentence ends (then whitespace) into pieces <= hard."""
    cuts = [start] + [start + m.end() for m in SENTENCE_END.finditer(text[start:end])] + [end]
    out, s = [], start
    for a, b in zip(cuts, cuts[1:]):
        if b - s > hard and a > s:
            out.append((s, a))
            s = a
        while b - s > hard:  # one sentence longer than hard: cut at the last space before the limit
            cut = text.rfind(" ", s + 1, s + hard)
            cut = cut if cut > s else s + hard
            out.append((s, cut))
            s = cut
    if s < end:
        out.append((s, end))
    return out


def _trim(text: str, s: int, e: int) -> tuple[int, int]:
    while s < e and text[s].isspace():
        s += 1
    while e > s and text[e - 1].isspace():
        e -= 1
    return s, e


def chunk(text: str, target: int = 1200, hard: int = 2000) -> list[Chunk]:
    out: list[Chunk] = []
    cur: list[int] | None = None  # [start, end]
    cur_heading = ""
    has_body = False

    def flush():
        nonlocal cur
        if cur is not None and has_body:  # a heading with no body lives on in its children's heading path
            s, e = _trim(text, *cur)
            if e > s:
                out.append(Chunk(s, e, cur_heading))
        cur = None

    for s, e, kind, heading in _blocks(text):
        if kind == "heading":
            flush()
            cur, cur_heading, has_body = [s, e], heading, False  # the heading line opens its section's first chunk
            continue
        has_body = True
        pieces = _split_long(text, s, e, hard) if e - s > hard else [(s, e)]
        for ps, pe in pieces:
            if cur is not None and heading != cur_heading:
                flush()
            # over target: start a new chunk, unless the current one is still tiny (e.g. just its heading)
            if cur is not None and pe - cur[0] > target and (cur[1] - cur[0] >= target // 3 or pe - cur[0] > hard):
                flush()
            if cur is None:
                cur, cur_heading = [ps, pe], heading
            else:
                cur[1] = pe
    flush()
    return out
