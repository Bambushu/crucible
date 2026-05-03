#!/usr/bin/env python3
"""Splits source files >max-lines into logical chunks for Crucible."""

import argparse
import re
import sys
import tempfile
from pathlib import Path


def get_split_points(lines, suffix):
    pts = []
    if suffix == ".py":
        pat = re.compile(r"^(def |class |async def )")
    elif suffix in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        pat = re.compile(r"^(function |class |export |const |let |var )")
    elif suffix in (".sh", ".bash"):
        pat = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*\s*\(\s*\)\s*\{?\s*$|function\s+)")
    elif suffix == ".go":
        pat = re.compile(r"^(func |type )")
    elif suffix == ".rs":
        pat = re.compile(r"^((pub\s+)?(fn |impl |struct |enum |mod |trait |union |type ))")
    else:
        return pts
    for i, line in enumerate(lines):
        if pat.match(line):
            pts.append(i)
    return pts


def dumb_slice(start, end, max_lines, overlap):
    chunks = []
    s = start
    while s < end:
        e = min(s + max_lines, end)
        chunks.append((s, e))
        if e == end:
            break
        nxt = e - overlap
        if nxt <= s:
            nxt = e
        s = nxt
    return chunks


def greedy_chunks(sections, max_lines, overlap):
    chunks = []
    cur_s = None
    cur_e = None
    cur_len = 0
    for s, e in sections:
        sec_len = e - s
        if sec_len > max_lines:
            if cur_len:
                chunks.append((cur_s, cur_e))
                cur_s = None
                cur_len = 0
            chunks.extend(dumb_slice(s, e, max_lines, overlap))
            continue
        if cur_len + sec_len <= max_lines:
            if cur_s is None:
                cur_s = s
            cur_e = e
            cur_len += sec_len
        else:
            if cur_len:
                chunks.append((cur_s, cur_e))
            cur_s = s
            cur_e = e
            cur_len = sec_len
    if cur_len:
        chunks.append((cur_s, cur_e))
    return chunks


def main():
    parser = argparse.ArgumentParser(description="Split source files into logical chunks.")
    parser.add_argument("--file", required=True)
    parser.add_argument("--max-lines", type=int, default=1500)
    parser.add_argument("--overlap", type=int, default=50)
    args = parser.parse_args()

    path = Path(args.file)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        sys.exit(1)

    n = len(lines)
    if n <= args.max_lines:
        print(f"{args.file}\t{args.file}\t1-{n}")
        return

    chunks = None
    try:
        pts = get_split_points(lines, path.suffix)
        if 0 not in pts:
            pts.insert(0, 0)
        pts = sorted(set(pts))
        if len(pts) < 2:
            raise ValueError
        sections = [(pts[i], pts[i + 1] if i + 1 < len(pts) else n) for i in range(len(pts))]
        chunks = greedy_chunks(sections, args.max_lines, args.overlap)
    except Exception:
        pass

    if chunks is None:
        chunks = dumb_slice(0, n, args.max_lines, args.overlap)

    total = len(chunks)
    td = tempfile.mkdtemp(prefix="crucible-chunks-")
    stem = path.stem
    ext = path.suffix
    for i, (s, e) in enumerate(chunks, 1):
        out = Path(td) / f"{stem}.chunk{i:02d}{ext}"
        start_l = s + 1
        end_l = e
        header = (
            f"# CRUCIBLE CHUNK {i} of {total}: original lines {start_l}-{end_l} from {args.file}\n"
            f"# Add {start_l - 1} to any line number below to get the original file's line.\n"
        )
        out.write_text(header + "\n".join(lines[s:e]) + "\n", encoding="utf-8")
        print(f"{out}\t{args.file}\t{start_l}-{end_l}")

    sys.stderr.write(f"✂ split {args.file} into {total} chunks at {td}\n")


if __name__ == "__main__":
    main()
