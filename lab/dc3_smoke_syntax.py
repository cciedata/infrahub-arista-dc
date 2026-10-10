#!/usr/bin/env python3
"""DC3 smoke test: check the unverified EOS syntax items against what cEOS kept in its running-config.

Reads the captures lab/smoke.sh wrote (<results>/<node>/show_running-config.txt, show_logging.txt,
show_interfaces_status.txt) and the rendered startup-configs (lab/configs/<node>.cfg). Python 3 standard library only.

Items:
  heartbeat-interval   'heartbeat-interval 4000' kept under mlag configuration (F2 unit); when absent
                       (hidden, most likely as the EOS default) PASS only if show mlag detail reports 4000 ms
  errdisable           'dual-primary detection delay 5 action errdisable all-interfaces' kept (F3 spelling)
  recovery-delay       'dual-primary recovery delay mlag 360 non-mlag 300' kept (F1)
  bfd-multihop         'multihop interval 999 min-rx 999 multiplier 3' kept under router bfd
  interface-names      every rendered 'interface EthernetN[/1]' exists in the running-config (interface mapping file)
  management1          'interface Management1' with 'vrf MGMT'
  logging              no config error message in show logging
  missing-lines        rendered lines (with their block) not in the running-config after the display rules below:
                       rejected, or a default EOS hides (information; the list is the evidence to read)
  display-normalised   rendered lines matched only through a display rule (information, one line per match)

Display rules (from the smoke runs):
  1. 'no shutdown' in a block that exists in the running-config without 'shutdown' (EOS hides it)
  2. 'protocol https' under 'management api http-commands' when the block exists without 'no protocol https' (hidden)
  3. 'spanning-tree portfast' and 'spanning-tree portfast edge' compare as the same line (EOS shows one as the other)
  4. MAC addresses compare in one form: 0200.0000.00aa, 02:00:00:00:00:aa and 02-00-00-00-00-AA are equal
Hidden-default rules (from a smoke run; absent from the running-config with no config error in show logging,
most likely EOS defaults until checked with show running-config all):
  5. 'aaa authentication login default local' (top level)
  6. 'graceful-restart-helper' under 'router bgp <asn>' when the router bgp block exists
Exit code: 0 no FAIL, 1 a FAIL, 2 missing input.

  python3 lab/dc3_smoke_syntax.py --results lab/results/smoke-<UTC> --nodes DC3-S1,DC3-S2,DC3-L1,DC3-L2,DC3-L11
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent
LOG_ERRORS = re.compile(r"(?i)(invalid input|incomplete command|ambiguous command|not supported|unavailable command|"
                        r"config(?:uration)? error|startup-config.*(?:error|fail)|CONFIG_[A-Z_]*ERR)")


def keyed_lines(text: str) -> set[tuple[str, str]]:
    """(top-level block header, stripped line) for every line; top-level lines are (line, '')."""
    out, head = set(), ""
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s == "!" or s.startswith("!") or s == "end":
            continue
        if not raw.startswith(" "):
            head = s
            out.add((s, ""))
        else:
            out.add((head, s))
    return out


MAC_DOTTED = re.compile(r"\b([0-9a-fA-F]{4})\.([0-9a-fA-F]{4})\.([0-9a-fA-F]{4})\b")
MAC_SEP = re.compile(r"\b([0-9a-fA-F]{2})[:-]([0-9a-fA-F]{2})[:-]([0-9a-fA-F]{2})[:-]([0-9a-fA-F]{2})[:-]([0-9a-fA-F]{2})[:-]"
                     r"([0-9a-fA-F]{2})\b")


def display_form(line: str) -> str:
    """Rules 3 and 4: one comparison form for lines EOS displays differently from how they were written."""
    line = MAC_DOTTED.sub(lambda m: ".".join(g.lower() for g in m.groups()), line)
    line = MAC_SEP.sub(lambda m: "%s%s.%s%s.%s%s" % tuple(g.lower() for g in m.groups()), line)
    if line == "spanning-tree portfast edge":
        line = "spanning-tree portfast"
    return line


def split_missing(rendered_keys: set, running_keys: set) -> tuple[list, list]:
    """Rendered (header, line) keys not in the running-config, split into (real gaps, [(key, rule)] display matches)."""
    run_norm = {(display_form(h), display_form(l)) for h, l in running_keys}
    run_heads = {h for h, l in running_keys if l == ""}
    real, normalised = [], []
    for h, l in sorted(rendered_keys - running_keys):
        if (display_form(h), display_form(l)) in run_norm:
            normalised.append(((h, l), "3" if "portfast" in l else "4"))
        elif l == "no shutdown" and h in run_heads and (h, "shutdown") not in running_keys:
            normalised.append(((h, l), "1"))
        elif (l == "protocol https" and h == "management api http-commands" and h in run_heads
              and (h, "no protocol https") not in running_keys):
            normalised.append(((h, l), "2"))
        elif h == "aaa authentication login default local" and l == "":
            normalised.append(((h, l), "5"))
        elif (l == "graceful-restart-helper" and h.startswith("router bgp ") and h in run_heads
              and (h, "no graceful-restart-helper") not in running_keys):
            normalised.append(((h, l), "6"))
        else:
            real.append((h, l))
    return real, normalised


def block(text: str, header: str) -> list[str]:
    lines, inside = [], False
    for raw in text.splitlines():
        if not raw.startswith(" "):
            inside = raw.strip() == header
            continue
        if inside:
            lines.append(raw.strip())
    return lines


def mlag_heartbeat_ms(detail_text: str = "", detail_json: str = "") -> list[int]:
    """Heartbeat interval values (ms) reported by 'show mlag detail': JSON keys containing 'heartbeatInterval' (any
    depth), else text lines '... heartbeat interval ... : <n> [ms]' (wording not yet seen on 4.36.2F)."""
    vals = []
    try:
        doc = json.loads(detail_json) if detail_json.strip() else None
    except ValueError:
        doc = None
    stack = [doc]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            for k, v in x.items():
                if "heartbeatinterval" in k.lower() and isinstance(v, (int, float)):
                    vals.append(int(v))
                else:
                    stack.append(v)
        elif isinstance(x, list):
            stack.extend(x)
    if not vals:
        vals = [int(m.group(1)) for m in re.finditer(r"(?im)heartbeat[ -]interval[^:\n]*:\s*(\d+)", detail_text)]
    return vals


def check_node(node: str, rendered: str, running: str, logging: str, status: str,
               mlag_detail: str = "", mlag_detail_json: str = "") -> list[dict]:
    res = []

    def item(name, result, seen):
        res.append({"node": node, "item": name, "result": result, "seen": seen})

    rk = keyed_lines(rendered)
    mlag_r, mlag_k = block(rendered, "mlag configuration"), block(running, "mlag configuration")
    for name, pat in (("heartbeat-interval", r"^heartbeat-interval "), ("errdisable", r"^dual-primary detection "),
                      ("recovery-delay", r"^dual-primary recovery delay ")):
        want = [l for l in mlag_r if re.match(pat, l)]
        if not want:
            item(name, "N/A", "not rendered on this node")
            continue
        have = [l for l in mlag_k if re.match(pat, l)]
        if name == "heartbeat-interval" and want[0] not in mlag_k and not have:
            # absent from the running-config: EOS hides a value equal to its default -> judge by show mlag detail
            ms = int(want[0].split()[1])
            seen = mlag_heartbeat_ms(mlag_detail, mlag_detail_json)
            item(name, "PASS" if ms in seen else "FAIL",
                 f"rendered '{want[0]}'; running '(absent)'; show mlag detail heartbeat interval "
                 + (", ".join(f"{v} ms" for v in seen) if seen else "(not found)"))
            continue
        item(name, "PASS" if want[0] in mlag_k else "FAIL", f"rendered '{want[0]}'; running '{have[0] if have else '(absent)'}'")
    bfd_r, bfd_k = block(rendered, "router bfd"), block(running, "router bfd")
    want = [l for l in bfd_r if l.startswith("multihop ")]
    if want:
        have = [l for l in bfd_k if l.startswith("multihop ")]
        item("bfd-multihop", "PASS" if want[0] in bfd_k else "FAIL",
             f"rendered '{want[0]}'; running '{have[0] if have else '(absent)'}'")
    else:
        item("bfd-multihop", "N/A", "not rendered on this node")
    r_ifs = [h for h, l in rk if l == "" and re.match(r"interface Ethernet\d+(/\d+)?$", h)]
    k_ifs = {h for h, l in keyed_lines(running) if l == ""}
    missing_ifs = [h.split()[1] for h in r_ifs if h not in k_ifs]
    item("interface-names", "FAIL" if missing_ifs else "PASS",
         (f"missing {', '.join(missing_ifs)}" if missing_ifs else f"{len(r_ifs)} Ethernet interfaces present")
         + ("; " + ", ".join(sorted(set(re.findall(r"\bEt\d+(?:/\d+)?\b", status)))[:6]) + " ... in show interfaces status"
            if status else ""))
    m1 = block(running, "interface Management1")
    item("management1", "PASS" if m1 and "vrf MGMT" in m1 else "FAIL",
         "interface Management1: " + ("; ".join(m1) if m1 else "(absent)"))
    errs = [l.strip() for l in logging.splitlines() if LOG_ERRORS.search(l)]
    item("logging", "FAIL" if errs else "PASS", "; ".join(errs[:5]) if errs else "no config error message")
    missing, normalised = split_missing(rk, keyed_lines(running))
    item("missing-lines", "INFO", f"{len(missing)} rendered lines not in the running-config" if missing else "none")
    res[-1]["lines"] = [f"{h} | {l}" if l else h for h, l in missing]
    item("display-normalised", "INFO", f"{len(normalised)} rendered lines matched by a display rule" if normalised else "none")
    res[-1]["lines"] = [f"rule {rule}: " + (f"{h} | {l}" if l else h) for (h, l), rule in normalised]
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--results", required=True, help="smoke results folder (one sub-folder per node)")
    ap.add_argument("--nodes", required=True, help="comma-separated EOS nodes")
    ap.add_argument("--configs", default=str(LAB / "configs"), help="rendered configs folder (default lab/configs)")
    a = ap.parse_args(argv)
    res_dir, cfg_dir = Path(a.results), Path(a.configs)
    out = []
    for node in [n.strip() for n in a.nodes.split(",") if n.strip()]:
        try:
            rendered = (cfg_dir / f"{node}.cfg").read_text()
            running = (res_dir / node / "show_running-config.txt").read_text()
        except OSError as e:
            print(f"missing input: {e}", file=sys.stderr)
            return 2
        read = lambda f: (res_dir / node / f).read_text() if (res_dir / node / f).exists() else ""  # noqa: E731
        out += check_node(node, rendered, running, read("show_logging.txt"), read("show_interfaces_status.txt"),
                          read("show_mlag_detail.txt"), read("show_mlag_detail.json"))
    (res_dir / "syntax.json").write_text(json.dumps(out, indent=2) + "\n")
    lines = [f"{r['node']:<10} {r['item']:<18} {r['result']:<5} {r['seen']}" for r in out]
    for r in out:
        if r.get("lines"):
            what = ("rendered lines matched by a display rule" if r["item"] == "display-normalised"
                    else "rendered lines not in the running-config")
            lines.append(f"--- {r['node']}: {what}")
            lines += [f"    {x}" for x in r["lines"]]
    (res_dir / "syntax.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(l for l in lines if not l.startswith("    ")))
    return 1 if any(r["result"] == "FAIL" for r in out) else 0


if __name__ == "__main__":
    sys.exit(main())
