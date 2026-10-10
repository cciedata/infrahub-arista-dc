#!/usr/bin/env python3
"""Compare a render set with tests/expected (READ ONLY; python3 stdlib only).

tests/expected holds the 43 reference renders; tests/expected-delta.diff records every line a render set may differ
from them by (the file is empty: no line may differ). A render set is correct when
  1. it has exactly the 43 files of tests/expected (30 *.cfg, 13 *.sh), and
  2. its unified diff against tests/expected is exactly tests/expected-delta.diff (no other line differs).

Run:
  python3 scripts/dc3_render_diff.py lab/configs                  exit 0 = PASS, 1 = FAIL (prints what differs)
  python3 scripts/dc3_render_diff.py lab/configs --show           also print the full diff against tests/expected
Options: --expected DIR (default tests/expected), --delta FILE (default tests/expected-delta.diff).
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUFFIXES = (".cfg", ".sh")


def render_files(folder: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(folder.iterdir()) if p.is_file() and p.name.endswith(SUFFIXES)}


def unified(expected: dict[str, str], actual: dict[str, str]) -> str:
    """One unified diff over every file present in both sets, in file-name order (labels a/<file>, b/<file>)."""
    out = []
    for name in sorted(set(expected) & set(actual)):
        out += difflib.unified_diff(expected[name].splitlines(), actual[name].splitlines(), f"a/{name}", f"b/{name}", lineterm="")
    return "\n".join(out) + ("\n" if out else "")


def strip_header(delta: str) -> str:
    """The delta file may start with '#' comment lines; the diff starts at the first '--- ' line."""
    lines = delta.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("--- "):
            return "\n".join(lines[i:]) + "\n"
    return ""


def check(expected: dict[str, str], actual: dict[str, str], delta: str) -> list[str]:
    """Problems (empty = PASS)."""
    problems = []
    missing, extra = sorted(set(expected) - set(actual)), sorted(set(actual) - set(expected))
    if missing:
        problems.append(f"missing renders: {', '.join(missing)}")
    if extra:
        problems.append(f"renders not in tests/expected: {', '.join(extra)}")
    got, want = unified(expected, actual), strip_header(delta)
    if got != want:
        problems.append("the diff against tests/expected is not the recorded delta; difference (recorded -> actual):")
        problems += ["  " + x for x in difflib.unified_diff(want.splitlines(), got.splitlines(), "recorded", "actual", lineterm="")]
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare a render set with tests/expected + tests/expected-delta.diff.")
    ap.add_argument("configs", help="folder with the rendered files (lab/configs)")
    ap.add_argument("--expected", default=str(ROOT / "tests" / "expected"))
    ap.add_argument("--delta", default=str(ROOT / "tests" / "expected-delta.diff"))
    ap.add_argument("--show", action="store_true", help="print the full diff against tests/expected")
    a = ap.parse_args(argv)
    expected, actual = render_files(Path(a.expected)), render_files(Path(a.configs))
    delta = Path(a.delta).read_text() if Path(a.delta).exists() else ""
    if a.show:
        sys.stdout.write(unified(expected, actual))
    problems = check(expected, actual, delta)
    changed = sum(1 for n in set(expected) & set(actual) if expected[n] != actual[n])
    if problems:
        print("\n".join(problems))
        print(f"FAIL: {len(actual)} renders, {changed} differ from tests/expected, not as recorded in {a.delta}")
        return 1
    print(f"PASS: {len(actual)} renders = tests/expected ({len(expected)}) + the recorded delta ({changed} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
