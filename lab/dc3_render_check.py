#!/usr/bin/env python3
"""DC3 render cross-check (READ ONLY): static consistency checks over the rendered EOS startup-configs
(DC3-*.cfg) and FRR stand-in scripts (*.sh), against the containerlab topology, the cEOS interface
mapping files and the ASN table in plans/dc3-values.md.

python3 stdlib only. Nothing is written except to stdout (and --json FILE if given).

Usage:
  python3 lab/dc3_render_check.py                       # tests/expected, lab/dc3.clab.yml
  python3 lab/dc3_render_check.py --configs lab/configs # another render set
  python3 lab/dc3_render_check.py --json out.json       # also write the findings as JSON

Exit: 0 no blocker/major, 1 at least one major, 2 at least one blocker.

Checks (ids used in the findings):
  LINK  both ends of every topology link: descriptions, /31 complementary, MTU, BFD, VRF, channel-group;
        FRR end references the port; EOS descriptions that name a device must match a topology link
  BGP   every neighbor resolves to a device that owns the IP, has the reverse neighbor and the right remote-as
        (ASN table), peer groups defined and activated in the right AF, EVPN overlay full mesh leaf<->spine,
        MLAG iBGP both sides, VRF neighbors to FRR stand-ins (IP, ASN, local-as, VRF, VLAN), FRR<->FRR sessions
  MLAG  domain-id, peer-address / heartbeat cross-match, peer-link, keepalive VRF, timer values,
        Vlan3900 /31, trunk group, Loopback1 anycast equal / Loopback2 distinct, mlag port-channels
  VXLAN VLAN->VNI, VNI->RT, L3 VNI per VRF, RD format and uniqueness, anycast MAC,
        ip address virtual per VLAN, MAC-VRF / VRF blocks present
  REF   route-maps / prefix-lists / peer groups / BFD profiles referenced exist; defined but unused
  MISC  ip routing, vrf instance, BFD timers, duplicate IPs, invalid CIDRs, static next hops,
        values (user, AAA, STP, GR helper), values left out (ES/link tracking), topology mgmt-ipv4
"""
import argparse
import ipaddress
import json
import os
import re
import sys
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEV_ORDER = {"blocker": 0, "major": 1, "minor": 2, "": 3, "info": 4}

FINDINGS = []
STATS = defaultdict(int)


def add(sev, check, file, line, msg, fix=""):
    FINDINGS.append({"severity": sev, "check": check, "file": file, "line": line, "message": msg, "fix": fix})


# ----------------------------------------------------------------------------------------------- EOS parser
class Eos:
    def __init__(self, path):
        self.path = path
        self.file = os.path.basename(path)
        self.name = self.file[:-4]
        self.lines = open(path).read().split("\n")
        self.intfs = {}
        self.vlans = {}
        self.no_stp_vlans = set()
        self.vrfs = {}
        self.ip_routing = None
        self.ip_routing_vrfs = {}
        self.no_ip_routing_vrfs = {}
        self.prefix_lists = {}
        self.route_maps = {}
        self.vr_macs = []
        self.mlag = {}
        self.mlag_line = None
        self.vxlan = {"vlans": {}, "vrfs": {}, "src": None, "udp": None, "vrmac": None}
        self.bgp = None
        self.bfd_multihop = None
        self.statics = []
        self.stp_lines = []
        self.username = False
        self.toplevel = {}
        self.parse()
        self.parse_vxlan()

    def parse_vxlan(self):
        vx = self.intfs.get("Vxlan1")
        if vx is None:
            return
        for ln in range(vx["line"] + 1, len(self.lines) + 1):
            t = self.lines[ln - 1]
            if not t.startswith("   "):
                break
            t = t.strip()
            m = re.match(r"vxlan vlan (\d+) vni (\d+)$", t)
            if m:
                self.vxlan["vlans"][int(m.group(1))] = (int(m.group(2)), ln)
            m = re.match(r"vxlan vrf (\S+) vni (\d+)$", t)
            if m:
                self.vxlan["vrfs"][m.group(1)] = (int(m.group(2)), ln)
            if t.startswith("vxlan source-interface"):
                self.vxlan["src"] = (t.split()[2], ln)
            if t.startswith("vxlan udp-port"):
                self.vxlan["udp"] = (t.split()[2], ln)
            if t.startswith("vxlan virtual-router encapsulation"):
                self.vxlan["vrmac"] = (t, ln)

    def parse(self):
        ctx = []  # list of (indent, text, lineno)
        for i, raw in enumerate(self.lines, 1):
            if not raw.strip() or raw.strip() == "!":
                continue
            ind = len(raw) - len(raw.lstrip(" "))
            txt = raw.strip()
            while ctx and ctx[-1][0] >= ind:
                ctx.pop()
            parents = [c[1] for c in ctx]
            ctx.append((ind, txt, i))
            if ind == 0:
                self.top(txt, i)
            else:
                self.sub(parents, txt, i, ind)

    # top-level lines
    def top(self, t, i):
        self.toplevel.setdefault(t.split()[0], []).append(i)
        m = re.match(r"interface (\S+)$", t)
        if m:
            self.intfs[m.group(1)] = {"line": i, "name": m.group(1)}
            return
        m = re.match(r"vlan (\d+)$", t)
        if m:
            self.vlans[int(m.group(1))] = {"line": i}
            return
        m = re.match(r"no spanning-tree vlan-id (\S+)$", t)
        if m:
            for v in expand_vlans(m.group(1)):
                self.no_stp_vlans.add(v)
            return
        if t.startswith("spanning-tree"):
            self.stp_lines.append(i)
        if t.startswith("username"):
            self.username = True
        m = re.match(r"vrf instance (\S+)$", t)
        if m:
            self.vrfs[m.group(1)] = i
            return
        if t == "ip routing":
            self.ip_routing = i
            return
        m = re.match(r"ip routing vrf (\S+)$", t)
        if m:
            self.ip_routing_vrfs[m.group(1)] = i
            return
        m = re.match(r"no ip routing vrf (\S+)$", t)
        if m:
            self.no_ip_routing_vrfs[m.group(1)] = i
            return
        m = re.match(r"ip prefix-list (\S+)$", t)
        if m:
            self.prefix_lists[m.group(1)] = {"line": i, "entries": []}
            return
        m = re.match(r"route-map (\S+) (permit|deny) (\d+)$", t)
        if m:
            self.route_maps.setdefault(m.group(1), {"line": i, "matches": []})
            self._rm = m.group(1)
            return
        m = re.match(r"ip virtual-router mac-address (\S+)$", t)
        if m:
            self.vr_macs.append((m.group(1), i))
            return
        if t in ("mlag configuration", "mlag"):
            self.mlag_line = i
            return
        m = re.match(r"router bgp (\d+)$", t)
        if m:
            self.bgp = {"asn": int(m.group(1)), "line": i, "rid": None, "pgs": {}, "nbrs": {}, "redist": [],
                        "af": {"evpn": {}, "ipv4": {}}, "vlans": {}, "vrfs": {}, "maxpaths": None,
                        "no_default_v4": False}
            return
        m = re.match(r"ip route vrf (\S+) (\S+) (\S+)$", t)
        if m:
            self.statics.append((m.group(1), m.group(2), m.group(3), i))
            return
        m = re.match(r"ip route (\S+) (\S+)$", t)
        if m:
            self.statics.append(("default", m.group(1), m.group(2), i))

    def sub(self, parents, t, i, ind):
        p0 = parents[0]
        m = re.match(r"interface (\S+)$", p0)
        if m:
            itf = self.intfs[m.group(1)]
            self.intf_line(itf, t, i)
            return
        m = re.match(r"vlan (\d+)$", p0)
        if m:
            v = self.vlans[int(m.group(1))]
            if t.startswith("name "):
                v["name"] = t[5:]
            mm = re.match(r"trunk group (\S+)$", t)
            if mm:
                v["tgroup"] = mm.group(1)
            return
        m = re.match(r"ip prefix-list (\S+)$", p0)
        if m:
            self.prefix_lists[m.group(1)]["entries"].append((t, i))
            return
        m = re.match(r"route-map (\S+) ", p0)
        if m:
            mm = re.match(r"match ip address prefix-list (\S+)$", t)
            if mm:
                self.route_maps[m.group(1)]["matches"].append((mm.group(1), i))
            return
        if p0 in ("mlag configuration", "mlag"):
            parts = t.split()
            key = " ".join(parts[:-1]) if parts[0] not in ("dual-primary", "peer-address") else t
            if parts[0] == "peer-address" and parts[1] == "heartbeat":
                self.mlag["heartbeat"] = (parts[2], parts[4] if len(parts) > 4 else None, i)
            elif parts[0] == "peer-address":
                self.mlag["peer-address"] = (parts[1], i)
            elif parts[0] == "dual-primary":
                self.mlag[" ".join(parts[:2])] = (t, i)
            else:
                self.mlag[key] = (parts[-1], i)
            return
        if p0 == "router bfd":
            if t.startswith("multihop interval"):
                self.bfd_multihop = (t, i)
            return
        if p0.startswith("router bgp"):
            self.bgp_line(parents, t, i)

    def intf_line(self, itf, t, i):
        if t.startswith("description "):
            itf["desc"] = (t[12:], i)
        elif t == "no switchport":
            itf["routed"] = i
        elif t.startswith("mtu "):
            itf["mtu"] = (int(t.split()[1]), i)
        elif t.startswith("vrf "):
            itf["vrf"] = (t.split()[1], i)
        elif t.startswith("ip address virtual "):
            itf["vip"] = (t.split()[3], i)
        elif t.startswith("ip address "):
            itf["ip"] = (t.split()[2], i)
        elif t.startswith("bfd interval"):
            itf["bfd"] = (t, i)
        elif t.startswith("channel-group "):
            p = t.split()
            itf["cg"] = (int(p[1]), p[3] if len(p) > 3 else None, i)
        elif t.startswith("mlag "):
            itf["mlag"] = (int(t.split()[1]), i)
        elif t.startswith("switchport trunk allowed vlan "):
            itf["allowed"] = (t.split()[4], i)
        elif t.startswith("switchport trunk group "):
            itf["tgroup"] = (t.split()[3], i)
        elif t.startswith("switchport mode "):
            itf["mode"] = (t.split()[2], i)
        elif t.startswith("switchport access vlan "):
            itf["access"] = (int(t.split()[3]), i)
        elif t == "no autostate":
            itf["noautostate"] = i
        elif t == "shutdown":
            itf["shutdown"] = i
        elif t.startswith("evpn ethernet-segment"):
            itf["es"] = i
        elif t.startswith("link tracking group"):
            itf["lt"] = (t, i)

    def bgp_line(self, parents, t, i):
        b = self.bgp
        depth = len(parents)
        # vrf block
        vrf = None
        af = None
        for p in parents[1:]:
            m = re.match(r"vrf (\S+)$", p)
            if m:
                vrf = m.group(1)
            m = re.match(r"address-family (\S+)$", p)
            if m:
                af = m.group(1)
            m = re.match(r"vlan (\d+)$", p)
            if m:
                vb = b["vlans"][int(m.group(1))]
                if t.startswith("rd "):
                    vb["rd"] = (t.split()[1], i)
                elif t.startswith("route-target both "):
                    vb.setdefault("rt", []).append((t.split()[2], i))
                elif t.startswith("route-target"):
                    vb.setdefault("rt_other", []).append((t, i))
                elif t == "redistribute learned":
                    vb["learned"] = i
                return
        if depth == 1:
            m = re.match(r"vlan (\d+)$", t)
            if m:
                b["vlans"][int(m.group(1))] = {"line": i}
                return
            m = re.match(r"vrf (\S+)$", t)
            if m:
                b["vrfs"][m.group(1)] = {"line": i, "nbrs": {}, "rt_imp": [], "rt_exp": [], "rd": None, "rid": None,
                                         "redist": [], "statements": []}
                return
            if t.startswith("address-family"):
                return
            if t.startswith("router-id "):
                b["rid"] = (t.split()[1], i)
                return
            if t == "no bgp default ipv4-unicast":
                b["no_default_v4"] = i
                return
            if t.startswith("maximum-paths"):
                b["maxpaths"] = (t, i)
                return
            m = re.match(r"redistribute connected(?: route-map (\S+))?$", t)
            if m:
                b["redist"].append((m.group(1), i))
                return
            m = re.match(r"neighbor (\S+) peer group$", t)
            if m:
                b["pgs"].setdefault(m.group(1), {"line": i, "attrs": {}})
                return
            m = re.match(r"neighbor (\S+) peer group (\S+)$", t)
            if m:
                b["nbrs"].setdefault(m.group(1), {"line": i})["pg"] = (m.group(2), i)
                return
            m = re.match(r"neighbor (\S+) (.+)$", t)
            if m:
                who, rest = m.group(1), m.group(2)
                target = b["pgs"].get(who)
                if target is None:
                    target = b["nbrs"].setdefault(who, {"line": i})
                    target.setdefault("attrs", {})
                key = rest.split()[0]
                target["attrs"][key] = (rest, i)
                return
            return
        if vrf and af is None:
            vb = b["vrfs"][vrf]
            if t.startswith("rd "):
                vb["rd"] = (t.split()[1], i)
            elif t.startswith("route-target import evpn "):
                vb["rt_imp"].append((t.split()[3], i))
            elif t.startswith("route-target export evpn "):
                vb["rt_exp"].append((t.split()[3], i))
            elif t.startswith("router-id "):
                vb["rid"] = (t.split()[1], i)
            elif t.startswith("redistribute connected"):
                p = t.split()
                vb["redist"].append((p[3] if len(p) > 3 else None, i))
            else:
                m = re.match(r"neighbor (\S+) (\S+)(?: (.*))?$", t)
                if m:
                    n = vb["nbrs"].setdefault(m.group(1), {"line": i, "attrs": {}})
                    n["attrs"][m.group(2)] = (m.group(3), i)
                else:
                    vb["statements"].append((t, i))
            return
        if vrf and af:
            vb = b["vrfs"][vrf]
            m = re.match(r"neighbor (\S+) activate$", t)
            if m:
                vb["nbrs"].setdefault(m.group(1), {"line": i, "attrs": {}})["activated"] = i
                return
            m = re.match(r"neighbor (\S+) route-map (\S+) (in|out)$", t)
            if m:
                vb["nbrs"].setdefault(m.group(1), {"line": i, "attrs": {}})["rm_" + m.group(3)] = (m.group(2), i)
            return
        if af and not vrf:
            m = re.match(r"(no )?neighbor (\S+) activate$", t)
            if m:
                b["af"].setdefault(af, {})[m.group(2)] = (not m.group(1), i)
            return

    # helpers
    def asn(self):
        return self.bgp["asn"] if self.bgp else None

    def lo(self, n):
        itf = self.intfs.get("Loopback%d" % n)
        if itf and "ip" in itf:
            return itf["ip"][0].split("/")[0]
        return None

    def nbr_remote_as(self, ip):
        n = self.bgp["nbrs"].get(ip, {})
        if "remote-as" in n.get("attrs", {}):
            return int(n["attrs"]["remote-as"][0].split()[1]), n["attrs"]["remote-as"][1]
        pg = n.get("pg")
        if pg and pg[0] in self.bgp["pgs"] and "remote-as" in self.bgp["pgs"][pg[0]]["attrs"]:
            a = self.bgp["pgs"][pg[0]]["attrs"]["remote-as"]
            return int(a[0].split()[1]), a[1]
        return None, n.get("line")


def expand_vlans(s):
    out = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


# ----------------------------------------------------------------------------------------------- FRR parser
class Frr:
    """One FRR node (a whole script, or the hostname section of a shared script)."""

    def __init__(self, node, path, start=1, end=None):
        self.name = node
        self.path = path
        self.file = os.path.basename(path)
        all_lines = open(path).read().split("\n")
        self.all_lines = all_lines
        self.start, self.end = start, end or len(all_lines)
        self.addrs = {}       # ifname -> [(cidr, line)]
        self.vlan_of = {}     # subif -> (parent, vlan id, line)
        self.master = {}      # ifname -> (master, line)
        self.refs = {}        # physical-looking ifnames referenced -> line
        self.bgp = []         # list of {asn, vrf, line, nbrs{ip:{remote_as, local_as, line, rm_in, rm_out, bfd, pg}}, pgs, listen}
        self.prefix_lists = {}
        self.route_maps = {}
        self.aspath = {}
        self.bfd_profiles = {}
        self.statics = []
        self.bad_cidr = []
        self.parse()

    def parse(self):
        in_frr = False
        cur = None
        cur_rm = None
        cur_prof = None
        cur_vrf_static = "default"
        for i in range(self.start, self.end + 1):
            raw = self.all_lines[i - 1]
            t = raw.strip()
            if t.startswith("cat > /etc/frr/frr.conf"):
                in_frr = True
                continue
            if t == "FRREOF":
                in_frr = False
                continue
            if not in_frr:
                for m in re.finditer(r"ip addr (?:replace|add) (\S+) dev (\S+?)(?:;|$|\s)", t):
                    cidr, dev = m.group(1), m.group(2)
                    self.addrs.setdefault(dev, []).append((cidr, i))
                    if cidr.count("/") != 1:
                        self.bad_cidr.append((cidr, i))
                    else:
                        try:
                            ipaddress.ip_interface(cidr)
                        except ValueError:
                            self.bad_cidr.append((cidr, i))
                m = re.search(r"ip link add link (\S+) name (\S+) type vlan id (\d+)", t)
                if m:
                    self.vlan_of[m.group(2)] = (m.group(1), int(m.group(3)), i)
                for m in re.finditer(r"ip link set (\S+) master (\S+)", t):
                    self.master[m.group(1)] = (m.group(2), i)
                for m in re.finditer(r"/sys/class/net/(\S+) \]", t):
                    self.refs.setdefault(m.group(1), i)
                for m in re.finditer(r"ip link set (\S+) (?:mtu|up|down)", t):
                    self.refs.setdefault(m.group(1), i)
                m = re.search(r"ip route replace default via (\S+)", t)
                if m:
                    self.statics.append(("default", "0.0.0.0/0", m.group(1), i))
                continue
            # FRR config
            if not t or t == "!":
                continue
            ind = len(raw) - len(raw.lstrip(" "))
            if ind == 0:
                cur_rm = None
                if not t.startswith(("exit", "ip route")):
                    cur_prof = None
                m = re.match(r"router bgp (\d+)(?: vrf (\S+))?$", t)
                if m:
                    cur = {"asn": int(m.group(1)), "vrf": m.group(2) or "default", "line": i, "nbrs": {}, "pgs": {},
                           "listen": [], "aggregates": [], "networks": []}
                    self.bgp.append(cur)
                    continue
                if t == "exit":
                    cur = None
                    cur_vrf_static = "default"
                    continue
                m = re.match(r"vrf (\S+)$", t)
                if m:
                    cur_vrf_static = m.group(1)
                    continue
                if t == "exit-vrf":
                    cur_vrf_static = "default"
                    continue
                m = re.match(r"ip route (\S+) (\S+)$", t)
                if m:
                    self.statics.append((cur_vrf_static, m.group(1), m.group(2), i))
                    continue
                m = re.match(r"ip prefix-list (\S+) seq \d+ (permit|deny) (.+)$", t)
                if m:
                    self.prefix_lists.setdefault(m.group(1), {"line": i, "entries": []})["entries"].append((m.group(3), i))
                    continue
                m = re.match(r"route-map (\S+) (permit|deny) (\d+)$", t)
                if m:
                    cur_rm = m.group(1)
                    self.route_maps.setdefault(cur_rm, {"line": i, "matches": [], "aspath": []})
                    continue
                m = re.match(r"bgp as-path access-list (\S+) ", t)
                if m:
                    self.aspath[m.group(1)] = i
                    continue
                continue
            # indented
            m = re.match(r"ip route (\S+) (\S+)$", t)
            if m and cur_vrf_static != "default" and cur is None:
                self.statics.append((cur_vrf_static, m.group(1), m.group(2), i))
                continue
            if cur_rm:
                m = re.match(r"match ip address prefix-list (\S+)$", t)
                if m:
                    self.route_maps[cur_rm]["matches"].append((m.group(1), i))
                m = re.match(r"match as-path (\S+)$", t)
                if m:
                    self.route_maps[cur_rm]["aspath"].append((m.group(1), i))
                continue
            m = re.match(r"profile (\S+)$", t)
            if m:
                cur_prof = m.group(1)
                self.bfd_profiles[cur_prof] = {"line": i}
                continue
            if cur_prof and cur is None:
                m = re.match(r"(transmit-interval|receive-interval|detect-multiplier) (\d+)$", t)
                if m:
                    self.bfd_profiles[cur_prof][m.group(1)] = (int(m.group(2)), i)
                continue
            if cur is not None:
                m = re.match(r"neighbor (\S+) peer-group$", t)
                if m:
                    cur["pgs"][m.group(1)] = {"line": i}
                    continue
                m = re.match(r"bgp listen range (\S+) peer-group (\S+)$", t)
                if m:
                    cur["listen"].append((m.group(1), m.group(2), i))
                    continue
                m = re.match(r"aggregate-address (\S+)", t)
                if m:
                    cur["aggregates"].append((m.group(1), i))
                m = re.match(r"network (\S+)$", t)
                if m:
                    cur["networks"].append((m.group(1), i))
                m = re.match(r"import vrf route-map (\S+)$", t)
                if m:
                    cur.setdefault("rm_refs", []).append((m.group(1), i))
                    continue
                m = re.match(r"neighbor (\S+) (\S+)(?: (.*))?$", t)
                if m:
                    who, key, rest = m.group(1), m.group(2), m.group(3) or ""
                    tgt = cur["pgs"][who] if who in cur["pgs"] else cur["nbrs"].setdefault(who, {"line": i})
                    if key == "remote-as":
                        tgt["remote_as"] = (int(rest), i)
                    elif key == "local-as":
                        tgt["local_as"] = (int(rest.split()[0]), i)
                    elif key == "route-map":
                        rm, d = rest.split()
                        tgt["rm_" + d] = (rm, i)
                    elif key == "bfd":
                        tgt["bfd"] = (rest, i)
                    elif key == "default-originate":
                        if "route-map" in rest:
                            tgt["rm_dorig"] = (rest.split()[-1], i)
                    elif key == "update-source":
                        tgt["usrc"] = (rest, i)

    def vrf_of(self, ifname):
        """Linux VRF device of an interface (vrfN) or 'default'."""
        seen = set()
        cur = ifname
        while cur in self.master and cur not in seen:
            seen.add(cur)
            mst = self.master[cur][0]
            if mst.startswith("vrf"):
                return mst
            cur = mst
        return "default"


# ----------------------------------------------------------------------------------------------- inputs
def read_topology(path):
    txt = open(path).read()
    nodes = {}
    cur = None
    in_nodes = False
    for line in txt.split("\n"):
        if re.match(r"^  nodes:", line):
            in_nodes = True
            continue
        if re.match(r"^  links:", line):
            in_nodes = False
        if in_nodes:
            m = re.match(r"^    (\S+):$", line)
            if m:
                cur = m.group(1)
                nodes[cur] = {"binds": [], "mgmt": None, "kind": None, "startup": None}
                continue
            if cur:
                m = re.match(r"^      kind: (\S+)", line)
                if m:
                    nodes[cur]["kind"] = m.group(1)
                m = re.match(r"^      mgmt-ipv4: (\S+)", line)
                if m:
                    nodes[cur]["mgmt"] = m.group(1)
                m = re.match(r"^      startup-config: (\S+)", line)
                if m:
                    nodes[cur]["startup"] = m.group(1)
                m = re.match(r"^      - (\S+:/\S+)", line)
                if m:
                    nodes[cur]["binds"].append(m.group(1))
    links = re.findall(r"- endpoints:\n\s+- (\S+)\n\s+- (\S+)", txt)
    return nodes, [tuple(a.split(":", 1)) + tuple(b.split(":", 1)) for a, b in links]


def read_plan_asns(path):
    """Expected ASN per DC3 switch from the ASN table of plans/dc3-values.md."""
    exp = {}
    txt = open(path).read()
    for line in txt.split("\n"):
        m = re.match(r"\|\s*(.+?)\s*\|\s*(\d+)(?:\s*\.\.\s*(\d+))?\s*\|$", line)
        if not m:
            continue
        who, a, b = m.group(1), int(m.group(2)), m.group(3)
        if who.startswith("Spines"):
            for d in re.findall(r"S\d+", who):
                exp["DC3-" + d] = a
        elif who.startswith("BL1-BL2"):
            exp["DC3-BL1"] = exp["DC3-BL2"] = a
        elif who.startswith("SL1-SL2"):
            exp["DC3-SL1"] = exp["DC3-SL2"] = a
        elif who.startswith("L1-L2"):
            exp["DC3-L1"] = exp["DC3-L2"] = a
        else:
            mm = re.match(r"L(\d+) \.\. L(\d+)", who)
            if mm and b:
                lo, hi = int(mm.group(1)), int(mm.group(2))
                for k, n in enumerate(range(lo, hi + 1)):
                    exp["DC3-L%d" % n] = a + k
    return exp


# Stand-in ASNs (plans/dc3-values.md "Outside the fabric")
STANDIN_ASN = {"DC3-INTFW1": 65310, "DC3-SDWANFW1": 65400, "DC3-VPNFW1": 65330, "DC3-INETFW1": 65340,
               "DC3-ISP1": 65050, "DC3-ISP2": 65050, "DC3-CLOUD1": 65370, "DC3-CLOUD2": 65370,
               "DC3-ISP-A": 64496, "DC3-ISP-B": 64497, "DC3-AWS": 65500, "DC3-AZURE": 12076,
               "DC3-BR001": 65400, "DC3-BR002": 65400}
STANDIN_LOCAL_AS = {"DC3-SDWANFW1": 65320}

# Decided timer values
EXP_MLAG = {"heartbeat-interval": "4000",
            "dual-primary detection": "dual-primary detection delay 5 action errdisable all-interfaces",
            "dual-primary recovery": "dual-primary recovery delay mlag 360 non-mlag 300",
            "reload-delay mlag": "360", "reload-delay non-mlag": "360"}
EXP_BFD = "bfd interval 999 min-rx 999 multiplier 3"
EXP_BFD_MH = "multihop interval 999 min-rx 999 multiplier 3"
EXP_USER = "username dc3-automation privilege 15 role network-admin secret sha512 __DC3_AUTOMATION_HASH__"
EXP_STP = ("spanning-tree mode mstp", "spanning-tree mst 0 priority 8192")
ANYCAST_MAC = "0200.0000.00aa"
RT_ASN = 65300
MTU = 9214


def load_mapping(mapdir, model_json):
    with open(os.path.join(mapdir, os.path.basename(model_json))) as f:
        d = json.load(f)
    return d.get("EthernetIntf", {})


def frr_nodes_from_topology(nodes, cfgdir):
    """Map each linux node to its script (and hostname section for shared scripts)."""
    out = {}
    for n, v in nodes.items():
        if v["kind"] != "linux":
            continue
        script = None
        for b in v["binds"]:
            src = b.split(":")[0]
            if src.endswith(".sh"):
                script = os.path.join(cfgdir, os.path.basename(src))
        if not script or not os.path.exists(script):
            continue
        lines = open(script).read().split("\n")
        start, end = 1, len(lines)
        # shared script: section "<node>)" ... ";;"
        for i, l in enumerate(lines, 1):
            if l.strip() == n + ")":
                start = i
                for j in range(i, len(lines) + 1):
                    if lines[j - 1].strip() == ";;":
                        end = j
                        break
                break
        out[n] = Frr(n, script, start, end)
    return out


# ----------------------------------------------------------------------------------------------- checks
def cidr_net(c):
    return ipaddress.ip_interface(c).network


def check_links(eos, frr, nodes, links, mapdir):
    maps = {}
    for n, v in nodes.items():
        if v["kind"] == "arista_ceos":
            for b in v["binds"]:
                if "EosIntfMapping" in b:
                    maps[n] = load_mapping(mapdir, b.split(":")[0])

    def eos_name(n, i):
        return maps.get(n, {}).get(i, i)

    seen = set()
    for n1, i1, n2, i2 in links:
        e1 = eos_name(n1, i1) if n1 in eos else i1
        e2 = eos_name(n2, i2) if n2 in eos else i2
        seen.add((n1, e1))
        seen.add((n2, e2))
        ends = [(n1, e1, n2, e2), (n2, e2, n1, e1)]
        for a, ai, b, bi in ends:
            if a in eos:
                d = eos[a]
                itf = d.intfs.get(ai)
                if itf is None:
                    add("minor", "LINK", d.file, 0, f"topology link {a}:{ai} <-> {b}:{bi} has no interface stanza "
                        f"in {a}", "render the port (or drop the link from the topology)")
                    continue
                want = f"{b} {bi}"
                if "desc" not in itf:
                    add("minor", "LINK", d.file, itf["line"], f"{ai} has no description (expected '{want}')",
                        f"description {want}")
                elif itf["desc"][0] != want:
                    first = itf["desc"][0].split()[0]
                    if first in nodes:
                        add("major", "LINK", d.file, itf["desc"][1], f"{ai} description '{itf['desc'][0]}' but the "
                            f"topology connects it to {want}", f"description {want} (or fix the cabling data)")
                    else:
                        add("minor", "LINK", d.file, itf["desc"][1], f"{ai} description '{itf['desc'][0]}' does not "
                            f"follow '<peer device> <peer interface>' (topology peer: {want}); a chosen "
                            f"exception", f"keep the exception or use 'description {want}'")
                STATS["link ends checked"] += 1
            elif a in frr:
                f = frr[a]
                if ai not in f.refs and ai not in f.addrs and ai not in f.master:
                    add("major", "LINK", f.file, f.start, f"{a}: topology port {ai} (to {b} {bi}) is never "
                        f"configured in the script", "configure the port or remove the link")
        # EOS<->EOS consistency
        if n1 in eos and n2 in eos:
            a, b = eos[n1].intfs.get(e1), eos[n2].intfs.get(e2)
            if not a or not b:
                continue
            STATS["EOS<->EOS links checked"] += 1
            fa, fb = eos[n1].file, eos[n2].file
            if ("ip" in a) != ("ip" in b):
                add("blocker", "LINK", fa, a["line"], f"{n1} {e1} / {n2} {e2}: only one end has an IP address",
                    "address both ends")
            if "ip" in a and "ip" in b:
                ia, ib = ipaddress.ip_interface(a["ip"][0]), ipaddress.ip_interface(b["ip"][0])
                if ia.network != ib.network or ia.ip == ib.ip or ia.network.prefixlen != 31:
                    add("blocker", "LINK", fa, a["ip"][1], f"{n1} {e1} {ia} vs {n2} {e2} {ib}: not a complementary "
                        f"/31", "use the two addresses of one /31")
            ma = a.get("mtu", (None,))[0]
            mb = b.get("mtu", (None,))[0]
            if ma != mb:
                add("major", "LINK", fa, a.get("mtu", (0, a["line"]))[1], f"{n1} {e1} MTU {ma} vs {n2} {e2} MTU {mb}",
                    f"mtu {MTU} on both")
            if ("bfd" in a) != ("bfd" in b) or (a.get("bfd", ("",))[0] != b.get("bfd", ("",))[0]):
                add("major", "LINK", fa, a["line"], f"{n1} {e1} / {n2} {e2}: BFD differs "
                    f"('{a.get('bfd', ('none',))[0]}' vs '{b.get('bfd', ('none',))[0]}')", EXP_BFD)
            va = a.get("vrf", ("default",))[0]
            vb = b.get("vrf", ("default",))[0]
            if va != vb:
                add("blocker", "LINK", fa, a["line"], f"{n1} {e1} VRF {va} vs {n2} {e2} VRF {vb}", "same VRF")
            ca, cb = a.get("cg"), b.get("cg")
            if (ca is None) != (cb is None) or (ca and cb and (ca[0] != cb[0] or ca[1] != cb[1])):
                add("major", "LINK", fa, a["line"], f"{n1} {e1} channel-group {ca and ca[:2]} vs {n2} {e2} "
                    f"{cb and cb[:2]}", "same port-channel number and LACP mode on both members")
    # EOS descriptions that name a lab device but have no topology link
    devnames = set(nodes)
    for d in eos.values():
        for name, itf in d.intfs.items():
            if not name.startswith("Ethernet") or "desc" not in itf:
                continue
            first = itf["desc"][0].split()[0]
            if first in devnames and (d.name, name) not in seen:
                add("major", "LINK", d.file, itf["desc"][1], f"{name} describes peer '{itf['desc'][0]}' but the "
                    f"topology has no link on {d.name} {name}", "add the link or fix the description")
    # FRR references to physical-looking ports that are not in the topology
    topo_ports = defaultdict(set)
    for n1, i1, n2, i2 in links:
        topo_ports[n1].add(i1)
        topo_ports[n2].add(i2)
    for f in frr.values():
        created = set(f.vlan_of) | {k for k in f.addrs if k in ("lo",)}
        for ifn, ln in f.refs.items():
            line_txt = f.all_lines[ln - 1]
            if re.search(r"ip link add (?:link \S+ )?(?:name )?%s\b" % re.escape(ifn), " ".join(f.all_lines[f.start - 1:f.end])):
                continue
            if ifn in created or ifn.startswith(("vrf", "agg-", "BE", "lan", "LO-", "ADVPN")):
                continue
            if ifn not in topo_ports[f.name]:
                add("major", "LINK", f.file, ln, f"{f.name}: script uses port {ifn} that the topology does not "
                    f"connect", "fix the port name or add the link")


def build_ip_index(eos, frr):
    idx = defaultdict(list)  # ip -> [(device, ifname, cidr, vrf, file, line, kind)]
    for d in eos.values():
        for name, itf in d.intfs.items():
            if "ip" in itf:
                c = itf["ip"][0]
                idx[c.split("/")[0]].append((d.name, name, c, itf.get("vrf", ("default",))[0], d.file, itf["ip"][1], "eos"))
    for f in frr.values():
        for ifn, lst in f.addrs.items():
            for c, ln in lst:
                if c.count("/") != 1:
                    continue
                idx[c.split("/")[0]].append((f.name, ifn, c, f.vrf_of(ifn), f.file, ln, "frr"))
    return idx


def check_dup_ips(eos, frr, idx):
    pairs = mlag_pairs(eos)
    for ip, owners in idx.items():
        devs = {o[0] for o in owners}
        if len(owners) < 2:
            continue
        if len(devs) == 2 and all(o[1] == "Loopback1" for o in owners):
            a, b = sorted(devs)
            if (a, b) in pairs:
                continue
        if ip.startswith("10.250."):
            continue
        o = owners[1]
        add("blocker", "MISC", o[4], o[5], f"IP {ip} is configured on " + ", ".join(f"{x[0]} {x[1]}" for x in owners),
            "give each end its own address")
    for f in frr.values():
        for c, ln in f.bad_cidr:
            add("blocker", "MISC", f.file, ln, f"{f.name}: invalid address '{c}' (script runs with set -eu: "
                f"`ip addr` fails and the script stops here)", "render the bare address before the prefix length")


def mlag_pairs(eos):
    by_dom = defaultdict(list)
    for d in eos.values():
        if "domain-id" in d.mlag:
            by_dom[d.mlag["domain-id"][0]].append(d.name)
    out = set()
    for v in by_dom.values():
        if len(v) == 2:
            out.add(tuple(sorted(v)))
    return out


def owner_of(idx, ip, vrf=None):
    o = idx.get(ip, [])
    if vrf:
        o = [x for x in o if x[3] == vrf] or o
    return o[0] if o else None


def check_bgp(eos, frr, idx, plan_asn):
    spines = [d for d in eos.values() if d.bgp and plan_asn.get(d.name) == RT_ASN]
    vteps = [d for d in eos.values() if "Vxlan1" in d.intfs]
    spine_lo = {d.lo(0): d.name for d in spines}
    vtep_lo = {d.lo(0): d.name for d in vteps}
    lo0_owner = {d.lo(0): d for d in eos.values() if d.lo(0)}
    for d in eos.values():
        if not d.bgp:
            continue
        b = d.bgp
        exp = plan_asn.get(d.name)
        if exp is not None and exp != b["asn"]:
            add("blocker", "BGP", d.file, b["line"], f"{d.name} router bgp {b['asn']}, ASN table says {exp}",
                f"router bgp {exp}")
        if not b["no_default_v4"]:
            add("major", "BGP", d.file, b["line"], "no `no bgp default ipv4-unicast`", "add it")
        if not b["rid"] or b["rid"][0] != d.lo(0):
            add("major", "BGP", d.file, b["line"], f"router-id {b['rid']} is not Loopback0 {d.lo(0)}",
                f"router-id {d.lo(0)}")
        for rm, ln in b["redist"]:
            if rm and rm not in d.route_maps:
                add("blocker", "REF", d.file, ln, f"route-map {rm} not defined", "define it")
        # peer groups
        used_pgs = set()
        for ip, n in b["nbrs"].items():
            pg = n.get("pg")
            if pg:
                used_pgs.add(pg[0])
                if pg[0] not in b["pgs"]:
                    add("blocker", "REF", d.file, pg[1], f"peer group {pg[0]} not defined", "define it")
        for pg, v in b["pgs"].items():
            if pg not in used_pgs:
                add("minor", "REF", d.file, v["line"], f"peer group {pg} has no members", "remove it")
            ev = b["af"].get("evpn", {}).get(pg)
            v4 = b["af"].get("ipv4", {}).get(pg)
            if pg.startswith("EVPN"):
                if not ev or not ev[0]:
                    add("blocker", "BGP", d.file, v["line"], f"{pg} not activated in address-family evpn", "activate")
                if v4 and v4[0]:
                    add("major", "BGP", d.file, v4[1], f"{pg} activated in IPv4", "no neighbor ... activate")
                for need in ("update-source", "ebgp-multihop", "send-community", "bfd"):
                    if need not in v["attrs"]:
                        add("major", "BGP", d.file, v["line"], f"{pg} lacks {need}", f"neighbor {pg} {need} ...")
                if "update-source" in v["attrs"] and v["attrs"]["update-source"][0] != "update-source Loopback0":
                    add("blocker", "BGP", d.file, v["attrs"]["update-source"][1], "EVPN update-source not Loopback0", "Loopback0")
                if d in spines and "next-hop-unchanged" not in v["attrs"]:
                    add("blocker", "BGP", d.file, v["line"], "spine EVPN peer group without next-hop-unchanged", "add it")
            else:
                if not v4 or not v4[0]:
                    add("blocker", "BGP", d.file, v["line"], f"{pg} not activated in address-family ipv4", "activate")
                if ev and ev[0]:
                    add("major", "BGP", d.file, ev[1], f"{pg} activated in EVPN", "remove")
                if pg.startswith("IPV4-UNDERLAY") and "bfd" not in v["attrs"]:
                    add("major", "BGP", d.file, v["line"], f"{pg} without bfd", f"neighbor {pg} bfd")
                if pg.startswith("MLAG") and "next-hop-self" not in v["attrs"]:
                    add("major", "BGP", d.file, v["line"], f"{pg}: no next-hop-self. Routes the peer learned from the "
                        f"spines keep the spine /31 next hop, which this switch cannot resolve (only loopbacks are "
                        f"redistributed): the iBGP backup path over Vlan3900 is unusable when this switch loses its "
                        f"uplinks. Not decided yet",
                        f"decision needed: neighbor {pg} next-hop-self (AVD form)")
        # each neighbor
        for ip, n in b["nbrs"].items():
            ras, rln = d.nbr_remote_as(ip)
            pg = n.get("pg", (None,))[0]
            own = owner_of(idx, ip, "default")
            peer = None
            if ip in lo0_owner:
                peer = lo0_owner[ip]
                my_ip = d.lo(0)
            elif own and own[6] == "eos":
                peer = eos[own[0]]
                mine = [itf["ip"][0] for itf in d.intfs.values() if "ip" in itf and
                        cidr_net(itf["ip"][0]) == cidr_net(own[2])]
                my_ip = mine[0].split("/")[0] if mine else None
            STATS["EOS default-VRF neighbor statements checked"] += 1
            if peer is None:
                add("blocker", "BGP", d.file, n["line"], f"neighbor {ip}: no device in the render set owns this IP",
                    "fix the neighbor address")
                continue
            if ras is None:
                add("blocker", "BGP", d.file, n["line"], f"neighbor {ip} ({peer.name}) has no remote-as", "set it")
            elif peer.bgp and ras != peer.bgp["asn"]:
                add("blocker", "BGP", d.file, rln, f"neighbor {ip} remote-as {ras}, {peer.name} runs AS {peer.bgp['asn']}",
                    f"remote-as {peer.bgp['asn']}")
            if not peer.bgp or my_ip not in peer.bgp["nbrs"]:
                add("blocker", "BGP", d.file, n["line"], f"neighbor {ip} ({peer.name}): {peer.name} has no neighbor "
                    f"{my_ip}", f"add neighbor {my_ip} on {peer.name}")
            else:
                rpg = peer.bgp["nbrs"][my_ip].get("pg", (None,))[0]
                kind = lambda p: (p or "").split("-")[0]
                if kind(rpg) != kind(pg):
                    add("major", "BGP", d.file, n["line"], f"neighbor {ip} in {pg}, the reverse on {peer.name} is in {rpg}",
                        "same session type on both ends")
            if pg and pg.startswith("MLAG"):
                if (min(d.name, peer.name), max(d.name, peer.name)) not in mlag_pairs(eos):
                    add("blocker", "BGP", d.file, n["line"], f"MLAG iBGP neighbor {ip} is not the MLAG peer", "fix")
                if not ip.startswith(d.intfs.get("Vlan3900", {}).get("ip", ("x",))[0].rsplit(".", 1)[0]):
                    add("major", "BGP", d.file, n["line"], f"MLAG iBGP neighbor {ip} not on Vlan3900", "use Vlan3900 /31")
        # overlay completeness
        evpn_n = {ip for ip, n in b["nbrs"].items() if n.get("pg", ("",))[0].startswith("EVPN")}
        if d in vteps:
            if evpn_n != set(spine_lo):
                add("blocker", "BGP", d.file, b["line"], f"EVPN neighbors {sorted(evpn_n)} != spine Loopback0 "
                    f"{sorted(spine_lo)}", "peer with both spines")
        if d in spines:
            missing = set(vtep_lo) - evpn_n
            extra = evpn_n - set(vtep_lo)
            if missing or extra or len(evpn_n) != 24:
                add("blocker", "BGP", d.file, b["line"], f"spine EVPN neighbors: {len(evpn_n)}, missing "
                    f"{sorted(vtep_lo[x] for x in missing)}, extra {sorted(extra)}", "one per VTEP leaf (24)")
            if not b["maxpaths"]:
                add("minor", "BGP", d.file, b["line"], "spine has no maximum-paths: BGP installs 1 path (EOS default "
                    ") although every leaf has 2 links per spine and MLAG pairs advertise the same anycast VTEP from "
                    "2 switches. The ECMP value covers leaves only", "decision needed: maximum-paths <n> ecmp <m> on spines")
        # VRF neighbors
        for vrf, vb in b["vrfs"].items():
            for ip, n in vb["nbrs"].items():
                a = n["attrs"]
                ras = int(a["remote-as"][0]) if "remote-as" in a else None
                if ras is None:
                    add("blocker", "BGP", d.file, n["line"], f"VRF {vrf} neighbor {ip} without remote-as", "set it")
                if "activated" not in n:
                    add("blocker", "BGP", d.file, n["line"], f"VRF {vrf} neighbor {ip} not activated in ipv4 "
                        "(no bgp default ipv4-unicast)", "activate")
                if "bfd" not in a:
                    add("major", "BGP", d.file, n["line"], f"VRF {vrf} neighbor {ip} without bfd", "neighbor bfd")
                for k in ("rm_in", "rm_out"):
                    if k in n and n[k][0] not in d.route_maps:
                        add("blocker", "REF", d.file, n[k][1], f"route-map {n[k][0]} not defined", "define it")
                own = owner_of(idx, ip)
                if not own or own[6] != "frr":
                    add("blocker", "BGP", d.file, n["line"], f"VRF {vrf} neighbor {ip}: no stand-in owns this IP", "fix")
                    continue
                f = frr[own[0]]
                STATS["EOS VRF neighbor statements to stand-ins checked"] += 1
                svi = [(nm, itf) for nm, itf in d.intfs.items() if "ip" in itf and cidr_net(itf["ip"][0]) == cidr_net(own[2])]
                if not svi:
                    add("blocker", "BGP", d.file, n["line"], f"VRF {vrf} neighbor {ip}: no local interface in {own[2]}", "fix")
                    continue
                sname, sitf = svi[0]
                my_ip = sitf["ip"][0].split("/")[0]
                if sitf.get("vrf", ("default",))[0] != vrf:
                    add("blocker", "BGP", d.file, sitf["line"], f"{sname} is in VRF {sitf.get('vrf')} but the session is "
                        f"in VRF {vrf}", "same VRF")
                if "bfd" not in sitf:
                    add("major", "BGP", d.file, sitf["line"], f"{sname} has no bfd interval for the stand-in session", EXP_BFD)
                # FRR side
                fv = f.vrf_of(own[1])
                sess = [s for s in f.bgp if s["vrf"] == fv and my_ip in s["nbrs"]]
                if not sess:
                    add("blocker", "BGP", f.file, own[5], f"{f.name}: no BGP neighbor {my_ip} ({d.name}) in {fv}",
                        f"neighbor {my_ip} remote-as {b['asn']}")
                    continue
                s = sess[0]
                fn = s["nbrs"][my_ip]
                if fn.get("remote_as", (None,))[0] != b["asn"]:
                    add("blocker", "BGP", f.file, fn["line"], f"{f.name} neighbor {my_ip} remote-as "
                        f"{fn.get('remote_as', (None,))[0]}, {d.name} is AS {b['asn']}", f"remote-as {b['asn']}")
                f_as = fn.get("local_as", (s["asn"],))[0]
                if ras != f_as:
                    add("blocker", "BGP", d.file, a["remote-as"][1], f"VRF {vrf} neighbor {ip} remote-as {ras}, "
                        f"{f.name} presents AS {f_as}", f"remote-as {f_as}")
                exp_f = STANDIN_LOCAL_AS.get(f.name, STANDIN_ASN.get(f.name))
                if exp_f and f_as != exp_f:
                    add("blocker", "BGP", f.file, fn["line"], f"{f.name} presents AS {f_as}, plan says {exp_f}", "fix")
                if "bfd" not in fn:
                    add("major", "BGP", f.file, fn["line"], f"{f.name} neighbor {my_ip} without bfd", "bfd profile")
                vid = int(sname[4:]) if sname.startswith("Vlan") else None
                fvl = f.vlan_of.get(own[1])
                if vid and (not fvl or fvl[1] != vid):
                    add("blocker", "BGP", f.file, own[5], f"{f.name} {own[1]} VLAN {fvl and fvl[1]} vs {d.name} {sname}",
                        "same VLAN")
    # FRR side sessions
    for f in frr.values():
        for s in f.bgp:
            exp = STANDIN_ASN.get(f.name)
            if exp and s["asn"] != exp:
                add("blocker", "BGP", f.file, s["line"], f"{f.name} router bgp {s['asn']}, plan says {exp}", "fix")
            for ip, n in s["nbrs"].items():
                own = owner_of(idx, ip)
                if not own:
                    add("blocker", "BGP", f.file, n["line"], f"{f.name} neighbor {ip}: nobody owns this IP", "fix")
                    continue
                if own[0] == f.name:
                    add("blocker", "BGP", f.file, n["line"], f"{f.name} neighbor {ip} is its own address", "fix")
                    continue
                if own[6] == "eos":
                    d = eos[own[0]]
                    found = any(ip2 in vb["nbrs"] for vb in d.bgp["vrfs"].values()
                                for ip2 in [x for x in f_ips(f)]) if d.bgp else False
                    if not found:
                        add("blocker", "BGP", f.file, n["line"], f"{f.name} neighbor {ip} ({d.name}): no VRF neighbor back", "fix")
                    continue
                g = frr[own[0]]
                STATS["FRR<->FRR neighbor statements checked"] += 1
                mine = [c for ifn, lst in f.addrs.items() for c, _ in lst if c.count("/") == 1 and
                        cidr_net(c) == cidr_net(own[2])]
                my_ip = mine[0].split("/")[0] if mine else None
                back = [(t, t["nbrs"][my_ip]) for t in g.bgp if my_ip in t["nbrs"]]
                if not back:
                    lr = [(t, pg) for t in g.bgp for (rng, pg, _) in t["listen"]
                          if my_ip and ipaddress.ip_address(my_ip) in ipaddress.ip_network(rng)]
                    if lr:
                        t, pg = lr[0]
                        back = [(t, t["pgs"][pg])]
                if not back:
                    add("blocker", "BGP", f.file, n["line"], f"{f.name} neighbor {ip} ({g.name}): {g.name} has no "
                        f"neighbor {my_ip}", "add it")
                    continue
                t, bn = back[0]
                if n.get("remote_as", (None,))[0] != t["asn"]:
                    add("blocker", "BGP", f.file, n["line"], f"{f.name} neighbor {ip} remote-as "
                        f"{n.get('remote_as', (None,))[0]}, {g.name} is AS {t['asn']}", f"remote-as {t['asn']}")
                if bn.get("remote_as", (None,))[0] != s["asn"]:
                    add("blocker", "BGP", g.file, bn["line"], f"{g.name} -> {f.name}: remote-as "
                        f"{bn.get('remote_as', (None,))[0]}, {f.name} is AS {s['asn']}", f"remote-as {s['asn']}")
                if ("bfd" in n) != ("bfd" in bn):
                    add("minor", "BGP", f.file, n["line"], f"{f.name} <-> {g.name}: BFD on one end only", "both or none")


def f_ips(f):
    return [c.split("/")[0] for lst in f.addrs.values() for c, _ in lst if c.count("/") == 1]


def check_mlag(eos):
    by_dom = defaultdict(list)
    for d in eos.values():
        if d.mlag_line:
            if "domain-id" not in d.mlag:
                add("blocker", "MLAG", d.file, d.mlag_line, "mlag without domain-id", "domain-id")
                continue
            by_dom[d.mlag["domain-id"][0]].append(d)
    for dom, ds in by_dom.items():
        if len(ds) != 2:
            add("blocker", "MLAG", ds[0].file, ds[0].mlag["domain-id"][1], f"domain-id {dom} on {len(ds)} switches", "two")
            continue
        STATS["MLAG pairs checked"] += 1
        for a, b in (ds, ds[::-1]):
            m = a.mlag
            exp_dom = "DC3-MLAG-%s-%s" % tuple(sorted([a.name[4:], b.name[4:]], key=natkey))
            if dom != exp_dom:
                add("major", "MLAG", a.file, m["domain-id"][1], f"domain-id {dom} pattern gives {exp_dom}", exp_dom)
            v_a, v_b = a.intfs.get("Vlan3900"), b.intfs.get("Vlan3900")
            if not v_a or "ip" not in v_a:
                add("blocker", "MLAG", a.file, a.mlag_line, "no Vlan3900 address", "add")
                continue
            ia, ib = ipaddress.ip_interface(v_a["ip"][0]), ipaddress.ip_interface(v_b["ip"][0])
            if ia.network != ib.network or ia.ip == ib.ip or ia.network.prefixlen != 31:
                add("blocker", "MLAG", a.file, v_a["ip"][1], f"Vlan3900 {ia} vs {b.name} {ib}", "complementary /31")
            if "noautostate" not in v_a:
                add("major", "MLAG", a.file, v_a["line"], "Vlan3900 without no autostate", "no autostate")
            if v_a.get("mtu", (None,))[0] != v_b.get("mtu", (None,))[0]:
                add("major", "MLAG", a.file, v_a["line"], "Vlan3900 MTU differs between peers", "same")
            if m.get("local-interface", (None,))[0] != "Vlan3900":
                add("blocker", "MLAG", a.file, a.mlag_line, "local-interface is not Vlan3900", "local-interface Vlan3900")
            pa = m.get("peer-address")
            if not pa or pa[0] != str(ib.ip):
                add("blocker", "MLAG", a.file, pa[1] if pa else a.mlag_line, f"peer-address {pa and pa[0]}, "
                    f"{b.name} Vlan3900 is {ib.ip}", f"peer-address {ib.ip}")
            hb = m.get("heartbeat")
            ka_b = [(n, i) for n, i in b.intfs.items() if i.get("vrf", ("",))[0] == "VPC-KEEPALIVE" and "ip" in i]
            ka_a = [(n, i) for n, i in a.intfs.items() if i.get("vrf", ("",))[0] == "VPC-KEEPALIVE" and "ip" in i]
            if not hb or not ka_b or hb[0] != ka_b[0][1]["ip"][0].split("/")[0] or hb[1] != "VPC-KEEPALIVE":
                add("blocker", "MLAG", a.file, hb[2] if hb else a.mlag_line, f"peer-address heartbeat {hb and hb[:2]}, "
                    f"{b.name} keepalive {ka_b and ka_b[0][1]['ip'][0]}", "heartbeat <peer keepalive ip> vrf VPC-KEEPALIVE")
            if "VPC-KEEPALIVE" not in a.vrfs:
                add("blocker", "MLAG", a.file, a.mlag_line, "vrf instance VPC-KEEPALIVE missing", "add")
            pl = m.get("peer-link", (None, a.mlag_line))
            pc = a.intfs.get(pl[0] or "")
            if not pc:
                add("blocker", "MLAG", a.file, pl[1], f"peer-link {pl[0]} not defined", "define Port-Channel1000")
            else:
                if pl[0] != "Port-Channel1000":
                    add("major", "MLAG", a.file, pl[1], f"peer-link {pl[0]} (Port-Channel1000)", "Port-Channel1000")
                if pc.get("tgroup", (None,))[0] != a.vlans.get(3900, {}).get("tgroup"):
                    add("blocker", "MLAG", a.file, pc["line"], "peer-link trunk group differs from VLAN 3900 trunk group", "MLAGPEER")
                if pc.get("mode", (None,))[0] != "trunk":
                    add("blocker", "MLAG", a.file, pc["line"], "peer-link not a trunk", "switchport mode trunk")
                if "allowed" in pc:
                    allowed = set(expand_vlans(pc["allowed"][0]))
                    miss = set(a.vlans) - allowed - {3900}
                    if miss:
                        add("major", "MLAG", a.file, pc["allowed"][1], f"peer-link does not allow VLANs {sorted(miss)}", "allow all")
                members = [n for n, i in a.intfs.items() if i.get("cg", (None,))[0] == int(pl[0][12:])]
                bad = [n for n in members if a.intfs[n].get("cg")[1] != "active"]
                if not members:
                    add("blocker", "MLAG", a.file, pc["line"], "peer-link has no members", "channel-group")
                if bad:
                    add("major", "MLAG", a.file, a.intfs[bad[0]]["line"], "peer-link member not LACP active", "mode active")
            if 3900 not in a.no_stp_vlans:
                add("major", "MLAG", a.file, a.vlans.get(3900, {}).get("line", 0), "no `no spanning-tree vlan-id 3900`", "add")
            for k, v in EXP_MLAG.items():
                got = m.get(k)
                if got is None or got[0] != v:
                    add("major", "MLAG", a.file, got[1] if got else a.mlag_line, f"{k}: '{got and got[0]}' expected '{v}'", v)
            # loopbacks
            if "Vxlan1" in a.intfs:
                if a.lo(1) != b.lo(1):
                    add("blocker", "MLAG", a.file, a.intfs["Loopback1"]["line"], f"Loopback1 {a.lo(1)} vs {b.name} "
                        f"{b.lo(1)}: MLAG VTEP anycast must be equal", "same anycast")
                if not a.lo(2) or a.lo(2) == b.lo(2) or a.lo(2) == a.lo(1):
                    add("blocker", "MLAG", a.file, a.intfs.get("Loopback2", {"line": 0})["line"], "Loopback2 missing or "
                        "not distinct ", "own address")
                if a.vxlan.get("vrmac") is None:
                    add("major", "MLAG", a.file, a.intfs["Vxlan1"]["line"], "no vxlan virtual-router encapsulation "
                        "mac-address mlag-system-id", "add")
            # mlag port-channels mirrored
            for n, i in a.intfs.items():
                if "mlag" in i:
                    j = b.intfs.get(n)
                    if not j or j.get("mlag", (None,))[0] != i["mlag"][0]:
                        add("blocker", "MLAG", a.file, i["mlag"][1], f"{n} mlag {i['mlag'][0]} has no twin on {b.name}", "mirror it")
                    elif j.get("allowed", (None,))[0] != i.get("allowed", (None,))[0]:
                        add("major", "MLAG", a.file, i["line"], f"{n} allowed VLANs differ from {b.name}", "same list")
                    if i["mlag"][0] != int(n[12:]):
                        add("minor", "MLAG", a.file, i["mlag"][1], f"mlag id {i['mlag'][0]} != port-channel number", "same")


def natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def check_vxlan(eos):
    vlan_vni = defaultdict(set)
    vni_vlan = defaultdict(set)
    vrf_vni = defaultdict(set)
    vip_by_vlan = defaultdict(set)
    vrf_by_vlan = defaultdict(set)
    rds = defaultdict(list)
    for d in eos.values():
        vx = d.intfs.get("Vxlan1")
        if vx is not None:
            if not d.vxlan["src"] or d.vxlan["src"][0] != "Loopback1":
                add("blocker", "VXLAN", d.file, vx["line"], "vxlan source-interface is not Loopback1", "Loopback1")
            if not d.vxlan["udp"] or d.vxlan["udp"][0] != "4789":
                add("minor", "VXLAN", d.file, vx["line"], "vxlan udp-port 4789 missing", "add")
        has_vip = False
        for name, itf in d.intfs.items():
            if "vip" in itf:
                has_vip = True
                vid = int(name[4:])
                vip_by_vlan[vid].add((itf["vip"][0], d.name, itf["vip"][1]))
                vrf_by_vlan[vid].add(itf.get("vrf", ("default",))[0])
                if vid not in d.vxlan["vlans"]:
                    add("blocker", "VXLAN", d.file, itf["line"], f"{name} has an anycast gateway but VLAN {vid} has no VNI", "vxlan vlan .. vni ..")
                if vid not in d.vlans:
                    add("blocker", "VXLAN", d.file, itf["line"], f"{name}: vlan {vid} not defined", "vlan")
                if itf.get("mtu", (None,))[0] != MTU:
                    add("minor", "VXLAN", d.file, itf["line"], f"{name} mtu {itf.get('mtu')} ({MTU})", f"mtu {MTU}")
            if "vrf" in itf and itf["vrf"][0] not in d.vrfs:
                add("blocker", "MISC", d.file, itf["vrf"][1], f"{name} in VRF {itf['vrf'][0]} without vrf instance", "vrf instance")
        if has_vip:
            macs = [m for m, _ in d.vr_macs]
            if ANYCAST_MAC not in macs:
                add("blocker", "VXLAN", d.file, 0, f"ip address virtual without ip virtual-router mac-address {ANYCAST_MAC}", "add")
        for vid, (vni, ln) in d.vxlan["vlans"].items():
            STATS["VLAN->VNI mappings checked"] += 1
            vlan_vni[vid].add(vni)
            vni_vlan[vni].add(vid)
            if vid not in d.vlans:
                add("blocker", "VXLAN", d.file, ln, f"vxlan vlan {vid}: vlan not defined", "vlan")
            mv = d.bgp["vlans"].get(vid) if d.bgp else None
            if not mv:
                add("blocker", "VXLAN", d.file, ln, f"VLAN {vid} VNI {vni}: no MAC-VRF (router bgp / vlan {vid})", "add")
                continue
            rt = [r for r, _ in mv.get("rt", [])]
            if rt != ["%d:%d" % (RT_ASN, vni)]:
                add("blocker", "VXLAN", d.file, mv["line"], f"VLAN {vid} route-target {rt}, expected {RT_ASN}:{vni}", "fix")
            if not mv.get("rd") or mv["rd"][0] != "%s:%d" % (d.lo(0), vni):
                add("major", "VXLAN", d.file, mv["line"], f"VLAN {vid} rd {mv.get('rd')} ( {d.lo(0)}:{vni})", "fix")
            else:
                rds[mv["rd"][0]].append((d.name, d.file, mv["rd"][1]))
            if "learned" not in mv:
                add("major", "VXLAN", d.file, mv["line"], f"VLAN {vid} without redistribute learned", "add")
        if d.bgp:
            for vid, mv in d.bgp["vlans"].items():
                if vid not in d.vxlan["vlans"]:
                    add("major", "VXLAN", d.file, mv["line"], f"MAC-VRF vlan {vid} has no VNI", "remove or map")
        for vrf, (vni, ln) in d.vxlan["vrfs"].items():
            vrf_vni[vrf].add(vni)
            if vrf not in d.vrfs:
                add("blocker", "VXLAN", d.file, ln, f"vxlan vrf {vrf}: no vrf instance", "add")
            if vrf not in d.ip_routing_vrfs:
                add("blocker", "VXLAN", d.file, ln, f"vxlan vrf {vrf}: no ip routing vrf {vrf}", "add")
            vb = d.bgp["vrfs"].get(vrf) if d.bgp else None
            if not vb:
                add("blocker", "VXLAN", d.file, ln, f"vxlan vrf {vrf}: no router bgp vrf block", "add")
                continue
            rt = "%d:%d" % (RT_ASN, vni)
            if [r for r, _ in vb["rt_imp"]] != [rt] or [r for r, _ in vb["rt_exp"]] != [rt]:
                add("blocker", "VXLAN", d.file, vb["line"], f"VRF {vrf} RTs import {vb['rt_imp']} export {vb['rt_exp']}, expected {rt}", "fix")
            if not vb["rd"] or vb["rd"][0] != "%s:%d" % (d.lo(0), vni):
                add("major", "VXLAN", d.file, vb["line"], f"VRF {vrf} rd {vb['rd']}", "fix")
            else:
                rds[vb["rd"][0]].append((d.name, d.file, vb["rd"][1]))
            if not vb["rid"] or vb["rid"][0] != d.lo(0):
                add("minor", "VXLAN", d.file, vb["line"], f"VRF {vrf} router-id not Loopback0", "fix")
            if not any(rm is None for rm, _ in vb["redist"]):
                add("major", "VXLAN", d.file, vb["line"], f"VRF {vrf} without redistribute connected", "add")
        if d.bgp:
            for vrf, vb in d.bgp["vrfs"].items():
                if vrf not in d.vxlan["vrfs"]:
                    add("blocker", "VXLAN", d.file, vb["line"], f"BGP VRF {vrf} has no L3 VNI", "vxlan vrf")
    for vid, s in vlan_vni.items():
        if len(s) > 1:
            add("blocker", "VXLAN", "*", 0, f"VLAN {vid} maps to several VNIs {sorted(s)}", "one VNI")
    for vni, s in vni_vlan.items():
        if len(s) > 1:
            add("blocker", "VXLAN", "*", 0, f"VNI {vni} used by VLANs {sorted(s)}", "one VLAN")
    for vrf, s in vrf_vni.items():
        if len(s) > 1:
            add("blocker", "VXLAN", "*", 0, f"VRF {vrf} has L3 VNIs {sorted(s)}", "one")
    l2 = set(vni_vlan)
    for vrf, s in vrf_vni.items():
        if s & l2:
            add("blocker", "VXLAN", "*", 0, f"L3 VNI {s & l2} of {vrf} also used as L2 VNI", "distinct")
    for vid, s in vip_by_vlan.items():
        gws = {g for g, _, _ in s}
        if len(gws) > 1:
            cnt = defaultdict(int)
            for g, _, _ in s:
                cnt[g] += 1
            odd = min(cnt, key=cnt.get)
            x = sorted(e for e in s if e[0] == odd)[0]
            add("blocker", "VXLAN", x[1] + ".cfg", x[2], f"VLAN {vid} anycast gateway differs: {sorted(gws)}", "same")
        if len(vrf_by_vlan[vid]) > 1:
            add("blocker", "VXLAN", "*", 0, f"VLAN {vid} in different VRFs {vrf_by_vlan[vid]}", "same")
    for rd, lst in rds.items():
        if len(lst) > 1:
            add("blocker", "VXLAN", lst[1][1], lst[1][2], f"RD {rd} used more than once: {[x[0] for x in lst]}", "unique")


def check_refs_and_misc(eos, frr, plan_asn):
    for d in eos.values():
        used_pl = set()
        used_rm = set()
        for rm, v in d.route_maps.items():
            for pl, ln in v["matches"]:
                used_pl.add(pl)
                if pl not in d.prefix_lists:
                    add("blocker", "REF", d.file, ln, f"prefix-list {pl} not defined", "define it")
        if d.bgp:
            for rm, _ in d.bgp["redist"]:
                used_rm.add(rm)
            for vb in d.bgp["vrfs"].values():
                for n in vb["nbrs"].values():
                    for k in ("rm_in", "rm_out"):
                        if k in n:
                            used_rm.add(n[k][0])
        for pl, v in d.prefix_lists.items():
            if pl not in used_pl:
                add("minor", "REF", d.file, v["line"], f"prefix-list {pl} defined but not used", "remove it (or reference it)")
        for rm, v in d.route_maps.items():
            if rm not in used_rm:
                add("minor", "REF", d.file, v["line"], f"route-map {rm} defined but not used", "remove it")
        # loopbacks covered by PL-LOOPBACKS
        if d.bgp and "PL-LOOPBACKS" in d.prefix_lists:
            nets = []
            for t, _ in d.prefix_lists["PL-LOOPBACKS"]["entries"]:
                m = re.match(r"seq \d+ permit (\S+) eq 32", t)
                if m:
                    nets.append(ipaddress.ip_network(m.group(1)))
            for k in (0, 1, 2):
                ip = d.lo(k)
                if ip and not any(ipaddress.ip_address(ip) in n for n in nets):
                    add("blocker", "BGP", d.file, d.intfs["Loopback%d" % k]["line"], f"Loopback{k} {ip} not matched by "
                        f"PL-LOOPBACKS: never advertised", "extend PL-LOOPBACKS")
        # ip routing / vrfs
        if d.bgp and not d.ip_routing:
            add("blocker", "MISC", d.file, d.bgp["line"], "router bgp without ip routing", "ip routing")
        if d.bgp:
            for vrf, vb in d.bgp["vrfs"].items():
                if vrf not in d.vrfs:
                    add("blocker", "MISC", d.file, vb["line"], f"BGP VRF {vrf} without vrf instance", "add")
                if vrf not in d.ip_routing_vrfs:
                    add("blocker", "MISC", d.file, vb["line"], f"BGP VRF {vrf} without ip routing vrf", "add")
        for vrf, ln in d.vrfs.items():
            if vrf not in ("MGMT", "VPC-KEEPALIVE") and vrf not in d.ip_routing_vrfs:
                add("major", "MISC", d.file, ln, f"vrf instance {vrf} without ip routing vrf", "add")
        if "MGMT" not in d.vrfs:
            add("blocker", "MISC", d.file, 0, "no vrf instance MGMT", "add")
        mg = d.intfs.get("Management1")
        if not mg or mg.get("vrf", ("",))[0] != "MGMT":
            add("blocker", "MISC", d.file, mg["line"] if mg else 0, "Management1 missing or not in VRF MGMT", "fix")
        # BFD values
        for name, itf in d.intfs.items():
            if "bfd" in itf and itf["bfd"][0] != EXP_BFD:
                add("major", "MISC", d.file, itf["bfd"][1], f"{name}: '{itf['bfd'][0]}'", EXP_BFD)
            if "routed" in itf and itf.get("mtu", (None,))[0] != MTU:
                add("major", "MISC", d.file, itf["line"], f"{name} routed without mtu {MTU}", f"mtu {MTU}")
            if "cg" in itf and ("Port-Channel%d" % itf["cg"][0]) not in d.intfs:
                add("blocker", "REF", d.file, itf["cg"][2], f"{name} channel-group {itf['cg'][0]}: port-channel not defined", "add")
            for k in ("allowed", "access"):
                if k in itf:
                    vl = expand_vlans(str(itf[k][0]))
                    miss = [v for v in vl if v not in d.vlans]
                    if miss:
                        add("blocker", "REF", d.file, itf[k][1], f"{name} uses undefined VLANs {miss}", "define them")
        if d.bgp:
            if d.bfd_multihop is None or d.bfd_multihop[0] != EXP_BFD_MH:
                add("major", "MISC", d.file, d.bfd_multihop[1] if d.bfd_multihop else 0, "router bfd multihop timers", EXP_BFD_MH)
        # values
        top = {t for t in d.lines if t and not t.startswith(" ")}
        bgp_block = []
        if d.bgp:
            for t in d.lines[d.bgp["line"]:]:
                if not t.startswith("   "):
                    break
                bgp_block.append(t.strip())
        is_spine = bool(d.bgp) and d.bgp["asn"] == 65300
        for line in ("aaa authentication login default local", "aaa authorization exec default local", EXP_USER):
            if line not in top:
                add("major", "MISC", d.file, 0, f"missing `{line}`", line)
        if not is_spine:
            for line in EXP_STP:
                if line not in top:
                    add("major", "MISC", d.file, 0, f"missing `{line}`", line)
        elif d.stp_lines:
            add("major", "MISC", d.file, d.stp_lines[0], "spanning-tree line on a spine (none on the spines)", "remove")
        if d.bgp and "graceful-restart-helper" not in bgp_block:
            add("major", "MISC", d.file, d.bgp["line"], "router bgp without graceful-restart-helper", "graceful-restart-helper")
        if d.bgp and "graceful-restart" in bgp_block:
            add("major", "MISC", d.file, d.bgp["line"], "full graceful-restart configured (helper only)", "remove")
        # ESI leaves
        if d.bgp and d.bgp["asn"] in range(65374, 65382):
            if not any("es" in i for i in d.intfs.values()):
                add("info", "MISC", d.file, d.bgp["line"], "ESI pair member without any evpn ethernet-segment or link "
                    "tracking group: it behaves as a single leaf. Deferred by design", "render ES port-channels + link tracking when server ports exist")
    # FRR references and BFD timers
    for f in frr.values():
        used_pl = set()
        for rm, v in f.route_maps.items():
            for pl, ln in v["matches"]:
                used_pl.add(pl)
                if pl not in f.prefix_lists:
                    add("blocker", "REF", f.file, ln, f"{f.name}: prefix-list {pl} not defined", "define it")
            for ap, ln in v["aspath"]:
                if ap not in f.aspath:
                    add("blocker", "REF", f.file, ln, f"{f.name}: as-path list {ap} not defined", "define it")
        used_rm = set()
        for s in f.bgp:
            for nm, n in list(s["nbrs"].items()) + list(s["pgs"].items()):
                for k in ("rm_in", "rm_out", "rm_dorig"):
                    if k in n:
                        used_rm.add(n[k][0])
                        if n[k][0] not in f.route_maps:
                            add("blocker", "REF", f.file, n[k][1], f"{f.name}: route-map {n[k][0]} not defined", "define it")
                if "bfd" in n:
                    prof = n["bfd"][0].split()[-1] if n["bfd"][0] else None
                    if prof and prof not in f.bfd_profiles:
                        add("blocker", "REF", f.file, n["bfd"][1], f"{f.name}: BFD profile {prof} not defined", "define it")
                if "pg" in n and n["pg"] not in s["pgs"]:
                    add("blocker", "REF", f.file, n["line"], f"{f.name}: peer-group {n['pg']} not defined", "define")
            for rm, ln in s.get("rm_refs", []):
                used_rm.add(rm)
                if rm not in f.route_maps:
                    add("blocker", "REF", f.file, ln, f"{f.name}: route-map {rm} not defined", "define it")
            # eBGP neighbors without policy (FRR 10 default: bgp ebgp-requires-policy)
            txt = "\n".join(f.all_lines[f.start - 1:f.end])
            if "no bgp ebgp-requires-policy" not in txt:
                for ip, n in s["nbrs"].items():
                    if n.get("remote_as", (s["asn"],))[0] != s["asn"] and ("rm_in" not in n or "rm_out" not in n):
                        add("major", "BGP", f.file, n["line"], f"{f.name}: eBGP neighbor {ip} without in/out route-map "
                            f"(FRR ebgp-requires-policy is on by default ): no routes exchanged", "add policy")
        for pl, v in f.prefix_lists.items():
            if pl not in used_pl:
                add("minor", "REF", f.file, v["line"], f"{f.name}: prefix-list {pl} not used", "remove")
        for rm, v in f.route_maps.items():
            if rm not in used_rm:
                add("minor", "REF", f.file, v["line"], f"{f.name}: route-map {rm} not used", "remove")
        for p, v in f.bfd_profiles.items():
            vals = (v.get("transmit-interval", (None,))[0], v.get("receive-interval", (None,))[0],
                    v.get("detect-multiplier", (None,))[0])
            if vals != (999, 999, 3):
                add("", "MISC", f.file, v["line"], f"{f.name}: BFD profile {p} = {vals} says 999 ms x 3 on "
                    f"every BFD session. Chosen for the cloud handoff, not a recorded decision",
                    "confirm the cloud handoff exception to or set 999/999/3")
        # static routes: next hop syntax / own address
        mine = set(f_ips(f))
        for vrf, pfx, nh, ln in f.statics:
            if "/" in nh:
                add("blocker", "MISC", f.file, ln, f"{f.name}: static route {pfx} next hop '{nh}' carries a prefix "
                    f"length (FRR expects a bare address or an interface)", "bare next-hop address")
            nh0 = nh.split("/")[0]
            if nh0 in mine:
                add("blocker", "MISC", f.file, ln, f"{f.name}: static route {pfx} points to its own address {nh0}", "peer address")


def check_topology_mgmt(nodes, eos, frr, topo_file):
    miss = [n for n, v in nodes.items() if (n in eos or n in frr) and not v["mgmt"]]
    if miss:
        add("minor", "MISC", topo_file, 0, f"{len(miss)} cEOS / stand-in nodes have no mgmt-ipv4 in the committed "
            f"topology (written before lab/configs existed); containerlab then assigns addresses itself, which can "
            f"differ from or collide with the rendered Management1 / eth0 addresses", "re-run scripts/dc3_lab_topology.py "
            "after the render loop, and make it stop when a config is missing")


# ----------------------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", default=os.path.join(REPO, "tests", "expected"))
    ap.add_argument("--topology", default=os.path.join(REPO, "lab", "dc3.clab.yml"))
    ap.add_argument("--plan", default=os.path.join(REPO, "plans", "dc3-values.md"))
    ap.add_argument("--mapping-dir", default=os.path.join(REPO, "lab", "intf-mapping"))
    ap.add_argument("--json")
    a = ap.parse_args()

    eos = {}
    for fn in sorted(os.listdir(a.configs)):
        if fn.endswith(".cfg"):
            d = Eos(os.path.join(a.configs, fn))
            eos[d.name] = d
    nodes, links = read_topology(a.topology)
    frr = frr_nodes_from_topology(nodes, a.configs)
    plan_asn = read_plan_asns(a.plan)

    idx = build_ip_index(eos, frr)
    check_links(eos, frr, nodes, links, a.mapping_dir)
    check_dup_ips(eos, frr, idx)
    check_bgp(eos, frr, idx, plan_asn)
    check_mlag(eos)
    check_vxlan(eos)
    check_refs_and_misc(eos, frr, plan_asn)
    check_topology_mgmt(nodes, eos, frr, os.path.relpath(a.topology, REPO))

    # de-duplicate identical messages on mirrored pairs: keep every file:line (they are evidence)
    FINDINGS.sort(key=lambda x: (SEV_ORDER[x["severity"]], x["check"], x["file"], x["line"]))
    counts = defaultdict(int)
    for f in FINDINGS:
        counts[f["severity"]] += 1
    print(f"# dc3_render_check: {len(eos)} EOS configs, {len(frr)} FRR nodes, {len(links)} links")
    print("checked: " + ", ".join(f"{k} {v}" for k, v in sorted(STATS.items())))
    print("counts: " + ", ".join(f"{k} {counts[k]}" for k in SEV_ORDER if counts[k]))
    print()
    print("| # | Severity | Check | File:line | Finding | Proposed fix |")
    print("|---|---|---|---|---|---|")
    for n, f in enumerate(FINDINGS, 1):
        msg = f["message"].replace("|", "\\|")
        fix = f["fix"].replace("|", "\\|")
        print(f"| {n} | {f['severity']} | {f['check']} | {f['file']}:{f['line']} | {msg} | {fix} |")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(FINDINGS, fh, indent=1)
    if counts["blocker"]:
        return 2
    if counts["major"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
