"""Lab tests: lab intent parser, post-check judging, outage measurement, failure-test catalog, smoke subset topology.

No lab, no docker: the configs and stand-in scripts are rendered from the fake store of tests/test_dc3_stage3.py
into a temporary lab folder; the post-check runs against a fake runner that answers with JSON shaped as the post-check
expects (field names until the smoke test).
"""
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "lab"))
sys.path.insert(0, str(ROOT / "scripts"))
import test_dc3_stage3 as T  # noqa: E402  (fake store and renders)
import dc3_intent as I  # noqa: E402
import dc3_postcheck as PC  # noqa: E402
import dc3_failtests as F  # noqa: E402
import dc3_lab_topology as L  # noqa: E402

SMOKE = ("DC3-S1", "DC3-S2", "DC3-L1", "DC3-L2", "DC3-L11", "DC3-HOST-PROD")
ARTIFACT_FILE = {"startup-config": "{d}.cfg", "frr-lab-init": "{d}.sh", "carrier-lab-init": "{d}.carrier.sh",
                 "branch-lab-init": "{d}.branch.sh", "partner-lab-init": "{d}.partner.sh"}


def render_lab(out: Path) -> Path:
    """Every rendered file of the fake store in lab/configs/ layout, then both topologies."""
    (out / "configs").mkdir(parents=True, exist_ok=True)
    for name in T.G.dev:
        for art, text in T.artifacts_for(name).items():
            (out / "configs" / ARTIFACT_FILE[art].format(d=name)).write_text(text)
    (out / "configs" / "DC3.cloud-provider.sh").write_text(T.render("dc3_frr_cloud_provider", "DC3"))
    L.write(out)
    L.write(out, subset="smoke")
    return out


TMP = tempfile.TemporaryDirectory()
LABDIR = render_lab(Path(TMP.name) / "lab")
FULL = I.build_intent(LABDIR)
SMOKE_INTENT = I.build_intent(LABDIR, LABDIR / "dc3-smoke.clab.yml")


class IntentParserTest(unittest.TestCase):
    def test_yaml_subset_reader_equals_pyyaml(self):
        for f in ("dc3.clab.yml", "dc3-smoke.clab.yml"):
            text = (LABDIR / f).read_text()
            self.assertEqual(I.load_yaml_subset(text), yaml.safe_load(text), f)
        text = (ROOT / "lab" / "dc3.clab.yml").read_text()
        self.assertEqual(I.load_yaml_subset(text), yaml.safe_load(text))

    def test_eos_service_leaf(self):
        cfg = I.parse_eos((LABDIR / "configs" / "DC3-SL1.cfg").read_text())
        self.assertEqual(cfg["hostname"], "DC3-SL1")
        self.assertEqual(cfg["bgp"]["asn"], 65372)
        kinds = {}
        for n in cfg["bgp"]["neighbors"]:
            kinds.setdefault(n["kind"], []).append(n)
        self.assertEqual({k: len(v) for k, v in kinds.items()}, {"underlay": 2, "mlag": 1, "overlay": 2, "vrf": 5})
        self.assertTrue(all(n["remote_as"] == 65300 and n["bfd"] for n in kinds["underlay"] + kinds["overlay"]))
        self.assertTrue(all(n["af"] == "evpn" for n in kinds["overlay"]))
        self.assertEqual(kinds["mlag"][0]["remote_as"], 65372)
        self.assertFalse(kinds["mlag"][0]["bfd"])
        prod = next(n for n in kinds["vrf"] if n["vrf"] == "PROD")
        self.assertEqual((prod["ip"], prod["remote_as"], prod["bfd"]), ("10.27.8.3", 65310, True))
        self.assertEqual(I.route_map_prefixes(cfg, prod["route_map_in"]), ["0.0.0.0/0"])
        self.assertEqual(cfg["mlag"]["peer-address"], "10.27.3.5")
        self.assertEqual(cfg["mlag"]["heartbeat-interval"], "4000")
        self.assertEqual(cfg["bfd_multihop"], "interval 999 min-rx 999 multiplier 3")
        self.assertEqual(set(cfg["vxlan"]["vrfs"]), {"CITRIX", "DEV", "PARTNER", "PROD", "TRANSIT"})
        self.assertEqual(cfg["bgp"]["vrfs"]["PROD"]["aggregates"], ["10.24.0.0/18"])
        self.assertEqual(cfg["bgp"]["vrfs"]["PARTNER"]["networks"], ["10.24.160.0/21"])
        self.assertEqual(I.vtep_address(cfg), "10.27.2.3")                       # anycast VTEP on Loopback1

    def test_frr_internal_fw_and_multi_node_script(self):
        fw = I.parse_frr((LABDIR / "configs" / "DC3-INTFW1.sh").read_text(), "DC3-INTFW1")
        self.assertEqual(fw["hostname"], "DC3-INTFW1")
        self.assertEqual(len(fw["bgp"]), 5)
        vrf1 = next(b for b in fw["bgp"] if b["vrf"] == "vrf1")
        self.assertEqual(vrf1["asn"], 65310)
        self.assertEqual(sorted(vrf1["neighbors"]), ["10.27.16.1", "10.27.16.2"])
        self.assertTrue(all(n["bfd"] and n["remote_as"] == "65372" for n in vrf1["neighbors"].values()))
        text = (LABDIR / "configs" / "DC3.cloud-provider.sh").read_text()
        aws, azure = I.parse_frr(text, "DC3-AWS"), I.parse_frr(text, "DC3-AZURE")
        self.assertIn("169.254.120.1", [a["ip"] for a in aws["addresses"]])
        self.assertNotIn("169.254.120.1", [a["ip"] for a in azure["addresses"]])
        self.assertEqual((aws["bgp"][0]["asn"], azure["bgp"][0]["asn"]), (65500, 12076))

    def test_every_session_has_both_ends(self):
        checks = I.expected_checks(FULL)
        bgp = [c for c in checks if c["type"] == "bgp"]
        self.assertTrue(bgp)
        self.assertEqual([c["id"] for c in bgp if I.UNKNOWN in c["requires"]], [])
        ends = {(c["node"], c["peer_node"]) for c in bgp}
        for node, peer in ends:
            if node.startswith("DC3-BR"):                  # the SD-WAN hub accepts branches by listen range
                continue
            self.assertIn((peer, node), ends, f"{node} -> {peer} has no session back")

    def test_default_route_chain(self):
        req, anyg = I.default_chain(FULL, "PROD")
        self.assertEqual(req, {"DC3-INTFW1", "DC3-INETFW1"})
        self.assertEqual(anyg, [["DC3-ISP1", "DC3-ISP2"], ["DC3-ISP-A", "DC3-ISP-B"]])
        self.assertEqual(I.default_chain(FULL, "TRANSIT")[0], {"DC3-INETFW1"})
        self.assertEqual(I.internet_hosts(FULL), {"198.18.1.10": "DC3-HOST-ISP-A", "198.18.2.10": "DC3-HOST-ISP-B"})
        self.assertEqual(I.host_vrf(FULL, "DC3-HOST-PROD"), "PROD")

    def test_smoke_selection(self):
        checks = I.expected_checks(SMOKE_INTENT)
        judged = [c for c in checks if I.selected_ok(c, set(SMOKE))[0]]
        count = {}
        for c in judged:
            count[c["type"]] = count.get(c["type"], 0) + 1
        # 12 spine-leaf links x 2 ends underlay + 3 leaves x 2 spines x 2 ends overlay + MLAG iBGP both ways
        self.assertEqual(count, {"bgp": 24 + 12 + 2, "bfd": 24 + 12, "mlag": 2, "vxlan-up": 3, "vtep": 3, "flood": 3,
                                 "ping": 1})
        self.assertEqual(sorted(SMOKE_INTENT["lab_nodes"]), sorted(SMOKE))
        skipped = [c for c in checks if c["type"] == "route"]
        self.assertTrue(skipped and not any(I.selected_ok(c, set(SMOKE))[0] for c in skipped))


class FakeRunner:
    """Answers the post-check show commands for the smoke lab from the intent: everything up, BFD per argument."""

    prefix, docker = "clab-dc3-", "docker"

    def __init__(self, intent, bfd=True, down=()):
        self.intent, self.bfd, self.down = intent, bfd, set(down)

    def _cfg(self, node):
        return self.intent["nodes"][node]["cfg"]

    def eos_json(self, node, cmd):
        cfg = self._cfg(node)
        nb = cfg["bgp"]["neighbors"] if cfg["bgp"] else []
        st = lambda n: "Idle" if (node, n["ip"]) in self.down else "Established"  # noqa: E731
        if cmd == "show ip bgp summary vrf all":
            vrfs = {}
            for n in nb:
                if n["af"] == "ipv4":
                    vrfs.setdefault(n["vrf"], {"peers": {}})["peers"][n["ip"]] = {"peerState": st(n), "upDownTime": 1.0}
            return {"vrfs": vrfs}
        if cmd == "show bgp evpn summary":
            return {"vrfs": {"default": {"peers": {n["ip"]: {"peerState": st(n)} for n in nb if n["af"] == "evpn"}}}}
        if cmd == "show bfd peers":
            if not self.bfd:
                return {"vrfs": {}}
            vrfs = {}
            for n in nb:
                if n["bfd"]:
                    vrfs.setdefault(n["vrf"], {"ipv4Neighbors": {}})["ipv4Neighbors"][n["ip"]] = {
                        "peerStats": {"": {"status": "down" if (node, n["ip"]) in self.down else "up"}}}
            return {"vrfs": vrfs}
        if cmd == "show mlag":
            return {"state": "active", "negStatus": "connected", "peerLinkStatus": "up", "localIntfStatus": "up",
                    "configSanity": "consistent"} if cfg["mlag"] else {"state": "disabled"}
        if cmd == "show interfaces Vxlan1":
            return {"interfaces": {"Vxlan1": {"lineProtocolStatus": "up"}}}
        if cmd in ("show vxlan vtep", "show vxlan flood vtep"):
            others = [I.vtep_address(self._cfg(o)) for o in self.intent["lab_nodes"]
                      if self.intent["nodes"][o]["kind"] == "eos" and o != node and self._cfg(o)["vxlan"]]
            return {"vteps": [v for v in others if v and v != I.vtep_address(cfg)]}
        if cmd == "show ip route vrf all":
            return {"vrfs": {"default": {"routes": {"10.27.0.1/32": {}}}}}
        return None

    def frr_json(self, node, cmd):
        return {}

    def eos_text(self, node, cmd):          # text fallback: nothing to add (JSON above is complete)
        return 1, ""

    def ping(self, node, target, count=3, deadline=5):
        return (3, 3)


class PostcheckTest(unittest.TestCase):
    def setUp(self):
        self.checks = I.expected_checks(SMOKE_INTENT)
        self.sel = set(SMOKE)

    def run_pc(self, runner, baseline=None):
        snap = PC.collect(SMOKE_INTENT, self.sel, runner, self.checks)
        return PC.evaluate(self.checks, snap, self.sel, baseline)

    def test_all_up(self):
        res = self.run_pc(FakeRunner(SMOKE_INTENT))
        s = PC.summary(res)
        self.assertEqual(s["FAIL"] + s["ERROR"], 0, [r for r in res if r["result"] in ("FAIL", "ERROR")][:3])
        self.assertEqual(s["PASS"], 86)
        self.assertFalse(PC.failed(res))

    def test_bfd_blocked_platform_and_session_down(self):
        res = self.run_pc(FakeRunner(SMOKE_INTENT, bfd=False, down={("DC3-L11", "10.27.4.40")}))
        by = {r["id"]: r for r in res}
        self.assertEqual(by["bfd:DC3-L11:default:10.27.4.40"]["result"], "BLOCKED-PLATFORM")
        self.assertEqual(by["bgp:DC3-L11:default:10.27.4.40"]["result"], "FAIL")
        self.assertEqual(by["bgp:DC3-L11:default:10.27.4.40"]["seen"], "Idle")
        self.assertTrue(PC.failed(res))

    def test_baseline_compare(self):
        """Finding 3.5: a session down before the break is not judged during the test."""
        base_res = self.run_pc(FakeRunner(SMOKE_INTENT, down={("DC3-L11", "10.27.4.40")}))
        baseline = {r["id"]: r["result"] for r in base_res}
        during = self.run_pc(FakeRunner(SMOKE_INTENT, down={("DC3-L11", "10.27.4.40")}), baseline)
        by = {r["id"]: r for r in during}
        self.assertEqual(by["bgp:DC3-L11:default:10.27.4.40"]["result"], "SKIP")
        self.assertIn("not up before the break", by["bgp:DC3-L11:default:10.27.4.40"]["seen"])
        self.assertFalse(PC.failed(during))

    def test_ping_summary(self):
        self.assertEqual(PC.parse_ping_summary("3 packets transmitted, 2 packets received, 33% packet loss"), (3, 2))
        self.assertEqual(PC.parse_ping_summary("3 packets transmitted, 3 received, 0% packet loss, time 2003ms"), (3, 3))
        self.assertIsNone(PC.parse_ping_summary("ping: bad address"))


class OutageTest(unittest.TestCase):
    """Finding 3.6: outage = break to the first reply after it; open-ended when none came before the window end."""

    @staticmethod
    def replies(start, stop, step=0.2):
        out, t = [], start
        while t < stop - 1e-9:
            out.append(round(t, 3))
            t += step
        return out

    def test_gap_then_recovery(self):
        r = self.replies(5.0, 10.0) + self.replies(12.4, 20.0)
        o = F.outage(r, 10.0, 20.0)
        self.assertEqual((o["measurable"], o["open_ended"], o["outage_s"]), (True, False, 2.4))
        self.assertEqual(F.judge_outage(o, 5)[0], "PASS")
        self.assertEqual(F.judge_outage(o, 2)[0], "FAIL")

    def test_open_ended(self):
        o = F.outage(self.replies(5.0, 10.0), 10.0, 25.0)
        self.assertEqual((o["open_ended"], o["outage_s"], o["replies_in_window"]), (True, 15.0, 0))
        result, seen = F.judge_outage(o, 5)
        self.assertEqual(result, "FAIL")
        self.assertIn("open-ended", seen)
        self.assertEqual(F.judge_outage(o, None)[0], "INFO")

    def test_no_loss_and_late_loss(self):
        o = F.outage(self.replies(5.0, 20.0), 10.05, 20.0)
        self.assertLessEqual(o["outage_s"], 0.2)
        self.assertEqual(F.judge_outage(o, 0.5)[0], "PASS")
        late = self.replies(5.0, 11.0) + self.replies(14.0, 20.0)       # loss starts 1 s after the break
        o = F.outage(late, 10.0, 20.0)
        self.assertLessEqual(o["outage_s"], 0.2)
        self.assertAlmostEqual(o["max_gap_s"], 3.2, places=1)            # reported, not judged

    def test_not_measurable(self):
        o = F.outage(self.replies(12.0, 20.0), 10.0, 20.0)
        self.assertFalse(o["measurable"])
        self.assertEqual(F.judge_outage(o, 5)[0], "SKIP")
        self.assertEqual(F.judge_outage(None, 5)[0], "SKIP")

    def test_bond_members(self):
        text = ("Bonding Mode: IEEE 802.3ad Dynamic link aggregation\nMII Status: up\n\nSlave Interface: port19\n"
                "MII Status: down\n\nSlave Interface: port20\nMII Status: up\n")
        self.assertEqual(F.bond_members_up(text), 1)


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.cat = F.build_catalog(FULL)

    def test_sixteen_tests(self):
        self.assertEqual([t["id"] for t in self.cat], list(range(1, 17)))      # failure tests 1-15 + TRANSIT path test 16
        limits = {t["id"]: t["limit"] for t in self.cat}
        # tests 1, 2, 14 = 5 s; 12, 13 = 0.5 s; 9, 10, 11, 15 measured only
        self.assertEqual({k: v for k, v in limits.items() if v is not None},
                         {1: 5, 2: 5, 3: 5, 4: 5, 5: 2, 6: 5, 7: 0.5, 8: 2, 12: 0.5, 13: 0.5, 14: 5})
        for tid in (9, 10, 11, 15, 16):
            self.assertIsNone(limits[tid])
        t1 = next(t for t in self.cat if t["id"] == 1)
        self.assertNotIn("limit", t1["phases"][0])               # phase 1 uses the test limit (5 s)
        self.assertIsNone(t1["phases"][1]["limit"])              # phase 2: loss expected, not judged

    def test_selection(self):
        self.assertEqual([t["id"] for t in F.select_tests(self.cat, "3,5,6")], [3, 5, 6])
        self.assertEqual([t["id"] for t in F.select_tests(self.cat, "6,3")], [3, 6])
        self.assertEqual([t["id"] for t in F.select_tests(self.cat, "1-3,15")], [1, 2, 3, 15])
        self.assertEqual(len(F.select_tests(self.cat, None)), 16)
        with self.assertRaises(ValueError):
            F.select_tests(self.cat, "3,17")

    def test_interfaces_from_the_configs(self):
        by = {t["id"]: t for t in self.cat}
        self.assertEqual(by[5]["phases"][0]["break"][0]["ifaces"], ["Ethernet49/1"])
        self.assertEqual(by[6]["phases"][0]["break"][0]["ifaces"], ["Ethernet3/1", "Ethernet4/1", "Ethernet5/1", "Ethernet6/1"])
        self.assertEqual(by[7]["phases"][0]["break"][0]["ifaces"], ["Ethernet24/1"])
        self.assertEqual(by[12]["phases"][0]["break"][0]["lines"][2], "router bgp 65310 vrf vrf1")
        self.assertEqual(by[13]["phases"][0]["checks"][0]["prefix"], "198.18.3.0/24")
        # node death is links down, never docker pause
        for t in self.cat:
            for ph in t["phases"]:
                for a in ph["break"]:
                    self.assertNotIn("pause", str(a))

    def test_smoke_lab_runs_3_5_15(self):
        cat = F.build_catalog(SMOKE_INTENT)
        runnable = {t["id"] for t in cat if not F.missing_nodes(t, set(SMOKE))}
        self.assertEqual(runnable, {3, 5, 15})

    def test_dry_run_plan(self):
        out = "\n\n".join(F.describe(t, set(SMOKE)) for t in F.select_tests(self.cat, "3,5,6"))
        self.assertIn("DC3-S1: ip link set every data link down", out)
        self.assertIn("DC3-L11: shutdown Ethernet49/1", out)
        self.assertIn("SKIP     not in the lab: DC3-BL1", out)
        self.assertIn("limit 0.5 s", F.describe(self.cat[6]))


class SubsetTopologyTest(unittest.TestCase):
    def setUp(self):
        self.topo = L.subset_topology(L.topology(LABDIR), L.SUBSETS["smoke"])

    def test_six_nodes_and_links_among_them(self):
        t = self.topo["topology"]
        self.assertEqual(list(t["nodes"]), list(SMOKE))
        self.assertEqual(self.topo["name"], "dc3")                      # same container names as the full lab
        ends = [tuple(e.split(":")[0] for e in l["endpoints"]) for l in t["links"]]
        self.assertTrue(all(a in SMOKE and b in SMOKE for a, b in ends))
        pairs = {}
        for a, b in ends:
            key = tuple(sorted((a, b)))
            pairs[key] = pairs.get(key, 0) + 1
        for leaf in ("DC3-L1", "DC3-L2", "DC3-L11"):
            for spine in ("DC3-S1", "DC3-S2"):
                self.assertEqual(pairs[tuple(sorted((leaf, spine)))], 2, (leaf, spine))
        self.assertEqual(pairs[("DC3-L1", "DC3-L2")], 3)                 # 2 peer-link members + keepalive
        self.assertEqual(pairs[("DC3-HOST-PROD", "DC3-L11")], 1)
        self.assertEqual(len(t["links"]), 16)
        full = L.topology(LABDIR)["topology"]
        expected = [l for l in full["links"] if all(e.split(":")[0] in SMOKE for e in l["endpoints"])]
        self.assertEqual(t["links"], expected)

    def test_written_file_and_full_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            L.write(Path(d), subset="smoke")
            self.assertTrue((Path(d) / "dc3-smoke.clab.yml").exists())
            self.assertFalse((Path(d) / "dc3.clab.yml").exists())
            doc = yaml.safe_load((Path(d) / "dc3-smoke.clab.yml").read_text())
            self.assertEqual(len(doc["topology"]["nodes"]), 6)
        with self.assertRaises(ValueError):
            L.subset_topology(L.topology(LABDIR), ["DC3-S1", "DC3-NOPE"])
        # the committed topologies equal the generator output for the reviewed renders (tests/expected = a real lab/configs render;
        # lab/configs itself is local and git-ignored)
        import shutil
        with tempfile.TemporaryDirectory() as d:
            lab = Path(d)
            shutil.copytree(ROOT / "tests" / "expected", lab / "configs")
            (lab / "intf-mapping").symlink_to(ROOT / "lab" / "intf-mapping")
            for name, sub in (("dc3.clab.yml", None), ("dc3-smoke.clab.yml", "smoke")):
                text = (ROOT / "lab" / name).read_text()
                body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
                want = L.topology(lab) if sub is None else L.subset_topology(L.topology(lab), L.SUBSETS[sub])
                self.assertEqual(yaml.safe_load(body), want, name)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------------------------- TRANSIT path test (test 16)
def expected_lab(out: Path) -> Path:
    """tests/expected/ (reference renders) laid out as lab/configs/, then the full topology."""
    (out / "configs").mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "tests" / "expected").iterdir():
        if f.suffix in (".cfg", ".sh"):
            (out / "configs" / f.name).write_text(f.read_text())
    L.write(out)
    return out


EXP = I.build_intent(expected_lab(Path(TMP.name) / "expected"))


class TransitPathTest(unittest.TestCase):
    def test_probe_addresses_from_the_renders(self):
        self.assertFalse(EXP["missing"])
        for node, (addr, prefix, how) in F.TRANSIT_PROBES.items():
            cfg = EXP["nodes"][node]["cfg"]
            nets = [x for inst in cfg["bgp"] for x in inst["networks"]]
            self.assertTrue(any(I.ipaddress.ip_address(addr) in I.ipaddress.ip_network(n) for n in nets), node)
            owned = [a["ip"] for a in cfg["addresses"]]
            self.assertEqual(addr in owned, how == "rendered", node)    # AWS / Azure: blackhole only, probe added
        self.assertEqual(I.owner(EXP, "10.64.1.1"), "DC3-BR001")

    def test_classify_hops(self):
        c = lambda ip: F.classify_hop(EXP, ip)  # noqa: E731
        self.assertEqual(c("10.24.10.1")["role"], "leaf")                   # anycast gateway (ip address virtual)
        self.assertEqual((c("10.27.8.1")["node"], c("10.27.8.1")["role"]), ("DC3-SL1", "service_leaf"))
        self.assertEqual(c("10.27.8.3")["role"], "internal_fw")
        self.assertEqual(c("10.27.16.3")["node"], "DC3-INTFW1")
        self.assertEqual(c("10.27.16.25")["node"], "DC3-BL1")
        self.assertEqual(c("10.27.16.27")["node"], "DC3-CLOUD1")
        self.assertEqual(c("10.27.16.19")["role"], "sdwan_fw")
        self.assertEqual(c("10.27.16.11")["role"], "internet_fw")
        self.assertEqual(c("10.27.2.1")["role"], "leaf")                    # MLAG anycast VTEP L1/L2
        self.assertEqual(c(None)["role"], None)
        self.assertEqual(c("192.0.2.200")["role"], "unknown")

    def test_judge_cloud_and_branch_paths(self):
        cloud = F.TRANSIT_PROBE_PATHS["cloud"]
        good = ["10.24.10.1", "10.27.8.1", "10.27.8.3", "10.27.16.1", "10.27.16.25", "10.27.16.27", "172.16.0.1"]
        self.assertEqual(F.judge_path(EXP, good, "172.16.0.1", cloud)[0], "PASS")
        silent_sl = ["10.24.10.1", None, "10.27.8.3", "10.27.16.2", "10.27.16.26", "10.27.16.35", "172.16.0.1"]
        self.assertEqual(F.judge_path(EXP, silent_sl, "172.16.0.1", cloud)[0], "PASS")
        via_inet = ["10.24.10.1", "10.27.8.3", "10.27.16.1", "10.27.16.11", "172.16.0.1"]
        r, seen, _ = F.judge_path(EXP, via_inet, "172.16.0.1", cloud)
        self.assertEqual(r, "FAIL")
        self.assertIn("via DC3-INETFW1", seen)
        no_fw = ["10.24.10.1", "10.27.16.1", "10.27.16.25", "10.27.16.27", "172.16.0.1"]
        r, seen, _ = F.judge_path(EXP, no_fw, "172.16.0.1", cloud)
        self.assertEqual(r, "FAIL")
        self.assertIn("missing", seen)
        self.assertEqual(F.judge_path(EXP, good[:-1] + [None], "172.16.0.1", cloud)[0], "FAIL")   # target silent
        branch = ["10.24.10.1", "10.27.8.1", "10.27.8.3", "10.27.16.1", "10.27.16.17", "10.27.16.19", "10.64.1.1"]
        self.assertEqual(F.judge_path(EXP, branch, "10.64.1.1", F.TRANSIT_PROBE_PATHS["branch"])[0], "PASS")
        self.assertEqual(F.judge_path(EXP, branch, "10.64.1.1", cloud)[0], "FAIL")     # no cloud router on the way
        t16 = [None, None, "10.27.8.3", "10.27.16.1", None, "10.27.16.35", "172.16.0.1"]           # full-lab trace, test 16
        self.assertEqual(F.judge_path(EXP, t16, "172.16.0.1", cloud)[0], "PASS")
        short = [None, "10.27.8.3", "10.27.16.1", "10.27.16.35", "172.16.0.1"]                     # too few '*' for leaf + BL
        self.assertEqual(F.judge_path(EXP, short, "172.16.0.1", cloud)[0], "FAIL")
        swapped = [None, "10.27.16.1", "10.27.8.3", None, "10.27.16.35", "172.16.0.1"]           # SL before FW
        self.assertEqual(F.judge_path(EXP, swapped, "172.16.0.1", cloud)[0], "FAIL")
        odd = ["10.24.10.1", "10.27.8.3", "10.27.16.1", "10.27.16.25", "10.27.9.3", "10.27.16.27", "172.16.0.1"]
        self.assertIn("unexpected hop", F.judge_path(EXP, odd, "172.16.0.1", cloud)[1])

    def test_parse_traceroute(self):
        busybox = ("traceroute to 172.16.0.1 (172.16.0.1), 15 hops max, 46 byte packets\n"
                   " 1  10.24.10.1  1.1 ms  0.9 ms\n 2  *  *\n 3  10.27.8.3  2.0 ms  1.8 ms\n"
                   " 4  10.27.16.1  2.4 ms  *\n 5  172.16.0.1  3.0 ms  2.9 ms\n")
        self.assertEqual(F.parse_traceroute(busybox), ["10.24.10.1", None, "10.27.8.3", "10.27.16.1", "172.16.0.1"])

    def test_catalog_and_postcheck_routes(self):
        t16 = next(t for t in F.build_catalog(EXP) if t["id"] == 16)
        self.assertTrue(t16["title"].startswith("Tenant to cloud and branch via TRANSIT"), t16["title"])
        self.assertEqual([c["target"] for c in t16["phases"][0]["checks"]], ["172.16.0.1", "172.30.0.1", "10.64.1.1"])
        self.assertEqual([a["node"] for a in t16["phases"][0]["break"]], ["DC3-AWS", "DC3-AZURE"])
        self.assertIn("DC3-SDWANFW1", F.test_nodes(t16))
        self.assertIn(16, {t["id"] for t in F.select_tests(F.build_catalog(EXP), "16")})
        self.assertEqual(F.missing_nodes(t16, set(SMOKE))[:1], ["DC3-AWS"])
        learned = {p for p, _sources, _origins in I.transit_learned(EXP)}
        ids = sorted(c["id"] for c in I.expected_checks(EXP)
                     if c["type"] == "route" and c.get("vrf") == "TRANSIT" and c.get("prefix") in learned)
        self.assertEqual(ids, [f"route:DC3-SL{n}:TRANSIT:{p}" for n in (1, 2)
                               for p in ("10.64.0.0/12", "172.16.0.0/13", "172.30.0.0/15")])


# ---------------------------------------------------------------------------------------------- smoke run fixes
FIX = ROOT / "tests" / "fixtures"
import dc3_smoke_syntax as SX  # noqa: E402


class SmokeRun8OctTest(unittest.TestCase):
    """Real cEOS 4.36.2F text captures of the first smoke run (tests/fixtures/ceos-4.36.2F-*.txt, excerpts)."""

    def test_bfd_text_fixture(self):
        bfd = PC.eos_bfd_text((FIX / "ceos-4.36.2F-L1-show_bfd_peers.txt").read_text())
        self.assertEqual(bfd, {("default", ip): "up" for ip in
                               ("10.27.0.1", "10.27.0.2", "10.27.4.0", "10.27.4.2", "10.27.4.88", "10.27.4.90")})

    def test_bgp_text_fixture(self):
        bgp = PC.eos_bgp_text((FIX / "ceos-4.36.2F-S2-show_bgp_evpn_summary.txt").read_text(), "evpn")
        self.assertEqual(bgp[("evpn", "default", "10.27.0.3")]["state"], "Established")
        self.assertEqual(bgp[("evpn", "default", "10.27.0.13")]["state"], "Established")
        self.assertEqual(bgp[("evpn", "default", "10.27.0.5")]["state"], "Active")
        self.assertEqual(len(bgp), 8)

    def test_bfd_json_tolerant(self):
        classic = {"vrfs": {"default": {"ipv4Neighbors": {"10.27.4.0": {"peerStats": {"": {"status": "up"}}}}}}}
        v4 = {"vrfs": {"default": {"v4Neighbors": {"10.27.4.0": {"peerStats": {"Ethernet49/1": {"status": "up"}}}}}}}
        nested = {"vrfs": {"default": {"ipv4Neighbors": {"10.27.0.1": {"peerStats": {"": {"peerStatsDetail": {
            "state": "Up"}}}}}}}}
        down_and_up = {"vrfs": {"default": {"ipv4Neighbors": {"10.27.4.0": {"peerStats": {
            "a": {"status": "down"}, "b": {"status": "up"}}}}}}}
        self.assertEqual(PC.eos_bfd_peers(classic), {("default", "10.27.4.0"): "up"})
        self.assertEqual(PC.eos_bfd_peers(v4), {("default", "10.27.4.0"): "up"})
        self.assertEqual(PC.eos_bfd_peers(nested), {("default", "10.27.0.1"): "up"})
        self.assertEqual(PC.eos_bfd_peers(down_and_up), {("default", "10.27.4.0"): "up"})
        self.assertEqual(PC.eos_bfd_peers({"vrfs": {}}), {})

    def test_collect_falls_back_to_text(self):
        """JSON unreadable for BGP and BFD: the text captures decide, and L1's checks PASS."""
        bfd_txt = (FIX / "ceos-4.36.2F-L1-show_bfd_peers.txt").read_text()
        bgp_txt = ("BGP summary information for VRF default\nRouter identifier 10.27.0.3, local AS number 65373\n"
                   "  Neighbor   V AS           MsgRcvd   MsgSent  InQ OutQ  Up/Down State   PfxRcd PfxAcc PfxAdv\n"
                   + "".join(f"  {ip} 4 {asn} 10 10 0 0 00:05:00 Estab   1 1 1\n" for ip, asn in
                             (("10.27.4.0", 65300), ("10.27.4.2", 65300), ("10.27.4.88", 65300), ("10.27.4.90", 65300),
                              ("10.27.3.1", 65373))))
        evpn_txt = ("BGP summary information for VRF default\n"
                    "  10.27.0.1  4 65300 76 103 0 0 00:05:24 Estab   84     84     170\n"
                    "  10.27.0.2  4 65300 76 103 0 0 00:05:24 Estab   84     84     170\n")

        class R(FakeRunner):
            def eos_json(self, node, cmd):
                if cmd in ("show ip bgp summary vrf all", "show bgp evpn summary"):
                    return None
                if cmd == "show bfd peers":
                    return {"vrfs": {"default": {"somethingNew": {"x": 1}}}}
                return super().eos_json(node, cmd)

            def eos_text(self, node, cmd):
                return 0, {"show bfd peers": bfd_txt, "show ip bgp summary vrf all": bgp_txt,
                           "show bgp evpn summary": evpn_txt}[cmd]

        checks = I.expected_checks(SMOKE_INTENT)
        snap = PC.collect(SMOKE_INTENT, {"DC3-L1"}, R(SMOKE_INTENT), checks)
        self.assertEqual(snap["DC3-L1"]["source"], {"bgp": "text", "bfd": "text"})
        res = [r for r in PC.evaluate(checks, snap, set(SMOKE), None) if r["node"] == "DC3-L1" and r["type"] in ("bgp", "bfd")]
        self.assertEqual(len(res), 13)              # 4 underlay + MLAG iBGP + 2 overlay, 6 BFD
        self.assertEqual({r["result"] for r in res}, {"PASS"}, [r for r in res if r["result"] != "PASS"])

    def test_raw_output_saved(self):
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "docker"
            stub.write_text('#!/bin/sh\necho \'{"vrfs": {}}\'\n')
            stub.chmod(0o755)
            r = PC.DockerRunner(docker=str(stub), raw_dir=Path(d) / "raw")
            self.assertEqual(r.eos_json("DC3-L1", "show bfd peers"), {"vrfs": {}})
            r.eos_text("DC3-L1", "show mlag detail")
            self.assertEqual((Path(d) / "raw" / "DC3-L1" / "show_bfd_peers.json").read_text().strip(), '{"vrfs": {}}')
            self.assertTrue((Path(d) / "raw" / "DC3-L1" / "show_mlag_detail.txt").exists())
            calls = (Path(d) / "raw" / "calls.jsonl").read_text().splitlines()
            self.assertEqual(len(calls), 2)
        self.assertEqual(PC.raw_name("show ip bgp summary vrf all | json"), "show_ip_bgp_summary_vrf_all.json")
        self.assertEqual(PC.raw_name("show bgp vrf all summary json"), "show_bgp_vrf_all_summary.json")

    def test_retry_when_one_attempt_is_longer_than_wait(self):
        """A single attempt took 372 s with --wait 300, and the old rule never re-ran it."""
        calls = []

        def fake_run_once(intent, checks, selected, runner, baseline=None):
            calls.append(runner.raw_dir)
            r = "FAIL" if len(calls) == 1 else "PASS"
            return [{"id": "x", "node": "DC3-L1", "type": "bgp", "check": "c", "result": r, "seen": ""}], {}

        orig = PC.run_once
        PC.run_once = fake_run_once
        try:
            with tempfile.TemporaryDirectory() as d:
                rc = PC.main(["--lab-dir", str(LABDIR), "--topology", str(LABDIR / "dc3-smoke.clab.yml"),
                              "--wait", "1", "--interval", "2", "--out", str(Path(d) / "pc.json")])
                doc = yaml.safe_load((Path(d) / "pc.json").read_text())
        finally:
            PC.run_once = orig
        self.assertEqual((rc, doc["attempts"]), (0, 2))
        self.assertEqual([c.name for c in calls], ["attempt-1", "attempt-2"])

    def test_heartbeat_from_mlag_detail(self):
        rendered = "mlag configuration\n   heartbeat-interval 4000\n   peer-link Port-Channel1000\n"
        running = (FIX / "ceos-4.36.2F-L1-running-mlag.txt").read_text()
        self.assertNotIn("heartbeat-interval", running)

        def hb(detail="", detail_json=""):
            r = SX.check_node("DC3-L1", rendered, running, "", "", detail, detail_json)
            return next(x for x in r if x["item"] == "heartbeat-interval")

        self.assertEqual(hb("Heartbeat interval                 :            4000 ms\n")["result"], "PASS")
        self.assertEqual(hb("heartbeat interval : 2000\n")["result"], "FAIL")
        self.assertEqual(hb("")["result"], "FAIL")
        self.assertEqual(hb("", '{"detail": {"heartbeatInterval": 4000}}')["result"], "PASS")
        self.assertEqual(SX.mlag_heartbeat_ms("", '{"heartbeatInterval": 4000, "x": {"heartbeatIntervalEffective": 4000}}'),
                         [4000, 4000])
        # a rendered line kept in the running-config still passes as before
        self.assertEqual(SX.check_node("DC3-L1", rendered, rendered, "", "")[0]["result"], "PASS")

    def test_display_rules(self):
        # the four display differences seen in the smoke runs
        rendered = ("interface Ethernet1\n   no shutdown\n   spanning-tree portfast\n   switchport access vlan 1010\n"
                    "interface Ethernet2\n   no shutdown\n   description gone\n"
                    "ip virtual-router mac-address 0200.0000.00aa\n"
                    "management api http-commands\n   protocol https\n   no shutdown\n   vrf MGMT\n      no shutdown\n")
        running = ("interface Ethernet1\n   spanning-tree portfast edge\n   switchport access vlan 1010\n"
                   "interface Ethernet2\n   shutdown\n"
                   "ip virtual-router mac-address 02:00:00:00:00:aa\n"
                   "management api http-commands\n   vrf MGMT\n")
        r = {x["item"]: x for x in SX.check_node("DC3-L11", rendered, running, "", "")}
        self.assertEqual(r["missing-lines"]["lines"], ["interface Ethernet2 | description gone",
                                                        "interface Ethernet2 | no shutdown"])
        self.assertEqual(sorted(r["display-normalised"]["lines"]), [
            "rule 1: interface Ethernet1 | no shutdown",
            "rule 1: management api http-commands | no shutdown",
            "rule 2: management api http-commands | protocol https",
            "rule 3: interface Ethernet1 | spanning-tree portfast",
            "rule 4: ip virtual-router mac-address 0200.0000.00aa"])
        # an explicit 'no protocol https' in the running-config is a real gap, not a display difference
        r2 = {x["item"]: x for x in SX.check_node("DC3-L11", "management api http-commands\n   protocol https\n",
                                                  "management api http-commands\n   no protocol https\n", "", "")}
        self.assertEqual(r2["missing-lines"]["lines"], ["management api http-commands | protocol https"])
        self.assertEqual(r2["display-normalised"]["seen"], "none")

    def test_hidden_default_rules(self):
        # aaa authentication login default local and graceful-restart-helper hidden by EOS
        rendered = ("aaa authentication login default local\naaa authorization exec default local\n"
                    "router bgp 65382\n   no bgp default ipv4-unicast\n   graceful-restart-helper\n")
        running = "aaa authorization exec default local\nrouter bgp 65382\n   no bgp default ipv4-unicast\n"
        r = {x["item"]: x for x in SX.check_node("DC3-L11", rendered, running, "", "")}
        self.assertEqual(r["missing-lines"]["seen"], "none")
        self.assertEqual(sorted(r["display-normalised"]["lines"]), [
            "rule 5: aaa authentication login default local", "rule 6: router bgp 65382 | graceful-restart-helper"])
        r2 = {x["item"]: x for x in SX.check_node("DC3-L11", rendered, running.replace(
            "   no bgp default ipv4-unicast\n", "   no bgp default ipv4-unicast\n   no graceful-restart-helper\n"), "", "")}
        self.assertEqual(r2["missing-lines"]["lines"], ["router bgp 65382 | graceful-restart-helper"])

    def test_error_retry_replaces_only_error_rows(self):
        results = [{"id": 1, "node": "DC3-L1", "result": "ERROR"}, {"id": 2, "node": "DC3-L2", "result": "PASS"},
                   {"id": 3, "node": "DC3-L1", "result": "FAIL"}]
        calls = []

        def run(intent, checks, selected, runner):
            calls.append(sorted(selected))
            return [{"id": 1, "node": "DC3-L1", "result": "PASS"}, {"id": 2, "node": "DC3-L2", "result": "SKIP"},
                    {"id": 3, "node": "DC3-L1", "result": "PASS"}], {}

        new, nodes = PC.retry_errors(None, [], results, lambda: None, run=run)
        self.assertEqual((calls, nodes), ([["DC3-L1"]], ["DC3-L1"]))
        self.assertEqual([r["result"] for r in new], ["PASS", "PASS", "FAIL"])      # FAIL stays: only ERROR rows are re-read
        self.assertEqual(PC.retry_errors(None, [], new[1:2], lambda: None, run=run), (new[1:2], []))

    def test_mlag_text_fixture_matches_judged_fields(self):
        text = (FIX / "ceos-4.36.2F-L1-show_mlag.txt").read_text()
        for line in ("state                              :              Active",
                     "negotiation status                 :           Connected",
                     "peer-config                        :          consistent"):
            self.assertIn(line, text)
