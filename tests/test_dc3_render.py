"""Render tests: every DC3 EOS template rendered with a fake query response, and the lab topology.
The tenant fields (segments, VRFs, attachments, L2 VLANs) are empty here, so these tests pin the fabric-core output;
tests/test_dc3_stage3.py renders the full data written by the generator.

No Infrahub needed. The fake responses are built from generators/dc3_plan.py (devices, ports, links, pairs) in the
shape of queries/dc3_*.gql. TEST DATA ONLY: every address below is fabricated deterministically for the test
(fake_addresses); the real addresses come from the DC3 pools in Infrahub.

The Jinja2 environment mirrors infrahub_sdk 1.23 file-based templates (trim_blocks, lstrip_blocks, only the
SDK's allowed builtin filters) infrahub_sdk/template/__init__.py + filters.py (stable branch, read 2026-10-08).
netutils is not installed here: ipaddress_interface, get_first_usable and sort_interface_list are test shims
(exact netutils behaviour). StrictUndefined makes a missing field fail the test instead of rendering empty.

Run: PYTHONPATH=/usr/lib/python3/dist-packages pytest tests/
"""
import importlib.util
import ipaddress
import re
import sys
import tempfile
import unittest
from pathlib import Path

import jinja2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "generators"))
import dc3_plan as P  # noqa: E402

SITE, VPC, ESI, SINGLE = "DC3", 1, 4, 10

# ---------------------------------------------------------------- SDK-like Jinja2 environment
SDK_BUILTIN_FILTERS = {   # infrahub_sdk/template/filters.py BUILTIN_FILTERS (names only)
    "abs", "attr", "batch", "capitalize", "center", "count", "d", "default", "dictsort", "e", "escape",
    "filesizeformat", "first", "float", "forceescape", "format", "groupby", "indent", "int", "items", "join",
    "last", "length", "list", "lower", "map", "max", "min", "pprint", "random", "reject", "rejectattr", "replace",
    "reverse", "round", "safe", "select", "selectattr", "slice", "sort", "string", "striptags", "sum", "title",
    "tojson", "trim", "truncate", "unique", "upper", "urlencode", "urlize", "wordcount", "wordwrap", "xmlattr",
}


def _natural(name: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def shim_ipaddress_interface(ip: str, attr: str):          # netutils: getattr(ipaddress.ip_interface(ip), attr)
    return getattr(ipaddress.ip_interface(ip), attr)


def shim_get_first_usable(network: str) -> str:            # netutils: first host (network address on /31, /32)
    net = ipaddress.ip_network(network, strict=False)
    return str(net[0] if net.prefixlen >= 31 else net[1])


def shim_sort_interface_list(names) -> list:               # netutils: natural sort of interface names
    return sorted(names, key=_natural)


def environment() -> jinja2.Environment:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), trim_blocks=True,
                             lstrip_blocks=True, undefined=jinja2.StrictUndefined)
    for name in list(env.filters):
        if name not in SDK_BUILTIN_FILTERS:
            del env.filters[name]
    env.filters.update({"ipaddress_interface": shim_ipaddress_interface, "get_first_usable": shim_get_first_usable,
                        "sort_interface_list": shim_sort_interface_list})
    return env


# ---------------------------------------------------------------- TEST DATA: fabricated addresses
def fake_addresses(plan, links, pairs, domains) -> dict:
    """TEST DATA ONLY. Deterministic addresses in the DC3 pool ranges; NOT the Infrahub allocations."""
    a = {"mgmt": {}, "lo0": {}, "lo1": {}, "p2p": {}, "anycast": {}, "keepalive": {}, "backup": {}}
    lo0 = [r["name"] for r in plan if r["role"] not in P.NO_LOOPBACK0_ROLES]
    lo1 = [r["name"] for r in plan if r["role"] in P.VTEP_ROLES]
    for i, r in enumerate(plan):
        a["mgmt"][r["name"]] = f"10.250.32.{11 + i}/24"
    for i, n in enumerate(lo0):
        a["lo0"][n] = f"10.27.0.{1 + i}/32"
    for i, n in enumerate(lo1):
        a["lo1"][n] = f"10.27.1.{1 + i}/32"
    base = int(ipaddress.ip_address("10.27.4.0"))
    for j, l in enumerate(links):
        a["p2p"][(l["spine"], l["spine_port"])] = f"{ipaddress.ip_address(base + 2 * j)}/31"
        a["p2p"][(l["leaf"], l["leaf_port"])] = f"{ipaddress.ip_address(base + 2 * j + 1)}/31"
    k = 0
    for p in pairs:
        if p["domain_id"] is not None and p["nve"]:
            k += 1
            a["anycast"][p["name"]] = f"10.27.2.{k}/32"
    for k, d in enumerate(domains):
        m1, m2 = d["members"]
        a["keepalive"][m1], a["keepalive"][m2] = f"10.27.17.{2 * k}/31", f"10.27.17.{2 * k + 1}/31"
        a["backup"][m1], a["backup"][m2] = f"10.27.3.{2 * k}/31", f"10.27.3.{2 * k + 1}/31"
    return a


def v(x):
    return {"value": x}


def ips(*addrs):
    return {"edges": [{"node": {"address": v(x)}} for x in addrs]}


class Fabric:
    """Fabric-core objects (as dc3_site writes them) with TEST DATA addresses, and query responses built from them."""

    def __init__(self):
        self.plan = P.build_plan(SITE, VPC, ESI, SINGLE)
        self.links = P.build_links(SITE, self.plan)
        self.pairs = P.build_pairs(SITE, self.plan, VPC, ESI)
        self.domains = P.mlag_domains(self.plan, self.pairs)
        self.addr = fake_addresses(self.plan, self.links, self.pairs, self.domains)
        self.rows = {r["name"]: r for r in self.plan}
        self.intf = {r["name"]: [] for r in self.plan}
        self._interfaces()

    def _add(self, device, typename, name, description, role=None, mtu=None, addresses=(), **extra):
        node = {"__typename": typename, "name": v(name), "description": v(description), "role": v(role), "mtu": v(mtu)}
        if typename in ("InterfacePhysical", "InterfaceVirtual"):
            node["ip_addresses"] = ips(*addresses)
        if typename == "InterfacePhysical":
            node["port_channel"] = {"node": extra.get("port_channel")}
            node["access_segment"] = {"node": None}
        if typename == "InterfacePortChannel":
            node["members"] = {"edges": [{"node": {"name": v(m)}} for m in extra.get("members", ())]}
        self.intf[device].append(node)

    def _interfaces(self):
        """As dc3_site writes them (MLAG VTEP members have Loopback1 = anycast, Loopback2 = own address)."""
        mlag_vtep = P.mlag_vtep_members(self.pairs)
        for r in self.plan:
            n = r["name"]
            self._add(n, "InterfacePhysical", P.MGMT_IF, P.MGMT_DESCRIPTION, P.MGMT_ROLE, None, [self.addr["mgmt"][n]])
            for key, ifname, description, _ident in P.device_loopbacks(r, mlag_vtep):
                self._add(n, "InterfaceVirtual", ifname, description,
                          addresses=[self.addr["lo0" if key == "loopback0" else "lo1"][n]])
            if n in mlag_vtep:                        # loopback fallback: Loopback1 without address; anycast on the pair only
                self._add(n, "InterfaceVirtual", P.LOOPBACK1,
                          P.LOOPBACK1_ANYCAST_DESCRIPTION.format(pair=self.pair_of(n)["name"]))
        for l in self.links:
            for dev, port, peer, peer_port in ((l["spine"], l["spine_port"], l["leaf"], l["leaf_port"]),
                                               (l["leaf"], l["leaf_port"], l["spine"], l["spine_port"])):
                self._add(dev, "InterfacePhysical", port, f"{peer} {peer_port}", P.FABRIC_ROLE, P.FABRIC_MTU,
                          [self.addr["p2p"][(dev, port)]])
        for d in self.domains:
            m1, m2 = d["members"]
            for dev, peer in ((m1, m2), (m2, m1)):
                self._add(dev, "InterfacePhysical", d["keepalive"], f"{peer} {d['keepalive']}", P.ROLE_KEEPALIVE,
                          P.MLAG_MTU, [self.addr["keepalive"][dev]])
                self._add(dev, "InterfaceVirtual", P.MLAG_SVI, f"{peer} {P.MLAG_SVI}", P.ROLE_BACKUP, P.MLAG_MTU,
                          [self.addr["backup"][dev]])
                for port in d["peer_link"]:
                    self._add(dev, "InterfacePhysical", port, f"{peer} {port}", P.ROLE_PEER_LINK, P.MLAG_MTU,
                              port_channel={"name": v(P.PEER_LINK)})
                self._add(dev, "InterfacePortChannel", P.PEER_LINK, f"{peer} {P.PEER_LINK}", P.ROLE_PEER_LINK,
                          P.MLAG_MTU, members=d["peer_link"])

    def pair_of(self, name):
        return next((p for p in self.pairs if name in p["members"]), None)

    def switch_pair(self, name):
        p = self.pair_of(name)
        if p is None:
            return {"node": None}
        node = {"__typename": p["kind"], "name": v(p["name"]), "members": {"edges": []}}
        if p["kind"] == "FabricVpcDomain":
            node["domain_id"] = v(p["domain_id"])
            node["attachments"] = {"edges": []}          # tenant fields, empty here (fabric core only; see test_dc3_stage3.py)
            node["l2_vlans"] = {"edges": []}
            anycast = self.addr["anycast"].get(p["name"])
            node["anycast_vtep"] = {"node": {"address": v(anycast)} if anycast else None}
        for m in p["members"]:
            ka = [i for i in self.intf[m] if i["role"]["value"] == P.ROLE_KEEPALIVE]
            node["members"]["edges"].append({"node": {"name": v(m), "interfaces": {"edges": [
                {"node": {"name": i["name"], "ip_addresses": i["ip_addresses"]}} for i in ka]}}})
        return {"node": node}

    def device_node(self, name, with_pair=True, with_asn=True):
        r = self.rows[name]
        node = {"name": v(name), "role": v(r["role"]),
                "interfaces": {"edges": [{"node": i} for i in self.intf[name]]}}
        if with_asn:
            node["location"] = {"node": {"name": v(SITE), "segments": {"edges": []}, "vrfs": {"edges": []}}}  # tenant fields: empty
            node["asn"] = {"node": {"asn": v(r["asn"])} if r["asn"] else None}
        if with_pair:
            node["switch_pair"] = self.switch_pair(name)
        return node

    def as_root(self):
        by_asn = {}
        for r in self.plan:
            if r["asn"] is None:
                continue
            lo = [i for i in self.intf[r["name"]] if i["name"]["value"] == P.LOOPBACK0]
            by_asn.setdefault(r["asn"], []).append({"name": v(r["name"]), "role": v(r["role"]), "interfaces": {
                "edges": [{"node": {"name": i["name"], "ip_addresses": i["ip_addresses"]}} for i in lo]}})
        # an AS of another site in the same response: must be ignored by the templates
        by_asn[65100] = [{"name": v("SITE-A-S1"), "role": v("spine"), "interfaces": {"edges": [
            {"node": {"name": v("loopback0"), "ip_addresses": ips("10.19.0.1/32")}}]}}]
        return {"edges": [{"node": {"asn": v(a), "devices": {"edges": [{"node": d} for d in devs]}}}
                          for a, devs in sorted(by_asn.items())]}

    @staticmethod
    def pools_root():
        pools = {"DC3-LOOPBACK0": "10.27.0.0/24", "DC3-LOOPBACK1-VTEP": "10.27.1.0/24",
                 "DC3-ANYCAST-VTEP": "10.27.2.0/25", "DC3-MGMT0": "10.250.32.0/24", "SITE-A-LOOPBACK0": "10.19.0.0/24"}
        return {"edges": [{"node": {"name": v(n), "resources": {"edges": [{"node": {"prefix": v(p)}}]}}}
                          for n, p in pools.items()]}

    def response(self, name, query):
        """Query response for one device in the shape of queries/<query>.gql."""
        if query == "dc3_l2_pair_config":
            return {"DcimDevice": {"count": 1, "edges": [{"node": self.device_node(name, with_asn=False)}]}}
        node = self.device_node(name, with_pair=(query != "dc3_spine_config"))
        return {"DcimDevice": {"count": 1, "edges": [{"node": node}]},
                "FabricAutonomousSystem": self.as_root(), "CoreIPAddressPool": self.pools_root()}


TEMPLATE_BY_ROLE = {   # role -> (transform/template, query) as declared in .infrahub.yml
    "spine": ("dc3_spine_config", "dc3_spine_config"),
    "leaf": ("dc3_leaf_config", "dc3_leaf_config"),
    "border_leaf": ("dc3_border_service_leaf_config", "dc3_border_service_leaf_config"),
    "service_leaf": ("dc3_border_service_leaf_config", "dc3_border_service_leaf_config"),
    "edge": ("dc3_l2_pair_config", "dc3_l2_pair_config"),
    "dmz": ("dc3_l2_pair_config", "dc3_l2_pair_config"),
}

FAB = Fabric()
ENV = environment()
CONFIGS = {}


def render(name: str) -> str:
    if name not in CONFIGS:
        template, query = TEMPLATE_BY_ROLE[FAB.rows[name]["role"]]
        CONFIGS[name] = ENV.get_template(f"{template}.j2").render(data=FAB.response(name, query))
    return CONFIGS[name]


def block(cfg: str, header: str) -> list[str]:
    """Lines of the top-level block that starts with `header` (indented lines up to the next '!')."""
    lines = cfg.splitlines()
    i = lines.index(header)
    out = []
    for line in lines[i + 1:]:
        if not line.startswith(" "):
            break
        out.append(line.strip())
    return out


def ip(cidr: str) -> str:
    return cidr.split("/")[0]


class RenderTest(unittest.TestCase):
    def test_every_device_renders(self):
        self.assertEqual(len(FAB.plan), 30)
        for r in FAB.plan:
            cfg = render(r["name"])
            self.assertTrue(cfg.startswith(f"hostname {r['name']}\n"), r["name"])
            self.assertTrue(cfg.rstrip().endswith("end"), r["name"])
            self.assertIn("service routing protocols model multi-agent", cfg)
            self.assertNotIn("{#", cfg, "Jinja comments must not reach the config")
            self.assertNotIn("{", cfg)
            self.assertNotIn("None", cfg, r["name"])
            self.assertNotIn("password", cfg)
            self.assertNotRegex(cfg, r"\n\n", "no blank lines")
            self.assertEqual(block(cfg, f"interface {P.MGMT_IF}"),
                             [f"description {P.MGMT_DESCRIPTION}", "vrf MGMT", f"ip address {FAB.addr['mgmt'][r['name']]}"])
            self.assertIn("ip route vrf MGMT 0.0.0.0/0 10.250.32.1", cfg)
            self.assertIn("vrf instance MGMT", cfg)

    def test_bgp_asn_per_device(self):
        for r in FAB.plan:
            cfg = render(r["name"])
            if r["asn"] is None:
                self.assertNotIn("router bgp", cfg, r["name"])
                self.assertNotIn("ip routing\n", cfg)
            else:
                self.assertIn(f"\nrouter bgp {r['asn']}\n", cfg, r["name"])
        self.assertIn("router bgp 65300", render("DC3-S1"))
        self.assertIn("router bgp 65373", render("DC3-L1"))
        self.assertIn("router bgp 65373", render("DC3-L2"))
        self.assertIn("router bgp 65371", render("DC3-BL2"))
        self.assertIn("router bgp 65372", render("DC3-SL1"))
        self.assertIn("router bgp 65374", render("DC3-L3"))
        self.assertIn("router bgp 65391", render("DC3-L20"))

    def test_routed_fabric_links(self):
        for l in FAB.links:
            for dev, port, peer, peer_port in ((l["spine"], l["spine_port"], l["leaf"], l["leaf_port"]),
                                               (l["leaf"], l["leaf_port"], l["spine"], l["spine_port"])):
                self.assertEqual(block(render(dev), f"interface {port}"), [
                    f"description {peer} {peer_port}", "no shutdown", "mtu 9214", "no switchport",
                    f"ip address {FAB.addr['p2p'][(dev, port)]}", "bfd interval 999 min-rx 999 multiplier 3"])

    def test_spine_bgp(self):
        cfg = render("DC3-S1")
        bgp = block(cfg, "router bgp 65300")
        self.assertIn(f"router-id {ip(FAB.addr['lo0']['DC3-S1'])}", bgp)
        self.assertIn("no bgp default ipv4-unicast", bgp)
        for pg in ("IPV4-UNDERLAY-PEERS", "EVPN-OVERLAY-PEERS"):
            self.assertIn(f"neighbor {pg} peer group", bgp)
            self.assertIn(f"neighbor {pg} bfd", bgp)
        self.assertNotIn("neighbor MLAG-IPV4-UNDERLAY-PEER peer group", bgp)
        for line in ("neighbor EVPN-OVERLAY-PEERS next-hop-unchanged", "neighbor EVPN-OVERLAY-PEERS update-source Loopback0",
                     "neighbor EVPN-OVERLAY-PEERS ebgp-multihop 3", "neighbor EVPN-OVERLAY-PEERS send-community",
                     "redistribute connected route-map RM-CONN-2-BGP", "neighbor EVPN-OVERLAY-PEERS activate",
                     "neighbor IPV4-UNDERLAY-PEERS activate"):
            self.assertIn(line, bgp)
        self.assertIn("maximum-paths 4 ecmp 4", bgp)
        # underlay: one neighbor per spine link (44 per spine), remote-as = the leaf's ASN
        links = [l for l in FAB.links if l["spine"] == "DC3-S1"]
        self.assertEqual(len(links), 44)
        for l in links:
            peer_ip = ip(FAB.addr["p2p"][(l["leaf"], l["leaf_port"])])
            self.assertIn(f"neighbor {peer_ip} peer group IPV4-UNDERLAY-PEERS", bgp)
            self.assertIn(f"neighbor {peer_ip} remote-as {FAB.rows[l['leaf']]['asn']}", bgp)
        # overlay: Loopback0 of every leaf with a link (20 server + BL1/2 + SL1/2), never another site or edge/DMZ
        leaves = sorted({l["leaf"] for l in links})
        self.assertEqual(len(leaves), 24)
        for leaf in leaves:
            self.assertIn(f"neighbor {ip(FAB.addr['lo0'][leaf])} peer group EVPN-OVERLAY-PEERS", bgp)
            self.assertIn(f"neighbor {ip(FAB.addr['lo0'][leaf])} remote-as {FAB.rows[leaf]['asn']}", bgp)
        self.assertEqual(sum(1 for x in bgp if x.endswith("peer group EVPN-OVERLAY-PEERS")), 24)
        self.assertNotIn("10.19.0.1", cfg)
        self.assertNotIn("interface Loopback1", cfg)
        self.assertNotIn("Vxlan1", cfg)

    def test_prefix_list_and_route_map(self):
        for name in ("DC3-S1", "DC3-L1", "DC3-L11", "DC3-BL1"):
            cfg = render(name)
            self.assertEqual(block(cfg, "ip prefix-list PL-LOOPBACKS"), [
                "seq 10 permit 10.27.0.0/24 eq 32", "seq 20 permit 10.27.1.0/24 eq 32", "seq 30 permit 10.27.2.0/25 eq 32"])
            self.assertEqual(block(cfg, "route-map RM-CONN-2-BGP permit 10"), ["match ip address prefix-list PL-LOOPBACKS"])
            self.assertEqual(block(cfg, "router bfd"), ["multihop interval 999 min-rx 999 multiplier 3"])

    def test_leaf_bgp(self):
        for name in ("DC3-L1", "DC3-L3", "DC3-L20", "DC3-BL1", "DC3-SL2"):
            r = FAB.rows[name]
            bgp = block(render(name), f"router bgp {r['asn']}")
            self.assertIn(f"router-id {ip(FAB.addr['lo0'][name])}", bgp)
            self.assertIn("maximum-paths 4 ecmp 4", bgp)
            self.assertIn("no bgp default ipv4-unicast", bgp)
            self.assertIn("neighbor IPV4-UNDERLAY-PEERS remote-as 65300", bgp)
            self.assertIn("neighbor EVPN-OVERLAY-PEERS remote-as 65300", bgp)
            self.assertIn("neighbor EVPN-OVERLAY-PEERS ebgp-multihop 3", bgp)
            self.assertIn("neighbor EVPN-OVERLAY-PEERS send-community", bgp)
            self.assertNotIn("neighbor EVPN-OVERLAY-PEERS next-hop-unchanged", bgp)
            for spine in ("DC3-S1", "DC3-S2"):                                         # both spines' Loopback0
                self.assertIn(f"neighbor {ip(FAB.addr['lo0'][spine])} peer group EVPN-OVERLAY-PEERS", bgp)
            for l in (l for l in FAB.links if l["leaf"] == name):
                self.assertIn(f"neighbor {ip(FAB.addr['p2p'][(l['spine'], l['spine_port'])])} peer group IPV4-UNDERLAY-PEERS", bgp)
            uplinks = 4 if r["role"] == "leaf" else 2
            self.assertEqual(sum(1 for x in bgp if x.endswith("peer group IPV4-UNDERLAY-PEERS")), uplinks)

    def test_mlag_ibgp(self):
        bgp = block(render("DC3-L1"), "router bgp 65373")
        peer_svi = ip(FAB.addr["backup"]["DC3-L2"])
        self.assertIn("neighbor MLAG-IPV4-UNDERLAY-PEER peer group", bgp)
        self.assertIn("neighbor MLAG-IPV4-UNDERLAY-PEER remote-as 65373", bgp)
        self.assertIn("neighbor MLAG-IPV4-UNDERLAY-PEER next-hop-self", bgp)
        self.assertIn(f"neighbor {peer_svi} peer group MLAG-IPV4-UNDERLAY-PEER", bgp)
        self.assertIn("neighbor MLAG-IPV4-UNDERLAY-PEER activate", bgp)
        for name in ("DC3-L3", "DC3-L11"):
            self.assertNotIn("MLAG-IPV4-UNDERLAY-PEER", render(name))
            self.assertNotIn("mlag configuration", render(name))

    def test_mlag_block(self):
        for d in FAB.domains:
            for me, peer in (d["members"], d["members"][::-1]):
                cfg = render(me)
                self.assertEqual(block(cfg, "mlag configuration"), [
                    f"domain-id {d['name']}",
                    "local-interface Vlan3900",
                    f"peer-address {ip(FAB.addr['backup'][peer])}",
                    f"peer-address heartbeat {ip(FAB.addr['keepalive'][peer])} vrf VPC-KEEPALIVE",
                    "peer-link Port-Channel1000",
                    "heartbeat-interval 4000",
                    "dual-primary detection delay 5 action errdisable all-interfaces",
                    "dual-primary recovery delay mlag 360 non-mlag 300",
                    "reload-delay mlag 360",
                    "reload-delay non-mlag 360"])
                self.assertEqual(block(cfg, "vlan 3900"), ["name VPC-BACKUP-UNDERLAY", "trunk group MLAGPEER"])
                self.assertIn("\nno spanning-tree vlan-id 3900\n", cfg)
                self.assertEqual(block(cfg, "interface Vlan3900"), [
                    f"description {peer} Vlan3900", "mtu 9214", "no autostate", f"ip address {FAB.addr['backup'][me]}"])
                self.assertEqual(block(cfg, "interface Port-Channel1000"), [
                    f"description {peer} Port-Channel1000", "switchport mode trunk", "switchport trunk group MLAGPEER"])
                for port in d["peer_link"]:
                    self.assertEqual(block(cfg, f"interface {port}"),
                                     [f"description {peer} {port}", "no shutdown", "channel-group 1000 mode active"])
                self.assertEqual(block(cfg, f"interface {d['keepalive']}"), [
                    f"description {peer} {d['keepalive']}", "no shutdown", "mtu 9214", "no switchport",
                    "vrf VPC-KEEPALIVE", f"ip address {FAB.addr['keepalive'][me]}"])
                self.assertIn("vrf instance VPC-KEEPALIVE", cfg)

    def test_vxlan_and_gateway_mac(self):
        for r in FAB.plan:
            cfg = render(r["name"])
            if r["role"] not in P.VTEP_ROLES:
                self.assertNotIn("interface Vxlan1", cfg)
                self.assertNotIn("ip virtual-router", cfg)
                continue
            pair = FAB.pair_of(r["name"])
            mlag = pair is not None and pair["kind"] == "FabricVpcDomain"
            vx = block(cfg, "interface Vxlan1")
            self.assertEqual(vx[:2], ["vxlan source-interface Loopback1", "vxlan udp-port 4789"])
            self.assertEqual("vxlan virtual-router encapsulation mac-address mlag-system-id" in vx, mlag)
            self.assertIn("\nip virtual-router mac-address 0200.0000.00aa\n", cfg)
            self.assertEqual("ip virtual-router mac-address mlag-peer" in cfg, mlag)
            lo1 = block(cfg, "interface Loopback1")
            expect = FAB.addr["anycast"][pair["name"]] if mlag else FAB.addr["lo1"][r["name"]]
            desc = P.LOOPBACK1_ANYCAST_DESCRIPTION.format(pair=pair["name"]) if mlag else P.LOOPBACK1_DESCRIPTION
            self.assertEqual(lo1, [f"description {desc}", f"ip address {expect}"])
            if mlag:                                                                                 # own address
                self.assertEqual(block(cfg, "interface Loopback2"),
                                 [f"description {P.LOOPBACK2_DESCRIPTION}", f"ip address {FAB.addr['lo1'][r['name']]}"])
            else:
                self.assertNotIn("Loopback2", cfg)
            self.assertNotIn("vxlan vlan", cfg)                                                      # comes with tenant data
            self.assertNotIn("vxlan vrf", cfg)

    def test_l2_pairs(self):
        for name in ("DC3-EDGE1", "DC3-EDGE2", "DC3-DMZ1", "DC3-DMZ2"):
            cfg = render(name)
            for absent in ("router bgp", "Loopback", "Vxlan1", "router bfd", "prefix-list", "\nip routing"):
                self.assertNotIn(absent, cfg, name)
            self.assertIn("mlag configuration", cfg)
        self.assertIn("interface Ethernet34\n", render("DC3-EDGE1"))                   # keepalive
        self.assertIn("interface Ethernet1/1\n", render("DC3-EDGE1"))                  # peer-link member


class RepoConfigTest(unittest.TestCase):
    def test_infrahub_yml(self):
        import yaml
        cfg = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
        queries = {q["name"]: q["file_path"] for q in cfg["queries"]}
        transforms = {t["name"]: t for t in cfg["jinja2_transforms"]}
        for template, query in set(TEMPLATE_BY_ROLE.values()):
            self.assertEqual(transforms[template]["query"], query)
            self.assertEqual(transforms[template]["template_path"], f"templates/{template}.j2")
            self.assertTrue((ROOT / queries[query]).exists())
        targets = {a["targets"]: a for a in cfg["artifact_definitions"]}
        for role, group in P.ARTIFACT_GROUPS.items():
            a = targets[group]
            self.assertEqual(a["transformation"], TEMPLATE_BY_ROLE[role][0])
            self.assertEqual((a["artifact_name"], a["parameters"]), ("startup-config", {"device": "name__value"}))
        for g in cfg["generator_definitions"]:
            self.assertIs(g["execute_in_proposed_change"], False)
            self.assertIs(g["execute_after_merge"], False)

    def test_query_files_balanced(self):
        for f in (ROOT / "queries").glob("dc3_*_config.gql"):
            text = "\n".join(line.split("#", 1)[0] for line in f.read_text().splitlines())
            self.assertEqual(text.count("{"), text.count("}"), f.name)
            self.assertIn("($device: String!)", text, f.name)


class TopologyTest(unittest.TestCase):
    def test_topology(self):
        spec = importlib.util.spec_from_file_location("dc3_lab_topology", ROOT / "scripts" / "dc3_lab_topology.py")
        lab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lab)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "configs").mkdir()
            (tmp / "configs" / "DC3-S1.cfg").write_text(render("DC3-S1"))
            topo = lab.write(tmp)
            t = topo["topology"]
            ceos = {n for n, x in t["nodes"].items() if x["kind"] == "arista_ceos"}
            self.assertEqual(len(ceos), 30)
            core_links = [l for l in t["links"] if all(e.split(":")[0] in ceos for e in l["endpoints"])]   # the full topology adds stand-in links
            fabric = [l for l in t["links"] if any(e.startswith(("DC3-S1:", "DC3-S2:")) for e in l["endpoints"])]
            self.assertEqual(len(fabric), 88)
            self.assertIn({"endpoints": ["DC3-S1:eth1", "DC3-L1:eth49"]}, fabric)
            # MLAG: peer-link members + one keepalive link per pair
            mlag_links = sum(len(d["peer_link"]) + 1 for d in FAB.domains)
            self.assertEqual(mlag_links, 2 + 1 + 4 + 1 + 4 + 1 + 4 + 1 + 2 + 1)
            self.assertEqual(len(core_links), 88 + mlag_links)
            self.assertIn({"endpoints": ["DC3-BL1:eth24", "DC3-BL2:eth24"]}, t["links"])
            self.assertIn({"endpoints": ["DC3-EDGE1:eth34", "DC3-EDGE2:eth34"]}, t["links"])
            ends = [e for l in t["links"] for e in l["endpoints"]]
            self.assertEqual(len(ends), len(set(ends)), "an interface used twice")
            self.assertEqual(t["kinds"]["arista_ceos"]["image"], "ceos:4.36.2F")
            s1 = t["nodes"]["DC3-S1"]
            self.assertEqual(s1["startup-config"], "configs/DC3-S1.cfg")
            self.assertEqual(s1["binds"], ["intf-mapping/DCS-7060DX5-64S.json:/mnt/flash/EosIntfMapping.json:ro"])
            self.assertEqual(s1["mgmt-ipv4"], ip(FAB.addr["mgmt"]["DC3-S1"]))
            self.assertNotIn("mgmt-ipv4", t["nodes"]["DC3-S2"])
            # every link endpoint maps to the EOS name the fabric-core data uses
            maps = {m: lab.mapping(m) for m in set(P.DEVICE_TYPE.values())}
            for l in FAB.links:
                for dev, port in ((l["spine"], l["spine_port"]), (l["leaf"], l["leaf_port"])):
                    self.assertEqual(maps[FAB.rows[dev]["model"]]["EthernetIntf"][lab.eth(port)], port)
            self.assertEqual(maps["DCS-7050SX3-48YC8C"]["ManagementIntf"], {"eth0": "Management1"})
            self.assertTrue((tmp / "intf-mapping" / "DCS-7280DR3-24.json").exists())

    def test_committed_lab_files_are_current(self):
        spec = importlib.util.spec_from_file_location("dc3_lab_topology2", ROOT / "scripts" / "dc3_lab_topology.py")
        lab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lab)
        import json
        for m in set(P.DEVICE_TYPE.values()):
            self.assertEqual(json.loads((ROOT / "lab" / "intf-mapping" / f"{m}.json").read_text()), lab.mapping(m))


if __name__ == "__main__":
    unittest.main()
