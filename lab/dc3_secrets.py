#!/usr/bin/env python3
"""Fill delivery-time secrets into rendered DC3 EOS configs.

Rendered configs and baselines carry placeholders only (the repo is public); every delivery path (lab deploy in
lab/smoke.sh, the ZTP server) fills them just before the config leaves. Values come from ~/.dc3/secrets.env (mode 600),
written by scripts/dc3_make_secret.sh. A missing secret drops the line that carries its placeholder and returns a
warning; delivery goes on.

  python3 lab/dc3_secrets.py fill <folder>    fill every *.cfg in place, print one line per file and every warning
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

SECRETS_FILE = os.path.expanduser(os.environ.get("DC3_SECRETS", "~/.dc3/secrets.env"))
PLACEHOLDERS = {"__DC3_AUTOMATION_HASH__": "DC3_AUTOMATION_HASH"}   # placeholder in the config -> key in secrets.env
OTHER_KEYS = ["DC3_AUTOMATION_PASSWORD", "DC3_ZTP_CALLBACK_TOKEN"]


def load(path: str = SECRETS_FILE) -> dict:
    vals = {}
    if os.path.exists(path):
        for line in open(path):
            m = re.match(r"\s*([A-Z0-9_]+)=(.*)$", line.rstrip("\n"))
            if m:
                vals[m.group(1)] = m.group(2).strip().strip("'\"")
    for k in list(PLACEHOLDERS.values()) + OTHER_KEYS:
        if os.environ.get(k):
            vals[k] = os.environ[k]
    return vals


def fill(text: str, vals: dict | None = None) -> tuple[str, list[str]]:
    """Return (text, warnings). Each placeholder is replaced by its secret; with no secret, its whole line is dropped."""
    vals = load() if vals is None else vals
    warnings = []
    for ph, key in PLACEHOLDERS.items():
        if ph not in text:
            continue
        if vals.get(key):
            text = text.replace(ph, vals[key])
        else:
            n = sum(1 for line in text.splitlines() if ph in line)
            text = "".join(line for line in text.splitlines(keepends=True) if ph not in line)
            warnings.append(f"{key} not set ({SECRETS_FILE}): dropped {n} line(s) carrying {ph}")
    return text, warnings


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "fill":
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    vals = load()
    for f in sorted(Path(argv[1]).glob("*.cfg")):
        text, warns = fill(f.read_text(), vals)
        f.write_text(text)
        print(f"{f.name}: {'filled' if not warns else 'line dropped'}")
        for w in warns:
            print(f"WARNING {f.name}: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
