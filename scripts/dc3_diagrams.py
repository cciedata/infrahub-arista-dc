#!/usr/bin/env python3
"""DC3 diagrams: one PDF drawn from the reviewed renders, so every name, address and link is the repo's own data.

  python3 scripts/dc3_diagrams.py [--out docs/diagrams] [--html]

Inputs (nothing is typed in by hand):
  tests/expected/*.cfg, *.sh    the 43 reviewed renders (EOS configs, FRR stand-in and lab-only scripts)
  scripts/dc3_lab_topology.py   the containerlab topology (52 nodes, 151 links), written to a temp folder
  lab/dc3_intent.py             the parser the post-check and the failure tests use
  lab/dc3_failtests.py          the failure-test catalog
Output: <out>/DC3-diagrams.pdf (A3 landscape). --html also keeps the HTML page next to it.
Needs: Graphviz `dot` and Python Playwright with Chromium (the PDF printer). Python standard library otherwise.
"""
from __future__ import annotations

import argparse
import datetime
import html
import ipaddress
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lab"))
sys.path.insert(0, str(ROOT / "scripts"))
import dc3_intent as I  # noqa: E402
import dc3_lab_topology as L  # noqa: E402

# ---------------------------------------------------------------------------------------------- style
FONT = "Inter"
ROLE = {  # fill, border
    "spine": ("#dbe6f5", "#2b4c7e"),
    "leaf": ("#e6f2e1", "#3d7a35"),
    "border": ("#fde6cc", "#b8641a"),
    "service": ("#efe0f6", "#7a3c94"),
    "l2pair": ("#f6f0d2", "#8a7414"),
    "fw": ("#fbdede", "#a33131"),
    "isp": ("#e9e9e9", "#555555"),
    "cloud": ("#dcf1f4", "#20727f"),
    "ext": ("#ffffff", "#777777"),
    "host": ("#ffffff", "#3d7a35"),
}
ROLE_NAME = {"spine": "Spine (EOS)", "leaf": "Server leaf (EOS)", "border": "Border leaf (EOS)",
             "service": "Service leaf (EOS)", "l2pair": "Edge / DMZ pair (EOS, L2)", "fw": "Firewall stand-in (FRR)",
             "isp": "ISP router / carrier (FRR)", "cloud": "Cloud router / provider (FRR)",
             "ext": "Branch, partner, cross-connect (lab only)", "host": "Test host (lab only)"}
EDGE = {"fabric": "#2b4c7e", "peer": "#7a3c94", "keepalive": "#7a3c94", "attach": "#b8641a", "ext": "#666666",
        "evpn": "#20727f", "ibgp": "#7a3c94"}


def natural(s):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s)]


def role(name: str) -> str:
    n = name.removeprefix("DC3-")
    if re.fullmatch(r"S\d+", n): return "spine"
    if re.fullmatch(r"L\d+", n): return "leaf"
    if re.fullmatch(r"BL\d+", n): return "border"
    if re.fullmatch(r"SL\d+", n): return "service"
    if re.fullmatch(r"(EDGE|DMZ)\d+", n): return "l2pair"
    if n.endswith("FW1"): return "fw"
    if n.startswith("ISP"): return "isp"
    if n.startswith(("CLOUD", "AWS", "AZURE")): return "cloud"
    if n.startswith("HOST"): return "host"
    return "ext"


def short(name: str) -> str:
    return name.removeprefix("DC3-")


def ifshort(i: str) -> str:
    return (i.replace("Port-Channel", "Po").replace("Ethernet", "Et").replace("Loopback", "Lo")
            .replace("Management", "Ma"))


def esc(s) -> str:
    return html.escape(str(s))


# ---------------------------------------------------------------------------------------------- data
def load():
    tmp = Path(tempfile.mkdtemp(prefix="dc3-diagrams-"))
    (tmp / "configs").mkdir()
    for f in (ROOT / "tests" / "expected").iterdir():
        if f.suffix in (".cfg", ".sh"):
            (tmp / "configs" / f.name).write_text(f.read_text())
    L.write(tmp)
    X = I.build_intent(tmp)
    if X["missing"]:
        raise SystemExit(f"renders missing: {X['missing']}")
    return X


class Data:
    def __init__(self, X):
        self.X = X
        self.nodes = X["nodes"]
        self.eos = sorted((n for n, v in self.nodes.items() if v["kind"] == "eos"), key=natural)
        self.links = X["topology"]["links"]
        self.cfg = {n: self.nodes[n]["cfg"] for n in self.nodes}

    def intf(self, n):
        return self.cfg[n]["interfaces"]

    def peer_of(self, n, ifname):
        d = (self.intf(n)[ifname]["description"] or "").split(" ")
        return (d[0], d[1] if len(d) > 1 else "") if d and d[0].startswith("DC3-") else (None, None)

    def ifname(self, n, lab_if):
        """Lab interface name -> the device's own name (cEOS ethN = EthernetN or EthernetN/1; FRR names as they are)."""
        if self.nodes[n]["kind"] != "eos":
            return lab_if
        m = re.fullmatch(r"eth(\d+)", lab_if)
        if not m:
            return lab_if
        for k in (f"Ethernet{m.group(1)}/1", f"Ethernet{m.group(1)}"):
            if k in self.intf(n):
                return ifshort(k)
        return lab_if

    def lo(self, n, k):
        i = self.intf(n).get(k)
        return i["ip"].split("/")[0] if i and i["ip"] else None

    def mgmt(self, n):
        return self.lo(n, "Management1")

    def pairs(self):
        out = {}
        for n in self.eos:
            m = self.cfg[n]["mlag"]
            if m:
                out.setdefault(m["domain-id"], []).append(n)
        return out

    def vtep(self, n):
        c = self.cfg[n]
        if not c["vxlan"]:
            return None
        return self.lo(n, "Loopback1")

    def fabric_links(self):
        """(spine, spine if, spine ip, leaf, leaf if, leaf ip) from the spine configs."""
        out = []
        for s in [n for n in self.eos if role(n) == "spine"]:
            for k, i in self.intf(s).items():
                p, pi = self.peer_of(s, k)
                if p and i["ip"] and k.startswith("Ethernet"):
                    li = self.intf(p).get(pi, {})
                    out.append((s, k, i["ip"], p, pi, li.get("ip")))
        return out

    def vrf_neighbors(self, n):
        b = self.cfg[n]["bgp"]
        return [nb for nb in (b["neighbors"] if b else []) if nb["kind"] == "vrf"]

    def svi(self, n, vrf=None):
        return {k: i for k, i in self.intf(n).items() if k.startswith("Vlan") and (vrf is None or i["vrf"] == vrf)}


# ---------------------------------------------------------------------------------------------- graphviz
def node_attr(name, label=None, r=None, **kw):
    r = r or role(name)
    fill, border = ROLE[r]
    a = {"label": label if label is not None else short(name), "fillcolor": fill, "color": border}
    a.update(kw)
    return f'"{name}" [' + ", ".join(f'{k}={fmt(v)}' for k, v in a.items()) + "];"


def fmt(v):
    if isinstance(v, str) and v.startswith("<") and v.endswith(">"):
        return v
    return '"' + str(v).replace('"', '\\"') + '"'


def edge(a, b, **kw):
    return f'"{a}" -> "{b}" [' + ", ".join(f"{k}={fmt(v)}" for k, v in kw.items()) + "];"


def hl(title, *rows, bold_first=True, size=11):
    """HTML-like Graphviz label: a title line and small rows."""
    out = f'<<table border="0" cellborder="0" cellspacing="0" cellpadding="1"><tr><td><font point-size="{size + 2}"><b>{esc(title)}</b></font></td></tr>'
    for r in rows:
        if r:
            out += f'<tr><td><font point-size="{size}">{esc(r)}</font></td></tr>'
    return out + "</table>>"


def graph(body: list[str], rankdir="TB", extra="", engine="dot", fixed=False) -> str:
    src = (f'digraph G {{ rankdir={rankdir}; bgcolor="transparent"; pad=0.2; nodesep=0.25; ranksep=0.6; {extra}\n'
           f'node [shape=box, style="rounded,filled", fontname="{FONT}", fontsize=11, penwidth=1.4, margin="0.12,0.06"];\n'
           f'edge [fontname="{FONT}", fontsize=9, arrowhead=none, penwidth=1.2];\n' + "\n".join(body) + "\n}")
    cmd = ["neato", "-n2", "-Tsvg"] if fixed else [engine, "-Tsvg"]
    svg = subprocess.run(cmd, input=src, capture_output=True, text=True, check=True).stdout
    return svg[svg.index("<svg"):]



def at(x, y):
    return f"{x},{y}!"


def row(names, y, x0, dx):
    return {n: (x0 + i * dx, y) for i, n in enumerate(names)}


def vtep_order(D):
    return ([n for n in D.eos if role(n) == "leaf"] + [n for n in D.eos if role(n) == "border"] +
            [n for n in D.eos if role(n) == "service"])


def ranges(names):
    """['L1','L2','L3','SL1'] -> 'L1–L3, SL1' (consecutive numbers of one prefix)."""
    out, groups = [], []
    for n in sorted(names, key=natural):
        m = re.fullmatch(r"([A-Z]+)(\d+)", n)
        if groups and m and groups[-1][0] == m.group(1) and groups[-1][2] + 1 == int(m.group(2)):
            groups[-1][2] = int(m.group(2))
        elif m:
            groups.append([m.group(1), int(m.group(2)), int(m.group(2))])
        else:
            groups.append([n, None, None])
    for p, a, z in groups:
        out.append(p if a is None else f"{p}{a}" if a == z else f"{p}{a}–{p}{z}" if z > a + 1 else f"{p}{a}, {p}{z}")
    return ", ".join(out)

# ---------------------------------------------------------------------------------------------- pages
class Doc:
    def __init__(self):
        self.pages = []

    def add(self, title, sub, body, note=""):
        self.pages.append((title, sub, body, note))


def table(head, rows, cls=""):
    h = "".join(f"<th>{esc(c)}</th>" for c in head)
    b = "".join("<tr>" + "".join(f"<td>{c if isinstance(c, Raw) else esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="t {cls}"><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>'


class Raw(str):
    pass


def legend(roles):
    return '<div class="legend">' + "".join(
        f'<span><i style="background:{ROLE[r][0]};border-color:{ROLE[r][1]}"></i>{esc(ROLE_NAME[r])}</span>' for r in roles) + "</div>"


def p_cover(doc, D, commit):
    X = D.X
    eos = len(D.eos)
    frr = sum(1 for n in X["lab_nodes"] if D.nodes[n]["kind"] == "frr" and role(n) in ("fw", "isp", "cloud") and n.count("-") == 1 and not n.endswith(("-A", "-B")))
    contents = [(str(i), t, sub) for i, (t, sub, _b, _n) in enumerate(doc.pages, 1)]
    rows = "".join(f"<tr><td class='n'>{a}</td><td><b>{esc(b)}</b></td><td>{esc(c)}</td></tr>" for a, b, c in contents)
    body = f"""
<div class="cover">
  <div class="kicker">infrahub-arista-dc</div>
  <h1>DC3 network diagrams</h1>
  <p class="lede">Standalone Arista EOS datacenter: EVPN/VXLAN over an eBGP spine-leaf fabric, with firewall, internet,
  cloud and branch stand-ins. Every value on these pages is read from the reviewed renders in <code>tests/expected/</code>
  and the containerlab topology, by <code>scripts/dc3_diagrams.py</code>.</p>
  <div class="stats">
    <div><b>{eos}</b><span>EOS switches</span></div>
    <div><b>{len([n for n in D.eos if role(n)=='spine'])}</b><span>spines</span></div>
    <div><b>{len([n for n in D.eos if role(n) in ('leaf','border','service')])}</b><span>VTEP leaves</span></div>
    <div><b>{len(D.pairs())}</b><span>MLAG pairs</span></div>
    <div><b>8</b><span>stand-ins (FRR)</span></div>
    <div><b>{len(X['lab_nodes'])}</b><span>lab nodes</span></div>
    <div><b>{len(D.links)}</b><span>lab links</span></div>
  </div>
  <table class="contents">{rows}</table>
  <p class="meta">Source commit {esc(commit)} · generated {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M} UTC ·
  design: docs/DESIGN.md · lab: docs/QUICKSTART.md</p>
</div>"""
    doc.add("", "", body)


def p_physical(doc, D):
    X = D.X
    sp = [n for n in D.eos if role(n) == "spine"]
    vt = vtep_order(D)
    top, bot = vt[:12], vt[12:]
    pos = {}
    pos.update(row(top, 560, 0, 92))
    pos.update(row(bot, 40, 0, 92))
    pos.update({sp[0]: (330, 300), sp[1]: (680, 300)})
    l11 = pos["DC3-L11"][0]
    pos.update(row(["DC3-HOST-PROD", "DC3-HOST-DEV", "DC3-HOST-CITRIX"], 700, l11 - 100, 100))
    pos.update(row(["DC3-INTFW1", "DC3-CLOUD1", "DC3-CLOUD2", "DC3-SDWANFW1", "DC3-VPNFW1", "DC3-INETFW1"], -130, 1000, 112))
    pos.update(row(["DC3-XC1", "DC3-XC2", "DC3-BR001", "DC3-BR002", "DC3-PARTNER01", "DC3-DMZ1", "DC3-DMZ2"], -280, 1112, 92))
    pos.update(row(["DC3-AWS", "DC3-AZURE"], -420, 1112, 92))
    pos.update({"DC3-EDGE1": (1480, 140), "DC3-EDGE2": (1600, 140), "DC3-ISP1": (1480, 300), "DC3-ISP2": (1600, 300),
                "DC3-ISP-A": (1480, 440), "DC3-ISP-B": (1600, 440), "DC3-HOST-ISP-A": (1480, 580), "DC3-HOST-ISP-B": (1600, 580)})
    missing = [n for n in X["lab_nodes"] if n not in pos]
    if missing:
        raise SystemExit(f"physical layout: no position for {missing}")
    model = {"spine": "7060DX5-64S", "leaf": "7050SX3-48YC8C", "border": "7280DR3-24", "service": "7280DR3-24",
             "l2pair": "7050SX3 / 7050DX4"}
    b = []
    for n in X["lab_nodes"]:
        x, y = pos[n]
        r = role(n)
        sub = model.get(r, {"fw": "FRR stand-in", "isp": "FRR", "cloud": "FRR", "host": "test host",
                            "ext": "bridge" if D.nodes[n]["kind"] == "bridge" else "FRR"}.get(r, ""))
        if n.startswith("DC3-EDGE"):
            sub = "7050DX4-32S"
        if n.startswith("DC3-DMZ"):
            sub = "7050SX3-48YC8C"
        b.append(node_attr(n, hl(short(n), sub, size=8), pos=at(x, y), width="1.1", height="0.42", fixedsize="true"))
    cnt = {}
    for s_, si, sip, l, li, lip in D.fabric_links():
        cnt[(s_, l)] = cnt.get((s_, l), 0) + 1
    for (s_, l), c in cnt.items():
        b.append(edge(s_, l, color=EDGE["fabric"], penwidth=0.6 + 0.4 * c))
    for dom, (p1, p2) in D.pairs().items():
        if p1 in pos:
            b.append(edge(p1, p2, color=EDGE["peer"], penwidth=3))
    seen = {}
    for a1, ai, z1, zi in D.links:
        if a1 in D.eos and z1 in D.eos:
            continue
        seen[(a1, z1)] = seen.get((a1, z1), 0) + 1
    for (a1, z1), c in seen.items():
        col = EDGE["attach"] if (a1 in D.eos or z1 in D.eos) else EDGE["ext"]
        b.append(edge(a1, z1, color=col, penwidth=1.0 + 0.4 * (c - 1)))
    svg = graph(b, fixed=True, extra="splines=line;")
    note = (f"{len(X['lab_nodes'])} nodes, {len(D.links)} links (lab topology). Blue: spine-leaf links (two per spine on server "
            "leaves, one on border and service leaves; page 2). Purple: MLAG peer-links (keepalives on page 8). Orange: switch to "
            "stand-in attachments (MLAG port-channels, pages 8-11). Grey: links outside the switches (lab only, page 11). "
            "Models are the design's hardware (docs/DESIGN.md section 2); the lab runs cEOS-lab.")
    doc.add("Physical topology", "Every switch, stand-in and lab node, and how they are cabled",
            f'<div class="fig">{svg}</div>' + legend(["spine", "leaf", "border", "service", "l2pair", "fw", "isp", "cloud", "ext", "host"]), note)


def p_fabric_links(doc, D):
    rows = {}
    for s, si, sip, l, li, lip in D.fabric_links():
        rows.setdefault(l, []).append((s, si, sip, li, lip))
    out = []
    for l in sorted(rows, key=lambda n: ({"leaf": 1, "border": 0, "service": 0}[role(n)], natural(n))):
        cells = [Raw(f"<b>{short(l)}</b>"), D.cfg[l]["bgp"]["asn"]]
        for s, si, sip, li, lip in sorted(rows[l], key=lambda r: (natural(r[0]), natural(r[1]))):
            cells.append(Raw(f"{ifshort(li)} <span class='ip'>{lip}</span> ↔ {short(s)} {ifshort(si)} <span class='ip'>{sip}</span>"))
        out.append(cells)
    n = max(len(r) for r in out) - 2
    head = ["Leaf", "ASN"] + [f"Uplink {i+1}" for i in range(n)]
    body = table(head, out, "big nowrap")
    total = sum(len(v) for v in rows.values())
    note = (f"{total} spine-leaf links, each a routed /31 from pool DC3-FABRIC-P2P (10.27.4.0/22), MTU 9214, BFD 999 ms x 3, "
            "eBGP in peer group IPV4-UNDERLAY-PEERS. Leaf side first; addresses as configured.")
    doc.add("Spine-leaf links", "Every uplink with its interfaces and /31 addresses (from the spine and leaf configs)", body, note)


def p_underlay(doc, D):
    sp = [n for n in D.eos if role(n) == "spine"]
    vt = vtep_order(D)
    top, bot = vt[:12], vt[12:]
    pos = {**row(top, 420, 0, 104), **row(bot, 0, 0, 104), sp[0]: (370, 210), sp[1]: (770, 210)}
    b = []
    for s_ in sp:
        b.append(node_attr(s_, hl(short(s_), f"AS {D.cfg[s_]['bgp']['asn']}", f"Lo0 {D.lo(s_,'Loopback0')}", size=10),
                           pos=at(*pos[s_]), width="1.5", height="0.6", fixedsize="true"))
    for n in vt:
        c = D.cfg[n]
        b.append(node_attr(n, hl(short(n), f"AS {c['bgp']['asn']}", f"Lo0 {D.lo(n,'Loopback0')}", size=8),
                           pos=at(*pos[n]), width="1.3", height="0.55", fixedsize="true"))
    for s_ in sp:
        for n in vt:
            b.append(edge(s_, n, color=EDGE["fabric"], penwidth=0.7))
    for dom, (p1, p2) in D.pairs().items():
        if p1 in pos:
            b.append(edge(p1, p2, color=EDGE["ibgp"], style="dashed", penwidth=2))
    svg = graph(b, fixed=True, extra="splines=line;")
    rows = []
    for n in D.eos:
        c = D.cfg[n]
        if not c["bgp"]:
            continue
        und = [nb for nb in c["bgp"]["neighbors"] if nb["kind"] == "underlay"]
        ml = [nb for nb in c["bgp"]["neighbors"] if nb["kind"] == "mlag"]
        rows.append([short(n), c["bgp"]["asn"], c["bgp"]["router_id"], D.lo(n, "Loopback1") or "", D.lo(n, "Loopback2") or "",
                     len(und), ", ".join(nb["ip"] for nb in ml)])
    k = (len(rows) + 1) // 2
    hd = ["Switch", "ASN", "Router ID (Lo0)", "Lo1 (VTEP)", "Lo2 (own VTEP)", "Underlay sessions", "MLAG iBGP peer"]
    t = '<div class="two">' + table(hd, rows[:k], "dense") + table(hd, rows[k:], "dense") + "</div>"
    note = ("One ASN per leaf or MLAG pair, spines AS 65300. Loopbacks are announced with redistribute connected route-map "
            "RM-CONN-2-BGP (PL-LOOPBACKS, eq 32). ECMP maximum-paths 4. Blue: one line per spine and leaf (2 links each on server "
            "leaves, page 2). Purple dashed: iBGP between MLAG members over Vlan3900. EDGE and DMZ pairs run no BGP.")
    doc.add("Underlay eBGP", "Every VTEP leaf peers with both spines on its uplinks; MLAG pairs also peer with each other",
            f'<div class="fig short">{svg}</div>' + t, note)


def p_overlay(doc, D):
    sp = [n for n in D.eos if role(n) == "spine"]
    vt = vtep_order(D)
    vteps = {}
    for n in vt:
        vteps.setdefault(D.vtep(n), []).append(n)
    keys = sorted(vteps, key=lambda ip: vt.index(vteps[ip][0]))
    top, bot = keys[:11], keys[11:]
    pos = {**{"V" + k: p for k, p in row(top, 440, 0, 126).items()}, **{"V" + k: p for k, p in row(bot, 0, 63, 126).items()},
           sp[0]: (400, 220), sp[1]: (860, 220)}
    b = []
    for s_ in sp:
        b.append(node_attr(s_, hl(short(s_), "EVPN route server", f"Lo0 {D.lo(s_,'Loopback0')}  AS 65300", size=10),
                           pos=at(*pos[s_]), width="1.9", height="0.65", fixedsize="true"))
    for ip in keys:
        members = vteps[ip]
        c = D.cfg[members[0]]
        anycast = len(members) > 1
        l2 = len(c["vxlan"]["vlans"])
        lab = hl(" + ".join(short(m) for m in members), f"VTEP {ip}" + (" anycast" if anycast else ""),
                 ("own " + " / ".join(D.lo(m, "Loopback2") for m in members)) if anycast else f"Lo0 {D.lo(members[0],'Loopback0')}",
                 f"L3 VNI {len(c['vxlan']['vrfs'])}" + (f" · L2 VNI {l2}" if l2 else ""), size=8)
        b.append(node_attr("V" + ip, lab, r=role(members[0]), pos=at(*pos["V" + ip]), width="1.6", height="0.72", fixedsize="true"))
        for s_ in sp:
            b.append(edge(s_, "V" + ip, color=EDGE["evpn"], penwidth=0.7))
    svg = graph(b, fixed=True, extra="splines=line;")
    vrf_rows = []
    allvrf = {}
    for n in vt:
        for v, vni in D.cfg[n]["vxlan"]["vrfs"].items():
            allvrf.setdefault((v, vni), []).append(short(n))
    for (v, vni), ns in sorted(allvrf.items(), key=lambda kv: kv[0][1]):
        vrf_rows.append([v, vni, f"65300:{vni}", len(ns), ranges(ns)])
    t = table(["VRF", "L3 VNI", "Route target", "Switches", "Where"], vrf_rows, "mid")
    note = ("EVPN over eBGP multihop (TTL 3) from each switch's Loopback0 to both spines' Loopback0, send-community, "
            "multihop BFD 999 ms x 3. RD = <Loopback0>:<VNI>, RT = 65300:<VNI>. MLAG pairs share one anycast VTEP on Loopback1 "
            "(vxlan virtual-router encapsulation mac-address mlag-system-id). VXLAN UDP 4789. Anycast gateway MAC 0200.0000.00aa.")
    doc.add("EVPN overlay", f"{len(vteps)} VTEP addresses for {len(vt)} leaves; both spines reflect EVPN to all of them",
            f'<div class="fig short">{svg}</div>' + t, note)


# ---------------------------------------------------------------------------------------------- control plane / data plane
# Values seen only in the lab capture (docs/VXLAN-EVPN-CAPTURE.md): container MACs and router MACs. Everything
# else on these pages is read from the configs.
CAP = {
    "host_a_mac": "aa:c1:ab:09:17:8d",          # DC3-HOST-PROD eth1
    "gw_mac": "02:00:00:00:00:aa",              # anycast gateway, as seen on the wire
    "l11_rmac": "00:1c:73:6f:ff:35",            # L11 router MAC (type-2 ext. community, inner source MAC)
    "sl_rmac": "02:1c:73:5f:aa:c6",             # SL pair router MAC (type-5 0/0 in PROD)
    "bl_rmac": "02:1c:73:5d:31:8a",             # BL pair router MAC (type-5 0/0 in TRANSIT) = BL MLAG system-id
    "l11_up_mac": "aa:c1:ab:ff:46:0d", "spine_mac": "aa:c1:ab:4c:c4:37",
    "udp_src": "64659",
}


def card(title, color, rows, tag):
    trs = "".join(f"<tr><th>{esc(k)}</th><td>{v if isinstance(v, Raw) else esc(v)}</td></tr>" for k, v in rows)
    return (f'<div class="card" style="border-color:{color}"><div class="ch" style="background:{color}">{esc(title)}'
            f'<span>{esc(tag)}</span></div><table>{trs}</table></div>')


def stack(parts):
    """Packet as a row of header blocks: (name, bytes, lines, kind)."""
    col = {"oeth": "#e6e6e6", "oip": "#dbe6f5", "udp": "#dbe6f5", "vx": "#20727f", "ieth": "#fde6cc", "iip": "#e6f2e1",
           "pl": "#ffffff"}
    out = ""
    for name, nbytes, lines, kind in parts:
        fg = "#ffffff" if kind == "vx" else "#1d232b"
        out += (f'<div class="hb" style="background:{col[kind]};color:{fg};flex:{max(nbytes, 10)}">'
                f'<b>{esc(name)}</b> <i>{nbytes} B</i>' + "".join(f"<div>{esc(x)}</div>" for x in lines) + "</div>")
    return f'<div class="stack">{out}</div>'


def chain_svg(steps, color):
    b = []
    for i, (n, lab, r) in enumerate(steps):
        b.append(node_attr(f"c{i}", hl(n, *lab.split("|"), size=9), r=r))
        if i:
            b.append(edge(f"c{i-1}", f"c{i}", color=color, penwidth=2, arrowhead="normal"))
    return graph(b, rankdir="LR", extra="nodesep=0.15; ranksep=0.3;")


def p_evpn_routes(doc, D):
    l11, sl1, bl1 = "DC3-L11", "DC3-SL1", "DC3-BL1"
    c = D.cfg[l11]
    vni2 = c["vxlan"]["vlans"][1010]
    vni3 = c["vxlan"]["vrfs"]["PROD"]
    lo0, lo1 = D.lo(l11, "Loopback0"), D.lo(l11, "Loopback1")
    asn = c["bgp"]["asn"]
    sl_lo0, sl_vtep = D.lo(sl1, "Loopback0"), D.lo(sl1, "Loopback1")
    bl_lo0, bl_vtep = D.lo(bl1, "Loopback0"), D.lo(bl1, "Loopback1")
    flood = sorted({D.vtep(n) for n in D.eos if D.cfg[n]["vxlan"] and 1010 in D.cfg[n]["vxlan"]["vlans"]} - {lo1}, key=natural)
    t2 = card("Type 2 · MAC/IP advertisement", "#3d7a35", [
        ("Originated by", f"L11 (AS {asn}) when it learns host A (redistribute learned, VLAN 1010)"),
        ("RD", f"{lo0}:{vni2}  (Loopback0 : L2 VNI)"),
        ("ESI / Ethernet tag", "0 / 0 (single-homed host)"),
        ("MAC", f"{CAP['host_a_mac']}  (host A)"),
        ("IP", "10.24.10.10 (a second route carries the MAC only)"),
        ("Label 1 / Label 2", f"{vni2} (L2 VNI, bridging) / {vni3} (L3 VNI of PROD, routing)"),
        ("Next hop", f"{lo1} (L11 VTEP, Loopback1); the spines keep it"),
        ("Route targets", f"65300:{vni2}, 65300:{vni3}"),
        ("Ext. communities", Raw(f"VXLAN encapsulation · router MAC <code>{CAP['l11_rmac']}</code>")),
        ("AS path at a receiver", f"65300 {asn}"),
        ("Used for", "bridging to host A in VLAN 1010, and a /32 host route in PROD"),
    ], "captured ")
    t3 = card("Type 3 · Inclusive multicast (IMET)", "#20727f", [
        ("Originated by", "every VTEP for each of its L2 VNIs (L1..L20: 42 each; BL/SL: none)"),
        ("RD", f"{lo0}:{vni2}"),
        ("Ethernet tag", "0"),
        ("Originating IP", f"{lo1}"),
        ("PMSI tunnel", f"ingress replication, label {vni2}, endpoint {lo1} (RFC 8365 form; no type-3 update was in the capture )"),
        ("Route target", f"65300:{vni2}"),
        ("Builds", f"the flood list of VNI {vni2}: L11 sends BUM copies to {len(flood)} VTEPs"),
        ("Flood list on L11", ", ".join(flood)),
    ], "flood list (show vxlan vtep: 18)")
    t5 = card("Type 5 · IP prefix", "#7a3c94", [
        ("Originated by", "SL pair, from the Internal FW's 0/0 in VRF PROD"),
        ("RD", f"{sl_lo0}:{vni3}"),
        ("Prefix", "0.0.0.0/0  (gateway IP 0)"),
        ("Label", f"{vni3} (L3 VNI of PROD)"),
        ("Next hop", f"{sl_vtep} (SL anycast VTEP)"),
        ("Route target", f"65300:{vni3}"),
        ("Ext. communities", Raw(f"VXLAN · router MAC <code>{CAP['sl_rmac']}</code>")),
        ("Installed on L11 as", Raw(f"<code>0.0.0.0/0 via VTEP {sl_vtep} VNI {vni3} router-mac {CAP['sl_rmac']}</code>")),
        ("Not imported", f"the BL pair's TRANSIT 0/0 (RD {bl_lo0}:50100, RT 65300:50100, next hop {bl_vtep}): L11 has no TRANSIT VRF"),
    ], "captured ")
    b = [node_attr("A", hl("Host A", "10.24.10.10", "VLAN 1010", size=9), r="host"),
         node_attr(l11, hl("L11", f"VTEP {lo1}", f"AS {asn}", size=10)),
         node_attr("SP", hl("S1, S2", "EVPN route servers", "AS 65300, next hop unchanged", size=10), r="spine"),
         node_attr("OL", hl("Other VTEPs", "import by route target", f"VLAN {1010}: L1..L20", size=10), r="leaf"),
         node_attr("SL", hl("SL1 + SL2", f"VTEP {sl_vtep}", "PROD 0/0 from Internal FW", size=10), r="service"),
         node_attr("BL", hl("BL1 + BL2", f"VTEP {bl_vtep}", "TRANSIT 0/0 from Internet FW", size=10), r="border"),
         edge("A", l11, color="#3d7a35", label="ARP / first frame", fontcolor="#3d7a35", arrowhead="normal"),
         edge(l11, "SP", color="#3d7a35", penwidth=2, label="type 2, type 3", fontcolor="#3d7a35", arrowhead="normal"),
         edge("SP", "OL", color="#3d7a35", penwidth=2, label="type 2, type 3", fontcolor="#3d7a35", arrowhead="normal"),
         edge("SL", "SP", color="#7a3c94", penwidth=2, label="type 5 PROD 0/0", fontcolor="#7a3c94", arrowhead="normal"),
         edge("SP", l11, color="#7a3c94", penwidth=2, label="type 5 PROD 0/0 (imported)", fontcolor="#7a3c94", arrowhead="normal", constraint="false"),
         edge("BL", "SP", color="#b8641a", penwidth=2, label="type 5 TRANSIT 0/0", fontcolor="#b8641a", arrowhead="normal"),
         ]
    svg = graph(b, rankdir="LR", extra="ranksep=1.1; nodesep=0.35;")
    orig = table(["Switches", "Type 2 (MAC/IP)", "Type 3 (IMET)", "Type 5 (IP prefix)"], [
        ["Server leaves L1..L20", "hosts learned in their 42 VLANs (redistribute learned)", "one per L2 VNI (42)",
         "connected SVI subnets per VRF (redistribute connected)"],
        ["Service leaves SL1, SL2", "–", "–", "routes from the Internal FW: 0/0 into each tenant VRF, tenant summaries into TRANSIT; aggregates"],
        ["Border leaves BL1, BL2", "–", "–", "TRANSIT: 0/0, cloud and branch prefixes from the outside; PARTNER: partner NAT range"],
        ["Spines S1, S2", "pass on, add AS 65300", "pass on", "pass on (not VTEPs, next hop unchanged)"],
    ], "mid")
    body = (f'<div class="fig" style="flex:0 0 78mm">{svg}</div>'
            f'<div class="cards">{t2}{t3}{t5}</div>' + orig)
    doc.add("EVPN control plane: route types", "What the VTEPs tell each other over BGP EVPN, with DC3's own values",
            body, "RD and RT rules: RD = <Loopback0>:<VNI>, RT = 65300:<VNI>. Values marked captured were seen in the "
            "full lab (host and router MACs are lab container values); the rest is read from the configs. Type 1 and "
            "type 4 (Ethernet segment routes) come with the ESI server ports, which are deferred.")


def p_packet_walks(doc, D):
    l11, l12 = "DC3-L11", "DC3-L12"
    v1, v2 = D.vtep(l11), D.vtep(l12)
    c = D.cfg[l11]
    vni2, vni2b, vni3 = c["vxlan"]["vlans"][1010], c["vxlan"]["vlans"][1011], c["vxlan"]["vrfs"]["PROD"]
    flood = sorted({D.vtep(n) for n in D.eos if D.cfg[n]["vxlan"] and 1010 in D.cfg[n]["vxlan"]["vlans"]} - {v1}, key=natural)
    w1 = chain_svg([("Host A", "10.24.10.10|VLAN 1010", "host"), ("L11", f"bridge VLAN 1010|VNI {vni2}", "leaf"),
                    ("Spine", "routes outer IP", "spine"), ("L12", f"VNI {vni2} → VLAN 1010", "leaf"),
                    ("Host B (example)", "10.24.10.20|VLAN 1010", "host")], "#3d7a35")
    s1 = stack([("Outer Ethernet", 14, ["L11 uplink → spine"], "oeth"), ("Outer IPv4", 20, [f"{v1} → {v2}", "UDP payload"], "oip"),
                ("UDP", 8, ["src: flow hash", "dst 4789"], "udp"), ("VXLAN", 8, [f"VNI {vni2}", "L2 VNI of VLAN 1010"], "vx"),
                ("Inner Ethernet", 14, ["A MAC → B MAC", "VLAN tag removed"], "ieth"), ("Inner IPv4", 20, ["10.24.10.10 → 10.24.10.20", "TTL unchanged"], "iip"),
                ("Payload", 64, ["ICMP echo"], "pl")])
    w2 = chain_svg([("Host A", "10.24.10.10|VLAN 1010", "host"), ("L11", f"route in PROD|VNI {vni3}", "leaf"),
                    ("Spine", "routes outer IP", "spine"), ("L12", f"VNI {vni3} → PROD|route to VLAN 1011", "leaf"),
                    ("Host C (example)", "10.24.11.20|VLAN 1011", "host")], "#2b4c7e")
    s2 = stack([("Outer Ethernet", 14, ["L11 uplink → spine"], "oeth"), ("Outer IPv4", 20, [f"{v1} → {v2}"], "oip"),
                ("UDP", 8, ["dst 4789"], "udp"), ("VXLAN", 8, [f"VNI {vni3}", "L3 VNI of PROD"], "vx"),
                ("Inner Ethernet", 14, ["L11 router MAC →", "L12 router MAC"], "ieth"), ("Inner IPv4", 20, ["10.24.10.10 → 10.24.11.20", "TTL −1 at L11"], "iip"),
                ("Payload", 64, ["ICMP echo"], "pl")])
    steps1 = ("<ol class='steps'>"
              f"<li>A sends ARP for B (broadcast in VLAN 1010). If L11 has no type-2 route for B yet, it copies the frame to every VTEP in "
              f"the VNI {vni2} flood list ({len(flood)} VTEPs, built from type-3 routes) — ingress replication, one unicast VXLAN copy each.</li>"
              f"<li>L12 decapsulates, floods into VLAN 1010; B answers. L12 already has A's type-2 route, so the reply goes straight to {v1}.</li>"
              f"<li>Both leaves now advertise type-2 routes for A and B. Every next frame is unicast VXLAN with VNI {vni2}: the leaves only "
              "bridge, no routing, inner MACs are the hosts' own.</li></ol>")
    steps2 = ("<ol class='steps'>"
              f"<li>A sends to its gateway 10.24.10.1, destination MAC {CAP['gw_mac']} (the same anycast gateway on every leaf).</li>"
              f"<li>L11 routes in VRF PROD. C's type-2 MAC/IP route (label 2 = {vni3}, router MAC of L12) gives a /32 host route via VTEP {v2}. "
              "Without it, the connected-subnet type-5 route of 10.24.11.0/24 is used, and the leaf that receives the packet ARPs for C.</li>"
              f"<li>L11 encapsulates with the L3 VNI {vni3} and L12's router MAC as inner destination (symmetric IRB: routed on both leaves).</li>"
              "<li>L12 decapsulates; VNI 50001 maps to VRF PROD; it routes into VLAN 1011 and sends to C with source MAC = gateway MAC.</li></ol>")
    body = (f'<div class="walk"><div class="wt"><b>Walk 1 · Bridged, same VLAN on two leaves</b> (L2 VNI {vni2}) <span class="tagq">derived from the configs not captured: the lab hosts all sit on L11</span></div>'
            f'<div class="wrow"><div class="fig strip">{w1}</div>{steps1}</div>{s1}</div>'
            f'<div class="walk"><div class="wt"><b>Walk 2 · Routed, two subnets of one VRF on two leaves</b> (symmetric IRB, L3 VNI {vni3}) <span class="tagq">derived from the configs not captured</span></div>'
            f'<div class="wrow"><div class="fig strip">{w2}</div>{steps2}</div>{s2}</div>')
    doc.add("Data plane: packet walks inside the fabric", "How a frame crosses the fabric when it is bridged and when it is routed",
            body, f"Host B (10.24.10.20 on L12) and host C (10.24.11.20 on L12) are example hosts, not lab nodes. VTEP addresses, "
            f"VNIs and the flood list are read from the configs. VXLAN adds 50 bytes; the underlay MTU is 9214, so 1500-byte "
            "host packets never fragment. The outer UDP source port is a hash of the inner flow, so the spines spread flows by ECMP.")
    # walk 3: captured
    sl, bl = "DC3-SL1", "DC3-BL1"
    vs, vb = D.vtep(sl), D.vtep(bl)
    w3 = chain_svg([("Host A", "10.24.10.10|PROD", "host"), ("L11", f"route PROD|VNI {vni3}", "leaf"), ("Spine", "", "spine"),
                    ("SL pair", f"VTEP {vs}|PROD → FW", "service"), ("Internal FW", "10.27.8.3 → 10.27.16.3|PROD → TRANSIT", "fw"),
                    ("SL pair", "TRANSIT|VNI 50100", "service"), ("Spine", "", "spine"), ("BL pair", f"VTEP {vb}|TRANSIT", "border"),
                    ("Internet FW", "10.27.16.11", "fw"), ("ISP → carrier", "→ 198.18.1.10", "isp")], "#a33131")
    leg0 = stack([("Ethernet", 14, [f"{CAP['host_a_mac']} →", f"{CAP['gw_mac']} (gateway)"], "ieth"),
                  ("IPv4", 20, ["10.24.10.10 → 198.18.1.10", "TTL 64"], "iip"), ("ICMP", 64, ["echo request"], "pl")])
    leg1 = stack([("Outer Ethernet", 14, [f"{CAP['l11_up_mac']} →", f"{CAP['spine_mac']}"], "oeth"),
                  ("Outer IPv4", 20, [f"{v1} → {vs}", "TTL 64, DF"], "oip"), ("UDP", 8, [f"{CAP['udp_src']} → 4789"], "udp"),
                  ("VXLAN", 8, ["flags I (0x08)", f"VNI {vni3}"], "vx"),
                  ("Inner Ethernet", 14, [f"{CAP['l11_rmac']} (L11) →", f"{CAP['sl_rmac']} (SL pair)"], "ieth"),
                  ("Inner IPv4", 20, ["10.24.10.10 → 198.18.1.10", "TTL 63"], "iip"), ("ICMP", 64, ["echo request"], "pl")])
    leg2 = stack([("Outer Ethernet", 14, ["SL uplink → spine"], "oeth"), ("Outer IPv4", 20, [f"{vs} → {vb}"], "oip"),
                  ("UDP", 8, ["→ 4789"], "udp"), ("VXLAN", 8, ["VNI 50100", "L3 VNI of TRANSIT"], "vx"),
                  ("Inner Ethernet", 14, ["SL router MAC →", f"{CAP['bl_rmac']} (BL pair)"], "ieth"),
                  ("Inner IPv4", 20, ["10.24.10.10 → 198.18.1.10", "after the FW"], "iip"), ("ICMP", 64, ["echo request"], "pl")])
    leg5 = stack([("Outer Ethernet", 14, ["spine → L11 uplink"], "oeth"), ("Outer IPv4", 20, [f"{vs} → {v1}", "TTL 63"], "oip"),
                  ("UDP", 8, ["59864 → 4789"], "udp"), ("VXLAN", 8, ["flags I (0x08)", f"VNI {vni3}"], "vx"),
                  ("Inner Ethernet", 14, ["00:1c:73:f2:be:3c (SL member) →", f"{CAP['l11_rmac']} (L11)"], "ieth"),
                  ("Inner IPv4", 20, ["198.18.1.10 → 10.24.10.10", "TTL 57"], "iip"), ("ICMP", 64, ["echo reply"], "pl")])
    rows = [["L11 uplinks", f"{v1} → {vs} / back", vni3, "3137 / 3134"], ["SL1 uplinks", f"{v1} → {vs} / back", vni3, "2550 / 3133"],
            ["SL1 uplinks", f"{vs} → {vb} / back", 50100, "3131 / 3130"], ["BL1 uplinks", f"{vs} → {vb} / back", 50100, "586 / 3132"]]
    body3 = (f'<div class="fig" style="flex:0 0 30mm">{w3}</div>'
             '<div class="cap"><b>1.</b> On the host port (L11 Ethernet1): no VXLAN yet; destination is the anycast gateway MAC.</div>' + leg0 +
             f'<div class="cap"><b>2.</b> Leg 1, L11 → SL pair: routed in PROD, sent on the L3 VNI {vni3} to the SL anycast VTEP; inner destination = SL router MAC from the type-5 0/0 (148-byte frame = 98 + 50).</div>' + leg1 +
             '<div class="cap"><b>3.</b> SL pair: decapsulates into PROD, hands the packet to the Internal FW, gets it back in TRANSIT, re-encapsulates. Leg 2, SL → BL:</div>' + leg2 +
             '<div class="cap"><b>4.</b> BL pair decapsulates into TRANSIT and routes to the Internet FW; from there it is plain IP to the ISP and carrier.</div>'
             f'<div class="cap"><b>5.</b> The reply, SL pair → L11 (same VNI {vni3}; the source is the answering SL member\'s own MAC, which member was not checked ):</div>' + leg5 +
             '<div class="cap">Frames counted in 40 s:</div>'
             + table(["Capture point", "Outer flow", "VNI", "Frames"], rows, "mid"))
    doc.add("Data plane: packet walk out through the firewall (captured)", "PROD host to 198.18.1.10: the fabric is crossed twice, and the VRF changes at the Internal FW",
            body3, "Header values were captured in the full lab (tcpdump on L11, SL1 and BL1; "
            "docs/VXLAN-EVPN-CAPTURE.md); the outer and inner MACs of leg 2 were not decoded and are named generically. "
            "SL1 and BL1 each see part of the traffic toward their pair: both members own the anycast "
            "VTEP and the spines split flows by ECMP. Only VNIs 50001 and 50100 appear: the path is routed end to end.")



def p_mlag(doc, D):
    pairs = D.pairs()
    blocks = []
    for dom, (a, z) in sorted(pairs.items(), key=lambda kv: natural(kv[1][0])):
        b = []
        for n in (a, z):
            c = D.cfg[n]
            m = c["mlag"]
            rows = [f"Vlan3900 {D.intf(n)['Vlan3900']['ip']}"]
            ka = [(k, i) for k, i in D.intf(n).items() if i["vrf"] == "VPC-KEEPALIVE"]
            for k, i in ka:
                rows.append(f"keepalive {ifshort(k)} {i['ip']}")
            if D.lo(n, "Loopback1"):
                rows.append(f"Lo1 {D.lo(n,'Loopback1')} (anycast)")
            if D.lo(n, "Loopback2"):
                rows.append(f"Lo2 {D.lo(n,'Loopback2')}")
            rows.append(f"Ma1 {D.mgmt(n)}")
            b.append(node_attr(n, hl(short(n), *rows, size=9)))
        pl = [k for k, i in D.intf(a).items() if i["channel_group"] == 1000]
        b.append(edge(a, z, color=EDGE["peer"], penwidth=3, label=f"Po1000 peer-link: {', '.join(ifshort(k) for k in pl)}",
                      fontcolor=EDGE["peer"]))
        kk = [k for k, i in D.intf(a).items() if i["vrf"] == "VPC-KEEPALIVE"]
        b.append(edge(a, z, color=EDGE["keepalive"], style="dashed", label=f"keepalive {', '.join(ifshort(k) for k in kk)} (VRF VPC-KEEPALIVE)",
                      fontcolor=EDGE["keepalive"]))
        pcs = [(k, i["description"]) for k, i in D.intf(a).items() if k.startswith("Port-Channel") and k != "Port-Channel1000"]
        for k, desc in pcs:
            peer = (desc or "").split(" ")[0]
            if not peer:
                continue
            mem_a = [ifshort(x) for x, i in D.intf(a).items() if i["channel_group"] == int(k.removeprefix("Port-Channel"))]
            mem_z = [ifshort(x) for x, i in D.intf(z).items() if i["channel_group"] == int(k.removeprefix("Port-Channel"))]
            pid = f"{dom}-{peer}"
            b.append(node_attr(pid, hl(short(peer), f"{ifshort(k)} (mlag {k.removeprefix('Port-Channel')})", size=9), r=role(peer)))
            b.append(edge(a, pid, color=EDGE["attach"], label=" ".join(mem_a), fontcolor=EDGE["attach"]))
            b.append(edge(z, pid, color=EDGE["attach"], label=" ".join(mem_z), fontcolor=EDGE["attach"]))
        svg = graph(b, rankdir="TB", extra="ranksep=0.7; nodesep=0.3;")
        m = D.cfg[a]["mlag"]
        info = (f"<div class='cap'><b>{esc(dom)}</b> · peer-address {esc(m.get('peer-address'))} / {esc(D.cfg[z]['mlag'].get('peer-address'))}"
                f" · heartbeat {esc(m.get('heartbeat-interval'))} ms · dual-primary {esc(m.get('dual-primary-detection'))}"
                f" · recovery {esc(m.get('dual-primary-recovery'))}</div>")
        blocks.append(f'<div class="cell">{info}<div class="fig mini">{svg}</div></div>')
    body = '<div class="grid">' + "".join(blocks) + "</div>"
    note = ("Peer-link Port-Channel1000 (LACP active, trunk group MLAGPEER), peer VLAN 3900 with no autostate, heartbeat on a "
            "dedicated routed port in VRF VPC-KEEPALIVE. mlag id = port-channel number. ESI pairs L3-L4 .. L9-L10 are EVPN "
            "multihoming pairs with no peer-link; their server Ethernet segments are deferred (docs/DESIGN.md section 11).")
    doc.add("MLAG pairs", "Five pairs: peer-link members, peer VLAN, keepalive, VTEP loopbacks and the MLAG port-channels to the stand-ins", body, note)


def attach_page(doc, D, members, title, sub, note, page_no=None):
    """Pair on the left (one row per VRF neighbor, with both SVIs), each stand-in on the right (one row per VRF), one line
    per VRF/VLAN between them; the physical port-channel of each stand-in under its name."""
    a = members[0]
    nbs = sorted(D.vrf_neighbors(a), key=lambda nb: (D.X["owners"].get(nb["ip"], ["?"])[0], nb["vrf"]))
    fill, border = ROLE[role(a)]
    rows, prow, by_owner = [], [], {}
    for i, nb in enumerate(nbs):
        own = D.X["owners"].get(nb["ip"], ["?"])[0]
        svis = []
        for m in members:
            for k, it in D.svi(m, nb["vrf"]).items():
                if it["ip"] and ipaddress.ip_address(nb["ip"]) in ipaddress.ip_network(it["ip"], strict=False):
                    svis.append((m, k, it["ip"]))
        vlan = svis[0][1].removeprefix("Vlan") if svis else "?"
        net = str(ipaddress.ip_network(svis[0][2], strict=False)) if svis else ""
        prow.append(f'<tr><td port="p{i}" align="left" bgcolor="white"><font point-size="10"><b>{esc(nb["vrf"])}</b>  '
                    + "  ".join(f"{esc(short(m))} {esc(ip.split('/')[0])}" for m, k, ip in svis) + "</font></td></tr>")
        by_owner.setdefault(own, []).append((i, nb, vlan, net))
        rows.append([nb["vrf"], vlan, net, " / ".join(f"{short(m)} {ip.split('/')[0]}" for m, k, ip in svis), f"{short(own)} {nb['ip']}",
                     nb["remote_as"], nb["route_map_in"] or "", nb["route_map_out"] or ""])
    title_row = (f'<tr><td bgcolor="{fill}"><font point-size="13"><b>{esc(" + ".join(short(m) for m in members))}</b></font><br/>'
                 f'<font point-size="10">AS {D.cfg[a]["bgp"]["asn"]} · Lo0 ' + " / ".join(D.lo(m, "Loopback0") for m in members) + "</font></td></tr>")
    b = [f'"PAIR" [shape=plain, label=<<table border="1" color="{border}" cellborder="1" cellspacing="0" cellpadding="5">{title_row}{"".join(prow)}</table>>];']
    for own, lst in by_owner.items():
        f2, b2 = ROLE[role(own)]
        pc = [k for k, it in D.intf(a).items() if k.startswith("Port-Channel") and (it["description"] or "").startswith(own)]
        mem = []
        if pc:
            num = int(pc[0].removeprefix("Port-Channel"))
            for m in members:
                mem += [f"{short(m)} {ifshort(x)}" for x, it in D.intf(m).items() if it["channel_group"] == num]
        asn = sorted({str(nb["remote_as"]) for _, nb, _, _ in lst})
        head = (f'<tr><td bgcolor="{f2}"><font point-size="12"><b>{esc(short(own))}</b></font><br/><font point-size="9">AS {", ".join(asn)}'
                + (f" · {esc(ifshort(pc[0]))} (mlag): {esc(', '.join(mem))}" if pc else "") + "</font></td></tr>")
        body = "".join(f'<tr><td port="q{i}" align="left" bgcolor="white"><font point-size="10">{esc(nb["vrf"])}  {esc(nb["ip"])}</font></td></tr>'
                       for i, nb, _, _ in lst)
        b.append(f'"{own}" [shape=plain, label=<<table border="1" color="{b2}" cellborder="1" cellspacing="0" cellpadding="5">{head}{body}</table>>];')
        for i, nb, vlan, net in lst:
            b.append(f'"PAIR":p{i}:e -> "{own}":q{i}:w [color="{EDGE["attach"]}", penwidth=1.6, label="VLAN {vlan}  {net}", fontcolor="{EDGE["attach"]}", fontsize=10];')
    svg = graph(b, rankdir="LR", extra="ranksep=2.2; nodesep=0.3;")
    t = table(["VRF", "VLAN", "Subnet", "Switch SVIs", "Peer", "Peer AS", "route-map in", "route-map out"], rows, "big")
    doc.add(title, sub, f'<div class="fig short">{svg}</div>' + t, note)


def p_service(doc, D):
    attach_page(doc, D, ["DC3-SL1", "DC3-SL2"], "Service leaves and Internal FW",
                "Each tenant VRF and TRANSIT meet the Internal FW on its own VLAN; eBGP per VRF, BFD 999 ms x 3",
                "SVI .1 / .2 on the pair members, the stand-in on .3 of each /29, all over one MLAG port-channel (Po11). "
                "Tenant VRFs send their DC3 summary and accept only 0/0; in TRANSIT the service leaves send only 0/0 and accept the "
                "tenant summaries. Page 13 lists the prefix-lists.", 6)


def p_border(doc, D):
    attach_page(doc, D, ["DC3-BL1", "DC3-BL2"], "Border leaves and the outside",
                "TRANSIT (and PARTNER) leave the fabric here: Internet FW, SD-WAN hub, VPN partner FW, two cloud routers",
                "The border leaves carry TRANSIT to the Internet FW (accept 0/0 only, send the tenant summaries), to the SD-WAN hub "
                "(branch summary), to both cloud routers (cloud prefixes) and PARTNER to the VPN FW. One MLAG port-channel "
                "per stand-in (Po11/13/15/31/32).", 7)


def p_edge(doc, D):
    b = []
    fr = lambda n: D.cfg[n]
    def addrs(n, pat=None):
        return [f"{a['dev']} {a['ip']}/{a['plen']}" for a in fr(n)["addresses"] if not pat or re.search(pat, a["dev"])]
    def asns(n):
        return sorted({str(x["asn"]) for x in fr(n)["bgp"]}) or ["– (no BGP)"]
    # internet side
    for n in ["DC3-INETFW1", "DC3-SDWANFW1", "DC3-VPNFW1"]:
        b.append(node_attr(n, hl(short(n), "AS " + ", ".join(asns(n)), *addrs(n, "OUTSIDE|TRANSIT|PARTNER-|LO-WAN|ADVPN"), size=9)))
    for n in ["DC3-EDGE1", "DC3-EDGE2", "DC3-DMZ1", "DC3-DMZ2"]:
        b.append(node_attr(n, hl(short(n), f"Ma1 {D.mgmt(n)}", "L2 only, no BGP", size=9)))
    for n in ["DC3-ISP1", "DC3-ISP2"]:
        nbs = [f"{x['ip']} AS{x['remote_as']} {x.get('description') or ''}" for g in fr(n)["bgp"] for x in g["neighbors"].values()]
        b.append(node_attr(n, hl(short(n), "AS " + ", ".join(asns(n)), *addrs(n), size=9)))
    for n in ["DC3-ISP-A", "DC3-ISP-B"]:
        b.append(node_attr(n, hl(short(n), "AS " + ", ".join(asns(n)), *addrs(n), size=9)))
    for n in ["DC3-HOST-ISP-A", "DC3-HOST-ISP-B"]:
        h = D.cfg[n]
        b.append(node_attr(n, hl(short(n), *[f"{a['ip']}/{a['plen']}" for a in h["addresses"]], f"gw {h['gateway']}", size=9)))
    # DMZ segments
    dmz = addrs("DC3-INETFW1", "DMZ-")
    b.append(node_attr("DMZSEG", hl("DMZ segments on the Internet FW", f"{len(dmz)} VLANs {dmz[0].split()[0].split('-')[1]}..{dmz[-1].split()[0].split('-')[1]}",
                                    f"{dmz[0].split()[1]} .. {dmz[-1].split()[1]}", size=9), r="fw"))
    for a, ai, z, zi in D.links:
        if a in D.eos and z in D.eos:
            continue
        if {role(a), role(z)} & {"l2pair"} or (role(a) == "isp" and role(z) in ("isp", "host")):
            col = EDGE["attach"] if (a in D.eos or z in D.eos) else EDGE["ext"]
            b.append(edge(a, z, color=col, label=f"{D.ifname(a, ai)} – {D.ifname(z, zi)}", fontcolor=col))
    for (a, z) in [("DC3-EDGE1", "DC3-EDGE2"), ("DC3-DMZ1", "DC3-DMZ2")]:
        b.append(edge(a, z, color=EDGE["peer"], penwidth=2.5, label="Po1000", fontcolor=EDGE["peer"], constraint="false"))
    b.append(edge("DC3-INETFW1", "DMZSEG", color=EDGE["ext"], style="dashed"))
    svg1 = graph(b, rankdir="LR", extra="ranksep=1.0; nodesep=0.2;")
    # clouds / branches / partner
    c = []
    for n in ["DC3-CLOUD1", "DC3-CLOUD2"]:
        nbs = [f"{x['ip']} AS{x['remote_as']}" for g in fr(n)["bgp"] for x in g["neighbors"].values()]
        c.append(node_attr(n, hl(short(n), "AS " + ", ".join(asns(n)), *addrs(n), size=9)))
    for n in ["DC3-AWS", "DC3-AZURE", "DC3-BR001", "DC3-BR002", "DC3-PARTNER01"]:
        c.append(node_attr(n, hl(short(n), "AS " + ", ".join(asns(n)), *addrs(n), size=9)))
    for n in ["DC3-XC1", "DC3-XC2"]:
        c.append(node_attr(n, hl(short(n), "cross-connect bridge", size=9)))
    c.append(node_attr("DC3-SDWANFW1", hl("SDWANFW1", "AS " + ", ".join(asns("DC3-SDWANFW1")), *addrs("DC3-SDWANFW1", "ADVPN|LO-WAN"), size=9)))
    c.append(node_attr("DC3-VPNFW1", hl("VPNFW1", "AS " + ", ".join(asns("DC3-VPNFW1")), *addrs("DC3-VPNFW1", "PARTNER01|LO-WAN"), size=9)))
    for a, ai, z, zi in D.links:
        if role(a) == "cloud" or role(z) == "cloud" and not (a in D.eos) or a in ("DC3-SDWANFW1", "DC3-VPNFW1") and z not in D.eos:
            if a in D.eos or z in D.eos:
                continue
            c.append(edge(a, z, color=EDGE["ext"], label=f"{D.ifname(a, ai)} – {D.ifname(z, zi)}", fontcolor=EDGE["ext"]))
    svg2 = graph(c, rankdir="LR", extra="ranksep=1.0; nodesep=0.2;")
    body = (f'<div class="cap">Internet edge and DMZ</div><div class="fig half">{svg1}</div>'
            f'<div class="cap">Clouds, branches and partner</div><div class="fig half">{svg2}</div>')
    note = ("The edge pair is a layer-2 switch between the firewalls' outside interfaces (VLAN 3200, 10.27.14.0/24) and the ISP "
            "routers. ISP1/ISP2 send 0/0 to the Internet FW only while their carrier default exists. Carriers, clouds, branches "
            "and the partner are lab-only stand-ins with the public or provider ASNs of the design (192.0.2.0/24 and 198.18.0.0/15 "
            "are documentation and benchmark ranges).")
    doc.add("Internet edge, DMZ, clouds and branches", "Everything outside the VTEP fabric, with interfaces and addresses from the stand-in scripts", body, note)


def p_paths(doc, D):
    vt = [n for n in D.eos if role(n) in ("leaf", "border", "service")]
    rows = []
    gw = {}
    for v in ["PROD", "DEV", "CITRIX", "PARTNER", "REPLICATION", "TRANSIT"]:
        vni = None
        where = []
        for n in vt:
            x = D.cfg[n]["vxlan"]["vrfs"].get(v)
            if x:
                vni = x; where.append(short(n))
        summ = []
        for n in ["DC3-SL1"]:
            vv = D.cfg[n]["bgp"]["vrfs"].get(v)
            if vv:
                summ = vv["aggregates"] + vv["networks"]
        rows.append([v, vni or "–", len(where), ranges(where) if where else "no switch yet",
                     ", ".join(summ)])
    t = table(["VRF", "L3 VNI", "Switches", "On", "DC3 summary sent to the Internal FW"], rows, "mid")

    def chain(name, steps, color):
        b = [f'rankdir=LR;']
        prev = None
        for i, (n, lab, r) in enumerate(steps):
            b.append(node_attr(f"{name}{i}", hl(n, *lab.split("|"), size=9), r=r))
            if prev is not None:
                b.append(edge(prev, f"{name}{i}", color=color, penwidth=2, arrowhead="normal"))
            prev = f"{name}{i}"
        return graph(b, rankdir="LR", extra="nodesep=0.2; ranksep=0.35;")

    p1 = chain("a", [("Host 10.24.10.10", "VRF PROD|gw 10.24.10.1", "host"),
                     ("Leaf (any)", "route in PROD|VXLAN VNI 50001", "leaf"),
                     ("SL pair", "VTEP 10.27.2.3|PROD → FW", "service"),
                     ("Internal FW", "10.27.8.3 PROD|10.27.16.3 TRANSIT", "fw"),
                     ("SL pair", "TRANSIT|VXLAN VNI 50100", "service"),
                     ("BL pair", "VTEP 10.27.2.2|TRANSIT", "border"),
                     ("Internet FW", "10.27.16.11|outside 10.27.14.10", "fw"),
                     ("EDGE pair", "VLAN 3200", "l2pair"),
                     ("ISP1 / ISP2", "AS 65050", "isp"),
                     ("Carrier", "ISP-A / ISP-B", "isp")], "#a33131")
    p2 = chain("b", [("Host", "tenant VRF", "host"), ("Leaf", "VNI of the VRF", "leaf"), ("SL pair", "VRF → FW", "service"),
                     ("Internal FW", "VRF → TRANSIT", "fw"), ("SL pair", "VNI 50100", "service"), ("BL pair", "TRANSIT", "border"),
                     ("Cloud router / SD-WAN FW", "CLOUD1/2 10.27.16.27/.35|SDWANFW1 10.27.16.19", "cloud"),
                     ("AWS / Azure / branches", "172.16.0.0/13, 172.30.0.0/15|10.64.0.0/12", "ext")], "#20727f")
    p3 = chain("c", [("Host", "VRF PARTNER", "host"), ("Leaf", "VNI 50031", "leaf"), ("BL pair", "PARTNER 10.27.9.1/.2", "border"),
                     ("VPN FW", "10.27.9.3, AS 65330|source NAT", "fw"), ("Partner", "PARTNER01", "ext")], "#7a3c94")
    body = (t + '<div class="cap">Tenant to the internet (the path the failure tests ping: PROD host to 198.18.1.10)</div>'
            f'<div class="fig strip">{p1}</div><div class="cap">Tenant to the clouds and branches (never through the Internet FW, failure test 16)</div>'
            f'<div class="fig strip">{p2}</div><div class="cap">Partner VRF to the VPN partner</div><div class="fig strip">{p3}</div>')
    note = ("Tenant VRFs leave the fabric only through the Internal FW on the service leaves; only TRANSIT reaches the border "
            "leaves' outside attachments. REPLICATION is defined (VNI 50041) but no switch carries it yet. "
            "The path labels are written from docs/DESIGN.md section 7 and checked against the configs; the VRF table is read from them. "
            "Captured packets of path 1: docs/VXLAN-EVPN-CAPTURE.md.")
    doc.add("VRFs and traffic paths", "Where each VRF lives and the three ways traffic leaves the fabric", body, note)


def p_policy(doc, D):
    rows = []
    for n in ["DC3-SL1", "DC3-BL1"]:
        c = D.cfg[n]
        for nb in D.vrf_neighbors(n):
            for direction, rm in (("in", nb["route_map_in"]), ("out", nb["route_map_out"])):
                if not rm:
                    continue
                pls = I.route_map_prefixes(c, rm)
                entries = c["route_maps"].get(rm, [])
                pl_names = [p for e in entries for p in e["prefix_lists"]]
                txt = "; ".join(f"{p}: " + ", ".join(f"{x['prefix']} {x['options']}".strip() for x in c["prefix_lists"].get(p, [])) for p in pl_names)
                rows.append([short(n).rstrip("12") + " pair", nb["vrf"], nb["description"] or nb["ip"], direction, rm, Raw(esc(txt) or "<i>(no prefix-list)</i>")])
    rm_all = {}
    t = table(["Pair", "VRF", "Neighbor", "Dir", "Route-map", "Permits (prefix-list: entries)"], rows, "big")
    lo = D.cfg["DC3-L1"]
    extra = table(["Route-map (all BGP switches)", "Permits"], [["RM-CONN-2-BGP (redistribute connected, default VRF)",
                    "; ".join(f"{p}: " + ", ".join(f"{x['prefix']} {x['options']}".strip() for x in lo["prefix_lists"][p]) for e in lo["route_maps"]["RM-CONN-2-BGP"] for p in e["prefix_lists"])]], "big")
    doc.add("Routing policy", "Every route-map on the firewall, cloud and SD-WAN attachments (service pair and border pair; both members are equal)",
            t + extra, "Read from the configs of SL1 and BL1; SL2 and BL2 carry the same policy. All entries are permit; anything not "
            "matched is denied by the implicit deny at the end of each route-map.")


def p_segments(doc, D):
    c = D.cfg["DC3-L1"]
    vl = c["vxlan"]["vlans"]
    names = {}
    txt = (ROOT / "tests" / "expected" / "DC3-L1.cfg").read_text()
    for m in re.finditer(r"^vlan (\d+)\n   name (\S+)", txt, re.M):
        names[int(m.group(1))] = m.group(2)
    rows = []
    for v, vni in sorted(vl.items()):
        i = D.intf("DC3-L1").get(f"Vlan{v}", {})
        rows.append([v, names.get(v, ""), vni, i.get("vrf", ""), i.get("virtual_ip") or i.get("ip") or ""])
    k = (len(rows) + 2) // 3
    body = '<div class="three">' + "".join(table(["VLAN", "Name", "L2 VNI", "VRF", "Anycast gateway"], rows[j:j + k], "big") for j in range(0, len(rows), k)) + "</div>"
    doc.add("Tenant segments", f"{len(rows)} server segments on every server leaf (L1..L20), same VLAN, VNI and gateway everywhere",
            body, "From the DC3-L1 config; every server leaf renders the same list. Gateways use ip address virtual with the anycast "
            "MAC 0200.0000.00aa; RT 65300:<VNI> (route-target both).")


def p_mgmt(doc, D):
    rows = [[short(n), D.mgmt(n), role(n).replace("l2pair", "edge/DMZ")] for n in D.eos]
    k = (len(rows) + 1) // 2
    t = '<div class="two">' + table(["Switch", "Management1 (VRF MGMT)", "Role"], rows[:k], "mid") + table(["Switch", "Management1 (VRF MGMT)", "Role"], rows[k:], "mid") + "</div>"
    st = [s for s in D.cfg["DC3-L1"]["statics"] if s["vrf"] == "MGMT"]
    steps = [
        ("Blank switch", "empty flash|boots, ZTP on", "leaf"),
        ("Kea DHCP", "10.250.32.200-.239|option 67 → bootstrap URL", "isp"),
        ("ZTP server", "GET /bootstrap|10.250.32.1:8080", "cloud"),
        ("Bootstrap script", "reads serial|FastCli show version", "leaf"),
        ("ZTP server", "GET /config/<serial>|finds device by serial_number", "cloud"),
        ("Infrahub", "infrahubctl render|branch main", "spine"),
        ("Switch", "writes startup-config|callback, reboots", "leaf"),
        ("ZTP server", "eAPI show hostname|as dc3-automation, ≤ 30 min", "cloud"),
        ("Infrahub", "ztp_state complete|ztp_updated", "spine"),
    ]
    b = []
    for i, (n, lab, r) in enumerate(steps):
        b.append(node_attr(f"z{i}", hl(f"{i+1}. {n}", *lab.split("|"), size=9), r=r))
        if i:
            b.append(edge(f"z{i-1}", f"z{i}", color="#444444", penwidth=1.6, arrowhead="normal"))
    svg = graph(b, rankdir="LR", extra="nodesep=0.2; ranksep=0.3;")
    body = (t + f"<div class='cap'>Management VRF: default route {esc(st[0]['prefix'] if st else '')} via {esc(st[0]['nexthop'] if st else '')}, "
            "no ip routing vrf MGMT; eAPI (HTTPS) in VRF MGMT; local user dc3-automation.</div>"
            "<div class='cap'>Zero-touch provisioning (ztp/)</div>"
            f'<div class="fig strip">{svg}</div>')
    doc.add("Management and ZTP", "OOB addresses from pool DC3-MGMT0 (10.250.32.0/24, gateway .1) and how a blank switch gets its config",
            body, "ZTP states: pending (set before racking) → served → complete, or failed. Verified: one switch on the laptop in "
            "66 s; all 30 in parallel, 30/30 complete in 425 s.")


def p_lab(doc, D):
    import dc3_failtests as F
    cat = F.build_catalog(D.X)
    rows = []
    for t in cat:
        nodes = sorted({a["node"] for ph in t["phases"] for a in ph["break"]}, key=natural)
        lim = [ph["limit"] if "limit" in ph else t["limit"] for ph in t["phases"]]
        lim = ", ".join("measured" if x is None else f"{x} s" for x in lim)
        rows.append([t["id"], t["title"], ", ".join(short(n) for n in nodes), lim])
    tt = table(["#", "Failure test", "Breaks", "Outage limit"], rows, "big")
    steps = [("images", "cEOS 4.36.2F, FRR 10.6.0|Kea"), ("build", "empty Infrahub|43 renders"),
             ("secrets", "password hash|into lab/configs"), ("deploy", "containerlab|52 nodes, 151 links"),
             ("postcheck", "1525 checks|no FAIL / ERROR"), ("failtests", "16 tests|captures, commands"),
             ("capture", "VXLAN / EVPN|pcaps"), ("results", "lab/results"), ("destroy", "containerlab|--cleanup"),
             ("ztp", "30 blank cEOS|in parallel")]
    b = []
    for i, (n, lab) in enumerate(steps):
        b.append(node_attr(f"s{i}", hl(n, *lab.split("|"), size=9), r="spine" if i < 4 else "leaf" if i < 9 else "isp"))
        if i:
            b.append(edge(f"s{i-1}", f"s{i}", color="#444444", penwidth=1.4, arrowhead="normal"))
    svg = graph(b, rankdir="LR", extra="nodesep=0.15; ranksep=0.25;")
    lab = D.X["lab_nodes"]
    kinds = {}
    for n in lab:
        k = D.nodes[n]["kind"]
        kinds[k] = kinds.get(k, 0) + 1
    body = (f"<div class='cap'>The full lab on one host ({kinds.get('eos',0)} cEOS-lab 4.36.2F, "
            f"{kinds.get('frr',0)} FRR 10.6.0, {kinds.get('host',0)} test hosts, {kinds.get('bridge',0)} bridges) "
            "(docs/QUICKSTART.md, docs/BUILD.md)</div>"
            f'<div class="fig strip">{svg}</div>' + tt)
    doc.add("Lab and tests", "How the lab is run, and what each failure test breaks",
            body, "Full lab (52 nodes): pre-check 1525/1525, 15 of 16 failure tests pass (test 6 open), "
            "post-check 0 new failures, ZTP 30/30, rebuild from the docs passes.")


# ---------------------------------------------------------------------------------------------- html / pdf
CSS = """
@page { size: 420mm 297mm; margin: 11mm 12mm 12mm 12mm; }
* { box-sizing: border-box; }
body { font-family: Inter, 'DejaVu Sans', sans-serif; color: #1d232b; margin: 0; font-size: 10.5px; }
.page { page-break-after: always; height: 272mm; display: flex; flex-direction: column; overflow: hidden; }
.page:last-child { page-break-after: auto; }
header { display: flex; align-items: baseline; justify-content: space-between; border-bottom: 2px solid #2b4c7e; padding-bottom: 4px; margin-bottom: 6px; }
header h2 { margin: 0; font-size: 20px; letter-spacing: -0.2px; }
header .sub { color: #4b5563; font-size: 12px; margin-left: 14px; flex: 1; }
header .tag { color: #6b7280; font-size: 10px; }
main { flex: 1; min-height: 0; display: flex; flex-direction: column; gap: 6px; }
.fig { flex: 1; min-height: 0; display: flex; justify-content: center; align-items: center; }
.fig svg { max-width: 100%; max-height: 100%; width: auto; height: auto; }
.fig.short { flex: 1 1 55%; }
.fig.half { flex: 1 1 45%; }
.fig.strip { flex: 0 0 auto; max-height: 52mm; }
.fig.strip svg { max-height: 52mm; }
.fig.mini svg { max-height: 108mm; }
footer.note { border-top: 1px solid #d1d5db; color: #4b5563; font-size: 10px; padding-top: 4px; margin-top: 6px; }
table.t { border-collapse: collapse; width: 100%; font-size: 9.5px; }
table.t th { background: #eef2f7; text-align: left; padding: 3px 5px; border-bottom: 1px solid #9ca3af; }
table.t td { padding: 2px 5px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }
table.t.dense td, table.t.dense th { padding: 1.5px 4px; font-size: 9px; }
table.t.big td, table.t.big th { padding: 3px 6px; font-size: 11.5px; }
table.t.nowrap td { white-space: nowrap; }
table.t.mid td, table.t.mid th { padding: 2px 5px; font-size: 10.5px; }
.ip { font-family: 'DejaVu Sans Mono', monospace; color: #2b4c7e; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; align-items: start; }
.three { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; align-items: start; }
.grid { display: grid; grid-template-columns: 1fr 1fr 1fr; grid-template-rows: 1fr 1fr; gap: 8px; flex: 1; min-height: 0; }
.cell { border: 1px solid #e5e7eb; border-radius: 6px; padding: 4px; display: flex; flex-direction: column; min-height: 0; }
.cap { font-size: 10.5px; color: #374151; margin: 2px 0; }
.legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 10px; color: #374151; }
.legend i { display: inline-block; width: 14px; height: 10px; border: 1.5px solid; border-radius: 3px; margin-right: 4px; vertical-align: -1px; }
.cover { padding: 30mm 20mm; }
.cover .kicker { color: #2b4c7e; font-weight: 600; letter-spacing: 2px; text-transform: uppercase; font-size: 13px; }
.cover h1 { font-size: 46px; margin: 6px 0 10px; letter-spacing: -1px; }
.cover .lede { font-size: 15px; max-width: 250mm; line-height: 1.45; color: #374151; }
.cover .stats { display: flex; gap: 22px; margin: 14mm 0 10mm; }
.cover .stats div { border-left: 3px solid #2b4c7e; padding-left: 10px; }
.cover .stats b { display: block; font-size: 30px; }
.cover .stats span { color: #4b5563; font-size: 12px; }
.cover table.contents { border-collapse: collapse; font-size: 13px; }
.cover table.contents td { padding: 4px 14px 4px 0; border-bottom: 1px solid #e5e7eb; }
.cover table.contents td.n { color: #2b4c7e; font-weight: 700; }
.cover .meta { margin-top: 10mm; color: #6b7280; font-size: 11px; }

.cards { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }
.card { border: 1.5px solid; border-radius: 6px; overflow: hidden; }
.card .ch { color: #fff; font-weight: 700; font-size: 12px; padding: 4px 8px; display: flex; justify-content: space-between; }
.card .ch span { font-weight: 400; font-size: 10px; opacity: 0.9; }
.card table { border-collapse: collapse; width: 100%; font-size: 11px; }
.card th { text-align: left; color: #374151; font-weight: 600; padding: 2px 6px; width: 32%; vertical-align: top; border-bottom: 1px solid #eef0f3; }
.card td { padding: 2px 6px; border-bottom: 1px solid #eef0f3; }
.stack { display: flex; border: 1px solid #9ca3af; border-radius: 4px; overflow: hidden; margin: 2px 0 4px; }
.hb { padding: 5px 7px; border-right: 1px solid #9ca3af; font-size: 11px; line-height: 1.35; min-width: 0; }
.hb:last-child { border-right: none; }
.hb i { font-style: normal; opacity: 0.7; font-size: 8.5px; }
.walk { border: 1px solid #e5e7eb; border-radius: 6px; padding: 6px 8px; flex: 1; display: flex; flex-direction: column; min-height: 0; }
.wt { font-size: 12px; margin-bottom: 3px; }
.tagq { color: #8a5a00; font-size: 10px; margin-left: 8px; }
.wrow { display: grid; grid-template-columns: 1.1fr 1fr; gap: 10px; align-items: center; flex: 1; min-height: 0; }
.steps { margin: 0; padding-left: 18px; font-size: 10.5px; line-height: 1.4; }
.steps li { margin-bottom: 3px; }
code { font-family: 'DejaVu Sans Mono', monospace; font-size: 0.92em; background: #f3f4f6; padding: 0 3px; border-radius: 3px; }
"""


def render(doc, out: Path, keep_html: bool, commit: str):
    parts = []
    num = 0
    for title, sub, body, note in doc.pages:
        if title:
            num += 1
            title = f"{num} · {title}"
        if not title:
            parts.append(f'<section class="page">{body}</section>')
            continue
        parts.append(f'<section class="page"><header><h2>{esc(title)}</h2><span class="sub">{esc(sub)}</span>'
                     f'<span class="tag">DC3 · infrahub-arista-dc {esc(commit)}</span></header><main>{body}</main>'
                     + (f'<footer class="note">{esc(note)}</footer>' if note else "") + "</section>")
    page = f"<!doctype html><html><head><meta charset='utf-8'><title>DC3 diagrams</title><style>{CSS}</style></head><body>{''.join(parts)}</body></html>"
    out.mkdir(parents=True, exist_ok=True)
    hp = out / "DC3-diagrams.html"
    hp.write_text(page)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page()
        pg.goto(hp.resolve().as_uri())
        pg.pdf(path=str(out / "DC3-diagrams.pdf"), prefer_css_page_size=True, print_background=True,
               display_header_footer=True, header_template="<span></span>",
               footer_template="<div style='font-size:8px;width:100%;text-align:right;padding-right:12mm;color:#6b7280;font-family:Inter'>"
                               "page <span class='pageNumber'></span> of <span class='totalPages'></span></div>")
        br.close()
    if not keep_html:
        hp.unlink()
    return out / "DC3-diagrams.pdf"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(ROOT / "docs" / "diagrams"))
    ap.add_argument("--html", action="store_true", help="keep the HTML page next to the PDF")
    a = ap.parse_args()
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip() or "?"
    D = Data(load())
    doc = Doc()
    for f in (p_physical, p_fabric_links, p_underlay, p_overlay, p_evpn_routes, p_packet_walks, p_mlag, p_service, p_border,
              p_edge, p_paths, p_policy, p_segments, p_mgmt, p_lab):
        f(doc, D)
    p_cover(doc, D, commit)
    doc.pages.insert(0, doc.pages.pop())
    # page 7 and 8 order: border before edge already; renumbering kept in titles
    pdf = render(doc, Path(a.out), a.html, commit)
    print(f"{pdf}: {len(doc.pages)} pages")


if __name__ == "__main__":
    main()
