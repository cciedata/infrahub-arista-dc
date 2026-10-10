"""DC3 lab intent: what the running lab must show, derived from the rendered files only.

Inputs (all in lab/):
  configs/<device>.cfg      EOS startup-configs (infrahubctl render)
  configs/*.sh              FRR stand-in and lab-only scripts (bound at /frr-init.sh)
  dc3.clab.yml / dc3-smoke.clab.yml   containerlab topology (scripts/dc3_lab_topology.py)

Python 3 standard library only (the lab host needs no extra package): the topology file is read with a small reader
for the block-YAML subset that yaml.safe_dump writes.

Used by lab/dc3_postcheck.py and lab/dc3_failtests.py. Labels in comments: verified (source), unverified.
"""
from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path

LAB = Path(__file__).resolve().parent

# Names decided in the templates (plans/dc3-values.md): one place here
PG_UNDERLAY = "IPV4-UNDERLAY-PEERS"
PG_OVERLAY = "EVPN-OVERLAY-PEERS"
PG_MLAG = "MLAG-IPV4-UNDERLAY-PEER"
MGMT_VRF = "MGMT"
KEEPALIVE_VRF = "VPC-KEEPALIVE"
INFRA_VRFS = {MGMT_VRF, KEEPALIVE_VRF}
TRANSIT_VRF = "TRANSIT"
DEFAULT_ROUTE = "0.0.0.0/0"
EOS_KIND, LINUX_KIND = "arista_ceos", "linux"
INIT_TARGET = "/frr-init.sh"
UNKNOWN = "?unknown-peer"                  # placeholder in requires: a neighbor address no rendered file holds


# ---------------------------------------------------------------------------------------------- tiny YAML reader
def _scalar(s: str):
    s = s.strip()
    if s.startswith("'") and s.endswith("'") and len(s) >= 2:
        return s[1:-1].replace("''", "'")
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        return json.loads(s)
    if s in ("null", "~", ""):
        return None
    if s in ("true", "false"):
        return s == "true"
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def load_yaml_subset(text: str):
    """Block YAML as written by yaml.safe_dump(sort_keys=False): mappings, '- ' lists (also at the parent key's indent),
    plain or quoted scalars, '{}' / '[]'. Comments and blank lines are skipped. Enough for the containerlab files."""
    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), raw.strip()))
    pos = 0

    def parse_block(indent):
        nonlocal pos
        if pos >= len(lines):
            return None
        ind, s = lines[pos]
        if s.startswith("- ") or s == "-":
            return parse_list(ind)
        return parse_map(ind)

    def parse_value_after_key(key_indent, rest):
        nonlocal pos
        if rest == "{}":
            return {}
        if rest == "[]":
            return []
        if rest:
            return _scalar(rest)
        if pos < len(lines):
            ind, s = lines[pos]
            if ind > key_indent or (ind == key_indent and s.startswith("-")):
                return parse_block(ind)
        return None

    def parse_map(indent):
        nonlocal pos
        out = {}
        while pos < len(lines):
            ind, s = lines[pos]
            if ind != indent or s.startswith("- ") or s == "-":
                break
            m = re.match(r"^('(?:[^']|'')*'|\"[^\"]*\"|[^'\"\s][^:]*?):(?:\s+(.*))?$", s)
            if not m:
                raise ValueError(f"cannot read line: {s}")
            key = _scalar(m.group(1))
            pos += 1
            out[key] = parse_value_after_key(indent, (m.group(2) or "").strip())
        return out

    def parse_list(indent):
        nonlocal pos
        out = []
        while pos < len(lines):
            ind, s = lines[pos]
            if ind != indent or not (s.startswith("- ") or s == "-"):
                break
            item = s[2:].strip() if s != "-" else ""
            if re.match(r"^('(?:[^']|'')*'|\"[^\"]*\"|[^'\"\s][^:]*?):(\s|$)", item):
                # a mapping that starts on the dash line: re-read it as a map at the item's indent
                lines[pos] = (indent + 2, item)
                out.append(parse_map(indent + 2))
            else:
                pos += 1
                if item:
                    out.append(_scalar(item))
                else:
                    out.append(parse_block(indent + 2))
        return out

    return parse_block(0)


def read_topology(path: Path) -> dict:
    """{'name', 'nodes': {name: node dict}, 'links': [(node_a, if_a, node_b, if_b)], 'kinds'}"""
    doc = load_yaml_subset(Path(path).read_text())
    topo = doc["topology"]
    links = []
    for l in topo.get("links") or []:
        a, b = l["endpoints"]
        na, ia = a.split(":", 1)
        nb, ib = b.split(":", 1)
        links.append((na, ia, nb, ib))
    return {"name": doc["name"], "nodes": topo["nodes"], "links": links, "kinds": topo.get("kinds", {})}


# ---------------------------------------------------------------------------------------------- EOS
def _is_ip(s: str) -> bool:
    try:
        ipaddress.IPv4Address(s)
        return True
    except ValueError:
        return False


def _blocks(text: str):
    """Top-level blocks: (header line, [child lines with their indentation kept])."""
    out = []
    for raw in text.splitlines():
        if not raw.strip() or raw.strip() == "!":
            continue
        if not raw.startswith(" "):
            out.append([raw.strip(), []])
        elif out:
            out[-1][1].append(raw.rstrip())
    return out


def parse_eos(text: str) -> dict:
    """Intent of one EOS startup-config: hostname, interfaces, VRFs, statics, prefix-lists, route-maps, MLAG, Vxlan1,
    router bfd and router bgp (default and VRF neighbors, resolved through their peer groups)."""
    d = {"hostname": None, "interfaces": {}, "vrfs": [], "statics": [], "prefix_lists": {}, "route_maps": {},
         "mlag": None, "vxlan": None, "bfd_multihop": None, "bgp": None}
    for head, kids in _blocks(text):
        w = head.split()
        if w[0] == "hostname":
            d["hostname"] = w[1]
        elif head.startswith("vrf instance "):
            d["vrfs"].append(w[2])
        elif w[0] == "interface":
            name = w[1]
            i = {"name": name, "description": None, "ip": None, "virtual_ip": None, "vrf": "default", "bfd": False,
                 "channel_group": None, "routed": False, "shutdown": False}
            for k in kids:
                s = k.strip()
                if s.startswith("description "):
                    i["description"] = s[len("description "):]
                elif s.startswith("ip address virtual "):
                    i["virtual_ip"] = s.split()[3]
                elif s.startswith("ip address "):
                    i["ip"] = s.split()[2]
                elif s.startswith("vrf "):
                    i["vrf"] = s.split()[1]
                elif s.startswith("bfd interval"):
                    i["bfd"] = True
                elif s.startswith("channel-group "):
                    i["channel_group"] = int(s.split()[1])
                elif s == "no switchport":
                    i["routed"] = True
                elif s == "shutdown":
                    i["shutdown"] = True
                elif name == "Vxlan1":
                    d["vxlan"] = d["vxlan"] or {"source_interface": None, "vlans": {}, "vrfs": {}}
                    m = re.match(r"vxlan source-interface (\S+)", s)
                    if m:
                        d["vxlan"]["source_interface"] = m.group(1)
                    m = re.match(r"vxlan vlan (\d+) vni (\d+)", s)
                    if m:
                        d["vxlan"]["vlans"][int(m.group(1))] = int(m.group(2))
                    m = re.match(r"vxlan vrf (\S+) vni (\d+)", s)
                    if m:
                        d["vxlan"]["vrfs"][m.group(1)] = int(m.group(2))
            if name == "Vxlan1" and d["vxlan"] is None:
                d["vxlan"] = {"source_interface": None, "vlans": {}, "vrfs": {}}
            d["interfaces"][name] = i
        elif head.startswith("ip route "):
            m = re.match(r"ip route (?:vrf (\S+) )?(\S+) (\S+)", head)
            if m:
                d["statics"].append({"vrf": m.group(1) or "default", "prefix": m.group(2), "nexthop": m.group(3)})
        elif head.startswith("ip prefix-list "):
            pl = d["prefix_lists"].setdefault(w[2], [])
            for k in kids:
                m = re.match(r"\s*seq \d+ permit (\S+)(.*)$", k)
                if m:
                    pl.append({"prefix": m.group(1), "options": m.group(2).strip()})
        elif w[0] == "route-map" and len(w) >= 4:
            rm = d["route_maps"].setdefault(w[1], [])
            entry = {"action": w[2], "seq": int(w[3]), "prefix_lists": []}
            for k in kids:
                m = re.match(r"\s*match ip address prefix-list (\S+)", k)
                if m:
                    entry["prefix_lists"].append(m.group(1))
            rm.append(entry)
        elif head == "mlag configuration":
            m = {}
            for k in kids:
                s = k.strip()
                for key in ("domain-id", "local-interface", "peer-link", "heartbeat-interval"):
                    if s.startswith(key + " "):
                        m[key] = s[len(key) + 1:]
                if s.startswith("peer-address heartbeat "):
                    m["peer-address-heartbeat"] = s.split()[2]
                elif s.startswith("peer-address "):
                    m["peer-address"] = s.split()[1]
                elif s.startswith("dual-primary detection "):
                    m["dual-primary-detection"] = s[len("dual-primary detection "):]
                elif s.startswith("dual-primary recovery delay "):
                    m["dual-primary-recovery"] = s[len("dual-primary recovery delay "):]
            d["mlag"] = m
        elif head == "router bfd":
            for k in kids:
                if k.strip().startswith("multihop "):
                    d["bfd_multihop"] = k.strip()[len("multihop "):]
        elif w[0] == "router" and w[1] == "bgp":
            d["bgp"] = _parse_eos_bgp(int(w[2]), kids)
    return d


def _parse_eos_bgp(asn: int, kids: list[str]) -> dict:
    bgp = {"asn": asn, "router_id": None, "peer_groups": {}, "neighbors": [], "vrfs": {}}
    raw: dict = {}          # (vrf, neighbor) -> attributes
    vrf = "default"
    in_af = None
    for k in kids:
        ind = len(k) - len(k.lstrip(" "))
        s = k.strip()
        if ind == 3:
            in_af = None
            if s.startswith("vrf "):
                vrf = s.split()[1]
                bgp["vrfs"].setdefault(vrf, {"aggregates": [], "networks": [], "router_id": None})
                continue
            vrf = "default"
            if s.startswith("address-family "):
                in_af = s.split()[1]
                continue
        elif ind == 6 and vrf != "default" and s.startswith("address-family "):
            in_af = s.split()[1]
            continue
        if s.startswith("router-id "):
            if vrf == "default":
                bgp["router_id"] = s.split()[1]
            else:
                bgp["vrfs"][vrf]["router_id"] = s.split()[1]
            continue
        if vrf != "default" and s.startswith("aggregate-address "):
            bgp["vrfs"][vrf]["aggregates"].append(s.split()[1])
            continue
        if vrf != "default" and s.startswith("network "):
            bgp["vrfs"][vrf]["networks"].append(s.split()[1])
            continue
        m = re.match(r"neighbor (\S+) (.*)$", s)
        if not m:
            continue
        who, rest = m.group(1), m.group(2)
        if not _is_ip(who):
            pg = bgp["peer_groups"].setdefault(who, {"remote_as": None, "bfd": False, "update_source": None,
                                                      "activate": []})
            if rest == "peer group":
                continue
            if rest.startswith("remote-as "):
                pg["remote_as"] = int(rest.split()[1])
            elif rest == "bfd":
                pg["bfd"] = True
            elif rest.startswith("update-source "):
                pg["update_source"] = rest.split()[1]
            elif rest == "activate" and in_af:
                pg["activate"].append(in_af)
            continue
        n = raw.setdefault((vrf, who), {"vrf": vrf, "ip": who, "peer_group": None, "remote_as": None, "bfd": False,
                                        "description": None, "route_map_in": None, "route_map_out": None,
                                        "activate": []})
        if rest.startswith("peer group "):
            n["peer_group"] = rest.split()[2]
        elif rest.startswith("remote-as "):
            n["remote_as"] = int(rest.split()[1])
        elif rest == "bfd":
            n["bfd"] = True
        elif rest.startswith("description "):
            n["description"] = rest[len("description "):]
        elif rest == "activate" and in_af:
            n["activate"].append(in_af)
        elif rest.startswith("route-map "):
            parts = rest.split()
            n["route_map_" + parts[2]] = parts[1]
    for (vrf, ip), n in raw.items():
        pg = bgp["peer_groups"].get(n["peer_group"] or "", {})
        n["remote_as"] = n["remote_as"] if n["remote_as"] is not None else pg.get("remote_as")
        n["bfd"] = n["bfd"] or pg.get("bfd", False)
        afs = set(n["activate"]) | set(pg.get("activate", []))
        if n["peer_group"] == PG_OVERLAY:
            n["kind"], n["af"] = "overlay", "evpn"
        elif n["peer_group"] == PG_MLAG:
            n["kind"], n["af"] = "mlag", "ipv4"
        elif n["peer_group"] == PG_UNDERLAY:
            n["kind"], n["af"] = "underlay", "ipv4"
        else:
            n["kind"], n["af"] = ("vrf" if vrf != "default" else "other"), ("evpn" if afs == {"evpn"} else "ipv4")
        bgp["neighbors"].append(n)
    return bgp


def route_map_prefixes(cfg: dict, name: str | None) -> list[str]:
    """Prefixes a route-map permits (match ip address prefix-list), in order."""
    out = []
    for e in cfg["route_maps"].get(name or "", []):
        if e["action"] != "permit":
            continue
        for pl in e["prefix_lists"]:
            out += [x["prefix"] for x in cfg["prefix_lists"].get(pl, [])]
    return out


# ---------------------------------------------------------------------------------------------- FRR scripts
def script_part(text: str, hostname: str | None) -> str:
    """The part of a multi-node script for one hostname (`case "$(hostname)" in NAME) ... ;;`), else the whole text."""
    if hostname is None or 'case "$(hostname)" in' not in text:
        return text
    m = re.search(rf"^{re.escape(hostname)}\)\n(.*?)^;;", text, re.S | re.M)
    return m.group(1) if m else ""


def parse_frr(text: str, hostname: str | None = None) -> dict:
    """Addresses (ip addr add/replace) and BGP instances (router bgp <asn> [vrf <v>]) of one FRR node's script."""
    part = script_part(text, hostname)
    d = {"hostname": None, "addresses": [], "bgp": [], "vrf_of_dev": {}}
    m = re.search(r"^hostname (\S+)$", part, re.M)
    d["hostname"] = m.group(1) if m else hostname
    for m in re.finditer(r"ip addr (?:replace|add) (\d+\.\d+\.\d+\.\d+)/(\d+) dev (\S+?)(?:;|\s|$)", part):
        d["addresses"].append({"ip": m.group(1), "plen": int(m.group(2)), "dev": m.group(3)})
    for m in re.finditer(r"ip link set (\S+) master (vrf\d+)", part):
        d["vrf_of_dev"][m.group(1)] = m.group(2)
    inst = None
    for line in part.splitlines():
        m = re.match(r"^router bgp (\d+)(?: vrf (\S+))?\s*$", line)
        if m:
            inst = {"asn": int(m.group(1)), "vrf": m.group(2) or "default", "neighbors": {}, "listen_ranges": [],
                    "networks": []}
            d["bgp"].append(inst)
            continue
        if inst is None:
            continue
        if line.startswith("exit") or (line and not line.startswith(" ")):
            inst = None
            continue
        s = line.strip()
        m = re.match(r"neighbor (\S+) (.*)$", s)
        if m and _is_ip(m.group(1)):
            n = inst["neighbors"].setdefault(m.group(1), {"ip": m.group(1), "remote_as": None, "bfd": False,
                                                           "description": None, "vrf": inst["vrf"]})
            rest = m.group(2)
            if rest.startswith("remote-as "):
                n["remote_as"] = rest.split()[1]
            elif rest.startswith("bfd"):
                n["bfd"] = True
            elif rest.startswith("description "):
                n["description"] = rest[len("description "):]
        m = re.match(r"bgp listen range (\S+)", s)
        if m:
            inst["listen_ranges"].append(m.group(1))
        m = re.match(r"network (\d+\.\d+\.\d+\.\d+/\d+)", s)
        if m:
            inst["networks"].append(m.group(1))
    return d


def parse_host_exec(lines: list[str]) -> dict:
    """Test host / internet host commands from the topology (ip addr add, default route)."""
    d = {"addresses": [], "gateway": None}
    for s in lines or []:
        m = re.match(r"ip addr add (\S+)/(\d+) dev (\S+)", s)
        if m:
            d["addresses"].append({"ip": m.group(1), "plen": int(m.group(2)), "dev": m.group(3)})
        m = re.match(r"ip route replace default via (\S+)", s)
        if m:
            d["gateway"] = m.group(1)
    return d


# ---------------------------------------------------------------------------------------------- the lab intent
def _bind_script(node: dict) -> str | None:
    for b in node.get("binds") or []:
        src, _, rest = b.partition(":")
        if rest.startswith(INIT_TARGET):
            return src
    return None


def build_intent(lab_dir: Path = LAB, topology_file: Path | None = None) -> dict:
    """Parse the topology and every file it references. Missing configs are recorded, not fatal."""
    lab_dir = Path(lab_dir)
    topo = read_topology(topology_file or (lab_dir / "dc3.clab.yml"))
    nodes, missing = {}, []
    for name, n in topo["nodes"].items():
        if n.get("kind") == EOS_KIND:
            path = lab_dir / n["startup-config"]
            if not path.exists():
                missing.append(str(path))
                nodes[name] = {"kind": "eos", "cfg": None}
                continue
            nodes[name] = {"kind": "eos", "cfg": parse_eos(path.read_text())}
            continue
        script = _bind_script(n)
        if script:
            path = lab_dir / script
            if not path.exists():
                missing.append(str(path))
                nodes[name] = {"kind": "frr", "cfg": None}
                continue
            nodes[name] = {"kind": "frr", "cfg": parse_frr(path.read_text(), name)}
            continue
        host = parse_host_exec(n.get("exec"))
        nodes[name] = {"kind": "host" if host["gateway"] else "bridge", "cfg": host}
    lab_nodes = sorted(nodes)
    # rendered files of devices outside this topology (a subset lab): known for addresses and names only
    cfg_dir = lab_dir / "configs"
    if cfg_dir.is_dir():
        for path in sorted(cfg_dir.glob("*.cfg")):
            if path.stem not in nodes:
                nodes[path.stem] = {"kind": "eos", "cfg": parse_eos(path.read_text()), "outside": True}
        for path in sorted(cfg_dir.glob("*.sh")):
            if "." not in path.stem and path.stem not in nodes:
                nodes[path.stem] = {"kind": "frr", "cfg": parse_frr(path.read_text(), path.stem), "outside": True}
    owners: dict = {}
    for name, n in nodes.items():
        for ip in node_addresses(n):
            owners.setdefault(ip, [])
            if name not in owners[ip]:
                owners[ip].append(name)
    return {"topology": topo, "nodes": nodes, "lab_nodes": lab_nodes, "owners": owners, "missing": missing}


def node_addresses(n: dict) -> list[str]:
    cfg = n.get("cfg")
    if not cfg:
        return []
    if n["kind"] == "eos":
        return [i["ip"].split("/")[0] for i in cfg["interfaces"].values() if i["ip"]]
    return [a["ip"] for a in cfg["addresses"]]


def owner(intent: dict, ip: str) -> str | None:
    """The single node holding an address (None when unknown or shared, e.g. an MLAG anycast VTEP)."""
    o = intent["owners"].get(ip, [])
    return o[0] if len(o) == 1 else None


def vtep_address(cfg: dict) -> str | None:
    vx = cfg.get("vxlan")
    if not vx or not vx["source_interface"]:
        return None
    i = cfg["interfaces"].get(vx["source_interface"])
    return i["ip"].split("/")[0] if i and i["ip"] else None


def link_peers(intent: dict, node: str) -> list[tuple[str, str, str]]:
    """(local interface, peer node, peer interface) of every topology link of a node."""
    out = []
    for a, ia, b, ib in intent["topology"]["links"]:
        if a == node:
            out.append((ia, b, ib))
        elif b == node:
            out.append((ib, a, ia))
    return out


def eos_nodes(intent):
    return [n for n, x in intent["nodes"].items() if x["kind"] == "eos" and x["cfg"]]


def frr_nodes(intent):
    return [n for n, x in intent["nodes"].items() if x["kind"] == "frr" and x["cfg"]]


def host_nodes(intent):
    return [n for n, x in intent["nodes"].items() if x["kind"] == "host"]


# ---------------------------------------------------------------------------------------------- default-route chain
def default_sources(intent: dict, vrf: str) -> set[str]:
    """Stand-ins from which some EOS node accepts 0.0.0.0/0 in this VRF (its inbound route-map permits PL with 0/0)."""
    out = set()
    for name in eos_nodes(intent):
        cfg = intent["nodes"][name]["cfg"]
        for n in (cfg["bgp"] or {}).get("neighbors", []):
            if n["vrf"] == vrf and DEFAULT_ROUTE in route_map_prefixes(cfg, n["route_map_in"]):
                o = owner(intent, n["ip"])
                if o:
                    out.add(o)
    return out


def frr_peers(intent: dict, node: str) -> set[str]:
    cfg = intent["nodes"][node]["cfg"]
    out = set()
    for inst in cfg["bgp"]:
        for ip in inst["neighbors"]:
            o = owner(intent, ip)
            if o:
                out.add(o)
    return out


def default_chain(intent: dict, vrf: str) -> tuple[set[str], list[list[str]]]:
    """(nodes required, groups of which one is enough) for a VRF's 0/0 to exist anywhere in the fabric.
    Rule: the VRF's own default sources; for a non-TRANSIT VRF whose source also peers in
    TRANSIT (Internal FW leak) the TRANSIT chain as well; behind the TRANSIT source (Internet FW) any one of its
    non-fabric BGP peers (ISP routers, conditional default) and any one of those routers' carriers."""
    req, anyg = set(default_sources(intent, vrf)), []
    eos = set(eos_nodes(intent))
    if vrf != TRANSIT_VRF:
        for s in sorted(req):
            if any(owner(intent, n["ip"]) == s for e in eos for n in (intent["nodes"][e]["cfg"]["bgp"] or {}).get("neighbors", [])
                   if n["vrf"] == TRANSIT_VRF):
                r2, a2 = default_chain(intent, TRANSIT_VRF)
                req |= r2
                anyg += a2
                break
    else:
        for s in sorted(req):
            if intent["nodes"][s]["kind"] != "frr":
                continue
            isps = sorted(p for p in frr_peers(intent, s) if p not in eos and intent["nodes"][p]["kind"] == "frr")
            if isps:
                anyg.append(isps)
                carriers = sorted({c for i in isps for c in frr_peers(intent, i)
                                   if c not in eos and c not in isps and c != s})
                if carriers:
                    anyg.append(carriers)
    return req, anyg


# ---------------------------------------------------------------------------------------------- expected checks
def _check(cid, node, ctype, desc, requires=(), requires_any=(), **kw):
    return {"id": cid, "node": node, "type": ctype, "desc": desc, "requires": sorted(set(requires) | {node}),
            "requires_any": [sorted(g) for g in requires_any], **kw}


def expected_checks(intent: dict, internet_targets: dict | None = None) -> list[dict]:
    """Every check the lab must pass with all nodes present. Each check names the nodes it needs (requires: all of them;
    requires_any: one of each group); the post-check SKIPs a check whose nodes are not in the selection.
    internet_targets: {address: node} of the internet stand-ins (default: the internet test hosts behind the carriers)."""
    checks = []
    eos = eos_nodes(intent)
    in_lab = set(intent.get("lab_nodes", intent["nodes"]))
    # --- EOS BGP, BFD
    for name in [e for e in eos if e in in_lab]:
        cfg = intent["nodes"][name]["cfg"]
        for n in (cfg["bgp"] or {}).get("neighbors", []):
            peer = owner(intent, n["ip"])
            req = [peer or UNKNOWN]
            label = {"underlay": "underlay BGP", "overlay": "EVPN overlay BGP", "mlag": "MLAG iBGP",
                     "vrf": f"VRF {n['vrf']} BGP"}.get(n["kind"], "BGP")
            base = dict(vrf=n["vrf"], peer=n["ip"], peer_node=peer, af=n["af"], kind=n["kind"])
            checks.append(_check(f"bgp:{name}:{n['vrf']}:{n['ip']}", name, "bgp",
                                 f"{label} {n['ip']} ({peer or 'unknown node'}) Established", req, **base))
            if n["bfd"]:
                checks.append(_check(f"bfd:{name}:{n['vrf']}:{n['ip']}", name, "bfd",
                                     f"BFD {n['ip']} ({peer or 'unknown node'}) up"
                                     + (" multihop" if n["kind"] == "overlay" else ""), req, **base))
        # --- MLAG
        if cfg["mlag"]:
            peer = owner(intent, cfg["mlag"].get("peer-address", ""))
            checks.append(_check(f"mlag:{name}", name, "mlag", f"MLAG {cfg['mlag'].get('domain-id')} active, "
                                 f"peer {peer} connected, config consistent", [peer] if peer else [], peer_node=peer))
        # --- VXLAN
        if cfg["vxlan"]:
            checks.append(_check(f"vxlan:{name}", name, "vxlan-up", "Vxlan1 line protocol up"))
            mine = vtep_address(cfg)
            my_vnis = set(cfg["vxlan"]["vlans"].values())
            by_vtep: dict = {}
            for other in eos:
                ocfg = intent["nodes"][other]["cfg"]
                if other == name or not ocfg["vxlan"]:
                    continue
                theirs = vtep_address(ocfg)
                if theirs and theirs != mine and my_vnis & set(ocfg["vxlan"]["vlans"].values()):
                    by_vtep.setdefault(theirs, []).append(other)
            for theirs, holders in sorted(by_vtep.items()):
                who = "/".join(holders)
                checks.append(_check(f"vtep:{name}:{theirs}", name, "vtep", f"remote VTEP {theirs} ({who}) learned",
                                     [], [holders], vtep=theirs, peer_node=who))
                checks.append(_check(f"flood:{name}:{theirs}", name, "flood", f"VTEP {theirs} ({who}) in the flood list",
                                     [], [holders], vtep=theirs, peer_node=who))
        # --- routes per VRF: 0/0 and the VRF's own summaries
        tenant_vrfs = [v for v in cfg["vrfs"] if v not in INFRA_VRFS]
        overlay_peers = overlay_peer_nodes(intent, name)
        for v in tenant_vrfs:
            req, anyg = default_chain(intent, v)
            if req:
                checks.append(_check(f"route:{name}:{v}:{DEFAULT_ROUTE}", name, "route", f"VRF {v} has {DEFAULT_ROUTE}",
                                     req, anyg + fabric_groups(intent, name, req), vrf=v, prefix=DEFAULT_ROUTE))
            vb = (cfg["bgp"] or {}).get("vrfs", {}).get(v, {})
            for p in vb.get("aggregates", []) + vb.get("networks", []):
                net = ipaddress.ip_network(p)
                comps = sorted({e for e in eos for i in intent["nodes"][e]["cfg"]["interfaces"].values()
                                if i["vrf"] == v and (i["virtual_ip"] or i["ip"])
                                and ipaddress.ip_interface(i["virtual_ip"] or i["ip"]).network.subnet_of(net)
                                and ipaddress.ip_interface(i["virtual_ip"] or i["ip"]).network != net})
                static = any(s["vrf"] == v and s["prefix"] == p for s in cfg["statics"])
                groups = [] if static or not comps or name in comps else [comps]
                if groups and overlay_peers:
                    groups.append(overlay_peers)
                checks.append(_check(f"route:{name}:{v}:{p}", name, "route", f"VRF {v} has summary {p}",
                                     [], groups, vrf=v, prefix=p))
    # --- TRANSIT routes learned through EVPN: cloud and branch summaries on the nodes that do not peer with
    #     their stand-ins (the service leaves)
    for prefix, sources, origins in transit_learned(intent):
        for name in [e for e in eos if e in in_lab]:
            cfg = intent["nodes"][name]["cfg"]
            if TRANSIT_VRF not in cfg["vrfs"]:
                continue
            local = {owner(intent, n["ip"]) for n in (cfg["bgp"] or {}).get("neighbors", []) if n["vrf"] == TRANSIT_VRF}
            if local & set(sources):
                continue
            groups = [sources] + ([origins] if origins else []) + fabric_groups(intent, name, set(sources))[:-1]
            if overlay_peer_nodes(intent, name):
                groups.append(overlay_peer_nodes(intent, name))
            checks.append(_check(f"route:{name}:{TRANSIT_VRF}:{prefix}", name, "route",
                                 f"VRF {TRANSIT_VRF} has {prefix} (via EVPN)", [], groups,
                                 vrf=TRANSIT_VRF, prefix=prefix))
    # --- FRR stand-in sessions (and BFD where configured)
    for name in [f for f in frr_nodes(intent) if f in in_lab]:
        cfg = intent["nodes"][name]["cfg"]
        for inst in cfg["bgp"]:
            for ip, n in inst["neighbors"].items():
                peer = owner(intent, ip)
                req = [peer or UNKNOWN]
                base = dict(vrf=inst["vrf"], peer=ip, peer_node=peer, af="ipv4", kind="frr")
                checks.append(_check(f"bgp:{name}:{inst['vrf']}:{ip}", name, "bgp",
                                     f"BGP {inst['vrf']} {ip} ({peer or 'unknown node'}) Established", req, **base))
                if n["bfd"]:
                    checks.append(_check(f"bfd:{name}:{inst['vrf']}:{ip}", name, "bfd",
                                         f"BFD {inst['vrf']} {ip} ({peer or 'unknown node'}) up", req, **base))
    # --- test hosts: gateway and the internet stand-ins
    targets = internet_targets if internet_targets is not None else internet_hosts(intent)
    for h in [x for x in host_nodes(intent) if x in in_lab]:
        hc = intent["nodes"][h]["cfg"]
        if h in targets.values():
            continue
        gw = hc["gateway"]
        leaf = next((p for _li, p, _pi in link_peers(intent, h)), None)
        checks.append(_check(f"ping:{h}:{gw}", h, "ping", f"ping gateway {gw} ({leaf})", [leaf] if leaf else [],
                             target=gw))
        vrf = host_vrf(intent, h)
        if vrf is None:
            continue
        req, anyg = default_chain(intent, vrf)
        for addr, tnode in sorted(targets.items()):
            path = carrier_path(intent, tnode)
            groups = anyg + (fabric_groups(intent, leaf, req) if leaf else [])
            checks.append(_check(f"ping:{h}:{addr}", h, "ping", f"ping internet stand-in {addr} ({tnode})",
                                 set(req) | {tnode, leaf} | set(path), groups, target=addr))
    return checks


def transit_learned(intent: dict) -> list[tuple[str, list[str], list[str]]]:
    """(prefix, stand-ins that send it, FRR nodes that originate it or a component) for every non-default prefix an EOS
    TRANSIT inbound route-map permits from a stand-in that peers only in TRANSIT (cloud routers, SD-WAN FW; not the
    Internal FW, which also peers in the tenant VRFs, nor the Internet FW, which sends 0/0 only)."""
    eos = eos_nodes(intent)
    tenant_peers = {owner(intent, n["ip"]) for e in eos for n in (intent["nodes"][e]["cfg"]["bgp"] or {}).get("neighbors", [])
                    if n["kind"] == "vrf" and n["vrf"] != TRANSIT_VRF}
    by_prefix: dict = {}
    for e in eos:
        cfg = intent["nodes"][e]["cfg"]
        for n in (cfg["bgp"] or {}).get("neighbors", []):
            src = owner(intent, n["ip"])
            if n["vrf"] != TRANSIT_VRF or not src or src in tenant_peers:
                continue
            for pfx in route_map_prefixes(cfg, n["route_map_in"]):
                if pfx != DEFAULT_ROUTE:
                    by_prefix.setdefault(pfx, set()).add(src)
    out = []
    for pfx, srcs in sorted(by_prefix.items()):
        net = ipaddress.ip_network(pfx)
        origins = sorted({f for f in frr_nodes(intent) for inst in intent["nodes"][f]["cfg"]["bgp"]
                          for x in inst["networks"] if ipaddress.ip_network(x).subnet_of(net)})
        out.append((pfx, sorted(srcs), origins))
    return out


def overlay_peer_nodes(intent: dict, node: str) -> list[str]:
    cfg = intent["nodes"][node]["cfg"]
    if intent["nodes"][node]["kind"] != "eos" or not cfg or not cfg["bgp"]:
        return []
    return sorted({owner(intent, n["ip"]) for n in cfg["bgp"]["neighbors"] if n["kind"] == "overlay" and owner(intent, n["ip"])})


def fabric_groups(intent: dict, node: str, sources: set[str]) -> list[list[str]]:
    """Fabric nodes a route from the stand-ins needs to reach this EOS node: for each source the node does not peer
    with itself, one of the EOS nodes that do; and then one of the node's overlay peers (spines)."""
    eos = eos_nodes(intent)
    cfg = intent["nodes"][node]["cfg"] if intent["nodes"][node]["kind"] == "eos" else None
    local = {owner(intent, n["ip"]) for n in (cfg["bgp"] or {}).get("neighbors", [])} if cfg and cfg["bgp"] else set()
    groups = []
    for s in sorted(sources):
        if s in local:
            continue
        peers = sorted({e for e in eos for n in (intent["nodes"][e]["cfg"]["bgp"] or {}).get("neighbors", [])
                        if owner(intent, n["ip"]) == s})
        if peers and peers not in groups:
            groups.append(peers)
    if groups and overlay_peer_nodes(intent, node):
        groups.append(overlay_peer_nodes(intent, node))
    return groups


def internet_hosts(intent: dict) -> dict:
    """Internet test hosts: hosts whose gateway is a carrier stand-in (an FRR node that is not a fabric stand-in)."""
    out = {}
    for h in host_nodes(intent):
        hc = intent["nodes"][h]["cfg"]
        o = owner(intent, hc["gateway"] or "")
        if o and intent["nodes"][o]["kind"] == "frr" and not any(
                owner(intent, n["ip"]) == o for e in eos_nodes(intent)
                for n in (intent["nodes"][e]["cfg"]["bgp"] or {}).get("neighbors", [])):
            if hc["addresses"]:
                out[hc["addresses"][0]["ip"]] = h
    return out


def carrier_path(intent: dict, target_host: str) -> list[str]:
    """Internet host -> its carrier stand-in -> the ISP router(s) that carrier peers with."""
    hc = intent["nodes"][target_host]["cfg"]
    carrier = owner(intent, hc["gateway"] or "")
    if not carrier:
        return []
    return [carrier] + sorted(p for p in frr_peers(intent, carrier) if p != target_host)


def host_vrf(intent: dict, host: str) -> str | None:
    """VRF of the anycast gateway SVI that serves a test host's gateway address."""
    gw = intent["nodes"][host]["cfg"]["gateway"]
    for e in eos_nodes(intent):
        for i in intent["nodes"][e]["cfg"]["interfaces"].values():
            if i["virtual_ip"] and i["virtual_ip"].split("/")[0] == gw:
                return i["vrf"]
    return None


def selected_ok(check: dict, selected: set[str]) -> tuple[bool, str]:
    """Can this check be judged with the selected nodes? (False, reason) when a needed node is absent."""
    if UNKNOWN in check["requires"]:
        return False, "peer address not found in the rendered files"
    missing = [n for n in check["requires"] if n not in selected]
    if missing:
        return False, "not in the lab: " + ", ".join(missing)
    for g in check["requires_any"]:
        if not any(n in selected for n in g):
            return False, "none in the lab: " + ", ".join(g)
    return True, ""
