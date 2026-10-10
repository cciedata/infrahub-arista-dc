#!/usr/bin/env python3
"""DC3 failure tests: the failure and behavior test catalog for Arista EOS and DC3.

Runs on the lab host next to a deployed containerlab lab. Python 3 standard library only; every action and every check
goes through docker exec (EOS: Cli -p 15, FRR: vtysh / ip / tc, hosts: ping). Lab intent: lab/dc3_intent.py.

Measurement rules:
  - session checks judge only the sessions that were Established before the break (baseline post-check per test)
  - outage = time from the break to the first ping reply after it; no reply until the window end = open-ended
  - node death = every data link of the node down (ip link in the node), never docker pause

  python3 lab/dc3_failtests.py --list                    # the catalog
  python3 lab/dc3_failtests.py --run 3,5,6 --dry-run     # the plan, nothing touched
  python3 lab/dc3_failtests.py --run 3,5,6               # run; results in lab/results/failtests-<UTC>/
Per test: tNN-1-before/, tNN-2-during[-pK]/, tNN-3-after/ (show output per node), tNN.json; summary.json + summary.txt.
Exit code: 0 all run tests PASS, 1 a test FAILED, 2 usage / intent error.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dc3_intent as I  # noqa: E402
import dc3_postcheck as PC  # noqa: E402

SITE = "DC3"
# Test values
PING_SOURCE = f"{SITE}-HOST-PROD"        # the PROD host
PING_TARGET = "198.18.1.10"              # internet stand-in DC3-HOST-ISP-A
PING_INTERVAL = 0.2                      # seconds between pings
PRE_BREAK = 3                            # seconds of replies before each break
SETTLE = 15                              # seconds between a break and the during-checks
RESTORE_WAIT = 120                       # seconds of restore window
AFTER_TIMEOUT = 300                      # re-run the after post-check until no new failure or this many seconds
BOOT_TIMEOUT = 600                       # seconds of boot window (test 15)

# Test 16: tenant to cloud and branch via TRANSIT
TRANSIT_PROBES = {                      # node -> (probe address, originated prefix it lies in, how the address is owned)
    f"{SITE}-AWS": ("172.16.0.1", "172.16.0.0/13", "probe"),        # AWS only originates a blackhole /13: lab probe /32
    f"{SITE}-AZURE": ("172.30.0.1", "172.30.0.0/15", "probe"),      # Azure only originates a blackhole /15: lab probe /32
    f"{SITE}-BR001": ("10.64.1.1", "10.64.0.0/12", "rendered"),     # branch LAN gateway on dummy `lan` (rendered)
}
TRANSIT_PROBE_DEV = "transitprobe"          # dummy interface for the probe /32 on the cloud stand-ins (removed after the test)
TRACEROUTE = ["traceroute", "-n", "-q", "2", "-w", "2", "-m", "15"]   # BusyBox / iputils options in the FRR image
ROLE_PATTERNS = (                   # DC3 node names -> path role
    (r"S\d+", "spine"), (r"L\d+", "leaf"), (r"BL\d+", "border_leaf"), (r"SL\d+", "service_leaf"),
    (r"EDGE\d+", "edge"), (r"DMZ\d+", "dmz"), (r"INTFW\d+", "internal_fw"), (r"INETFW\d+", "internet_fw"),
    (r"SDWANFW\d+", "sdwan_fw"), (r"VPNFW\d+", "vpn_fw"), (r"ISP\d+", "isp_router"), (r"CLOUD\d+", "cloud_router"),
    (r"AWS|AZURE", "cloud_provider"), (r"BR\d+", "branch"), (r"PARTNER\d+", "partner"), (r"ISP-[AB]", "carrier"),
    (r"HOST-.*", "host"), (r"XC\d+", "exchange"),
)
TRANSIT_PROBE_PATHS = {                       # required roles, in order (other fabric hops allowed in between)
    "cloud": ["leaf", "internal_fw", "service_leaf", "border_leaf", "cloud_router"],
    "branch": ["leaf", "internal_fw", "service_leaf", "border_leaf", "sdwan_fw"],
}
TRANSIT_PROBE_ALLOWED = {"leaf", "service_leaf", "internal_fw", "border_leaf", "cloud_router", "sdwan_fw", "cloud_provider", "branch"}
TRANSIT_PROBE_FORBIDDEN = {"internet_fw"}

EOS_CAPTURE = ["show bgp evpn summary", "show ip bgp summary vrf all", "show mlag", "show bfd peers",
               "show vxlan vtep", "show ip route vrf all", "show interfaces status", "show port-channel summary"]
FRR_CAPTURE = ["show bgp vrf all summary", "show bfd peers", "show ip route vrf all"]


# ---------------------------------------------------------------------------------------------- outage
def outage(replies: list[float], t_break: float, t_end: float) -> dict:
    """Outage of one break window from the reply times (monotonic seconds).
    outage_s = first reply at or after the break minus the break; open_ended when no reply came before t_end
    (outage_s is then t_end - t_break, a lower bound). max_gap_s (information only): the longest silence in the window,
    counting the break and the window end as edges. measurable = a reply came in the PRE_BREAK seconds before the break."""
    before = [t for t in replies if t_break - PRE_BREAK - 1 <= t < t_break]
    after = sorted(t for t in replies if t_break <= t <= t_end)
    edges = [t_break] + after + ([t_end] if not after or after[-1] < t_end else [])
    max_gap = max((b - a for a, b in zip(edges, edges[1:])), default=t_end - t_break)
    if after:
        return {"measurable": bool(before), "outage_s": round(after[0] - t_break, 2), "open_ended": False,
                "max_gap_s": round(max_gap, 2), "replies_in_window": len(after)}
    return {"measurable": bool(before), "outage_s": round(t_end - t_break, 2), "open_ended": True,
            "max_gap_s": round(max_gap, 2), "replies_in_window": 0}


def judge_outage(o: dict | None, limit: float | None) -> tuple[str, str]:
    if o is None:
        return "SKIP", "no ping (source or target not in the lab)"
    if not o["measurable"]:
        return "SKIP", "not measurable: no reply before the break"
    seen = (f">= {o['outage_s']} s (open-ended, no reply until the window end)" if o["open_ended"]
            else f"{o['outage_s']} s") + f"; longest gap {o['max_gap_s']} s"
    if limit is None:
        return "INFO", seen
    if o["open_ended"] or o["outage_s"] > limit:
        return "FAIL", seen
    return "PASS", seen


class Pinger:
    """Continuous ping from a test host; each reply line gets a local monotonic timestamp (BusyBox ping prints nothing
    for lost packets, so lost time is measured from the replies)."""

    def __init__(self, runner, node: str, target: str, interval: float = PING_INTERVAL):
        self.argv = [runner.docker, "exec", runner.prefix + node, "ping", "-i", str(interval), target]
        # stopping the local `docker exec` client does not stop the ping inside the container (no TTY): 17 pings were still
        # running on DC3-HOST-PROD after a failure-test run on the full lab, so stop() also kills it in there
        self.kill_argv = [runner.docker, "exec", runner.prefix + node, "pkill", "-f", f"ping -i {interval} {target}"]
        self.replies: list[float] = []
        self.proc = None
        self.thread = None

    def start(self):
        self.proc = subprocess.Popen(self.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        for line in self.proc.stdout:
            if "bytes from" in line:
                self.replies.append(time.monotonic())

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            subprocess.run(self.kill_argv, capture_output=True, timeout=30)


# ---------------------------------------------------------------------------------------------- catalog
def _intf_to(cfg: dict, peer: str) -> list[str]:
    """EOS interfaces whose description starts with the peer device name ( '<peer device> <peer interface>')."""
    return sorted((n for n, i in cfg["interfaces"].items() if (i["description"] or "").split(" ")[0] == peer),
                  key=_natural)


def _natural(s):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)]


def _peer_link_members(cfg: dict) -> list[str]:
    po = (cfg.get("mlag") or {}).get("peer-link", "")
    num = int(po.replace("Port-Channel", "")) if po.startswith("Port-Channel") else None
    return sorted((n for n, i in cfg["interfaces"].items() if num is not None and i["channel_group"] == num), key=_natural)


def _keepalive(cfg: dict) -> list[str]:
    return sorted(n for n, i in cfg["interfaces"].items() if i["vrf"] == I.KEEPALIVE_VRF and n.startswith("Ethernet"))


def _cfg(intent, node):
    n = intent["nodes"].get(node)
    return n["cfg"] if n else None


def _frr_asn(intent, node, vrf="default"):
    cfg = _cfg(intent, node)
    for inst in (cfg or {}).get("bgp", []):
        if inst["vrf"] == vrf:
            return inst["asn"]
    return None


def _frr_vrf_of(intent, node, ip):
    cfg = _cfg(intent, node)
    for inst in (cfg or {}).get("bgp", []):
        if ip in inst["neighbors"]:
            return inst["vrf"]
    return None


def _transit_linux_vrf(intent, fw):
    """Linux VRF of a FW stand-in that peers in TRANSIT (vrf1 in the templates), found through the EOS side."""
    for e in I.eos_nodes(intent):
        for n in (intent["nodes"][e]["cfg"]["bgp"] or {}).get("neighbors", []):
            if n["vrf"] == I.TRANSIT_VRF and I.owner(intent, n["ip"]) == fw:
                return _frr_vrf_of(intent, fw, _eos_side_address(intent, e, n["ip"]))
    return None


def _eos_side_address(intent, eos_node, peer_ip):
    """The EOS node's own address on the subnet of a neighbor address (what the stand-in has as its neighbor)."""
    import ipaddress
    for i in intent["nodes"][eos_node]["cfg"]["interfaces"].values():
        if i["ip"] and ipaddress.ip_address(peer_ip) in ipaddress.ip_interface(i["ip"]).network:
            return i["ip"].split("/")[0]
    return None


def internet_target_behind(intent, carrier_node):
    for addr, h in I.internet_hosts(intent).items():
        if carrier_node in I.carrier_path(intent, h):
            return addr
    return None


def build_catalog(intent: dict) -> list[dict]:
    """The failure / behavior tests of DC3.
    Interfaces and addresses are read from the intent; a test whose nodes are missing is skipped at run time."""
    S = lambda x: f"{SITE}-{x}"  # noqa: E731
    cfg = lambda n: _cfg(intent, n) or {"interfaces": {}, "mlag": None}  # noqa: E731
    ispb_target = internet_target_behind(intent, S("ISP-B")) or "198.18.2.10"
    isp_net = next((n for inst in (_cfg(intent, S("ISP1")) or {}).get("bgp", []) for n in inst["networks"]), None)
    intfw_asn = _frr_asn(intent, S("INTFW1"), _transit_linux_vrf(intent, S("INTFW1")) or "vrf1")
    intfw_vrf = _transit_linux_vrf(intent, S("INTFW1")) or "vrf1"
    inetfw_vrf = _transit_linux_vrf(intent, S("INETFW1")) or "vrf1"
    l11_s1 = _intf_to(cfg(S("L11")), S("S1"))[:1]
    t = []

    def test(id_, title, ref, phases, limit=None, dc3_note="", target=None, capture=()):
        t.append({"id": id_, "title": title, "ref": ref, "phases": phases, "limit": limit, "dc3_note": dc3_note,
                  "target": target, "capture": list(capture)})

    test(1, "Carrier loss: ISP-A, then both carriers", "",
         [{"label": "ISP-A lost", "break": [{"do": "links_down", "node": S("ISP-A"), "ifaces": ["isp"]}],
           "checks": [{"kind": "sessions_kept", "node": S("ISP2"), "peer": S("ISP-B")},
                      {"kind": "frr_route", "node": S("ISP1"), "vrf": "default", "prefix": I.DEFAULT_ROUTE, "present": True},
                      {"kind": "frr_route", "node": S("INETFW1"), "vrf": inetfw_vrf, "prefix": I.DEFAULT_ROUTE, "present": True},
                      {"kind": "ping", "host": PING_SOURCE, "target": ispb_target, "ok": True}]},
          {"label": "both carriers lost", "limit": None,                     # loss expected, outage not judged
           "break": [{"do": "links_down", "node": S("ISP-B"), "ifaces": ["isp"]}],
           "checks": [{"kind": "frr_route", "node": S("INETFW1"), "vrf": inetfw_vrf, "prefix": I.DEFAULT_ROUTE, "present": False},
                      {"kind": "ping", "host": PING_SOURCE, "target": ispb_target, "ok": False}]}],
         limit=5, dc3_note="0/0 from the ISP routers is conditional (withdrawn when both carriers are lost) to be confirmed in a lab run; "
                           "outage limit 5 s for phase 1 only",
         target=ispb_target, capture=[S("ISP1"), S("ISP2"), S("INETFW1")])
    test(2, "ISP router down: DC3-ISP1", "",
         [{"label": "ISP1 isolated", "break": [{"do": "links_down", "node": S("ISP1"), "ifaces": None}],
           "checks": [{"kind": "sessions_kept", "node": S("INETFW1"), "peer": S("ISP2")},
                      {"kind": "frr_route", "node": S("INETFW1"), "vrf": inetfw_vrf, "prefix": I.DEFAULT_ROUTE, "present": True},
                      {"kind": "ping", "host": PING_SOURCE, "target": ispb_target, "ok": True}]}],
         limit=5, dc3_note="node death = every data link down; limit 5 s", target=ispb_target,
         capture=[S("ISP1"), S("ISP2"), S("INETFW1"), S("EDGE1")])
    test(3, "Spine down: DC3-S1", "",
         [{"label": "S1 isolated", "break": [{"do": "links_down", "node": S("S1"), "ifaces": None}],
           "checks": [{"kind": "sessions_kept", "node": S("L11"), "peer": S("S2")},
                      {"kind": "eos_route", "node": S("L11"), "vrf": "PROD", "prefix": I.DEFAULT_ROUTE, "present": True},
                      {"kind": "ping", "host": PING_SOURCE, "target": PING_TARGET, "ok": True}]}],
         limit=5, dc3_note="DC3 takes every S1 link down",
         capture=[S("L11"), S("S2")])
    test(4, "Border leaf down (DC3-BL1), then service leaf down (DC3-SL1)", "",
         [{"label": "BL1 isolated", "break": [{"do": "links_down", "node": S("BL1"), "ifaces": None}],
           "checks": [{"kind": "sessions_kept", "node": S("BL2"), "peer": "*standin"},
                      {"kind": "ping", "host": PING_SOURCE, "target": PING_TARGET, "ok": True}],
           "restore_now": True},
          {"label": "SL1 isolated", "break": [{"do": "links_down", "node": S("SL1"), "ifaces": None}],
           "checks": [{"kind": "sessions_kept", "node": S("SL2"), "peer": "*standin"},
                      {"kind": "ping", "host": PING_SOURCE, "target": PING_TARGET, "ok": True}],
           "restore_now": True}],
         limit=5, dc3_note="DC3 takes every link of the leaf down",
         capture=[S("BL1"), S("BL2"), S("SL1"), S("SL2")])
    test(5, "One leaf-spine link down: DC3-L11 " + (l11_s1[0] if l11_s1 else "?") + " -> DC3-S1", "",
         [{"label": "link down", "break": [{"do": "eos_shut", "node": S("L11"), "ifaces": l11_s1}],
           "checks": [{"kind": "sessions_kept", "node": S("L11"), "peer": S("S1"), "kinds": ["overlay"]},
                      {"kind": "sessions_kept", "node": S("L11"), "peer": S("S2")}]}],
         limit=2, capture=[S("L11"), S("S1")])
    test(6, "MLAG peer-link down (DC3-BL1 - DC3-BL2)", "",
         [{"label": "peer-link down", "break": [{"do": "eos_shut", "node": S("BL1"), "ifaces": _peer_link_members(cfg(S("BL1")))}],
           "checks": [{"kind": "ping", "host": PING_SOURCE, "target": PING_TARGET, "ok": True}]}],
         limit=5, capture=[S("BL1"), S("BL2")])
    test(7, "MLAG keepalive down (DC3-BL1 " + "/".join(_keepalive(cfg(S("BL1")))) + ")", "",
         [{"label": "keepalive down", "break": [{"do": "eos_shut", "node": S("BL1"), "ifaces": _keepalive(cfg(S("BL1")))}],
           "checks": [{"kind": "mlag", "node": S("BL1"), "expect": {"state": "active", "peerLinkStatus": "up"}},
                      {"kind": "ping", "host": PING_SOURCE, "target": PING_TARGET, "ok": True}]}],
         limit=0.5, capture=[S("BL1"), S("BL2")])
    test(8, "One FW port-channel member down: DC3-INTFW1 port19", "",
         [{"label": "port19 down", "break": [{"do": "links_down", "node": S("INTFW1"), "ifaces": ["port19"]}],
           "checks": [{"kind": "sessions_kept", "node": S("SL1"), "peer": S("INTFW1")},
                      {"kind": "sessions_kept", "node": S("SL2"), "peer": S("INTFW1")},
                      {"kind": "bond", "node": S("INTFW1"), "bond": "agg-inside", "min_up": 1},
                      {"kind": "ping", "host": PING_SOURCE, "target": PING_TARGET, "ok": True}]}],
         limit=2, dc3_note="checks only the sessions up before the break",
         capture=[S("SL1"), S("SL2"), S("INTFW1")])
    test(9, "Silent failure toward the Internal FW (netem 100 % loss on port19 and port20)", "",
         [{"label": "100 % loss", "break": [{"do": "netem", "node": S("INTFW1"), "ifaces": ["port19", "port20"]}],
           "settle": 5,
           "checks": [{"kind": "bfd_down", "node": S("SL1"), "peer": S("INTFW1")},
                      {"kind": "bfd_down", "node": S("SL2"), "peer": S("INTFW1")},
                      {"kind": "sessions_down", "node": S("SL1"), "peer": S("INTFW1")},
                      {"kind": "sessions_down", "node": S("SL2"), "peer": S("INTFW1")}]}],
         dc3_note="BFD 999 ms x 3: sessions down after about 3 s; check at 5 s; BLOCKED-PLATFORM if cEOS runs no BFD; "
                  "outage measured, not judged",
         capture=[S("SL1"), S("SL2"), S("INTFW1")])
    test(10, "Internal FW stand-in down: DC3-INTFW1", "",
         [{"label": "INTFW1 isolated", "break": [{"do": "links_down", "node": S("INTFW1"), "ifaces": None}],
           "checks": [{"kind": "eos_route", "node": S("L11"), "vrf": "PROD", "prefix": I.DEFAULT_ROUTE, "present": False},
                      {"kind": "ping", "host": PING_SOURCE, "target": PING_TARGET, "ok": False}]}],
         dc3_note="DC3 takes its links down; outage measured, not judged",
         capture=[S("L11"), S("SL1"), S("SL2")])
    test(11, "Internet FW stand-in down: DC3-INETFW1", "",
         [{"label": "INETFW1 isolated", "break": [{"do": "links_down", "node": S("INETFW1"), "ifaces": None}],
           "checks": [{"kind": "eos_route", "node": S("BL1"), "vrf": I.TRANSIT_VRF, "prefix": I.DEFAULT_ROUTE, "present": False},
                      {"kind": "ping", "host": PING_SOURCE, "target": PING_TARGET, "ok": False}]}],
         dc3_note="outage measured, not judged", capture=[S("BL1"), S("BL2")])
    test(12, "Filter test: Internal FW announces 10.99.0.0/16 in TRANSIT", "",
         [{"label": "10.99.0.0/16 announced",
           "break": [{"do": "vtysh", "node": S("INTFW1"),
                      "lines": ["configure terminal", f"ip route 10.99.0.0/16 blackhole vrf {intfw_vrf}",
                                f"router bgp {intfw_asn} vrf {intfw_vrf}", "address-family ipv4 unicast",
                                "redistribute static", "end"],
                      "undo": ["configure terminal", f"router bgp {intfw_asn} vrf {intfw_vrf}", "address-family ipv4 unicast",
                               "no redistribute static", "exit-address-family", "exit",
                               f"no ip route 10.99.0.0/16 blackhole vrf {intfw_vrf}", "end"]}],
           "checks": [{"kind": "eos_route", "node": S("SL1"), "vrf": I.TRANSIT_VRF, "prefix": "10.99.0.0/16", "present": False},
                      {"kind": "eos_route", "node": S("SL2"), "vrf": I.TRANSIT_VRF, "prefix": "10.99.0.0/16", "present": False}]}],
         limit=0.5, dc3_note="the stand-in's own RM-TRANSIT-TO-LEAF (PL-TRANSIT-SUMMARY) already drops the prefix, so the leaf "
                  "filter is not reached; limit 0.5 s",
         capture=[S("SL1"), S("SL2"), S("INTFW1")])
    test(13, "BGP graceful restart on an ISP router (bgpd restart on DC3-ISP1)", "",
         [{"label": "bgpd restarted",
           "break": [{"do": "sh", "node": S("ISP1"),
                      "cmd": "pkill -x bgpd; sleep 1; /usr/lib/frr/bgpd -d -A 127.0.0.1 -K; sleep 2; vtysh -f /etc/frr/frr.conf"}],
           "settle": 3,
           "checks": ([{"kind": "frr_route", "node": S("ISP-A"), "vrf": "default", "prefix": isp_net, "present": True}]
                      if isp_net else [])}],
         limit=0.5, dc3_note="EOS switches are graceful-restart helpers; bgpd restarts with -K (keeps the kernel "
                  "routes); limit 0.5 s",
         capture=[S("ISP1"), S("ISP-A")])
    test(14, "ISP1 return (DC3-ISP1 down, then back)", "",
         [{"label": "ISP1 isolated", "break": [{"do": "links_down", "node": S("ISP1"), "ifaces": None}],
           "checks": [{"kind": "frr_route", "node": S("INETFW1"), "vrf": inetfw_vrf, "prefix": I.DEFAULT_ROUTE, "present": True}],
           "restore_now": True,
           "after_checks": [{"kind": "sessions_up", "node": S("INETFW1"), "peer": S("ISP1")},
                            {"kind": "sessions_up", "node": S("ISP2"), "peer": S("ISP1")}]}],
         dc3_note="the FRR ISP stand-ins run no VRRP, so the BGP return is tested; "
                  "limit 5 s", limit=5, target=ispb_target, capture=[S("ISP1"), S("ISP2"), S("INETFW1")])
    test(15, "Node reload with its rendered config: DC3-L11", "",
         [{"label": "reload", "break": [{"do": "eos_reload", "node": S("L11")}], "checks": [], "restore_now": True,
           "after_checks": [{"kind": "sessions_up", "node": S("L11"), "peer": S("S1")},
                            {"kind": "sessions_up", "node": S("L11"), "peer": S("S2")},
                            {"kind": "ping", "host": PING_SOURCE, "target": (_cfg(intent, PING_SOURCE) or {}).get("gateway"),
                             "ok": True}]}],
         dc3_note="cEOS: 'reload now' inside the container (does it restart EOS with "
                  "the startup-config and keep the links?); outage measured, not judged", capture=[S("L11"), S("S1")])
    probes = [{"do": "probe_addr", "node": n, "addr": a} for n, (a, _p, how) in TRANSIT_PROBES.items() if how == "probe"]
    path_checks = []
    for n, (a, prefix, _how) in TRANSIT_PROBES.items():
        kind = "branch" if role_of(n) == "branch" else "cloud"
        path_checks.append({"kind": "path", "host": PING_SOURCE, "target": a, "dest": n, "prefix": prefix,
                            "via": TRANSIT_PROBE_PATHS[kind], "needs": [S("L11"), S("SL1"), S("SL2"), S("BL1"), S("BL2"),
                                                              S("INTFW1"), n] + ([S("SDWANFW1")] if kind == "branch" else
                                                                                  [S("CLOUD1"), S("CLOUD2")])})
    test(16, "Tenant to cloud and branch via TRANSIT", "",
         [{"label": "path probe", "break": probes, "settle": 3, "checks": path_checks}],
         dc3_note="non-destructive: TRANSIT policy unchanged (service leaves send only 0/0 to the Internal FW); ping and "
                  "traceroute from the PROD host must go leaf -> Internal FW -> service leaf -> border leaf -> cloud router / "
                  "SD-WAN FW, never via DC3-INETFW1. AWS / Azure own no address in their /13 / /15 (blackhole only), so a "
                  "lab probe /32 is added on a dummy for the test and removed after it",
         capture=[S("L11"), S("SL1"), S("BL1"), S("INTFW1"), S("CLOUD1"), S("SDWANFW1")])
    return t


# ---------------------------------------------------------------------------------------------- path test (test 16)
def role_of(node: str | None, site: str = SITE) -> str | None:
    if not node:
        return None
    name = node.removeprefix(f"{site}-")
    for pat, role in ROLE_PATTERNS:
        if re.fullmatch(pat, name):
            return role
    return None


def classify_hop(intent: dict, ip: str | None) -> dict:
    """One traceroute hop -> {ip, node, role}. Addresses come from the rendered files: an address held by one node names
    it; an address held by several nodes of one role (MLAG anycast VTEP) or an EOS `ip address virtual` (anycast gateway,
    every leaf) gives the role only. '*' (no answer) -> role None, node None."""
    if not ip:
        return {"ip": None, "node": None, "role": None}
    owners = intent["owners"].get(ip, [])
    if len(owners) == 1:
        return {"ip": ip, "node": owners[0], "role": role_of(owners[0])}
    roles = {role_of(o) for o in owners}
    if owners and len(roles) == 1:
        return {"ip": ip, "node": "/".join(owners), "role": roles.pop()}
    vnodes = [e for e in I.eos_nodes(intent) for i in intent["nodes"][e]["cfg"]["interfaces"].values()
              if i["virtual_ip"] and i["virtual_ip"].split("/")[0] == ip]
    if vnodes:
        roles = {role_of(v) for v in vnodes}
        return {"ip": ip, "node": "anycast-gw", "role": roles.pop() if len(roles) == 1 else "leaf"}
    return {"ip": ip, "node": None, "role": "unknown"}


def parse_traceroute(text: str) -> list[str | None]:
    """Hop list from `traceroute -n` (BusyBox or iputils): the first address on each numbered line, None for '* *'."""
    hops = []
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if not m:
            continue
        a = re.search(r"\b(\d+\.\d+\.\d+\.\d+)\b", m.group(2))
        hops.append(a.group(1) if a else None)
    return hops


def judge_path(intent: dict, hops: list[str | None], target: str, via: list[str]) -> tuple[str, str, list[dict]]:
    """PASS when the target answered last, no hop is a forbidden role (Internet FW), no hop is outside the allowed roles,
    and the required roles appear in order (other allowed hops and '*' in between are tolerated; a '*' may stand for a
    required role)."""
    cls = [classify_hop(intent, h) for h in hops]
    names = " > ".join((c["node"] or ("?" if c["ip"] else "*")) + (f" ({c['ip']})" if c["ip"] else "") for c in cls) or "(no hops)"
    if not hops or hops[-1] != target:
        return "FAIL", f"target {target} not reached: {names}", cls
    body = cls[:-1]
    bad = [c for c in body if c["role"] in TRANSIT_PROBE_FORBIDDEN]
    if bad:
        return "FAIL", "via " + ", ".join(c["node"] for c in bad) + f": {names}", cls
    odd = [c for c in body if c["role"] is not None and c["role"] not in TRANSIT_PROBE_ALLOWED]
    if odd:
        return "FAIL", "unexpected hop " + ", ".join(f"{c['node'] or c['ip']} ({c['role']})" for c in odd) + f": {names}", cls
    # a silent hop ('*', role None) may stand for one required role (EOS hops often do not answer traceroute); the
    # answering required roles must still appear in order, and the silent hops must be enough to fill the gaps
    roles = [c["role"] for c in body]

    def fits(i: int, k: int) -> bool:
        if k == len(via):
            return True
        if len(roles) - i < len(via) - k:
            return False
        if roles[i] == via[k] or roles[i] is None:
            if fits(i + 1, k + 1):
                return True
        return fits(i + 1, k)

    if not fits(0, 0):
        k = 0
        for r in roles:
            if k < len(via) and r == via[k]:
                k += 1
        silent = roles.count(None)
        return "FAIL", f"missing {', '.join(via[k:])} in order ({silent} silent hops): {names}", cls
    return "PASS", names, cls


def test_nodes(test: dict) -> set[str]:
    """Every node a test acts on or checks (the ping source and target are optional: outage SKIP without them)."""
    out = set()
    for ph in test["phases"]:
        for a in ph["break"]:
            out.add(a["node"])
        for c in ph.get("checks", []) + ph.get("after_checks", []):
            out.add(c.get("node") or c.get("host"))
            if c.get("peer") and not c["peer"].startswith("*"):
                out.add(c["peer"])
            out.update(c.get("needs", []))
    out.discard(None)
    return out


def select_tests(catalog: list[dict], spec: str | None) -> list[dict]:
    """--run '3,5,6' or '1-4,15' (catalog order kept); None or 'all' = every test."""
    if not spec or spec == "all":
        return list(catalog)
    ids = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            ids |= set(range(int(a), int(b) + 1))
        else:
            ids.add(int(part))
    known = {t["id"] for t in catalog}
    unknown = sorted(ids - known)
    if unknown:
        raise ValueError(f"unknown test id(s): {unknown}; catalog: {sorted(known)}")
    return [t for t in catalog if t["id"] in ids]


def missing_nodes(test: dict, lab_nodes: set[str]) -> list[str]:
    return sorted(n for n in test_nodes(test) if n not in lab_nodes)


def describe(test: dict, lab_nodes: set[str] | None = None, ping_target: str = PING_TARGET) -> str:
    lines = [f"Test {test['id']}: {test['title']}" + (f"  [{test['ref']}]" if test.get("ref") else "")]
    if test["dc3_note"]:
        lines.append(f"  DC3: {test['dc3_note']}")
    for k, ph in enumerate(test["phases"], 1):
        lines.append(f"  phase {k} ({ph['label']}):")
        for a in ph["break"]:
            lines.append(f"    break    {action_text(a)}")
        lines.append(f"    wait     {ph.get('settle', SETTLE)} s, then check:")
        for c in ph.get("checks", []):
            lines.append(f"    check    {check_text(c)}")
        if ph.get("restore_now"):
            for a in ph["break"]:
                lines.append(f"    restore  {action_text(a, undo=True)}")
            lines.append(f"    wait     {RESTORE_WAIT} s")
            for c in ph.get("after_checks", []):
                lines.append(f"    after    {check_text(c)}")
    late = [a for ph in test["phases"] if not ph.get("restore_now") for a in ph["break"]]
    for a in reversed(late):
        lines.append(f"  restore  {action_text(a, undo=True)}")
    lines.append(f"  outage   ping {PING_SOURCE} -> {test['target'] or ping_target} every {PING_INTERVAL} s; limit "
                 + (f"{test['limit']} s" if test["limit"] is not None else "none (measured, not judged)"))
    lines.append(f"  after    post-check until no new failure vs. before (max {AFTER_TIMEOUT} s)")
    if lab_nodes is not None:
        miss = missing_nodes(test, lab_nodes)
        if miss:
            lines.append(f"  SKIP     not in the lab: {', '.join(miss)}")
    return "\n".join(lines)


def action_text(a: dict, undo: bool = False) -> str:
    d = a["do"]
    if d == "links_down":
        what = ", ".join(a["ifaces"]) if a["ifaces"] else "every data link"
        return f"{a['node']}: ip link set {what} {'up' if undo else 'down'}"
    if d == "eos_shut":
        return f"{a['node']}: {'no shutdown' if undo else 'shutdown'} {', '.join(a['ifaces']) or '(no interface found)'}"
    if d == "netem":
        return f"{a['node']}: tc qdisc {'del' if undo else 'replace'} netem loss 100 % on {', '.join(a['ifaces'])}"
    if d == "vtysh":
        return f"{a['node']}: vtysh " + " / ".join(a["undo"] if undo else a["lines"])
    if d == "sh":
        return f"{a['node']}: (self-restoring)" if undo else f"{a['node']}: sh -c '{a['cmd']}'"
    if d == "probe_addr":
        return (f"{a['node']}: ip link del {TRANSIT_PROBE_DEV}" if undo else
                f"{a['node']}: ip link add {TRANSIT_PROBE_DEV} type dummy; ip addr add {a['addr']}/32 dev {TRANSIT_PROBE_DEV}; up")
    if d == "eos_reload":
        return f"{a['node']}: wait until Cli answers (max {BOOT_TIMEOUT} s)" if undo else f"{a['node']}: Cli reload now"
    return str(a)


def check_text(c: dict) -> str:
    k = c["kind"]
    if k in ("sessions_kept", "sessions_down", "sessions_up", "bfd_down"):
        peer = "every stand-in" if c["peer"] == "*standin" else c["peer"]
        verb = {"sessions_kept": "BGP sessions up before the break stay Established",
                "sessions_down": "BGP sessions not Established", "sessions_up": "BGP sessions Established",
                "bfd_down": "BFD sessions down"}[k]
        return f"{c['node']} -> {peer}: {verb}" + (f" ({', '.join(c['kinds'])})" if c.get("kinds") else "")
    if k in ("eos_route", "frr_route"):
        return f"{c['node']} VRF {c['vrf']} {c['prefix']} {'present' if c['present'] else 'absent'}"
    if k == "ping":
        return f"{c['host']} ping {c['target']} {'succeeds' if c['ok'] else 'fails'}"
    if k == "mlag":
        return f"{c['node']} MLAG " + ", ".join(f"{a}={b}" for a, b in c["expect"].items())
    if k == "path":
        return (f"{c['host']} -> {c['target']} ({c['dest']}, {c['prefix']}): replies; path "
                + " > ".join(c["via"]) + "; never via the Internet FW")
    if k == "bond":
        return f"{c['node']} {c['bond']}: at least {c['min_up']} member up"
    return str(c)


# ---------------------------------------------------------------------------------------------- actions
class Lab:
    def __init__(self, intent, runner, lab_nodes):
        self.intent, self.r, self.nodes = intent, runner, lab_nodes
        self.expected = {c["id"]: c for c in I.expected_checks(intent)}

    def data_links(self, node):
        return sorted({li for li, peer, _pi in I.link_peers(self.intent, node) if peer in self.nodes}, key=_natural)

    def act(self, a: dict, undo: bool = False) -> list[str]:
        d, node, log = a["do"], a["node"], []

        def run(argv, timeout=None):
            rc, out, err = self.r.exec(node, argv, timeout)
            log.append(f"{node}$ {' '.join(argv)} -> rc {rc} {(out + err).strip()[:200]}")
            return rc

        if d == "links_down":
            for i in a["ifaces"] or self.data_links(node):
                run(["ip", "link", "set", i, "up" if undo else "down"])
        elif d == "eos_shut":
            cmds = ["configure"] + [x for i in a["ifaces"] for x in (f"interface {i}", "no shutdown" if undo else "shutdown")] + ["end"]
            run(["Cli", "-p", "15", "-c", "\n".join(cmds)])
        elif d == "netem":
            for i in a["ifaces"]:
                run(["tc", "qdisc", "del", "dev", i, "root"] if undo else
                    ["tc", "qdisc", "replace", "dev", i, "root", "netem", "loss", "100%"])
        elif d == "vtysh":
            argv = ["vtysh"]
            for line in (a["undo"] if undo else a["lines"]):
                argv += ["-c", line]
            run(argv)
        elif d == "sh":
            if not undo:
                run(["sh", "-c", a["cmd"]])
        elif d == "probe_addr":
            if undo:
                run(["ip", "link", "del", TRANSIT_PROBE_DEV])
            else:
                run(["ip", "link", "add", TRANSIT_PROBE_DEV, "type", "dummy"])
                run(["ip", "addr", "replace", f"{a['addr']}/32", "dev", TRANSIT_PROBE_DEV])
                run(["ip", "link", "set", TRANSIT_PROBE_DEV, "up"])
        elif d == "eos_reload":
            if undo:
                t0 = time.time()
                while time.time() - t0 < BOOT_TIMEOUT:
                    rc, out, _ = self.r.exec(node, ["Cli", "-p", "15", "-c", "show version"], 30)
                    if rc == 0 and "Software image version" in out:
                        log.append(f"{node}: Cli answers after {int(time.time() - t0)} s")
                        break
                    time.sleep(10)
                else:
                    log.append(f"{node}: no Cli answer within {BOOT_TIMEOUT} s")
            else:
                run(["Cli", "-p", "15", "-c", "reload now"], 30)
                time.sleep(20)
        return log

    def capture(self, nodes, folder: Path, extra: dict | None = None):
        folder.mkdir(parents=True, exist_ok=True)
        for n in sorted(nodes):
            if n not in self.nodes:
                continue
            kind = self.intent["nodes"][n]["kind"]
            parts = []
            if kind == "eos":
                for c in EOS_CAPTURE:
                    rc, out = self.r.eos_text(n, c)
                    parts.append(f"===== {c} (rc {rc})\n{out}")
            elif kind == "frr":
                for c in FRR_CAPTURE:
                    rc, out = self.r.frr_text(n, c)
                    parts.append(f"===== {c} (rc {rc})\n{out}")
                for bond in ("agg-inside", "agg-outside", "agg-dmz", "BE21", "BE31"):
                    rc, out, _ = self.r.exec(n, ["cat", f"/proc/net/bonding/{bond}"])
                    if rc == 0:
                        parts.append(f"===== /proc/net/bonding/{bond}\n{out}")
            (folder / f"{n}.txt").write_text("\n".join(parts) + "\n")
        if extra:
            (folder / "postcheck.json").write_text(json.dumps(extra, indent=2) + "\n")


# ---------------------------------------------------------------------------------------------- checks during a test
def eval_checks(lab: Lab, specs: list[dict], pc_checks: list[dict], baseline: dict | None) -> list[dict]:
    nodes = {c.get("node") or c.get("host") for c in specs}
    if not specs:
        return []
    snap = PC.collect(lab.intent, nodes & lab.nodes, lab.r,
                      [{"type": "ping", "node": c["host"], "target": c["target"]} for c in specs if c["kind"] == "ping"])
    judged = {r["id"]: r for r in PC.evaluate(pc_checks, snap, lab.nodes, None)}
    out = []
    standins = {n for n in I.frr_nodes(lab.intent)}
    for c in specs:
        k, node = c["kind"], c.get("node") or c.get("host")
        if node not in lab.nodes:
            out.append({"check": check_text(c), "result": "SKIP", "seen": f"{node} not in the lab"})
            continue
        if k == "path":
            miss = [n for n in c.get("needs", []) if n not in lab.nodes]
            if miss:
                out.append({"check": check_text(c), "result": "SKIP", "seen": "not in the lab: " + ", ".join(miss)})
                continue
            p = lab.r.ping(node, c["target"])
            rc, txt, err = lab.r.exec(node, TRACEROUTE + [c["target"]], 60)
            res, seen, _cls = judge_path(lab.intent, parse_traceroute(txt + err), c["target"], c["via"])
            if not p or p[1] == 0:
                res, seen = "FAIL", f"ping {p[1] if p else 0}/{p[0] if p else '?'}; traceroute: {seen}"
            else:
                seen = f"ping {p[1]}/{p[0]}; traceroute: {seen}"
            out.append({"check": check_text(c), "result": res, "seen": seen, "traceroute": txt})
            continue
        why = needs_absent(lab, c)
        if why:
            out.append({"check": check_text(c), "result": "SKIP", "seen": why})
            continue
        if k in ("sessions_kept", "sessions_down", "sessions_up", "bfd_down"):
            ctype = "bfd" if k == "bfd_down" else "bgp"
            mine = [x for x in pc_checks if x["node"] == node and x["type"] == ctype
                    and (x["peer_node"] in standins if c["peer"] == "*standin" else x["peer_node"] == c["peer"])
                    and (not c.get("kinds") or x["kind"] in c["kinds"])]
            if k == "sessions_kept":
                mine = [x for x in mine if (baseline or {}).get(x["id"]) == "PASS"]
                if not mine:
                    out.append({"check": check_text(c), "result": "SKIP", "seen": "no such session up before the break"})
                    continue
            if not mine:
                out.append({"check": check_text(c), "result": "SKIP", "seen": "no such session in the intent"})
                continue
            res = [judged[x["id"]] for x in mine]
            if k == "bfd_down" and all(r["result"] == "BLOCKED-PLATFORM" for r in res):
                out.append({"check": check_text(c), "result": "BLOCKED-PLATFORM", "seen": "no BFD session on the node"})
                continue
            want_up = k in ("sessions_kept", "sessions_up")
            bad = [r for r in res if (r["result"] == "PASS") != want_up]
            seen = "; ".join(f"{r['check'].split(' (')[0]}: {r['seen']}" for r in res)
            out.append({"check": check_text(c), "result": "FAIL" if bad else "PASS", "seen": seen})
        elif k == "eos_route":
            r = (snap.get(node) or {}).get("routes")
            if r is None:
                out.append({"check": check_text(c), "result": "ERROR", "seen": "show ip route failed"})
                continue
            have = c["prefix"] in r.get(c["vrf"], set())
            out.append({"check": check_text(c), "result": "PASS" if have == c["present"] else "FAIL",
                        "seen": "present" if have else "absent"})
        elif k == "frr_route":
            cmd = "show ip route" + ("" if c["vrf"] == "default" else f" vrf {c['vrf']}")
            doc = lab.r.frr_json(node, cmd)
            if doc is None:
                out.append({"check": check_text(c), "result": "ERROR", "seen": f"{cmd} json failed"})
                continue
            have = c["prefix"] in doc
            out.append({"check": check_text(c), "result": "PASS" if have == c["present"] else "FAIL",
                        "seen": "present" if have else "absent"})
        elif k == "ping":
            p = (snap.get(node) or {}).get("ping", {}).get(c["target"])
            if p is None:
                out.append({"check": check_text(c), "result": "ERROR", "seen": "no ping summary"})
                continue
            out.append({"check": check_text(c), "result": "PASS" if (p[1] > 0) == c["ok"] else "FAIL", "seen": f"{p[1]}/{p[0]}"})
        elif k == "mlag":
            m = (snap.get(node) or {}).get("mlag")
            if m is None:
                out.append({"check": check_text(c), "result": "ERROR", "seen": "show mlag failed"})
                continue
            good = all(m.get(a) == b for a, b in c["expect"].items())
            out.append({"check": check_text(c), "result": "PASS" if good else "FAIL",
                        "seen": ", ".join(f"{a}={m.get(a)}" for a in c["expect"])})
        elif k == "bond":
            rc, txt, _ = lab.r.exec(node, ["cat", f"/proc/net/bonding/{c['bond']}"])
            up = bond_members_up(txt) if rc == 0 else None
            out.append({"check": check_text(c), "result": "ERROR" if up is None else ("PASS" if up >= c["min_up"] else "FAIL"),
                        "seen": "no bond file" if up is None else f"{up} member(s) up"})
    return out


def needs_absent(lab: Lab, c: dict) -> str | None:
    """Reason to SKIP a check whose route source or ping target is not in the lab (a subset lab such as the smoke)."""
    if c["kind"] == "eos_route":
        x = lab.expected.get(f"route:{c['node']}:{c['vrf']}:{c['prefix']}")
        if x is None:
            return None
        ok, why = I.selected_ok(x, lab.nodes)
        return None if ok else why
    if c["kind"] == "ping":
        o = I.owner(lab.intent, c["target"] or "")
        if o is None:
            for e in I.eos_nodes(lab.intent):             # an anycast gateway: on the leaf of the host
                for i in lab.intent["nodes"][e]["cfg"]["interfaces"].values():
                    if i["virtual_ip"] and i["virtual_ip"].split("/")[0] == c["target"] and e in lab.nodes:
                        return None
            return f"target {c['target']} not found in the rendered files"
        return None if o in lab.nodes else f"target {c['target']} ({o}) not in the lab"
    return None


def bond_members_up(text: str) -> int:
    """Slave interfaces with 'MII Status: up' in /proc/net/bonding/<bond>."""
    n, in_slave = 0, False
    for line in text.splitlines():
        if line.startswith("Slave Interface:"):
            in_slave = True
        elif in_slave and line.startswith("MII Status:"):
            n += line.split(":", 1)[1].strip() == "up"
            in_slave = False
    return n


# ---------------------------------------------------------------------------------------------- run one test
def run_test(test, lab: Lab, pc_checks, out_dir: Path, ping_source: str, ping_target: str = PING_TARGET) -> dict:
    tid = f"t{test['id']:02d}"
    res = {"id": test["id"], "title": test["title"], "ref": test["ref"], "dc3_note": test["dc3_note"],
           "limit_s": test["limit"], "phases": [], "log": []}
    miss = missing_nodes(test, lab.nodes)
    if miss:
        res.update(result="SKIP", reason="not in the lab: " + ", ".join(miss))
        return res
    involved = test_nodes(test) | set(test["capture"])
    before_res, _ = PC.run_once(lab.intent, pc_checks, lab.nodes, lab.r)
    baseline = {r["id"]: r["result"] for r in before_res}
    lab.capture(involved, out_dir / f"{tid}-1-before", {"summary": PC.summary(before_res), "results": before_res})
    target = test["target"] or ping_target
    pinger = None
    if ping_source in lab.nodes:
        pinger = Pinger(lab.r, ping_source, target)
        pinger.start()
        time.sleep(PRE_BREAK + 1)
    pending = []
    windows = []
    for k, ph in enumerate(test["phases"], 1):
        t_break = time.monotonic()
        for a in ph["break"]:
            res["log"] += lab.act(a)
        time.sleep(ph.get("settle", SETTLE))
        checks = eval_checks(lab, ph.get("checks", []), pc_checks, baseline)
        lab.capture(involved, out_dir / (f"{tid}-2-during" + (f"-p{k}" if len(test["phases"]) > 1 else "")))
        phase = {"label": ph["label"], "checks": checks,
                 "limit_s": ph["limit"] if "limit" in ph else test["limit"]}   # a phase may override the test limit
        if ph.get("restore_now"):
            t_end = time.monotonic()
            for a in reversed(ph["break"]):
                res["log"] += lab.act(a, undo=True)
            windows.append((phase, t_break, t_end))
            time.sleep(RESTORE_WAIT)
            phase["after_checks"] = eval_checks(lab, ph.get("after_checks", []), pc_checks, None)
        else:
            pending += ph["break"]
            windows.append((phase, t_break, None))
        res["phases"].append(phase)
    t_restore = time.monotonic()
    for a in reversed(pending):
        res["log"] += lab.act(a, undo=True)
    if pinger:
        time.sleep(2)
        pinger.stop()
    for phase, t_break, t_end in windows:
        if not pinger:
            phase["outage"] = None
        else:
            # window end: the restore of this phase, else the start of the next phase's break, else the final restore
            nxt = [tb for _p, tb, _e in windows if tb > t_break]
            end = t_end or (min(nxt) if nxt else t_restore)
            phase["outage"] = outage(pinger.replies, t_break, end)
        phase["outage_result"], phase["outage_seen"] = judge_outage(phase["outage"], phase["limit_s"])
    # after: post-check until no new failure compared with before, or AFTER_TIMEOUT
    bad_before = {r["id"] for r in before_res if r["result"] in ("FAIL", "ERROR")}
    t0 = time.time()
    time.sleep(0 if any(ph.get("restore_now") for ph in test["phases"]) and not pending else RESTORE_WAIT)
    while True:
        after_res, _ = PC.run_once(lab.intent, pc_checks, lab.nodes, lab.r)
        new = sorted(r["id"] for r in after_res if r["result"] in ("FAIL", "ERROR") and r["id"] not in bad_before)
        if not new or time.time() - t0 > AFTER_TIMEOUT:
            break
        time.sleep(20)
    lab.capture(involved, out_dir / f"{tid}-3-after", {"summary": PC.summary(after_res), "results": after_res})
    res["new_failures_after_restore"] = new
    res["postcheck_before"], res["postcheck_after"] = PC.summary(before_res), PC.summary(after_res)
    verdicts = [c["result"] for ph in res["phases"] for c in ph["checks"] + ph.get("after_checks", [])]
    verdicts += [ph["outage_result"] for ph in res["phases"]]
    res["result"] = "FAIL" if ("FAIL" in verdicts or "ERROR" in verdicts or new) else "PASS"
    return res


def result_rows(results: list[dict]) -> str:
    lines = [f"{'#':>2}  {'Scenario':<62} {'Result':<6} {'Outage (s)':<34} Limit (s)", "-" * 120]
    for r in results:
        out = "; ".join(ph.get("outage_seen", "") for ph in r.get("phases", [])) or r.get("reason", "")
        lim = "-" if r.get("limit_s") is None else str(r["limit_s"])
        lines.append(f"{r['id']:>2}  {r['title'][:62]:<62} {r['result']:<6} {out[:34]:<34} {lim}")
        for ph in r.get("phases", []):
            for c in ph["checks"] + ph.get("after_checks", []):
                lines.append(f"      {c['result']:<16} {c['check'][:70]}  [{str(c['seen'])[:60]}]")
        if r.get("new_failures_after_restore"):
            lines.append(f"      new failures after restore: {', '.join(r['new_failures_after_restore'][:8])}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lab-dir", default=str(I.LAB))
    ap.add_argument("--topology", help="topology file (default <lab-dir>/dc3.clab.yml)")
    ap.add_argument("--nodes", help="comma-separated nodes deployed (default: every node of the topology)")
    ap.add_argument("--run", help="test ids, e.g. 3,5,6 or 1-4 (default: all)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit; nothing is touched")
    ap.add_argument("--list", action="store_true", help="print the catalog (id, title, DC3 note) and exit")
    ap.add_argument("--prefix", default=PC.PREFIX)
    ap.add_argument("--ping-source", default=PING_SOURCE, help=f"outage ping source host (default {PING_SOURCE})")
    ap.add_argument("--ping-target", default=PING_TARGET,
                    help=f"outage ping target for tests without their own (default {PING_TARGET}; smoke lab: the host gateway)")
    ap.add_argument("--results", help="results folder (default <lab-dir>/results/failtests-<UTC time>)")
    a = ap.parse_args(argv)
    lab_dir = Path(a.lab_dir)
    try:
        intent = I.build_intent(lab_dir, Path(a.topology) if a.topology else None)
    except (OSError, ValueError, KeyError) as e:
        print(f"intent error: {e}", file=sys.stderr)
        return 2
    catalog = build_catalog(intent)
    if a.list:
        for t in catalog:
            print(f"{t['id']:>2}  {t['title']}" + (f"  [{t['ref']}]" if t.get("ref") else "")
                  + (f"\n    DC3: {t['dc3_note']}" if t["dc3_note"] else ""))
        return 0
    try:
        tests = select_tests(catalog, a.run)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    lab_nodes = set(x.strip() for x in a.nodes.split(",")) if a.nodes else set(intent["lab_nodes"])
    if a.dry_run:
        for t in tests:
            print(describe(t, lab_nodes, a.ping_target))
            print()
        return 0
    if intent["missing"]:
        print("missing rendered files:\n  " + "\n  ".join(intent["missing"]), file=sys.stderr)
        return 2
    out_dir = Path(a.results) if a.results else lab_dir / "results" / f"failtests-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    out_dir.mkdir(parents=True, exist_ok=True)
    runner = PC.DockerRunner(a.prefix)
    lab = Lab(intent, runner, lab_nodes)
    pc_checks = I.expected_checks(intent)
    results = []
    for t in tests:
        print(f"--- test {t['id']}: {t['title']}", flush=True)
        r = run_test(t, lab, pc_checks, out_dir, a.ping_source, a.ping_target)
        (out_dir / f"t{t['id']:02d}.json").write_text(json.dumps(r, indent=2) + "\n")
        # the plan and every command that ran (with rc and output), readable without the JSON
        (out_dir / f"t{t['id']:02d}-commands.txt").write_text(
            describe(t, lab_nodes, a.ping_target) + "\n\n# commands run (node$ command -> rc, output)\n" + "\n".join(r["log"]) + "\n")
        print(f"    {r['result']}", flush=True)
        results.append(r)
    (out_dir / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    text = result_rows(results)
    (out_dir / "summary.txt").write_text(text + "\n")
    print(text)
    print(f"results: {out_dir}")
    return 1 if any(r["result"] == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
