"""Tenant and stand-in tests: tenant VRFs / segments / attachments / stand-ins, end to end without Infrahub.

1. Plan tables: attachment slots, VLANs and /29s, attach ports, ISP carrier prefixes.
2. The generator (generators/dc3_site.py) runs against the in-memory fake client of test_dc3_site_converge.py; the object
   files of tenants and stand-ins (objects/dc3-06/07/08/10) are read from the repo. Query responses are built from that store in the
   shape of queries/dc3_*.gql, and every EOS template and FRR stand-in script is rendered from them.
3. Both ends of every BGP session agree (EOS neighbor address / remote-as <-> FRR neighbor address / remote-as).
4. The containerlab topology carries the stand-ins, the lab-only nodes and their links.

TEST DATA ONLY: pool allocations are the fake client's (deterministic, not the Infrahub ones); fixed addresses (attachment
/29s, ISP, cloud, edge VLAN) are the generator's own rule-based values.
"""
import importlib.util
import ipaddress
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "generators"))
sys.path.insert(0, str(ROOT / "tests"))
import dc3_plan as P  # noqa: E402
import test_dc3_render as R  # noqa: E402  (SDK-like Jinja2 environment and helpers)
import test_dc3_site_converge as C  # noqa: E402  (fake client)

SITE = "DC3"
PROVIDERS = {64496: "ISP-A", 64497: "ISP-B", 65500: "AWS", 12076: "Microsoft"}   # provider ASNs of objects/dc3-01-bootstrap.yml


def v(x):
    return {"value": x}


def objects(name):
    return [d for d in yaml.safe_load_all((ROOT / "objects" / name).read_text()) if d]


def prefix_site(prefix: str):
    """Site of a summary prefix by the site block (DC3 10.24; any other block: no site)."""
    return SITE if prefix.startswith("10.24.") else None


class Graph:
    """Generator output (fake client store) + the stage-3 object files, exposed as query responses."""

    def __init__(self):
        gen = C.load_generator(ROOT / "generators", "stage3")
        self.client = C.FakeClient()
        self.log = C.run(gen, self.client)
        self.log2 = C.run(gen, self.client)                     # idempotency run
        st = self.client.store
        self.st = st
        self.dev = {n.data["name"]: n for n in st["DcimDevice"].values()}
        self.dev_by_id = {n.id: n for n in st["DcimDevice"].values()}
        self.addr = {i: n.data["address"] for i, n in st["IpamIPAddress"].items()}
        self.asn_by_id = {i: n.data["asn"] for i, n in st["FabricAutonomousSystem"].items()}
        self.prefix_by_id = {i: n.data["prefix"] for i, n in st["IpamPrefix"].items()}
        self.vrf_by_id = {i: n.data for i, n in st["FabricVRF"].items()}
        self.pairs = {n.id: n for kind in ("FabricVpcDomain", "FabricEsiPair") for n in st.get(kind, {}).values()}
        self._load_objects()

    # ---------------------------------------------------------------- object files (tenants and stand-ins)
    def _load_objects(self):
        fab = objects("dc3-06-fabric.yml")
        self.vrf_objects = {x["name"]: x for d in fab if d["spec"]["kind"] == "FabricVRF" for x in d["spec"]["data"]}
        segs = objects("dc3-07-segments.yml")
        self.segments = [x for d in segs if d["spec"]["kind"] == "FabricSegment" for x in d["spec"]["data"]]
        sta = objects("dc3-08-standins.yml")
        self.partners = [x for d in sta if d["spec"]["kind"] == "FabricPartner" for x in d["spec"]["data"]]
        self.branches = [x for d in sta if d["spec"]["kind"] == "FabricBranch" for x in d["spec"]["data"]]
        self.access = [x for d in objects("dc3-10-lab-host-ports.yml") for x in d["spec"]["data"]]
        plan = objects("dc3-03-ip-plan.yml")
        self.site_prefixes = [x["prefix"] for d in plan if d["spec"]["kind"] == "IpamPrefix" for x in d["spec"]["data"]
                              if x.get("site") == SITE]

    def segment_node(self, s):
        vrf = self.vrf_objects[s["vrf"]]
        return {"name": v(s["name"]), "vlan_id": v(s["vlan_id"]), "l2_vni": v(s["l2_vni"]),
                "prefix": {"node": {"prefix": v(s["prefix"][1])}},
                "vrf": {"node": {"name": v(s["vrf"]), "l3_vni": v(vrf["l3_vni"])}}}

    def vrfs_node(self):
        return {"edges": [{"node": {"name": v(n), "summaries": {"edges": [
            {"node": {"prefix": v(p), "site": {"node": {"name": v(prefix_site(p))} if prefix_site(p) else None}}}
            for _ns, p in x.get("summaries", [])]}}} for n, x in self.vrf_objects.items() if SITE in x["sites"]]}

    def owned_prefixes(self):
        site_id = next(iter(self.st["LocationSite"]))
        gen = [n.data["prefix"] for n in self.st["IpamPrefix"].values() if n.data.get("site") == site_id]
        return {"edges": [{"node": {"prefix": v(p)}} for p in sorted(set(self.site_prefixes + gen))]}

    # ---------------------------------------------------------------- nodes
    def interfaces(self, device_id):
        st = self.st
        out = []
        pcs = [n for n in st.get("InterfacePortChannel", {}).values() if n.data["device"] == device_id]
        for kind in ("InterfacePhysical", "InterfaceVirtual", "InterfacePortChannel"):
            for n in st.get(kind, {}).values():
                d = n.data
                if d["device"] != device_id:
                    continue
                node = {"__typename": kind, "name": v(d["name"]), "description": v(d.get("description")),
                        "role": v(d.get("role")), "mtu": v(d.get("mtu")),
                        "ip_addresses": {"edges": [{"node": {"address": v(self.addr[a])}} for a in d.get("ip_addresses") or []]}}
                if kind == "InterfacePhysical":
                    pc = next((p for p in pcs if n.id in p.data["members"]), None)
                    node["port_channel"] = {"node": {"name": v(pc.data["name"])} if pc else None}
                    node["access_segment"] = {"node": None}
                if kind == "InterfacePortChannel":
                    names = {i: x.data["name"] for i, x in st["InterfacePhysical"].items()}
                    node["members"] = {"edges": [{"node": {"name": v(names[m])}} for m in d["members"]]}
                if kind == "InterfaceVirtual":
                    parent = d.get("parent_interface")
                    pname = None
                    if parent:
                        for k in ("InterfacePhysical", "InterfacePortChannel"):
                            if parent in st.get(k, {}):
                                pname = st[k][parent].data["name"]
                    node["parent_interface"] = {"node": {"name": v(pname)} if pname else None}
                out.append(node)
        name = self.dev_by_id[device_id].data["name"]
        for a in self.access:                                   # objects/dc3-10-lab-host-ports.yml
            if a["device"] == name:
                seg = next(s for s in self.segments if s["name"] == a["access_segment"])
                out.append({"__typename": "InterfacePhysical", "name": v(a["name"]), "description": v(a["description"]),
                            "role": v(a["role"]), "mtu": v(a["mtu"]), "ip_addresses": {"edges": []},
                            "port_channel": {"node": None},
                            "access_segment": {"node": {"name": v(seg["name"]), "vlan_id": v(seg["vlan_id"])}}})
        return {"edges": [{"node": x} for x in out]}

    def asn_node(self, d):
        a = d.data.get("asn")
        return {"node": {"asn": v(self.asn_by_id[a])} if a else None}

    def attachment_node(self, a):
        d = a.data
        pair = self.pairs[d["switch_pair"]]
        return {"name": v(d["name"]), "vlan_id": v(d["vlan_id"]),
                "prefix": {"node": {"prefix": v(self.prefix_by_id[d["prefix"]])}},
                "vrf": {"node": {"name": v(self.vrf_by_id[d["vrf"]]["name"]), "l3_vni": v(self.vrf_by_id[d["vrf"]]["l3_vni"])}},
                "switch_pair": {"node": {"name": v(pair.data["name"]), "members": {"edges": [
                    {"node": {"name": v(self.dev_by_id[m].data["name"]), "asn": self.asn_node(self.dev_by_id[m])}}
                    for m in pair.data["members"]]}}},
                "devices": {"edges": [{"node": {"name": v(self.dev_by_id[x].data["name"]), "role": v(self.dev_by_id[x].data["role"]),
                                                "asn": self.asn_node(self.dev_by_id[x])}} for x in d["devices"]]}}

    def switch_pair(self, device):
        pair = next((p for p in self.pairs.values() if device.id in p.data["members"]), None)
        if pair is None:
            return {"node": None}
        kind = "FabricVpcDomain" if pair.kind == "FabricVpcDomain" else "FabricEsiPair"
        node = {"__typename": kind, "name": v(pair.data["name"]), "members": {"edges": []}}
        if kind == "FabricVpcDomain":
            node["domain_id"] = v(pair.data["domain_id"])
            av = pair.data.get("anycast_vtep")
            node["anycast_vtep"] = {"node": {"address": v(self.addr[av])} if av else None}
            node["attachments"] = {"edges": [{"node": self.attachment_node(a)} for a in self.st["FabricAttachment"].values()
                                             if a.data["switch_pair"] == pair.id]}
            node["l2_vlans"] = {"edges": [{"node": {"name": v(x.data["name"]), "vlan_id": v(x.data["vlan_id"])}}
                                          for x in self.st["FabricL2Vlan"].values() if x.data["switch_pair"] == pair.id]}
        for m in pair.data["members"]:
            ka = [i["node"] for i in self.interfaces(m)["edges"] if i["node"]["role"]["value"] == P.ROLE_KEEPALIVE]
            node["members"]["edges"].append({"node": {"name": v(self.dev_by_id[m].data["name"]), "interfaces": {"edges": [
                {"node": {"name": i["name"], "ip_addresses": i["ip_addresses"]}} for i in ka]}}})
        return {"node": node}

    def device_node(self, name):
        d = self.dev[name]
        pa = d.data.get("primary_address")
        return {"name": v(name), "role": v(d.data["role"]), "asn": self.asn_node(d),
                "primary_address": {"node": {"address": v(self.addr[pa])} if pa else None},
                "location": {"node": {"name": v(SITE), "owned_prefixes": self.owned_prefixes(), "vrfs": self.vrfs_node(),
                                      "segments": {"edges": [{"node": self.segment_node(s)} for s in self.segments]}}},
                "switch_pair": self.switch_pair(d), "interfaces": self.interfaces(d.id)}

    def as_root(self):
        by = {}
        for d in self.dev.values():
            if d.data.get("asn") and d.data["role"] in P.DEVICE_TYPE:
                lo = [i["node"] for i in self.interfaces(d.id)["edges"] if i["node"]["name"]["value"] == P.LOOPBACK0]
                by.setdefault(self.asn_by_id[d.data["asn"]], []).append({"name": v(d.data["name"]), "role": v(d.data["role"]),
                                                                          "interfaces": {"edges": [{"node": x} for x in lo]}})
        return {"edges": [{"node": {"asn": v(a), "devices": {"edges": [{"node": x} for x in ds]}}} for a, ds in sorted(by.items())]}

    def provider_asns(self):
        asns = sorted(set(self.asn_by_id.values()) | set(PROVIDERS))
        return {"edges": [{"node": {"asn": v(a), "provider": {"node": {"name": v(PROVIDERS[a])} if a in PROVIDERS else None}}}
                          for a in asns]}

    def role_devices(self, role, iface_filter=None):
        out = []
        for d in self.dev.values():
            if d.data["role"] != role:
                continue
            ifs = [i for i in self.interfaces(d.id)["edges"] if iface_filter is None or iface_filter(i["node"])]
            out.append({"node": {"name": v(d.data["name"]), "role": v(role), "location": {"node": {"name": v(SITE)}},
                                 "asn": self.asn_node(d), "interfaces": {"edges": ifs}}})
        return {"edges": out}

    # ---------------------------------------------------------------- query responses
    def response(self, query, name):
        if query in ("dc3_spine_config", "dc3_leaf_config", "dc3_border_service_leaf_config", "dc3_l2_pair_config"):
            return {"DcimDevice": {"count": 1, "edges": [{"node": self.device_node(name)}]},
                    "FabricAutonomousSystem": self.as_root(), "CoreIPAddressPool": R.Fabric.pools_root()}
        dev = self.dev.get(name)
        atts = {"edges": [{"node": self.attachment_node(a)} for a in self.st["FabricAttachment"].values()
                          if dev is not None and dev.id in a.data["devices"]]}
        if query == "dc3_frr_fw":
            pfx = {i: n.data["prefix"] for i, n in self.st["IpamPrefix"].items()}
            pairs = {p.id: p.data["name"] for p in self.pairs.values()}
            return {"DcimDevice": {"count": 1, "edges": [{"node": self.device_node(name)}]}, "FabricAttachment": atts,
                    "FabricL2Vlan": {"edges": [{"node": {"name": v(x.data["name"]), "vlan_id": v(x.data["vlan_id"]),
                                                         "prefix": {"node": {"prefix": v(pfx[x.data["prefix"]])}},
                                                         "site": {"node": {"name": v(SITE)}},
                                                         "switch_pair": {"node": {"name": v(pairs[x.data["switch_pair"]])}}}}
                                               for x in self.st["FabricL2Vlan"].values()]},
                    "IspRouters": self.role_devices("isp_router", lambda i: i["role"]["value"] == P.ROLE_PEERING),
                    "FabricBranch": {"edges": [{"node": {"name": v(b["name"]), "branch_id": v(b["branch_id"]),
                                                         "lan_prefix": {"node": {"prefix": v(b["lan_prefix"][1])}},
                                                         "hubs": {"edges": [{"node": {"name": v(h)}} for h in b["hubs"]]}}}
                                               for b in self.branches]},
                    "FabricPartner": {"edges": [{"node": {"name": v(p["name"]), "description": v(p["description"]),
                                                          "peer_ip": v(f'{p["peer_ip"]}/32'),   # Infrahub returns the IP with its mask
                                                          "protected_prefixes": v(p["protected_prefixes"]),
                                                          "translated_prefixes": {"edges": [
                                                              {"node": {"prefix": v(t[1]), "site": {"node": {"name": v(prefix_site(t[1]))}}}}
                                                              for t in p["translated_prefixes"]]}}} for p in self.partners]}}
        if query == "dc3_frr_router":
            lo_wan = lambda i: i["name"]["value"] == P.FW_WAN_LOOPBACK   # noqa: E731
            return {"DcimDevice": {"count": 1, "edges": [{"node": self.device_node(name)}]}, "ProviderAsns": self.provider_asns(),
                    "InternetFws": self.role_devices("internet_fw", lambda i: False),
                    "SdwanFws": self.role_devices("sdwan_fw", lo_wan), "VpnFws": self.role_devices("vpn_fw", lo_wan),
                    "FabricAttachment": atts}
        if query == "dc3_frr_cloud_provider":
            return {"LocationSite": {"edges": [{"node": {"name": v(name)}}]}, "ProviderAsns": self.provider_asns(),
                    "CloudRouters": self.role_devices("cloud_router", lambda i: i["role"]["value"] == P.ROLE_CLOUD
                                                      and i["__typename"] == "InterfaceVirtual")}
        raise KeyError(query)


INFRAHUB = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
TRANSFORMS = {t["name"]: t for t in INFRAHUB["jinja2_transforms"]}
ARTIFACTS = INFRAHUB["artifact_definitions"]
GROUPS = {**P.ARTIFACT_GROUPS, **P.STANDIN_GROUPS}
G = Graph()
ENV = R.environment()
OUT = {}


def render(transform: str, target: str) -> str:
    key = (transform, target)
    if key not in OUT:
        t = TRANSFORMS[transform]
        OUT[key] = ENV.get_template(Path(t["template_path"]).name).render(data=G.response(t["query"], target))
    return OUT[key]


def artifacts_for(name: str) -> dict:
    """artifact_name -> rendered text for one device (every artifact definition whose target group holds it)."""
    role = G.dev[name].data["role"]
    group = GROUPS[role]
    return {a["artifact_name"]: render(a["transformation"], name) for a in ARTIFACTS if a["targets"] == group}


def eos(name: str) -> str:
    return artifacts_for(name)["startup-config"]


def frr_conf(script: str) -> str:
    return script.split("<<'FRREOF'\n", 1)[1].split("\nFRREOF", 1)[0]


def ip(cidr):
    return cidr.split("/")[0]


class PlanTablesTest(unittest.TestCase):
    """Slot tables, attach ports and ISP carrier prefixes of the plan."""

    @classmethod
    def setUpClass(cls):
        cls.plan = P.build_plan(SITE, 1, 4, 10)
        cls.standins = P.build_standins(SITE)
        cls.ap = P.build_attach_ports(SITE, cls.plan, cls.standins)
        cls.att = P.build_attachments(SITE, cls.standins, P.DC3_VRFS)

    def test_attachment_slots(self):
        ours = {a["name"]: (a["slot"], a["vlan"], a["prefix"]) for a in self.att}
        self.assertEqual(len(ours), 10)
        self.assertEqual(ours["DC3-TRANSIT-INTFW"], (0, 3000, "10.27.16.0/29"))
        self.assertEqual(ours["DC3-PROD-INTFW"], (0, 3100, "10.27.8.0/29"))
        self.assertEqual(ours["DC3-CITRIX-INTFW"], (5, 3105, "10.27.8.40/29"))
        self.assertEqual(ours["DC3-PARTNER-VPNFW"], (0, 3300, "10.27.9.0/29"))
        self.assertEqual({a["name"]: a["devices"] for a in self.att if a["name"].endswith("INTFW")}["DC3-PROD-INTFW"], ("DC3-INTFW1",))

    def test_attach_ports(self):
        # every FW stand-in is on one MLAG port-channel per switch pair, member port = same number on both switches
        for far in ("DC3-INETFW1", "DC3-INTFW1", "DC3-SDWANFW1", "DC3-VPNFW1"):
            by_pair = {}
            for ap in (x for x in self.ap if x["far"] == far):
                by_pair.setdefault(ap["device"][:-1], set()).add((ap["port"], ap["po"]))
            self.assertTrue(by_pair, far)
            self.assertTrue(all(len(x) == 1 for x in by_pair.values()), (far, by_pair))
        self.assertEqual(len(self.ap), 24)

    def test_isp_carrier_prefixes(self):
        self.assertEqual([r["carrier"]["prefix"] for r in P.build_isp(SITE)], ["192.0.2.16/30", "192.0.2.20/30"])


class GeneratorStage3Test(unittest.TestCase):
    def test_counts_and_idempotency(self):
        st = G.st
        self.assertEqual(len(G.dev), 38)
        self.assertEqual(sorted(n for n, d in G.dev.items() if d.data["role"] in P.STANDIN_ROLES),
                         ["DC3-CLOUD1", "DC3-CLOUD2", "DC3-INETFW1", "DC3-INTFW1", "DC3-ISP1", "DC3-ISP2", "DC3-SDWANFW1", "DC3-VPNFW1"])
        self.assertEqual(len(st["FabricAttachment"]), 10)
        self.assertEqual(len(st["FabricL2Vlan"]), 17)
        self.assertNotIn("DELETE", G.log + G.log2)
        self.assertEqual({G.asn_by_id[G.dev[n].data["asn"]] for n in ("DC3-ISP1", "DC3-ISP2")}, {65050})
        self.assertEqual(G.asn_by_id[G.dev["DC3-INTFW1"].data["asn"]], 65310)
        self.assertEqual(G.asn_by_id[G.dev["DC3-SDWANFW1"].data["asn"]], 65320)
        self.assertEqual(G.asn_by_id[G.dev["DC3-VPNFW1"].data["asn"]], 65330)
        self.assertEqual(G.asn_by_id[G.dev["DC3-INETFW1"].data["asn"]], 65340)
        self.assertEqual(G.asn_by_id[G.dev["DC3-CLOUD1"].data["asn"]], 65370)
        groups = {n.data["name"]: n.data.get("_members", []) for n in st["CoreStandardGroup"].values()}
        self.assertEqual(len(groups["dc3-isp-routers"]), 2)
        self.assertEqual(len(groups["dc3-internal-fws"]), 1)

    def test_rerun_changes_nothing(self):
        snap = C.snapshot(G.client)
        G.client.changes.clear()
        C.run(C.load_generator(ROOT / "generators", "stage3b"), G.client)
        self.assertEqual(C.snapshot(G.client), snap)
        self.assertFalse([c for c in G.client.changes if not c.startswith("save IpamPrefix")], G.client.changes)

    def test_reconciler_removes_stage3_orphans(self):
        client = C.FakeClient()
        gen = C.load_generator(ROOT / "generators", "stage3r")
        C.run(gen, client)
        st = client.store
        site_id = next(iter(st["LocationSite"]))
        fw = next(n for n in st["DcimDevice"].values() if n.data["name"] == "DC3-INTFW1")
        sl1 = next(n for n in st["DcimDevice"].values() if n.data["name"] == "DC3-SL1")
        pfx = C.Node(client, "IpamPrefix", {"prefix": "10.27.8.120/29", "site": site_id})
        client._put(pfx)
        addr = C.Node(client, "IpamIPAddress", {"address": "10.27.8.121/29"})
        client._put(addr)
        client._put(C.Node(client, "FabricAttachment", {"name": "DC3-OLD-INTFW", "site": site_id, "prefix": pfx.id}))
        client._put(C.Node(client, "InterfaceVirtual", {"name": "OLD-3115", "device": fw.id, "role": "peering", "ip_addresses": []}))
        client._put(C.Node(client, "InterfaceVirtual", {"name": "Vlan3115", "device": sl1.id, "role": "peering",
                                                        "ip_addresses": [addr.id]}))
        client._put(C.Node(client, "InterfacePhysical", {"name": "Ethernet20/1", "device": sl1.id, "role": "access"}))  # not owned
        out = C.run(gen, client)
        for label in ("Attachment    DC3-OLD-INTFW", "Interface     DC3-INTFW1 OLD-3115", "Interface     DC3-SL1 Vlan3115",
                      "IpamIPAddress 10.27.8.121/29", "IpamPrefix    10.27.8.120/29"):
            self.assertIn(f"DELETED       {label}", out)
        self.assertNotIn("DC3-SL1 Ethernet20/1", "\n".join(l for l in out.splitlines() if l.startswith("DELETED")))
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith("DELETED")), 5)

    def test_refuses_site_without_a_dc3_vrf(self):
        client = C.FakeClient()
        gen = C.load_generator(ROOT / "generators", "stage3c")
        C.run(gen, client)
        vrf = next(n for n in client.store["FabricVRF"].values() if n.data["name"] == "CITRIX")
        vrf.data["sites"] = []                                      # CITRIX no longer in DC3
        with self.assertRaisesRegex(ValueError, "CITRIX"):          # guard: every DC3 VRF must list the site
            C.run(gen, client)


class EosRenderTest(unittest.TestCase):
    def test_every_switch_renders(self):
        for name, d in G.dev.items():
            if d.data["role"] not in P.DEVICE_TYPE:
                continue
            cfg = eos(name)
            self.assertTrue(cfg.startswith(f"hostname {name}\n"), name)
            self.assertTrue(cfg.rstrip().endswith("end"), name)
            for bad in ("None", "{", "password", "allowas-in", "as-override"):
                self.assertNotIn(bad, cfg, (name, bad))
            self.assertNotRegex(cfg, r"\n\n", name)

    def test_server_leaf_tenants(self):
        cfg = eos("DC3-L11")
        segs = [s for s in G.segments if s["vrf"] in ("PROD", "DEV", "CITRIX")]
        self.assertEqual(len(segs), 42)
        lo0 = ip(G.addr[next(i for i in G.st["InterfaceVirtual"].values() if i.data["name"] == "Loopback0"
                             and i.data["device"] == G.dev["DC3-L11"].id).data["ip_addresses"][0]])
        self.assertEqual(R.block(cfg, "vlan 1010"), ["name DC3-PROD-1010"])
        self.assertEqual(R.block(cfg, "interface Vlan1010"), ["no shutdown", "mtu 9214", "vrf PROD", "ip address virtual 10.24.10.1/24"])
        self.assertEqual(R.block(cfg, "interface Vlan1500"), ["no shutdown", "mtu 9214", "vrf CITRIX", "ip address virtual 10.24.144.1/24"])
        vx = R.block(cfg, "interface Vxlan1")
        self.assertIn("vxlan vlan 1010 vni 11010", vx)
        self.assertIn("vxlan vrf PROD vni 50001", vx)
        self.assertIn("vxlan vrf CITRIX vni 50022", vx)
        self.assertEqual(sum(1 for x in vx if x.startswith("vxlan vlan ")), 42)
        self.assertNotIn("vxlan vlan 1598", "\n".join(vx))                      # REPLICATION is not a server VRF
        for vrf in ("CITRIX", "DEV", "PROD"):
            self.assertIn(f"\nvrf instance {vrf}\n", cfg)
            self.assertIn(f"\nip routing vrf {vrf}\n", cfg)
        bgp = cfg.split("\nrouter bgp 65382\n", 1)[1]
        self.assertIn(f"   vlan 1010\n      rd {lo0}:11010\n      route-target both 65300:11010\n      redistribute learned\n", bgp)
        self.assertIn(f"   vrf PROD\n      rd {lo0}:50001\n      route-target import evpn 65300:50001\n"
                      f"      route-target export evpn 65300:50001\n      router-id {lo0}\n      redistribute connected\n", bgp)
        # lab test hosts: one access port per server VRF
        self.assertEqual(R.block(cfg, "interface Ethernet1"), ["description DC3-HOST-PROD eth1", "no shutdown",
                                                               "switchport access vlan 1010", "spanning-tree portfast",
                                                               "spanning-tree bpduguard enable"])
        self.assertIn("switchport access vlan 1110", R.block(cfg, "interface Ethernet2"))
        self.assertIn("switchport access vlan 1500", R.block(cfg, "interface Ethernet3"))
        self.assertNotIn("link tracking", cfg)                                  # no ES ports in the data
        self.assertNotIn("interface Ethernet1\n", eos("DC3-L12"))

    def test_mlag_loopback1_renders_pair_anycast(self):                       # loopback fallback
        for name in ("DC3-L1", "DC3-L2", "DC3-BL1", "DC3-SL2"):
            d = G.dev[name]
            pair = next(p for p in G.pairs.values() if d.id in p.data["members"])
            lo1 = next(i for i in G.st["InterfaceVirtual"].values() if i.data["device"] == d.id and i.data["name"] == "Loopback1")
            self.assertFalse(lo1.data.get("ip_addresses"))                       # no address in the data
            self.assertEqual(R.block(eos(name), "interface Loopback1"),
                             [f"description VTEP source (Vxlan1), anycast from {pair.data['name']}",
                              f"ip address {G.addr[pair.data['anycast_vtep']]}"])
            self.assertIn("interface Loopback2", eos(name))
        self.assertEqual(R.block(eos("DC3-L11"), "interface Loopback1")[0], "description VTEP source (Vxlan1)")

    def test_service_leaf(self):
        cfg = eos("DC3-SL1")
        for vlan, vrf, addr in ((3000, "TRANSIT", "10.27.16.1/29"), (3100, "PROD", "10.27.8.1/29"), (3101, "DEV", "10.27.8.9/29"),
                                (3105, "CITRIX", "10.27.8.41/29"), (3106, "PARTNER", "10.27.8.49/29")):
            att = {3000: "DC3-TRANSIT-INTFW", 3100: "DC3-PROD-INTFW", 3101: "DC3-DEV-INTFW", 3105: "DC3-CITRIX-INTFW",
                   3106: "DC3-PARTNER-INTFW"}[vlan]
            self.assertEqual(R.block(cfg, f"vlan {vlan}"), [f"name {att}"])
            self.assertEqual(R.block(cfg, f"interface Vlan{vlan}"), [f"description {att}", "no shutdown", "mtu 9214", f"vrf {vrf}",
                                                                     f"ip address {addr}", "bfd interval 999 min-rx 999 multiplier 3"])
        self.assertEqual(R.block(cfg, "interface Port-Channel11"), [
            "description DC3-INTFW1 agg-inside", "switchport mode trunk", "switchport trunk allowed vlan 3000,3100-3101,3105-3106",
            "mlag 11", "spanning-tree portfast", "spanning-tree bpduguard enable"])
        self.assertEqual(R.block(cfg, "interface Ethernet7/1"), ["description DC3-INTFW1 port19", "no shutdown", "channel-group 11 mode active"])
        self.assertEqual(R.block(eos("DC3-SL2"), "interface Ethernet7/1"), ["description DC3-INTFW1 port20", "no shutdown",
                                                                            "channel-group 11 mode active"])
        self.assertEqual(R.block(cfg, "ip prefix-list PL-PROD-SUMMARY"), ["seq 10 permit 10.24.0.0/18"])   # summary only
        self.assertNotIn("COMPONENTS", cfg)
        self.assertEqual(R.block(cfg, "ip prefix-list PL-TRANSIT-SUMMARY"), [
            "seq 10 permit 10.24.144.0/20", "seq 20 permit 10.24.64.0/18", "seq 30 permit 10.24.160.0/21", "seq 40 permit 10.24.0.0/18"])
        self.assertNotIn("10.16.", cfg)                                          # summaries of other sites never reach DC3
        self.assertEqual(R.block(cfg, "route-map RM-PROD-TO-FW permit 10"), ["match ip address prefix-list PL-PROD-SUMMARY"])
        self.assertEqual(R.block(cfg, "route-map RM-PROD-FROM-FW permit 10"), ["match ip address prefix-list PL-DEFAULT"])
        self.assertEqual(R.block(cfg, "route-map RM-TRANSIT-FROM-FW permit 10"), ["match ip address prefix-list PL-TRANSIT-SUMMARY"])
        self.assertIn("ip route vrf PARTNER 10.24.160.0/21 Null0", cfg)
        bgp = cfg.split("\nrouter bgp 65372\n", 1)[1]
        self.assertIn("      neighbor 10.27.8.3 remote-as 65310\n      neighbor 10.27.8.3 description DC3-PROD-INTFW\n"
                      "      neighbor 10.27.8.3 bfd\n      aggregate-address 10.24.0.0/18\n      redistribute connected\n", bgp)
        self.assertIn("         neighbor 10.27.8.3 activate\n         neighbor 10.27.8.3 route-map RM-PROD-FROM-FW in\n"
                      "         neighbor 10.27.8.3 route-map RM-PROD-TO-FW out\n", bgp)
        self.assertIn("      network 10.24.160.0/21\n", bgp)
        self.assertNotIn("vxlan vlan", cfg)                                       # attachment VLANs are local to the pair
        for vrf, vni in (("TRANSIT", 50100), ("PROD", 50001), ("PARTNER", 50031)):
            self.assertIn(f"   vxlan vrf {vrf} vni {vni}\n", cfg)

    def test_border_leaf(self):
        cfg = eos("DC3-BL2")
        bgp = cfg.split("\nrouter bgp 65371\n", 1)[1]
        for peer, asn, att in (("10.27.16.11", 65340, "DC3-TRANSIT-INETFW"), ("10.27.16.19", 65320, "DC3-TRANSIT-SDWANFW"),
                               ("10.27.16.27", 65370, "DC3-TRANSIT-CLOUD1"), ("10.27.16.35", 65370, "DC3-TRANSIT-CLOUD2"),
                               ("10.27.9.3", 65330, "DC3-PARTNER-VPNFW")):
            self.assertIn(f"      neighbor {peer} remote-as {asn}\n      neighbor {peer} description {att}\n", bgp)
        self.assertIn("neighbor 10.27.16.19 route-map RM-TRANSIT-TO-SDWAN out", bgp)
        self.assertIn("neighbor 10.27.16.27 route-map RM-TRANSIT-FROM-CLOUD in", bgp)
        self.assertIn("neighbor 10.27.9.3 route-map RM-PARTNER-FROM-FW in", bgp)
        self.assertIn("      maximum-paths 2\n", bgp)
        self.assertEqual(R.block(cfg, "ip prefix-list PL-PARTNER-NAT"), ["seq 10 permit 10.24.160.0/21 ge 28 le 28"])
        self.assertEqual(R.block(cfg, "ip prefix-list PL-TRANSIT-SDWAN-OUT"), [
            "seq 10 permit 10.24.144.0/20", "seq 20 permit 10.24.64.0/18", "seq 30 permit 10.24.0.0/18"])
        self.assertEqual(R.block(cfg, "route-map RM-TRANSIT-TO-SDWAN permit 20"), ["match ip address prefix-list PL-CLOUD"])
        self.assertEqual(R.block(cfg, "interface Port-Channel31")[2], "switchport trunk allowed vlan 3003")
        self.assertEqual(R.block(cfg, "interface Port-Channel11")[2], "switchport trunk allowed vlan 3001")
        self.assertEqual(R.block(cfg, "interface Port-Channel15")[2], "switchport trunk allowed vlan 3300")
        self.assertEqual(R.block(cfg, "interface Ethernet13/1"), ["description DC3-CLOUD1 Gi0-0-0-1", "no shutdown",
                                                                  "channel-group 31 mode active"])
        self.assertNotIn("PL-PROD", cfg)

    def test_edge_and_dmz(self):
        cfg = eos("DC3-EDGE1")
        self.assertEqual(R.block(cfg, "vlan 3200"), ["name DC3-EDGE-OUTSIDE"])
        for po, far in ((11, "DC3-INETFW1 agg-outside"), (13, "DC3-SDWANFW1 agg-outside"), (15, "DC3-VPNFW1 agg-outside"),
                        (21, "DC3-ISP1 BE21"), (22, "DC3-ISP2 BE21")):
            self.assertEqual(R.block(cfg, f"interface Port-Channel{po}"), [
                f"description {far}", "switchport mode trunk", "switchport trunk allowed vlan 3200", f"mlag {po}",
                "spanning-tree portfast", "spanning-tree bpduguard enable"])
        self.assertEqual(R.block(cfg, "interface Ethernet5/1"), ["description DC3-ISP1 Gi0-0-0-0", "no shutdown", "channel-group 21 mode active"])
        dmz = eos("DC3-DMZ2")
        self.assertEqual(R.block(dmz, "vlan 1400"), ["name DC3-DMZ-LB-VIP"])
        self.assertEqual(R.block(dmz, "interface Port-Channel11")[2], "switchport trunk allowed vlan 1400-1415")
        self.assertEqual(R.block(dmz, "interface Ethernet49/1"), ["description DC3-INETFW1 port16", "no shutdown", "channel-group 11 mode active"])
        for c in (cfg, dmz):
            self.assertNotIn("router bgp", c)
            self.assertNotIn("\nip routing", c)


class FrrRenderTest(unittest.TestCase):
    STANDINS = ("DC3-INTFW1", "DC3-INETFW1", "DC3-SDWANFW1", "DC3-VPNFW1", "DC3-ISP1", "DC3-ISP2", "DC3-CLOUD1", "DC3-CLOUD2")

    def scripts(self):
        out = {}
        for name in self.STANDINS:
            for art, text in artifacts_for(name).items():
                out[(name, art)] = text
        out[("DC3", "cloud-provider-lab-init")] = render("dc3_frr_cloud_provider", "DC3")
        return out

    def test_every_script_renders_and_parses(self):
        scripts = self.scripts()
        self.assertEqual(len(scripts), 8 + 2 + 1 + 1 + 1)   # 8 stand-ins + 2 carriers + branches + partner + providers
        for key, text in scripts.items():
            self.assertTrue(text.startswith("#!/bin/sh\n"), key)
            for bad in ("None", "{{", "{%", "{#", "password", "allowas-in", "as-override"):
                self.assertNotIn(bad, text, (key, bad))
            r = subprocess.run(["sh", "-n"], input=text, text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, (key, r.stderr))

    def test_addresses_well_formed(self):
        """Every `ip addr` / `ip route` in every FRR script is well formed: one mask, bare next hop,
        next hop not one of the script's own addresses."""
        scripts = {**{k: v for k, v in self.scripts().items()}}
        for key, text in scripts.items():
            parts = re.split(r"^(\S+)\)$", text, flags=re.M)        # case branches: own addresses per node
            for chunk in parts:
                own = set()
                for m in re.finditer(r"ip addr (?:replace|add) (\S+)", chunk):
                    ipaddress.ip_interface(m.group(1))                  # raises on 198.18.100.1/32/31
                    self.assertEqual(m.group(1).count("/"), 1, (key, m.group(0)))
                    own.add(m.group(1).split("/")[0])
                for m in re.finditer(r"ip route (?:replace )?(\S+) (?:via )?(\S+)", chunk):
                    dst, nh = m.groups()
                    if dst == "del":                                     # `ip route del default dev eth0`
                        continue
                    if dst != "default":
                        ipaddress.ip_network(dst)
                    if nh in ("blackhole", "dev") or nh.startswith("vrf"):
                        continue
                    self.assertNotIn("/", nh, (key, m.group(0)))
                    ipaddress.ip_address(nh)
                    self.assertNotIn(nh, own, (key, m.group(0), "next hop is an own address"))

    def test_internal_fw_aspath_fix(self):
        """Route leaking between VRFs, with `set as-path exclude all` on every TO-LEAF route-map."""
        s = artifacts_for("DC3-INTFW1")["frr-lab-init"]
        conf = frr_conf(s)
        self.assertIn("ip addr replace 10.27.8.3/29 dev PROD-3100", s)
        self.assertIn("ip link set PROD-3100 master vrf10", s)
        self.assertIn("router bgp 65310 vrf vrf10\n bgp router-id 10.27.16.3\n", conf)
        self.assertIn(" neighbor 10.27.8.1 remote-as 65372\n neighbor 10.27.8.1 description DC3-SL1\n", conf)
        self.assertIn("ip prefix-list PL-PROD-SUMMARY seq 10 permit 10.24.0.0/18\n", conf)
        self.assertIn("route-map RM-PROD-FROM-LEAF permit 10\n match ip address prefix-list PL-PROD-SUMMARY\n", conf)
        self.assertIn("route-map RM-PROD-TO-LEAF permit 10\n match ip address prefix-list PL-DEFAULT\n set as-path exclude all\n", conf)
        self.assertIn("route-map RM-TRANSIT-TO-LEAF permit 10\n match ip address prefix-list PL-TRANSIT-SUMMARY\n"
                      " set as-path exclude all\n", conf)
        self.assertEqual(conf.count("set as-path exclude all"), 5)              # 4 tenant VRFs + TRANSIT
        self.assertIn("route-map RM-LEAK-TO-TRANSIT permit 40\n match ip address prefix-list PL-PROD-SUMMARY\n", conf)
        self.assertIn("route-map RM-LEAK-TRANSIT-TO-PROD permit 10\n match ip address prefix-list PL-DEFAULT\n", conf)
        for absent in ("aggregate-address", "default-originate", "COMPONENTS", "prepend", "as-override", "allowas-in"):
            self.assertNotIn(absent, conf)
        transit = conf.split("router bgp 65310 vrf vrf1\n", 1)[1].split("\nexit\n", 1)[0]
        self.assertIn("  import vrf vrf10\n", transit)
        self.assertNotIn("10.16.", conf)

    def test_internet_fw_isp_carrier(self):
        s = artifacts_for("DC3-INETFW1")["frr-lab-init"]
        conf = frr_conf(s)
        self.assertIn(" neighbor 10.27.16.9 remote-as 65371\n", conf)
        self.assertIn(" neighbor 10.27.14.2 remote-as 65050\n", conf)
        self.assertIn(" neighbor 10.27.14.3 remote-as 65050\n", conf)
        self.assertIn("ip prefix-list PL-PUBLIC seq 10 permit 198.18.3.0/24", conf)
        self.assertIn("--to-source 198.18.3.1-198.18.3.126", s)
        self.assertEqual(s.count("dev DMZ-14"), 16)
        isp = artifacts_for("DC3-ISP2")
        conf = frr_conf(isp["frr-lab-init"])
        self.assertIn(" neighbor 192.0.2.21 remote-as 64497\n", conf)
        self.assertIn(" neighbor 10.27.15.0 remote-as 65050\n", conf)
        self.assertIn(" neighbor 10.27.14.10 remote-as 65340\n", conf)
        self.assertIn("ip route 198.18.3.144/28 10.27.14.11\n", conf)
        self.assertIn("ip route 198.18.3.128/28 10.27.14.12\n", conf)
        self.assertIn("ip route 198.18.3.0/25 10.27.14.10\n", conf)
        self.assertIn("ip route 198.18.3.160/27 10.27.14.10\n", conf)
        carrier = isp["carrier-lab-init"]
        self.assertIn("hostname DC3-ISP-B\n", carrier)
        self.assertIn("ip addr replace 192.0.2.21/30 dev isp", carrier)
        self.assertIn("router bgp 64497\n", carrier)
        self.assertIn(" neighbor 192.0.2.22 remote-as 65050\n", carrier)
        self.assertIn("  network 198.18.2.0/24\n", carrier)

    def test_sdwan_and_branches(self):
        arts = artifacts_for("DC3-SDWANFW1")
        conf = frr_conf(arts["frr-lab-init"])
        self.assertIn("router bgp 65400 vrf vrf1\n", conf)
        self.assertIn(" neighbor 10.27.16.17 remote-as 65371\n neighbor 10.27.16.17 local-as 65320 no-prepend replace-as\n", conf)
        self.assertIn(" bgp listen range 10.79.16.0/21 peer-group BRANCHES\n", conf)
        self.assertIn("  aggregate-address 10.64.0.0/12\n", conf)
        self.assertIn("  aggregate-address 172.16.0.0/13 summary-only\n", conf)
        self.assertIn("ip addr replace 10.79.16.1/21 dev ADVPN-HUB", arts["frr-lab-init"])
        self.assertIn("ip addr replace 198.18.3.145/32 dev LO-WAN", arts["frr-lab-init"])
        self.assertIn("ip route 0.0.0.0/0 10.27.14.1\n", conf)
        br = arts["branch-lab-init"]
        self.assertIn("DC3-BR001)\n", br)
        self.assertIn("ip addr replace 10.79.16.2/21 dev eth1", br)
        self.assertIn("ip addr replace 10.79.16.3/21 dev eth1", br)
        self.assertIn("ip addr replace 10.64.2.1/24 dev lan", br)
        self.assertIn(" neighbor 10.79.16.1 remote-as 65400\n", br)

    def test_vpn_fw_and_partner(self):
        arts = artifacts_for("DC3-VPNFW1")
        s = arts["frr-lab-init"]
        conf = frr_conf(s)
        self.assertIn("router bgp 65330 vrf vrf16\n", conf)
        self.assertIn("  network 10.24.160.0/28\n", conf)
        self.assertIn(" ip route 10.24.160.0/28 blackhole\n", conf)
        self.assertIn(" ip route 192.168.100.0/24 198.18.100.1\n", conf)
        self.assertIn("ip addr replace 198.18.100.0/31 dev PARTNER01", s)
        self.assertIn("-s 192.168.100.0/24 -o PARTNER-3300 -j SNAT --to-source 10.24.160.0-10.24.160.15", s)
        self.assertNotIn("10.16.160.0/28", s)
        p = arts["partner-lab-init"]
        self.assertIn("DC3-PARTNER01)\n", p)
        self.assertIn("ip addr replace 198.18.100.1/31 dev eth1", p)
        self.assertIn("ip route replace default via 198.18.100.0 dev eth1", p)

    def test_cloud_router_and_providers(self):
        conf = frr_conf(artifacts_for("DC3-CLOUD2")["frr-lab-init"])
        self.assertIn(" neighbor 169.254.121.1 remote-as 65500\n", conf)
        self.assertIn(" neighbor 10.98.0.22 remote-as 12076\n", conf)
        self.assertIn(" neighbor 10.27.13.16 remote-as 65370\n", conf)
        self.assertIn(" neighbor 10.27.16.33 remote-as 65371\n", conf)
        self.assertIn("  aggregate-address 10.24.0.0/16\n", conf)
        self.assertIn("ip prefix-list PL-TO-CLOUD seq 30 permit 10.24.160.0/21\n", conf)
        prov = render("dc3_frr_cloud_provider", "DC3")
        aws = prov.split("DC3-AWS)\n", 1)[1].split(";;", 1)[0]
        azure = prov.split("DC3-AZURE)\n", 1)[1].split(";;", 1)[0]
        self.assertIn("ip addr replace 169.254.120.1/30 dev eth1.120", aws)
        self.assertIn("ip addr replace 169.254.121.1/30 dev eth2.121", aws)
        self.assertIn("router bgp 65500\n", aws)
        self.assertIn("  network 172.16.0.0/13\n", aws)
        self.assertIn("ip addr replace 10.98.0.18/30 dev eth1.220", azure)
        self.assertIn("ip addr replace 10.98.0.22/30 dev eth2.220", azure)
        self.assertIn("router bgp 12076\n", azure)
        self.assertIn("  network 172.30.0.0/15\n", azure)


class SessionsAgreeTest(unittest.TestCase):
    """Every EOS VRF neighbor toward a stand-in has a matching FRR neighbor (address and AS both ways)."""

    def test_both_ends(self):
        frr = {}
        for name in FrrRenderTest.STANDINS:
            conf = frr_conf(artifacts_for(name)["frr-lab-init"])
            local_as = None
            for line in conf.splitlines():
                m = re.match(r"router bgp (\d+)", line)
                if m:
                    local_as = int(m.group(1))
                m = re.match(r" neighbor (\S+) remote-as (\d+)$", line)
                if m:
                    frr[(name, m.group(1))] = int(m.group(2))
                m = re.match(r" neighbor (\S+) local-as (\d+)", line)
                if m:
                    frr[(name, m.group(1), "local")] = int(m.group(2))
            frr[(name, "as")] = local_as
        checked = 0
        for name, d in G.dev.items():
            if d.data["role"] not in ("border_leaf", "service_leaf"):
                continue
            cfg = eos(name)
            my_as = int(re.search(r"\nrouter bgp (\d+)\n", cfg).group(1))
            my_ips = {ip(G.addr[a]) for i in G.st["InterfaceVirtual"].values() if i.data["device"] == d.id
                      for a in i.data.get("ip_addresses") or []}
            for peer, ras in re.findall(r"\n      neighbor (\S+) remote-as (\d+)\n", cfg):
                standin = next(a for a in G.st["FabricAttachment"].values()
                               if ipaddress.ip_address(peer) in ipaddress.ip_network(G.prefix_by_id[a.data["prefix"]]))
                sname = G.dev_by_id[standin.data["devices"][0]].data["name"]
                mine = next(x for x in my_ips if ipaddress.ip_address(x) in ipaddress.ip_network(G.prefix_by_id[standin.data["prefix"]]))
                self.assertEqual(frr[(sname, mine)], my_as, (name, sname))
                self.assertEqual(frr.get((sname, mine, "local"), frr[(sname, "as")]), int(ras), (name, sname))
                checked += 1
        self.assertEqual(checked, 2 * 5 + 2 * 5)                                 # SL1/SL2 x 5 VRFs, BL1/BL2 x 5 attachments


class ObjectFilesTest(unittest.TestCase):
    def test_vrfs_dc3_only(self):
        """objects/dc3-06: sites DC3 only, DC3 summaries only."""
        self.assertEqual(set(G.vrf_objects), P.DC3_VRFS)
        for name, ours in G.vrf_objects.items():
            self.assertEqual(ours["sites"], ["DC3"], name)
            self.assertEqual([p for p in ours.get("summaries", []) if not p[1].startswith("10.24.")], [], name)

    def test_segments_dc3_only(self):
        for ours in G.segments:
            self.assertEqual(ours["sites"], ["DC3"])
            self.assertTrue(ours["prefix"][1].startswith("10.24."), ours["name"])
            self.assertTrue(ours["name"].startswith("DC3-") or "-DC3-" in ours["name"], ours["name"])
        self.assertEqual(len({x["name"] for x in G.segments}), len(G.segments))
        seg_prefixes = [x["prefix"] for d in objects("dc3-07-segments.yml") if d["spec"]["kind"] == "IpamPrefix" for x in d["spec"]["data"]]
        self.assertEqual(sorted(seg_prefixes), sorted(x["prefix"][1] for x in G.segments))

    def test_repo_config(self):
        groups = {x["name"] for f in ("dc3-00-groups.yml", "dc3-08-standins.yml") for d in objects(f)
                  if d["spec"]["kind"] == "CoreStandardGroup" for x in d["spec"]["data"]}
        queries = {q["name"]: q["file_path"] for q in INFRAHUB["queries"]}
        for a in ARTIFACTS:
            self.assertIn(a["targets"], groups, a["name"])
            self.assertIn(a["transformation"], TRANSFORMS, a["name"])
            self.assertTrue(a["name"].startswith("dc3_"), a["name"])
        for t in TRANSFORMS.values():
            self.assertTrue(t["name"].startswith("dc3_") and (ROOT / t["template_path"]).exists(), t["name"])
            self.assertIn(t["query"], queries)
        for name, path in queries.items():
            text = "\n".join(line.split("#", 1)[0] for line in (ROOT / path).read_text().splitlines())
            self.assertEqual(text.count("{"), text.count("}"), name)
            self.assertEqual(text.count("("), text.count(")"), name)
        for g in INFRAHUB["generator_definitions"]:
            self.assertIs(g["execute_in_proposed_change"], False)
            self.assertIs(g["execute_after_merge"], False)
        self.assertEqual(set(P.STANDIN_GROUPS.values()) - groups, set())


class TopologyStage3Test(unittest.TestCase):
    def test_topology_with_standins(self):
        spec = importlib.util.spec_from_file_location("dc3_lab_topology_s3", ROOT / "scripts" / "dc3_lab_topology.py")
        lab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lab)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "configs").mkdir()
            (tmp / "configs" / "DC3-INTFW1.sh").write_text(artifacts_for("DC3-INTFW1")["frr-lab-init"])
            topo = lab.write(tmp)
        t = topo["topology"]
        nodes, links = t["nodes"], t["links"]
        self.assertEqual(len(nodes), 30 + 8 + len(lab.LAB_ONLY_NODES))
        ends = [e for l in links for e in l["endpoints"]]
        self.assertEqual(len(ends), len(set(ends)), "an interface used twice")
        self.assertEqual(t["kinds"]["linux"]["image"], "quay.io/frrouting/frr:10.6.0")
        self.assertEqual(nodes["DC3-INTFW1"]["kind"], "linux")
        self.assertEqual(nodes["DC3-INTFW1"]["mgmt-ipv4"], ip(G.addr[G.dev["DC3-INTFW1"].data["primary_address"]]))
        for a, b in (("DC3-SL1:eth7", "DC3-INTFW1:port19"), ("DC3-SL2:eth7", "DC3-INTFW1:port20"),
                     ("DC3-BL1:eth13", "DC3-CLOUD1:Gi0-0-0-0"), ("DC3-EDGE2:eth5", "DC3-ISP1:Gi0-0-0-1"),
                     ("DC3-DMZ1:eth49", "DC3-INETFW1:port15"), ("DC3-ISP1:Gi0-0-0-2", "DC3-ISP2:Gi0-0-0-2"),
                     ("DC3-ISP1:Gi0-0-0-3", "DC3-ISP-A:isp"), ("DC3-ISP-B:host", "DC3-HOST-ISP-B:eth1"),
                     ("DC3-SDWANFW1:BR001", "DC3-BR001:eth1"), ("DC3-VPNFW1:PARTNER01", "DC3-PARTNER01:eth1"),
                     ("DC3-CLOUD1:Hu0-0-0-16", "DC3-XC1:eth1"), ("DC3-AWS:eth2", "DC3-XC2:eth2"),
                     ("DC3-L11:eth1", "DC3-HOST-PROD:eth1"), ("DC3-L11:eth3", "DC3-HOST-CITRIX:eth1")):
            self.assertIn({"endpoints": [a, b]}, links)
        attach = [l for l in links if any(e.split(":")[0] in {s for s, *_ in lab.standin_rows()} for e in l["endpoints"])
                  and any(e.split(":")[0] in {r["name"] for r in P.build_plan(SITE, 1, 4, 10)} for e in l["endpoints"])]
        self.assertEqual(len(attach), 24)                                       # every attach port of the generator
        host = nodes["DC3-HOST-PROD"]
        self.assertIn("ip addr add 10.24.10.10/24 dev eth1", host["exec"])
        self.assertIn("ip route replace default via 10.24.10.1 dev eth1", host["exec"])
        self.assertEqual(nodes["DC3-INTFW1"]["exec"], ["sh /frr-init.sh"])
        self.assertIn("configs/DC3-INTFW1.sh:/frr-init.sh:ro", nodes["DC3-INTFW1"]["binds"])
        self.assertIn("configs/DC3-ISP1.carrier.sh:/frr-init.sh:ro", nodes["DC3-ISP-A"]["binds"])


if __name__ == "__main__":
    unittest.main()
