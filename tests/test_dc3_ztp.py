"""DC3 ZTP: bootstrap script, ZTP server routes and states, DHCP file, secret fill. No Infrahub, no network."""
import json
import os
import py_compile
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ztp"))
sys.path.insert(0, str(ROOT / "lab"))
import dc3_secrets as SEC  # noqa: E402
import ztp_server as Z  # noqa: E402

SWITCHES = (["DC3-S1", "DC3-S2"] + [f"DC3-L{n}" for n in range(1, 21)]
            + ["DC3-BL1", "DC3-BL2", "DC3-SL1", "DC3-SL2", "DC3-EDGE1", "DC3-EDGE2", "DC3-DMZ1", "DC3-DMZ2"])


class FakeInfrahub:
    def __init__(self, devices):
        self.devices = devices          # serial -> (id, name)
        self.mutations = []

    def __call__(self, query, branch="dc3"):
        if query.startswith("mutation"):
            self.mutations.append(query)
            return {"DcimDeviceUpdate": {"ok": True}}
        sn = query.split('serial_number__value: "')[1].split('"')[0]
        hit = [self.devices[sn]] if sn in self.devices else []
        return {"DcimDevice": {"edges": [{"node": {"id": i, "name": {"value": n}}} for i, n in hit]}}


class ZtpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Z.LOG = os.path.join(self.tmp.name, "events.jsonl")
        Z.SERVED.clear()
        self.env = dict(os.environ)
        os.environ.update({"DC3_SECRETS": os.path.join(self.tmp.name, "none.env"), "DC3_ZTP_CALLBACK_TOKEN": "tok",
                           "DC3_AUTOMATION_HASH": "$6$salt$hash", "DC3_AUTOMATION_PASSWORD": "pw"})
        self.cfg = (ROOT / "tests" / "expected" / "DC3-L11.cfg").read_text().replace(
            "hostname DC3-L11\n", "hostname DC3-L11\nusername dc3-automation privilege 15 role network-admin secret sha512 "
            "__DC3_AUTOMATION_HASH__\n")

    def tearDown(self):
        os.environ.clear(); os.environ.update(self.env); self.tmp.cleanup()

    def events(self):
        if not os.path.exists(Z.LOG):
            return []
        with open(Z.LOG) as f:
            return [json.loads(l) for l in f]

    def test_transform_for_every_switch_and_no_standin(self):
        got = {n: Z.transform_for(n) for n in SWITCHES}
        self.assertTrue(all(got.values()), got)
        self.assertEqual(got["DC3-S1"], "dc3_spine_config")
        self.assertEqual(got["DC3-L20"], "dc3_leaf_config")
        self.assertEqual(got["DC3-SL2"], "dc3_border_service_leaf_config")
        self.assertEqual(got["DC3-DMZ1"], "dc3_l2_pair_config")
        for n in ("DC3-INTFW1", "DC3-ISP1", "DC3-CLOUD1", "DC3-HOST-PROD"):
            self.assertIsNone(Z.transform_for(n))

    def test_bootstrap_filled_and_compiles(self):
        body = Z.bootstrap_script("10.250.32.1", 8080).decode()
        self.assertIn('SRV = "10.250.32.1:8080"', body)
        self.assertNotIn("__SRV__", body)
        p = Path(self.tmp.name) / "bootstrap.py"; p.write_text(body)
        py_compile.compile(str(p), doraise=True)

    def test_kea_points_to_the_server(self):
        k = json.loads((ROOT / "ztp" / "kea-dhcp4.json").read_text())["Dhcp4"]
        sub = k["subnet4"][0]
        self.assertEqual(sub["subnet"], "10.250.32.0/24")
        self.assertEqual(sub["pools"][0]["pool"], "10.250.32.200 - 10.250.32.239")       # below the lab-only .240-.253
        opts = {o["name"]: o["data"] for o in sub["option-data"]}
        self.assertEqual(opts["boot-file-name"], "http://10.250.32.1:8080/bootstrap")
        self.assertEqual(k["interfaces-config"]["interfaces"], ["br-dc3-ztp"])

    def test_config_served_filled_and_state(self):
        ih = FakeInfrahub({"SN123": ("id-l11", "DC3-L11")})
        code, body = Z.handle("/config/SN123", "10.250.32.1", 8080, q=ih, rnd=lambda n: self.cfg)
        body = body.decode()
        self.assertEqual(code, 200)
        self.assertIn("secret sha512 $6$salt$hash\n", body)
        self.assertNotIn("__DC3_AUTOMATION_HASH__", body)
        self.assertEqual(Z.SERVED["SN123"][1:], ("DC3-L11", Z.mgmt_address(self.cfg)))
        self.assertRegex(Z.SERVED["SN123"][2], r"^10\.250\.32\.\d+$")
        self.assertEqual(len(ih.mutations), 1)
        self.assertIn('ztp_state: {value: "served"}', ih.mutations[0])

    def test_unknown_or_duplicate_serial_refused(self):
        ih = FakeInfrahub({})
        self.assertEqual(Z.handle("/config/NOPE", "10.250.32.1", 8080, q=ih, rnd=lambda n: self.cfg)[0], 404)
        self.assertEqual(Z.handle("/config/bad\"serial", "10.250.32.1", 8080, q=ih, rnd=lambda n: self.cfg)[0], 404)
        self.assertEqual(ih.mutations, [])
        self.assertEqual(self.events()[0]["event"], "config-refused")

    def test_callback_starts_check_and_failed_callback(self):
        ih = FakeInfrahub({"SN123": ("id-l11", "DC3-L11")})
        Z.handle("/config/SN123", "10.250.32.1", 8080, q=ih, rnd=lambda n: self.cfg)
        started = []
        code, _ = Z.handle("/callback/SN123", "10.250.32.1", 8080, q=ih, start_verify=lambda *a: started.append(a))
        self.assertEqual((code, started[0][:3]), (200, ("id-l11", "DC3-L11", "SN123")))
        Z.handle("/callback/SN123/failed/config-download:timeout", "10.250.32.1", 8080, q=ih)
        self.assertIn('ztp_state: {value: "failed"}', ih.mutations[-1])

    def test_callback_without_config_is_failed(self):
        ih = FakeInfrahub({"SN9": ("id-s1", "DC3-S1")})
        Z.handle("/callback/SN9", "10.250.32.1", 8080, q=ih, start_verify=lambda *a: self.fail("no check expected"))
        self.assertIn('ztp_state: {value: "failed"}', ih.mutations[-1])

    def test_verify_complete_and_timeout(self):
        ih = FakeInfrahub({})
        self.assertTrue(Z.verify("id", "DC3-L11", "SN", "10.250.32.11", ask=lambda ip, pw: "DC3-L11", every=0, max_s=5, q=ih))
        self.assertIn('ztp_state: {value: "complete"}', ih.mutations[-1])
        Z.verify("id", "DC3-L11", "SN", "10.250.32.11", ask=lambda ip, pw: "localhost", every=0, max_s=0.05, q=ih)
        self.assertIn('ztp_state: {value: "failed"}', ih.mutations[-1])

    def test_no_token_no_write(self):
        os.environ.pop("DC3_ZTP_CALLBACK_TOKEN")
        ih = FakeInfrahub({})
        self.assertFalse(Z.set_state("id", "DC3-L11", "served", q=ih))
        self.assertEqual(ih.mutations, [])

    def test_secret_missing_drops_line(self):
        text, warns = SEC.fill("hostname X\nusername dc3-automation secret sha512 __DC3_AUTOMATION_HASH__\n", {})
        self.assertEqual((text, len(warns)), ("hostname X\n", 1))

    def test_scripts_compile(self):
        for f in ("ztp_server.py", "setup_account.py", "set_serial.py", "bootstrap.py"):
            py_compile.compile(str(ROOT / "ztp" / f), doraise=True, cfile=os.path.join(self.tmp.name, f + "c"))


if __name__ == "__main__":
    unittest.main()
