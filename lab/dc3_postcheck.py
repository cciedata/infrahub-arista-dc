#!/usr/bin/env python3
"""DC3 lab post-check: compare the running containerlab lab with the intent of the rendered files.

Runs on the lab host. Python 3 standard library only; no eAPI: every show command goes through docker exec
  EOS (cEOS):      docker exec clab-dc3-<node> Cli -p 15 -c "<show> | json"
  FRR stand-ins:   docker exec clab-dc3-<node> vtysh -c "<show> json"
  test hosts:      docker exec clab-dc3-<node> ping -c 3 -w 5 <address>

Checks (lab/dc3_intent.py builds them from lab/configs/ and the topology):
  bgp       every underlay, MLAG iBGP, EVPN overlay and VRF session of every EOS node; every BGP session of every FRR
            node (FW / ISP / cloud stand-ins, carriers, cloud providers, branches) Established
  bfd       every BGP neighbor with BFD configured (single-hop and multihop) up; BLOCKED-PLATFORM when the node shows
            no BFD session at all
  mlag      state active, negotiation connected, peer-link and local interface up, config-sanity consistent
  vxlan-up  Vxlan1 line protocol up
  vtep / flood   every remote VTEP that shares an L2 VNI learned and in the flood list
  route     each tenant VRF has 0.0.0.0/0 (when its stand-ins are in the lab) and its own summaries
  ping      test hosts reach their gateway and the internet stand-ins
A check whose nodes are not all in the selection (--nodes) is SKIP with the reason.

Output: a table on stdout, a JSON results file (--out), exit code 0 = no FAIL / ERROR, 1 = FAIL or ERROR, 2 = usage or
intent error.

  python3 lab/dc3_postcheck.py                                  # full lab, lab/dc3.clab.yml
  python3 lab/dc3_postcheck.py --topology lab/dc3-smoke.clab.yml --nodes DC3-S1,DC3-S2,DC3-L1,DC3-L2,DC3-L11,DC3-HOST-PROD
  python3 lab/dc3_postcheck.py --wait 300                       # re-run until nothing fails or 300 s passed
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dc3_intent as I  # noqa: E402

PREFIX = "clab-dc3-"                  # containerlab container name: clab-<lab name>-<node> containerlab docs (naming)
PARALLEL = 8                          # nodes read at the same time
RESULTS = ("PASS", "FAIL", "SKIP", "BLOCKED-PLATFORM", "ERROR")

# Show commands. JSON field names: the first smoke run did not keep the JSON; every command's raw output is
# now saved (--raw-dir) so the next run verifies them. Text fallbacks for BGP and BFD are proven on cEOS text captures.
EOS_CMDS = {
    "bgp_ipv4": "show ip bgp summary vrf all",
    "bgp_evpn": "show bgp evpn summary",
    "bfd": "show bfd peers",
    "mlag": "show mlag",
    "vxlan_if": "show interfaces Vxlan1",
    "vtep": "show vxlan vtep",
    "flood": "show vxlan flood vtep",
    "routes": "show ip route vrf all",
}
FRR_CMDS = {"bgp": "show bgp vrf all summary", "bfd": "show bfd peers"}


# ---------------------------------------------------------------------------------------------- docker exec
class DockerRunner:
    """docker exec into containerlab nodes. No sudo. With raw_dir set, every call is saved:
    <raw_dir>/<node>/<command words>.<json|txt> (stdout) and <raw_dir>/calls.jsonl (node, argv, rc, seconds, stderr)."""

    def __init__(self, prefix: str = PREFIX, timeout: int = 120, docker: str = "docker", raw_dir: Path | None = None):
        self.prefix, self.timeout, self.docker, self.raw_dir = prefix, timeout, docker, raw_dir
        self._lock = threading.Lock()

    def exec(self, node: str, argv: list[str], timeout: int | None = None) -> tuple[int, str, str]:
        t0 = time.monotonic()
        try:
            p = subprocess.run([self.docker, "exec", self.prefix + node, *argv], capture_output=True, text=True,
                               timeout=timeout or self.timeout)
            rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            rc, out, err = 124, "", "timeout"
        except FileNotFoundError as e:
            rc, out, err = 127, "", str(e)
        self._save(node, argv, rc, out, err, time.monotonic() - t0)
        return rc, out, err

    def _save(self, node, argv, rc, out, err, secs):
        if not self.raw_dir:
            return
        cmd = argv[-1] if argv[:1] in (["Cli"], ["vtysh"]) else " ".join(argv)
        name = raw_name(cmd)
        d = Path(self.raw_dir) / node
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(out)
        with self._lock, open(Path(self.raw_dir) / "calls.jsonl", "a") as f:
            f.write(json.dumps({"node": node, "argv": argv, "rc": rc, "seconds": round(secs, 2), "file": f"{node}/{name}",
                                "stdout_bytes": len(out), "stderr": err[:500]}) + "\n")

    def eos_text(self, node: str, cmd: str) -> tuple[int, str]:
        rc, out, err = self.exec(node, ["Cli", "-p", "15", "-c", cmd])
        return rc, out + err

    def eos_json(self, node: str, cmd: str):
        rc, out, _err = self.exec(node, ["Cli", "-p", "15", "-c", f"{cmd} | json"])
        return _loads(out) if rc == 0 else None

    def frr_text(self, node: str, cmd: str) -> tuple[int, str]:
        rc, out, err = self.exec(node, ["vtysh", "-c", cmd])
        return rc, out + err

    def frr_json(self, node: str, cmd: str):
        rc, out, _err = self.exec(node, ["vtysh", "-c", f"{cmd} json"])
        return _loads(out) if rc == 0 else None

    def ping(self, node: str, target: str, count: int = 3, deadline: int = 5) -> tuple[int, int] | None:
        rc, out, err = self.exec(node, ["ping", "-c", str(count), "-w", str(deadline), target], timeout=deadline + 10)
        return parse_ping_summary(out + err)


def raw_name(cmd: str) -> str:
    """'show bfd peers | json' -> 'show_bfd_peers.json'; other commands -> '<words>.txt'."""
    is_json = cmd.rstrip().endswith("| json") or cmd.rstrip().endswith(" json")
    base = re.sub(r"\s*\|\s*json$|\s+json$", "", cmd.strip())
    return re.sub(r"[^A-Za-z0-9.-]+", "_", base).strip("_") + (".json" if is_json else ".txt")


def _loads(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def parse_ping_summary(text: str) -> tuple[int, int] | None:
    """(sent, received) from iputils ('3 packets transmitted, 3 received') or BusyBox ('3 packets received')."""
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


# ---------------------------------------------------------------------------------------------- normalizers
def eos_bgp_sessions(ipv4: dict | None, evpn: dict | None) -> dict:
    """{(af, vrf, ip): {'state', 'since'}} from 'show ip bgp summary vrf all | json' and 'show bgp evpn summary | json'
    (vrfs -> <vrf> -> peers -> <ip> -> peerState / upDownTime field names)."""
    out = {}
    for af, doc in (("ipv4", ipv4), ("evpn", evpn)):
        for vrf, v in ((doc or {}).get("vrfs") or {}).items():
            for ip, p in (v.get("peers") or {}).items():
                if isinstance(p, dict):
                    out[(af, vrf, ip)] = {"state": p.get("peerState") or p.get("state"), "since": p.get("upDownTime")}
    return out


BGP_STATES = {"Estab": "Established", "Established": "Established"}


def eos_bgp_text(text: str, af: str) -> dict:
    """{(af, vrf, ip): {'state'}} from the text of 'show ip bgp summary vrf all' / 'show bgp evpn summary'
    (format of a cEOS 4.36.2F capture: 'BGP summary information for VRF <v>', then rows
    '<ip> 4 <AS> <rcvd> <sent> <inq> <outq> <up/down> <state> ...'; 'Estab' = Established)."""
    out, vrf = {}, None
    for line in text.splitlines():
        m = re.match(r"^BGP summary information for VRF (\S+)", line)
        if m:
            vrf = m.group(1)
            continue
        f = line.split()
        if vrf and len(f) >= 9 and f[1] == "4" and _is_v4(f[0]):
            st = re.match(r"[A-Za-z]+", f[8])
            st = st.group(0) if st else f[8]
            out[(af, vrf, f[0])] = {"state": BGP_STATES.get(st, st), "since": f[7]}
    return out


def _is_v4(s: str) -> bool:
    try:
        ipaddress.IPv4Address(s)
        return True
    except ValueError:
        return False


def frr_bgp_sessions(doc: dict | None) -> dict:
    """{('ipv4', vrf, ip): {...}} from FRR 'show bgp vrf all summary json' (<vrf> -> ipv4Unicast -> peers -> <ip> -> state,
    peerUptimeEstablishedEpoch )."""
    out = {}
    for vrf, v in (doc or {}).items():
        if not isinstance(v, dict):
            continue
        for afi, a in v.items():
            if not isinstance(a, dict) or "peers" not in a:
                continue
            for ip, p in a["peers"].items():
                key = ("ipv4", vrf, ip)
                if key in out and out[key]["state"] == "Established":
                    continue
                out[key] = {"state": p.get("state") or p.get("peerState"),
                            "since": p.get("peerUptimeEstablishedEpoch") or p.get("peerUptimeMsec")}
    return out


def _statuses_in(x) -> list[str]:
    """Every session state in a BFD peer entry (keys status / state / sessionState, at any depth) field name."""
    out, stack = [], [x]
    while stack:
        y = stack.pop(0)
        if isinstance(y, dict):
            for k in ("status", "state", "sessionState"):
                if isinstance(y.get(k), str):
                    out.append(y[k])
            stack.extend(v for v in y.values() if isinstance(v, (dict, list)))
        elif isinstance(y, list):
            stack.extend(y)
    return out


def eos_bfd_peers(doc: dict | None) -> dict:
    """{(vrf, ip): status} from 'show bfd peers | json'. Tolerant (an earlier run judged every Up session 'no such BFD
    session' with the strict path vrfs -> ipv4Neighbors -> peerStats -> status): under each VRF, any mapping keyed by
    IPv4 addresses (ipv4Neighbors, v4Neighbors, multihop variants ...) is a peer table; the state is the first
    status / state / sessionState string inside the entry. Several entries for one peer: up if any is up."""
    out = {}
    for vrf, v in ((doc or {}).get("vrfs") or {}).items():
        if not isinstance(v, dict):
            continue
        stack = [v]
        while stack:
            y = stack.pop()
            if not isinstance(y, dict):
                continue
            for k, val in y.items():
                if _is_v4(k) and isinstance(val, dict):
                    sts = _statuses_in(val) + ([out[(vrf, k)]] if out.get((vrf, k)) else [])
                    out[(vrf, k)] = "up" if any(x.lower() == "up" for x in sts) else (sts[0] if sts else None)
                elif isinstance(val, dict):
                    stack.append(val)
    return out


def eos_bfd_text(text: str) -> dict:
    """{(vrf, ip): state} from the text of 'show bfd peers' (format of a cEOS 4.36.2F capture: per VRF
    'VRF name: <v>', a first table with DstAddr rows, a second table whose rows end with the State, same order)."""
    out, vrf, addrs, states = {}, None, [], []

    def flush():
        for ip, st in zip(addrs, states):
            out[(vrf, ip)] = st.lower()

    for line in text.splitlines():
        m = re.match(r"^VRF name: (\S+)", line)
        if m:
            if vrf is not None:
                flush()
            vrf, addrs, states = m.group(1), [], []
            continue
        f = line.split()
        if not f or vrf is None:
            continue
        if _is_v4(f[0]) and len(f) >= 4:
            addrs.append(f[0])
        elif f[-1] in ("Up", "Down", "Init", "AdminDown") and not set(f[0]) <= {"-"}:
            states.append(f[-1])
    if vrf is not None:
        flush()
    return out


def frr_bfd_peers(doc) -> dict:
    """{(vrf, ip): status} from FRR 'show bfd peers json' (list of {peer, vrf, status} )."""
    out = {}
    for p in doc or []:
        if isinstance(p, dict) and p.get("peer"):
            out[(p.get("vrf") or "default", p["peer"])] = p.get("status")
    return out


def eos_routes(doc: dict | None) -> dict:
    """{vrf: set(prefixes)} from 'show ip route vrf all | json' (vrfs -> <vrf> -> routes -> <prefix> )."""
    return {vrf: set((v.get("routes") or {}).keys()) for vrf, v in ((doc or {}).get("vrfs") or {}).items()}


def all_ipv4(doc) -> set[str]:
    """Every IPv4 address found anywhere in a JSON document (keys and values): tolerant VTEP / flood list reader."""
    out = set()
    stack = [doc]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            for k, v in x.items():
                stack.append(k)
                stack.append(v)
        elif isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, str):
            try:
                out.add(str(ipaddress.IPv4Address(x)))
            except ValueError:
                pass
    return out


def mlag_state(doc: dict | None) -> dict | None:
    if not doc:
        return None
    return {k: doc.get(k) for k in ("state", "negStatus", "peerLinkStatus", "localIntfStatus", "configSanity")}


def vxlan_up(doc: dict | None) -> bool | None:
    if not doc:
        return None
    i = (doc.get("interfaces") or {}).get("Vxlan1") or {}
    return i.get("lineProtocolStatus") == "up" if i else None


# ---------------------------------------------------------------------------------------------- collection
def collect_node(intent: dict, node: str, runner, pings: set) -> dict | None:
    kind = intent["nodes"].get(node, {}).get("kind")
    if kind == "eos":
        raw = {k: runner.eos_json(node, cmd) for k, cmd in EOS_CMDS.items()}
        bgp = eos_bgp_sessions(raw["bgp_ipv4"], raw["bgp_evpn"])
        src = {"bgp": "json", "bfd": "json"}
        for key, af in (("bgp_ipv4", "ipv4"), ("bgp_evpn", "evpn")):
            if raw[key] is None or not any(k[0] == af for k in bgp):        # JSON failed or unreadable: text
                rc, txt = runner.eos_text(node, EOS_CMDS[key])
                if rc == 0:
                    bgp.update(eos_bgp_text(txt, af))
                    src["bgp"] = "text"
        bfd = eos_bfd_peers(raw["bfd"])
        bfd_ok = raw["bfd"] is not None
        if not bfd or not any(st for st in bfd.values()):
            rc, txt = runner.eos_text(node, EOS_CMDS["bfd"])
            if rc == 0:
                bfd, bfd_ok, src["bfd"] = eos_bfd_text(txt), True, "text"
        return {"reachable": raw["bgp_ipv4"] is not None or raw["mlag"] is not None or src["bgp"] == "text",
                "bgp": bgp, "bfd": bfd, "bfd_raw_ok": bfd_ok, "source": src,
                "mlag": mlag_state(raw["mlag"]), "vxlan_up": vxlan_up(raw["vxlan_if"]),
                "vteps": all_ipv4(raw["vtep"]) if raw["vtep"] is not None else None,
                "flood": all_ipv4(raw["flood"]) if raw["flood"] is not None else None,
                "routes": eos_routes(raw["routes"]) if raw["routes"] is not None else None}
    if kind == "frr":
        bgp = runner.frr_json(node, FRR_CMDS["bgp"])
        bfd = runner.frr_json(node, FRR_CMDS["bfd"])
        return {"reachable": bgp is not None, "bgp": frr_bgp_sessions(bgp), "bfd": frr_bfd_peers(bfd),
                "bfd_raw_ok": bfd is not None}
    if kind == "host":
        return {"reachable": True, "ping": {t: runner.ping(node, t) for t in sorted(pings)}}
    return None


def collect(intent: dict, selected: set[str], runner, checks: list[dict]) -> dict:
    """One snapshot of the lab state for the selected nodes: {node: {...normalized...}}. Nodes are read in parallel
    (read one after the other they took 6 min for 5 cEOS nodes, so the first node was read before its
    sessions came up and the last one minutes later)."""
    pings: dict = {}
    for c in checks:
        if c["type"] == "ping" and c["node"] in selected:
            pings.setdefault(c["node"], set()).add(c["target"])
    nodes = sorted(selected)
    with ThreadPoolExecutor(max_workers=max(1, min(len(nodes), PARALLEL))) as ex:
        res = list(ex.map(lambda n: collect_node(intent, n, runner, pings.get(n, set())), nodes))
    return {n: r for n, r in zip(nodes, res) if r is not None}


# ---------------------------------------------------------------------------------------------- evaluation
def _res(c, result, seen):
    return {"id": c["id"], "node": c["node"], "type": c["type"], "check": c["desc"], "result": result, "seen": seen}


def evaluate(checks: list[dict], snap: dict, selected: set[str], baseline: dict | None = None) -> list[dict]:
    """Judge every check against one snapshot. baseline (optional): {check id: result} from before a break; a session
    check that did not PASS in the baseline is reported SKIP ('not up before the break')."""
    out = []
    for c in checks:
        ok, why = I.selected_ok(c, selected)
        if not ok:
            out.append(_res(c, "SKIP", why))
            continue
        if baseline is not None and c["type"] in ("bgp", "bfd") and baseline.get(c["id"]) != "PASS":
            out.append(_res(c, "SKIP", f"not up before the break ({baseline.get(c['id'], 'absent')})"))
            continue
        s = snap.get(c["node"])
        if not s or not s.get("reachable"):
            out.append(_res(c, "ERROR", "no answer from the node"))
            continue
        t = c["type"]
        if t == "bgp":
            sess = s["bgp"].get((c["af"], c["vrf"], c["peer"]))
            st = sess["state"] if sess else None
            out.append(_res(c, "PASS" if st == "Established" else "FAIL", st or "no such session"))
        elif t == "bfd":
            if not s["bfd"]:
                out.append(_res(c, "BLOCKED-PLATFORM" if s.get("bfd_raw_ok") else "ERROR",
                                "no BFD session on the node" if s.get("bfd_raw_ok") else "show bfd peers failed"))
                continue
            st = s["bfd"].get((c["vrf"], c["peer"]))
            out.append(_res(c, "PASS" if (st or "").lower() == "up" else "FAIL", (st or "no such BFD session")
                            + (" (text)" if s.get("source", {}).get("bfd") == "text" else "")))
        elif t == "mlag":
            m = s.get("mlag")
            if m is None:
                out.append(_res(c, "ERROR", "show mlag failed"))
                continue
            good = (m["state"] == "active" and m["negStatus"] == "connected" and m["peerLinkStatus"] == "up"
                    and m["localIntfStatus"] == "up" and m["configSanity"] in ("consistent", None))
            out.append(_res(c, "PASS" if good else "FAIL", ", ".join(f"{k}={v}" for k, v in m.items())))
        elif t == "vxlan-up":
            v = s.get("vxlan_up")
            out.append(_res(c, "PASS" if v else ("ERROR" if v is None else "FAIL"), {True: "up", False: "down"}.get(v, "no data")))
        elif t in ("vtep", "flood"):
            have = s.get("vteps" if t == "vtep" else "flood")
            if have is None:
                out.append(_res(c, "ERROR", "command failed"))
                continue
            out.append(_res(c, "PASS" if c["vtep"] in have else "FAIL", "present" if c["vtep"] in have else
                            f"absent ({len(have)} addresses seen)"))
        elif t == "route":
            r = s.get("routes")
            if r is None:
                out.append(_res(c, "ERROR", "show ip route failed"))
                continue
            present = c["prefix"] in r.get(c["vrf"], set())
            out.append(_res(c, "PASS" if present else "FAIL", "present" if present else "absent"))
        elif t == "ping":
            p = s.get("ping", {}).get(c["target"])
            if p is None:
                out.append(_res(c, "ERROR", "no ping summary"))
                continue
            out.append(_res(c, "PASS" if p[1] > 0 else "FAIL", f"{p[1]}/{p[0]}"))
        else:
            out.append(_res(c, "ERROR", f"unknown check type {t}"))
    return out


def summary(results: list[dict]) -> dict:
    return {r: sum(1 for x in results if x["result"] == r) for r in RESULTS}


def table(results: list[dict], show_skip: bool = False) -> str:
    rows = [r for r in results if show_skip or r["result"] != "SKIP"]
    w_node = max([len(r["node"]) for r in rows] + [4])
    w_chk = min(max([len(r["check"]) for r in rows] + [5]), 70)
    lines = [f"{'Node':<{w_node}}  {'Check':<{w_chk}}  {'Result':<16}  Seen", "-" * (w_node + w_chk + 40)]
    for r in rows:
        lines.append(f"{r['node']:<{w_node}}  {r['check'][:w_chk]:<{w_chk}}  {r['result']:<16}  {r['seen']}")
    s = summary(results)
    lines.append("-" * (w_node + w_chk + 40))
    lines.append("Total " + ", ".join(f"{k} {v}" for k, v in s.items()) + f" (of {len(results)})")
    return "\n".join(lines)


def run_once(intent, checks, selected, runner, baseline=None):
    snap = collect(intent, selected, runner, checks)
    return evaluate(checks, snap, selected, baseline), snap


def retry_errors(intent, checks, results, make_runner, run=None):
    """: one targeted re-read of the nodes whose checks ended ERROR (a command that returned nothing),
    even when the --wait window is used up by a long attempt. Only ERROR rows are replaced, and only by a non-SKIP result."""
    bad = sorted({r["node"] for r in results if r["result"] == "ERROR"})
    if not bad:
        return results, []
    print(f"error retry: re-reading {', '.join(bad)}", file=sys.stderr)
    new, _ = (run or run_once)(intent, checks, set(bad), make_runner())
    by_id = {r["id"]: r for r in new if r["result"] != "SKIP"}
    return [by_id.get(r["id"], r) if r["result"] == "ERROR" else r for r in results], bad


def failed(results) -> bool:
    return any(r["result"] in ("FAIL", "ERROR") for r in results)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lab-dir", default=str(I.LAB), help="folder with configs/ and the topology (default lab/)")
    ap.add_argument("--topology", help="topology file (default <lab-dir>/dc3.clab.yml)")
    ap.add_argument("--nodes", help="comma-separated node names to check (default: every node of the topology)")
    ap.add_argument("--prefix", default=PREFIX, help=f"container name prefix (default {PREFIX})")
    ap.add_argument("--out", help="JSON results file (default <lab-dir>/results/postcheck-<UTC time>.json)")
    ap.add_argument("--wait", type=int, default=0,
                    help="re-run while something fails and fewer than this many seconds passed since the first attempt")
    ap.add_argument("--settle", type=int, default=0, help="seconds to wait before the first attempt (default 0)")
    ap.add_argument("--raw-dir", help="save every command's raw output here (default <out without .json>-raw/attempt-N)")
    ap.add_argument("--interval", type=int, default=20, help="seconds between re-runs with --wait (default 20)")
    ap.add_argument("--show-skip", action="store_true", help="print SKIP rows in the table too")
    ap.add_argument("--list", action="store_true", help="print the checks derived from the files and exit (no lab needed)")
    a = ap.parse_args(argv)
    lab = Path(a.lab_dir)
    try:
        intent = I.build_intent(lab, Path(a.topology) if a.topology else None)
    except (OSError, ValueError, KeyError) as e:
        print(f"intent error: {e}", file=sys.stderr)
        return 2
    if intent["missing"]:
        print("missing rendered files (render them first):\n  " + "\n  ".join(intent["missing"]),
              file=sys.stderr)
        return 2
    all_nodes = set(intent["lab_nodes"])
    selected = set(x.strip() for x in a.nodes.split(",") if x.strip()) if a.nodes else all_nodes
    unknown = selected - all_nodes
    if unknown:
        print(f"nodes not in the topology: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    checks = I.expected_checks(intent)
    if a.list:
        for c in checks:
            ok, why = I.selected_ok(c, selected)
            print(f"{c['node']:<16} {c['desc']:<70} {'' if ok else 'SKIP: ' + why}")
        print(f"{len(checks)} checks")
        return 0
    out = Path(a.out) if a.out else lab / "results" / f"postcheck-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    raw_root = Path(a.raw_dir) if a.raw_dir else out.with_name(out.stem + "-raw")
    if a.settle:
        print(f"settle {a.settle} s before the first attempt", file=sys.stderr)
        time.sleep(a.settle)
    t0 = time.time()
    attempt, history = 0, []
    while True:
        attempt += 1
        runner = DockerRunner(a.prefix, raw_dir=raw_root / f"attempt-{attempt}")
        ta = time.time()
        results, snap = run_once(intent, checks, selected, runner)
        history.append({"attempt": attempt, "seconds": round(time.time() - ta, 1), "summary": summary(results),
                        "sources": {n: x.get("source") for n, x in snap.items() if x.get("source")}})
        # one attempt took 372 s > --wait 300, so the old rule (elapsed + interval > wait) never re-ran
        if not failed(results) or time.time() - t0 >= a.wait:
            break
        print(f"attempt {attempt} ({history[-1]['seconds']} s): {summary(results)}; again in {a.interval} s", file=sys.stderr)
        time.sleep(a.interval)
    results, retried = retry_errors(intent, checks, results, lambda: DockerRunner(a.prefix, raw_dir=raw_root / "error-retry"))
    if retried:
        history.append({"attempt": "error-retry", "nodes": retried, "summary": summary(results)})
    print(table(results, a.show_skip))
    out.write_text(json.dumps({"time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "attempts": attempt,
                               "history": history, "raw_dir": str(raw_root), "selected": sorted(selected),
                               "summary": summary(results), "results": results}, indent=2) + "\n")
    print(f"results: {out}")
    return 1 if failed(results) else 0


if __name__ == "__main__":
    sys.exit(main())
