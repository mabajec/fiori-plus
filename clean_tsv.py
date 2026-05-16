#!/usr/bin/env python3
"""
Clean a TSV file exported from the source system:
  1. Re-encode from Mac Central European (mac_latin2) to UTF-8,
     so characters like š, č, ć, ž, đ are restored.
  2. Strip leading empty tab-separated columns from each row.

Usage:
    python3 clean_tsv.py input.tsv -o output.tsv
    python3 clean_tsv.py input.tsv                  # writes to stdout
    python3 clean_tsv.py --input-encoding utf-8 ... # for already-UTF-8 input
"""
import argparse
import sys


def strip_leading_empty(row: list[str]) -> list[str]:
    i = 0
    while i < len(row) and row[i] == "":
        i += 1
    return row[i:]


def process(text: str) -> str:
    out_lines = []
    for line in text.splitlines():
        cells = strip_leading_empty(line.split("\t"))
        out_lines.append("\t".join(cells))
    return "\n".join(out_lines) + ("\n" if text.endswith("\n") else "")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("input", help="Input TSV file")
    ap.add_argument("-o", "--output", help="Output file (default: stdout)")
    ap.add_argument(
        "--input-encoding",
        default="mac_latin2",
        help="Encoding of the input file (default: mac_latin2 = Mac Central European)",
    )
    args = ap.parse_args()

    with open(args.input, encoding=args.input_encoding) as f:
        text = f.read()

    text = process(text)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
