"""Unit tests of the DC3 plan (generators/dc3_plan.py). No Infrahub needed.

Run: python3 -m pytest tests/      (or, without pytest: python3 -m unittest discover -s tests -v)
"""
import importlib.util
import re
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "generators"))
import dc3_plan as P  # noqa: E402

SITE, VPC, ESI, SINGLE = "DC3", 1, 4, 10


def load_script(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_asn_table() -> dict:
    """plans/dc3-values.md, section ASNs -> {device short name: asn}."""
    text = (ROOT / "plans" / "dc3-values.md").read_text()
    section = text.split("## ASNs", 1)[1].split("\n## ", 1)[0]
    out = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 2 or not re.search(r"\d{5}", cells[1]):
            continue
        devices, asn = cells
        m = re.match(r"(\d+)\s*\.\.\s*(\d+)", asn)
        if m:                                         # "L3 .. L10 (...)" | "65374 .. 65381": one ASN per device
            d = re.match(r"L(\d+)\s*\.\.\s*L(\d+)", devices)
            first, last = int(d.group(1)), int(d.group(2))
            asns = range(int(m.group(1)), int(m.group(2)) + 1)
            self_check = list(range(first, last + 1))
            assert len(self_check) == len(asns), line
            for n, a in zip(self_check, asns):
                out[f"L{n}"] = a
        elif devices.startswith("Spines"):            # "Spines S1, S2" | 65300
            for s in re.findall(r"S\d+", devices):
                out[s] = int(asn)
        else:                                         # "BL1-BL2 (MLAG)" | 65371: pair shares one ASN
            a, b = re.match(r"(\w+?\d+)-(\w+?\d+)", devices).groups()
            out[a] = out[b] = int(asn)
    return out


class Dc3PlanTest(unittest.TestCase):
    def setUp(self):
        self.plan = P.build_plan(SITE, VPC, ESI, SINGLE)
        self.links = P.build_links(SITE, self.plan)
        self.pairs = P.build_pairs(SITE, self.plan, VPC, ESI)
        self.domains = P.mlag_domains(self.plan, self.pairs)
        self.expected = P.expected_interfaces(self.plan, self.links, self.domains)
        self.rows = {r["name"]: r for r in self.plan}

    # ---- devices
    def test_30_devices_with_decided_names(self):
        names = [r["name"] for r in self.plan]
        want = (["DC3-S1", "DC3-S2"] + [f"DC3-L{n}" for n in range(1, 21)]
                + ["DC3-BL1", "DC3-BL2", "DC3-SL1", "DC3-SL2", "DC3-EDGE1", "DC3-EDGE2", "DC3-DMZ1", "DC3-DMZ2"])
        self.assertEqual(len(self.plan), 30)
        self.assertEqual(names, want)
        self.assertFalse([n for n in names if "BGW" in n or "DCI" in n])

    def test_models_and_platform(self):
        want = {"spine": "DCS-7060DX5-64S", "leaf": "DCS-7050SX3-48YC8C", "dmz": "DCS-7050SX3-48YC8C",
                "border_leaf": "DCS-7280DR3-24", "service_leaf": "DCS-7280DR3-24", "edge": "DCS-7050DX4-32S"}
        for r in self.plan:
            self.assertEqual(r["model"], want[r["role"]], r["name"])
            self.assertEqual(r["platform"], "Arista EOS")
        self.assertEqual(P.OS_VERSION, "4.36.2F")

    # ---- ASNs
    def test_asn_table_equals_plans_dc3_values(self):
        table = parse_asn_table()
        got = {r["short"]: r["asn"] for r in self.plan if r["asn"] is not None}
        self.assertEqual(got, table)
        for r in self.plan:
            if r["role"] in ("edge", "dmz"):
                self.assertIsNone(r["asn"], r["name"])

    def test_asn_objects_cover_the_plan(self):
        docs = list(yaml.safe_load_all((ROOT / "objects" / "dc3-05-asns.yml").read_text()))
        objs = {e["asn"]: e for d in docs for e in d["spec"]["data"]}
        for r in self.plan:
            if r["asn"] is not None:
                self.assertIn(r["asn"], objs, r["name"])
        self.assertEqual(objs[65300].get("fabric_site"), "DC3")
        for shared in (65050, 64496, 64497, 65400, 65500, 12076):    # defined in dc3-01-bootstrap.yml, not here
            self.assertNotIn(shared, objs)
        for standin in (65310, 65320, 65330, 65340, 65370):
            self.assertIn(standin, objs)

    # ---- interface names
    def test_every_interface_name_matches_the_naming_rule(self):
        fixed = {P.MGMT_IF, P.LOOPBACK0, P.LOOPBACK1, P.LOOPBACK2, P.PEER_LINK, P.MLAG_SVI}
        for device, names in self.expected.items():
            model = self.rows[device]["model"]
            for name in names:
                if name in fixed:
                    continue
                m = re.fullmatch(r"Ethernet(\d+)(/1)?", name)
                self.assertIsNotNone(m, f"{device} {name}")
                self.assertEqual(name, P.eos_port(model, int(m.group(1))), f"{device} {name}")

    def test_naming_rule_per_model(self):
        self.assertEqual(P.eos_port("DCS-7060DX5-64S", 1), "Ethernet1/1")
        self.assertEqual(P.eos_port("DCS-7060DX5-64S", 64), "Ethernet64/1")
        self.assertEqual(P.eos_port("DCS-7050SX3-48YC8C", 48), "Ethernet48")
        self.assertEqual(P.eos_port("DCS-7050SX3-48YC8C", 49), "Ethernet49/1")
        self.assertEqual(P.eos_port("DCS-7050SX3-48YC8C", 56), "Ethernet56/1")
        self.assertEqual(P.eos_port("DCS-7280DR3-24", 24), "Ethernet24/1")
        self.assertEqual(P.eos_port("DCS-7050DX4-32S", 32), "Ethernet32/1")
        self.assertEqual(P.eos_port("DCS-7050DX4-32S", 34), "Ethernet34")
        for model, n in (("DCS-7280DR3-24", 25), ("DCS-7050SX3-48YC8C", 57), ("DCS-7050DX4-32S", 35), ("DCS-7060DX5-64S", 65)):
            with self.assertRaises(ValueError):
                P.eos_port(model, n)

    def test_mlag_ports(self):
        d = {x["name"]: x for x in self.domains}
        self.assertEqual(d["DC3-MLAG-L1-L2"]["keepalive"], "Ethernet48")
        self.assertEqual(d["DC3-MLAG-DMZ1-DMZ2"]["keepalive"], "Ethernet48")
        self.assertEqual(d["DC3-MLAG-BL1-BL2"]["keepalive"], "Ethernet24/1")
        self.assertEqual(d["DC3-MLAG-SL1-SL2"]["keepalive"], "Ethernet24/1")
        self.assertEqual(d["DC3-MLAG-EDGE1-EDGE2"]["keepalive"], "Ethernet34")
        self.assertEqual(d["DC3-MLAG-L1-L2"]["peer_link"], ("Ethernet53/1", "Ethernet54/1"))
        self.assertEqual(d["DC3-MLAG-BL1-BL2"]["peer_link"], ("Ethernet3/1", "Ethernet4/1", "Ethernet5/1", "Ethernet6/1"))
        self.assertEqual(d["DC3-MLAG-EDGE1-EDGE2"]["peer_link"], ("Ethernet1/1", "Ethernet2/1", "Ethernet3/1", "Ethernet4/1"))
        self.assertTrue(all(x["backup"] for x in self.domains))                             # Vlan3900 on every pair
        self.assertEqual(P.PEER_LINK, "Port-Channel1000")
        self.assertEqual(P.MLAG_SVI, "Vlan3900")

    def test_loopbacks_and_management(self):
        for r in self.plan:
            names = self.expected[r["name"]]
            self.assertIn("Management1", names)
            self.assertEqual("Loopback0" in names, r["role"] not in ("edge", "dmz"), r["name"])
            self.assertEqual("Loopback1" in names, r["role"] in ("leaf", "border_leaf", "service_leaf"), r["name"])

    def test_g008_loopback2_on_mlag_vtep_members(self):
        # own VTEP address on Loopback2 for L1/L2, BL, SL; it keeps the Loopback1 identifier
        mlag_vtep = P.mlag_vtep_members(self.pairs)
        self.assertEqual(mlag_vtep, {"DC3-L1", "DC3-L2", "DC3-BL1", "DC3-BL2", "DC3-SL1", "DC3-SL2"})
        for r in self.plan:
            names = self.expected[r["name"]]
            self.assertEqual("Loopback2" in names, r["name"] in mlag_vtep, r["name"])
            lo = {ifname: (key, ident) for key, ifname, _d, ident in P.device_loopbacks(r, mlag_vtep)}
            if r["role"] in ("leaf", "border_leaf", "service_leaf"):
                own = "Loopback2" if r["name"] in mlag_vtep else "Loopback1"
                self.assertEqual(lo[own], ("loopback1", f"{r['name']}-Loopback1"))
                self.assertEqual(set(lo), {"Loopback0", own})
        self.assertEqual(P.LOOPBACK2_DESCRIPTION, "VTEP per-switch address")

    # ---- links
    def test_link_count(self):
        # 2 spines x (20 server leaves x 2 + BL1, BL2, SL1, SL2) = 88; DC3 has no BGW or DCI.
        self.assertEqual(len(self.links), 2 * (20 * 2 + 4))
        self.assertEqual(len(self.links), 88)
        self.assertTrue(all(l["spine_port"] not in ("Ethernet45/1", "Ethernet46/1") for l in self.links))

    # ---- pairs (slot rule, base 300)
    def test_pairs_and_domain_ids(self):
        mlag = {p["name"]: p["domain_id"] for p in self.pairs if p["kind"] == "FabricVpcDomain"}
        self.assertEqual(mlag, {"DC3-MLAG-L1-L2": 301, "DC3-MLAG-BL1-BL2": 302, "DC3-MLAG-SL1-SL2": 303,
                                "DC3-MLAG-EDGE1-EDGE2": 304, "DC3-MLAG-DMZ1-DMZ2": 305})
        esi = [p["name"] for p in self.pairs if p["kind"] == "FabricEsiPair"]
        self.assertEqual(esi, ["DC3-ESI-L3-L4", "DC3-ESI-L5-L6", "DC3-ESI-L7-L8", "DC3-ESI-L9-L10"])
        anycast = sorted(p["name"] for p in self.pairs if p["nve"] and p["domain_id"])
        self.assertEqual(anycast, ["DC3-MLAG-BL1-BL2", "DC3-MLAG-L1-L2", "DC3-MLAG-SL1-SL2"])

    def test_groups(self):
        g = P.group_members(self.plan)
        self.assertEqual(g["dc3-spines"], ["DC3-S1", "DC3-S2"])
        self.assertEqual(len(g["dc3-server-leaves"]), 20)
        self.assertEqual(g["dc3-border-service-leaves"], ["DC3-BL1", "DC3-BL2", "DC3-SL1", "DC3-SL2"])
        self.assertEqual(g["dc3-l2-pairs"], ["DC3-EDGE1", "DC3-EDGE2", "DC3-DMZ1", "DC3-DMZ2"])
        groups = {e["name"] for d in yaml.safe_load_all((ROOT / "objects" / "dc3-00-groups.yml").read_text())
                  for e in d["spec"]["data"]}
        self.assertTrue(set(g) <= groups)
        self.assertIn("dc3-sites", groups)

    # ---- objects, pools, repository config
    def test_site_object(self):
        site = next(yaml.safe_load_all((ROOT / "objects" / "dc3-02-site.yml").read_text()))["spec"]["data"][0]
        self.assertEqual((site["vpc_leaf_pairs"], site["esi_leaf_pairs"], site["single_leaves"]), (VPC, ESI, SINGLE))
        self.assertEqual(site["member_of_groups"], ["dc3-sites"])

    def test_pools_sit_on_ip_plan_prefixes(self):
        pools = load_script(ROOT / "scripts" / "dc3_create_pools.py", "dc3_create_pools").POOLS
        plan_prefixes = {e["prefix"]: e for d in yaml.safe_load_all((ROOT / "objects" / "dc3-03-ip-plan.yml").read_text())
                         if d["spec"]["kind"] == "IpamPrefix" for e in d["spec"]["data"]}
        self.assertEqual({p["name"] for p in pools}, {P.pool_name(SITE, k) for k in P.POOL_SUFFIX})
        for p in pools:
            self.assertIn(p["prefix"], plan_prefixes, p["name"])
            self.assertEqual(plan_prefixes[p["prefix"]].get("site"), "DC3", p["name"])

    def test_pool_script_refuses_main(self):
        mod = load_script(ROOT / "scripts" / "dc3_create_pools.py", "dc3_create_pools")
        for bad in (None, "", "main", " main "):
            with self.assertRaises(SystemExit):
                mod.check_branch(bad)
        self.assertEqual(mod.check_branch("dc3"), "dc3")

    def test_infrahub_yml_names_use_dc3_prefix(self):
        cfg = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
        for section in ("queries", "generator_definitions"):
            for item in cfg[section]:
                self.assertTrue(item["name"].startswith("dc3_"), item["name"])
        self.assertEqual(cfg["generator_definitions"][0]["targets"], "dc3-sites")

    def test_all_yaml_parses(self):
        for f in list((ROOT / "objects").glob("*.yml")) + [ROOT / ".infrahub.yml"]:
            list(yaml.safe_load_all(f.read_text()))


if __name__ == "__main__":
    unittest.main()
