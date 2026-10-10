"""Standalone tests: the repo builds DC3 on an empty Infrahub.

1. Schema files: bootstrap.sh loads every file of schemas/ and then schemas-dc3/; the OOB schema is left out and unused.
2. Object files: every kind and every field exists in schemas/ (or is an Infrahub core kind / field); every reference
   resolves to an object loaded EARLIER in the bootstrap order (scripts/bootstrap.sh arrays).
3. Kinds used by the generator, the pool script, the guard, the id snapshot and the queries exist.
4. Tooling: docker-compose.yml / pyproject.toml pins, no built-in secret, .env.example placeholders only, .gitignore,
   bootstrap.sh (no sudo, 43 renders = tests/expected).
5. Guard and pool-script rules with a fake client; id snapshot build / compare.
6. Renders: the 43 files rendered from the fake store equal tests/expected + the recorded delta
   (tests/expected-delta.diff).

No Infrahub, no Docker.
"""
import asyncio
import importlib.util
import ipaddress
import re
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "generators"))
import dc3_plan as P  # noqa: E402
import test_dc3_stage3 as S  # noqa: E402  (fake store, query responses, SDK-like Jinja2 environment)

INFRAHUB_VERSION = "1.11.2"
SDK_PIN = "infrahub-sdk[ctl]==1.23.2"
SECRET_VARS = ("INFRAHUB_INITIAL_ADMIN_TOKEN", "INFRAHUB_INITIAL_AGENT_TOKEN", "INFRAHUB_SECURITY_SECRET_KEY",
               "INFRAHUB_INITIAL_ADMIN_PASSWORD", "INFRAHUB_DB_PASSWORD", "INFRAHUB_BROKER_PASSWORD",
               "INFRAHUB_TASKMANAGER_DB_PASSWORD")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def docs(path: Path) -> list:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def bash_array(name: str) -> list[str]:
    text = (ROOT / "scripts" / "bootstrap.sh").read_text()
    m = re.search(rf"^{name}=\(\n(.*?)\n\)", text, re.M | re.S)
    if not m:
        raise AssertionError(f"array {name} not found in scripts/bootstrap.sh")
    return [x.strip() for x in m.group(1).splitlines() if x.strip() and not x.strip().startswith("#")]


# ==================================================================================================== schema index
CORE_KINDS = {"CoreStandardGroup", "CoreIPAddressPool", "CoreIPPrefixPool", "IpamNamespace", "CoreArtifactTarget",
              "BuiltinIPPrefix", "BuiltinIPAddress", "BuiltinTag", "CoreNode"}
NODE_FIELDS = {"member_of_groups", "subscriber_of_groups", "profiles"}          # every node (Infrahub core)
CORE_FIELDS = {
    "CoreStandardGroup": {"name", "label", "description", "group_type", "members", "subscribers"},
    "BuiltinIPPrefix": {"prefix", "description", "member_type", "is_pool", "is_top_level", "utilization", "netmask",
                        "hostmask", "network_address", "broadcast_address", "ip_namespace", "resource_pool", "parent",
                        "children", "ip_addresses"},
    "BuiltinIPAddress": {"address", "description", "ip_namespace", "ip_prefix"},
    "CoreArtifactTarget": {"artifacts"},
}
CORE_RELS = {("BuiltinIPPrefix", "ip_namespace"): ("IpamNamespace", "one"), ("CoreStandardGroup", "members"): ("CoreNode", "many")}


class Schema:
    """Kinds, fields, relationships and human-friendly ids of schemas/ (the files in bootstrap.sh SCHEMA_FILES)."""

    def __init__(self, files):
        self.defs, self.generics = {}, set()
        exts = []
        for f in files:
            d = yaml.safe_load((ROOT / f).read_text())
            for section in ("generics", "nodes"):
                for n in d.get(section) or []:
                    kind = n["namespace"] + n["name"]
                    self.defs[kind] = n
                    if section == "generics":
                        self.generics.add(kind)
            exts += (d.get("extensions") or {}).get("nodes") or []
        for e in exts:
            base = self.defs[e["kind"]]
            base.setdefault("attributes", []).extend(e.get("attributes") or [])
            base.setdefault("relationships", []).extend(e.get("relationships") or [])

    @property
    def nodes(self):
        return set(self.defs) - self.generics

    def ancestors(self, kind) -> list[str]:
        out = [kind]
        for parent in (self.defs.get(kind) or {}).get("inherit_from") or []:
            out += self.ancestors(parent)
        return out

    def fields(self, kind) -> set[str]:
        out = set(NODE_FIELDS)
        for k in self.ancestors(kind):
            n = self.defs.get(k)
            if n is None:
                out |= CORE_FIELDS.get(k, set())
                continue
            out |= {a["name"] for a in n.get("attributes") or []} | {r["name"] for r in n.get("relationships") or []}
            if n.get("hierarchical") or n.get("parent") is not None or n.get("children") is not None:
                out |= {"parent", "children"}
        return out

    def rel(self, kind, name):
        """(peer kind, cardinality) of a relationship, or None for an attribute."""
        n = self.defs.get(kind) or {}
        if name == "parent" and n.get("parent"):
            return n["parent"], "one"
        for k in self.ancestors(kind):
            for r in (self.defs.get(k) or {}).get("relationships") or []:
                if r["name"] == name:
                    return r["peer"], r.get("cardinality", "many")
            if (k, name) in CORE_RELS:
                return CORE_RELS[(k, name)]
        return None

    def hfid(self, kind) -> list[str]:
        for k in self.ancestors(kind):
            if (self.defs.get(k) or {}).get("human_friendly_id"):
                return self.defs[k]["human_friendly_id"]
        return ["name__value"]

    def key(self, kind, data) -> tuple:
        out = []
        for f in self.hfid(kind):
            if f == "ip_namespace__name__value":
                out.append(data.get("ip_namespace", "default"))
            elif f.endswith("__name__value"):
                out.append(data[f[: -len("__name__value")]])
            else:
                out.append(data[f[: -len("__value")]])
        return tuple(str(x) for x in out)


SCHEMA = Schema(bash_array("SCHEMA_FILES"))


def items(doc):
    """(kind, data, parent kind or None) of every object in a document, children included."""
    kind = doc["spec"]["kind"]
    for x in doc["spec"]["data"]:
        yield kind, x, None
        ch = x.get("children")
        if isinstance(ch, dict):
            for y in ch["data"]:
                yield ch["kind"], {**y, "parent": x["name"]}, kind


# ==================================================================================================== 1 schema files
class SchemaFilesTest(unittest.TestCase):
    def test_bootstrap_loads_every_schema_file(self):
        files = bash_array("SCHEMA_FILES")
        self.assertEqual({f.removeprefix("schemas/") for f in files if f.startswith("schemas/")},
                         {str(p.relative_to(ROOT / "schemas")) for p in (ROOT / "schemas").rglob("*.yml")})
        # DC3's own additions load after the schema files of schemas/
        own = [f for f in files if not f.startswith("schemas/")]
        self.assertEqual(own, sorted(str(p.relative_to(ROOT)) for p in (ROOT / "schemas-dc3").glob("*.yml")))
        self.assertEqual(files[-len(own):], own)

    def test_oob_left_out_and_unused(self):
        self.assertFalse((ROOT / "schemas" / "oob.yml").exists())
        pattern = re.compile(r"DcimCable|access_vlan|tagged_vlans")
        for folder, globs in (("queries", "*.gql"), ("templates", "*.j2"), ("generators", "*.py"), ("scripts", "*.py"),
                              ("objects", "*.yml"), ("lab", "*.py")):
            for p in (ROOT / folder).glob(globs):
                self.assertIsNone(pattern.search(p.read_text()), p)
        fabric = (ROOT / "schemas" / "fabric.yml").read_text()
        self.assertIsNone(pattern.search(fabric))


# ==================================================================================================== 2 object files
class ObjectFilesTest(unittest.TestCase):
    BEFORE, AFTER = bash_array("OBJECTS_BEFORE_GENERATOR"), bash_array("OBJECTS_AFTER_GENERATOR")

    def test_load_order_covers_objects_dir(self):
        order = self.BEFORE + self.AFTER
        self.assertEqual(len(order), len(set(order)))
        self.assertEqual(set(order), {f"objects/{p.name}" for p in (ROOT / "objects").glob("*.yml")})
        self.assertEqual(order, sorted(order), "load order = file-name order")

    def test_kinds_and_fields_exist(self):
        for p in sorted((ROOT / "objects").glob("*.yml")):
            for d in docs(p):
                self.assertEqual(d["apiVersion"], "infrahub.app/v1", p)
                for kind, data, _parent in items(d):
                    self.assertIn(kind, SCHEMA.nodes | CORE_KINDS, f"{p.name}: kind {kind}")
                    self.assertNotIn(kind, SCHEMA.generics, f"{p.name}: {kind} is a generic")
                    unknown = set(data) - SCHEMA.fields(kind) - {"children"}
                    self.assertEqual(unknown, set(), f"{p.name}: {kind} {data.get('name', '')}")

    def resolve(self, files, generator_devices=()):
        """Walk the files in order; every reference must name an object defined earlier. Returns the problems."""
        defined = {("IpamNamespace", ("default",))}                     # Infrahub creates namespace default
        problems = []

        def define(kind, data):
            for k in SCHEMA.ancestors(kind):
                defined.add((k, SCHEMA.key(kind, data)))

        for f in files:
            if f == "GENERATOR":
                for name in generator_devices:
                    define("DcimDevice", {"name": name})
                continue
            for d in docs(ROOT / f):
                for kind, data, parent_kind in items(d):
                    for field, value in data.items():
                        if field == "children" or (field == "parent" and parent_kind):
                            continue
                        rel = SCHEMA.rel(kind, field)
                        if rel is None:
                            continue
                        peer, card = rel
                        refs = [value] if card == "one" else list(value)
                        for ref in refs:
                            key = tuple(str(x) for x in (ref if isinstance(ref, list) else [ref]))
                            if peer == "CoreNode":
                                ok = any(k == key for _kind, k in defined)
                            else:
                                if len(key) == 1 and len(SCHEMA.hfid(peer)) == 2 and SCHEMA.hfid(peer)[0] == "ip_namespace__name__value":
                                    key = ("default",) + key
                                ok = (peer, key) in defined
                            if not ok:
                                problems.append(f"{f}: {kind} {data.get('name', data.get('prefix', data.get('asn')))}.{field} -> "
                                                f"{peer} {key} not loaded before")
                    define(kind, data)
        return problems

    def generator_devices(self):
        return [r["name"] for r in P.build_plan(P.SITE, 1, 4, 10)] + [s["name"] for s in P.build_standins(P.SITE)]

    def test_standalone_references_resolve_in_load_order(self):
        problems = self.resolve(self.BEFORE + ["GENERATOR"] + self.AFTER, self.generator_devices())
        self.assertEqual(problems, [])

    def test_without_bootstrap_file_references_fail(self):
        """Proves the check works: dc3-01-bootstrap.yml is what makes the set standalone."""
        files = [f for f in self.BEFORE if not f.endswith("dc3-01-bootstrap.yml")]
        problems = self.resolve(files + ["GENERATOR"] + self.AFTER, self.generator_devices())
        self.assertTrue(any("Ashburn" in p for p in problems), problems)
        self.assertTrue(any("10.64.1.0/24" in p for p in problems), problems)

    def test_standalone_names_no_other_site(self):
        for f in self.BEFORE + self.AFTER:
            for d in docs(ROOT / f):
                for kind, data, _ in items(d):
                    for field, value in data.items():
                        if SCHEMA.rel(kind, field) and field not in ("children",):
                            self.assertNotRegex(str(value), r"\bDC[12]\b", f"{f}: {kind}.{field}")

class BootstrapObjectsTest(unittest.TestCase):
    OURS = docs(ROOT / "objects" / "dc3-01-bootstrap.yml")

    def test_shared_objects_have_no_site_and_match_test_providers(self):
        for d in self.OURS:
            for kind, data, _ in items(d):
                self.assertNotIn("site", data, data)
        asns = {x["asn"]: x.get("provider") for d in self.OURS if d["spec"]["kind"] == "FabricAutonomousSystem"
                for x in d["spec"]["data"]}
        self.assertEqual({a: p for a, p in asns.items() if p}, S.PROVIDERS)       # the providers the render tests use
        self.assertIn(P.ISP_ROUTER_ASN, asns)                                        # generator: ISP routers
        self.assertEqual(set(P.CARRIER.values()) | set(P.CLOUD_PROVIDER.values()),
                         {x["name"] for d in self.OURS if d["spec"]["kind"] == "OrganizationProvider" for x in d["spec"]["data"]})


# ==================================================================================================== 3 kinds used
class KindsUsedTest(unittest.TestCase):
    GUARD = load_module("dc3_guard_t", ROOT / "scripts" / "dc3_guard.py")

    def known(self):
        return set(SCHEMA.defs) | CORE_KINDS

    def code_kinds(self) -> set[str]:
        """Every quoted kind name in the generator, the plan and the pool script."""
        out = set()
        for f in ("generators/dc3_site.py", "generators/dc3_plan.py", "scripts/dc3_create_pools.py"):
            out |= set(re.findall(r'"((?:Dcim|Interface|Ipam|Fabric|Location|Organization|Core)[A-Z]\w+)"', (ROOT / f).read_text()))
        out |= {p["kind"] for p in P.build_pairs(P.SITE, P.build_plan(P.SITE, 1, 4, 10), 1, 4)}
        return out

    def query_kinds(self) -> set[str]:
        out = set()
        texts = [p.read_text() for p in (ROOT / "queries").glob("*.gql")]
        snap = (ROOT / "scripts" / "dc3_id_snapshot.py").read_text()
        texts += re.findall(r'"""(query .*?)"""', snap, re.S)
        texts.append(self.GUARD.STATE_QUERY)
        for t in texts:
            t = "\n".join(line.split("#", 1)[0] for line in t.splitlines())
            out |= set(re.findall(r"\.\.\. on (\w+)", t))
            out |= set(re.findall(r"(?:^|[{\s])(?:\w+:\s*)?([A-Z][A-Za-z0-9]+)\s*[({]", t)) - {"query"}
        return {k for k in out if not k.startswith("Dc3")}

    def test_code_and_query_kinds_exist(self):
        self.assertTrue({"DcimDevice", "IpamIPAddress", "FabricVpcDomain", "FabricEsiPair", "CoreIPPrefixPool"} <= self.code_kinds())
        self.assertTrue({"DcimInterface", "InterfaceLayer3", "LocationSite", "FabricSwitchPair", "CoreIPAddressPool"} <= self.query_kinds())
        for k in sorted(self.code_kinds() | self.query_kinds()):
            self.assertIn(k, self.known(), k)

    def test_required_kinds_complete(self):
        req = set(self.GUARD.REQUIRED_KINDS)
        self.assertEqual(req - self.known(), set())
        obj = {kind for f in ObjectFilesTest.BEFORE + ObjectFilesTest.AFTER for d in docs(ROOT / f) for kind, _, _ in items(d)}
        self.assertEqual((obj | self.code_kinds() | self.query_kinds()) - req - {"CoreArtifactTarget"}, set())


# ==================================================================================================== 4 tooling
def compose_defaults(text: str) -> dict:
    return dict(re.findall(r"\$\{(\w+):-([^}]*)\}", text))


class ToolingTest(unittest.TestCase):
    COMPOSE_TEXT = (ROOT / "docker-compose.yml").read_text()
    COMPOSE = yaml.safe_load(COMPOSE_TEXT)

    def test_compose_project_images_ports(self):
        c = self.COMPOSE
        self.assertEqual(c["name"], "infrahub-arista-dc")
        s = c["services"]
        images = {n: x["image"] for n, x in s.items()}
        for n in ("task-manager", "infrahub-server", "task-worker"):
            self.assertEqual(images[n], f"registry.opsmill.io/opsmill/infrahub:{INFRAHUB_VERSION}", n)
        self.assertEqual(s["infrahub-server"]["ports"], ["${DC3_INFRAHUB_PORT:-8000}:8000"])
        self.assertEqual(s["database"]["ports"], ["${DC3_NEO4J_METRICS_PORT:-2004}:2004", "${DC3_NEO4J_BACKUP_PORT:-6362}:6362"])
        self.assertEqual(s["message-queue"]["ports"], ["${DC3_RABBITMQ_METRICS_PORT:-15692}:15692"])
        published = [p for x in s.values() for p in x.get("ports") or []]
        self.assertEqual(len(published), 4, "no other host port")
        worker = s["task-worker"]["environment"]
        self.assertEqual(worker["INFRAHUB_ADDRESS"], "http://infrahub-server:8000")
        self.assertEqual(worker["INFRAHUB_INTERNAL_ADDRESS"], "http://infrahub-server:8000")

    def test_compose_has_no_secret_default(self):
        for var, default in compose_defaults(self.COMPOSE_TEXT).items():
            if re.search(r"PASSWORD|TOKEN|SECRET|_KEY", var) and default not in ("", "true", "false") and not default.isdigit():
                self.fail(f"{var} has a built-in default")
        for k in SECRET_VARS:
            self.assertIn(f"${{{k}:?", self.COMPOSE_TEXT, k)
        self.assertIsNone(UUID_RE.search(self.COMPOSE_TEXT))

    def env_example(self) -> dict:
        return dict(re.findall(r"^([A-Z0-9_]+)=(.*)$", (ROOT / ".env.example").read_text(), re.M))

    def test_env_example_placeholders_only(self):
        env = self.env_example()
        for k in SECRET_VARS:
            self.assertEqual(env[k], "CHANGE_ME", k)
        self.assertEqual({k: env[k] for k in ("DC3_INFRAHUB_PORT", "DC3_NEO4J_METRICS_PORT", "DC3_NEO4J_BACKUP_PORT",
                                              "DC3_RABBITMQ_METRICS_PORT")},
                         {"DC3_INFRAHUB_PORT": "8000", "DC3_NEO4J_METRICS_PORT": "2004", "DC3_NEO4J_BACKUP_PORT": "6362",
                          "DC3_RABBITMQ_METRICS_PORT": "15692"})
        text = (ROOT / ".env.example").read_text()
        for alt in ("8001", "2005", "6363", "15693"):
            self.assertIn(alt, text)
        self.assertNotIn("INFRAHUB_ADDRESS", env, "would reach the containers through docker compose")
        self.assertNotIn("INFRAHUB_API_TOKEN", env)
        self.assertIsNone(UUID_RE.search(text))
        used = set(re.findall(r"\$\{(\w+)[:}]", self.COMPOSE_TEXT))
        self.assertTrue(set(SECRET_VARS) <= used)
        toml = tomllib.loads((ROOT / "infrahubctl.toml.example").read_text())
        self.assertEqual(toml["api_token"], "CHANGE_ME")
        self.assertEqual(toml["server_address"], "http://localhost:8000")

    def test_pyproject_pins(self):
        py = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(py["project"]["name"], "infrahub-arista-dc")
        self.assertEqual(py["project"]["dependencies"], [SDK_PIN])
        self.assertIn("pytest", " ".join(py["dependency-groups"]["dev"]))

    def test_gitignore(self):
        lines = (ROOT / ".gitignore").read_text().splitlines()
        for x in (".env", "infrahubctl.toml", "lab/configs/", "lab/results/*", "/results/", ".venv/"):
            self.assertIn(x, lines)

    def test_bootstrap_script(self):
        text = (ROOT / "scripts" / "bootstrap.sh").read_text()
        code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        self.assertNotRegex(code, r"\bsudo\b")
        self.assertEqual(subprocess.run(["bash", "-n", str(ROOT / "scripts" / "bootstrap.sh")]).returncode, 0)
        self.assertIn(f'INFRAHUB_VERSION="{INFRAHUB_VERSION}"', text)
        self.assertIn(f'SDK_VERSION="{SDK_PIN.split("==")[1]}"', text)
        self.assertIn('BRANCH="main"', text)
        self.assertIn("--standalone --branch", text)
        # the 43 renders: same file names as tests/expected, every transform defined in .infrahub.yml
        lines = re.findall(r'"(dc3_\w+)\|(\w+)=([\w$-]+)\|([\w$.-]+)"', text)
        loops = {"dc3_spine_config": ["DC3-S1", "DC3-S2"], "dc3_leaf_config": [f"DC3-L{n}" for n in range(1, 21)],
                 "dc3_border_service_leaf_config": ["DC3-BL1", "DC3-BL2", "DC3-SL1", "DC3-SL2"],
                 "dc3_l2_pair_config": ["DC3-EDGE1", "DC3-EDGE2", "DC3-DMZ1", "DC3-DMZ2"]}
        outs = []
        for transform, _var, value, out in lines:
            self.assertIn(transform, S.TRANSFORMS)
            if "$" in out:
                outs += [f"{d}.cfg" for d in loops[transform]]
            else:
                outs.append(out)
        expected = sorted(p.name for p in (ROOT / "tests" / "expected").iterdir() if p.suffix in (".cfg", ".sh"))
        self.assertEqual(sorted(outs), expected)
        self.assertEqual(len(outs), 43)

    def test_smoke_script_project_override(self):
        text = (ROOT / "lab" / "smoke.sh").read_text()
        # default = the repo's own stack; the project name is read from docker-compose.yml (name:)
        self.assertIn('INFRAHUB_DIR="${INFRAHUB_DIR:-$REPO}"', text)
        self.assertIn("awk '/^name:/{print $2; exit}' \"$INFRAHUB_DIR/docker-compose.yml\"", text)
        name = re.search(r"^name:\s*(\S+)", (ROOT / "docker-compose.yml").read_text(), re.M)
        self.assertIsNotNone(name)


# ==================================================================================================== 5 guard, pools, ids
class FakeGuardClient:
    def __init__(self, kinds, sites=(), devices_total=0, devices_site=0):
        self.kinds, self.sites, self.total, self.site = set(kinds), list(sites), devices_total, devices_site
        self.created = []
        outer = self

        class _Schema:
            async def all(self, branch=None, refresh=False):
                return {k: None for k in outer.kinds}

        self.schema = _Schema()

    async def execute_graphql(self, query, variables=None, branch_name=None):
        return {"LocationSite": {"count": len(self.sites), "edges": [{"node": {"name": {"value": s}}} for s in self.sites]},
                "all_devices": {"count": self.total}, "site_devices": {"count": self.site}}

    async def get(self, **kwargs):
        raise AssertionError("no read or write may happen after a refused guard")

    async def create(self, **kwargs):
        self.created.append(kwargs)
        raise AssertionError("no write may happen after a refused guard")


class GuardAndPoolsTest(unittest.TestCase):
    G = load_module("dc3_guard_t2", ROOT / "scripts" / "dc3_guard.py")
    POOLS = load_module("dc3_pools_t", ROOT / "scripts" / "dc3_create_pools.py")
    ALL = set(G.REQUIRED_KINDS)

    def test_rule(self):
        self.assertEqual(self.G.problems_from_state(None, None, None), [])
        self.assertEqual(self.G.problems_from_state([], 0, 0), [])
        self.assertEqual(self.G.problems_from_state(["DC3"], 38, 38), [])
        self.assertTrue(self.G.problems_from_state(["SITE-A", "SITE-B", "DC3"], 38, 38))
        self.assertTrue(self.G.problems_from_state(["DC3"], 40, 38))
        self.assertEqual(self.G.branches_to_check("main"), ["main"])
        self.assertEqual(self.G.branches_to_check("dev"), ["main", "dev"])

    def test_check_with_fake_instances(self):
        run = lambda c, **kw: asyncio.run(self.G.check(c, "main", **kw))  # noqa: E731
        self.assertEqual(run(FakeGuardClient(set())), [])                                       # fresh, no schema
        self.assertTrue(run(FakeGuardClient(set()), require_schema=True))                       # schema not loaded yet
        self.assertEqual(run(FakeGuardClient(self.ALL, ["DC3"], 38, 38), require_schema=True), [])
        other = run(FakeGuardClient(self.ALL, ["SITE-A", "SITE-B"], 200, 0))                    # main holds other sites
        self.assertTrue(any("SITE-A, SITE-B" in p for p in other), other)
        self.assertTrue(run(FakeGuardClient({"LocationSite"})))                                 # partial schema

    def test_pools_branch_rule(self):
        with self.assertRaises(SystemExit):
            self.POOLS.check_branch("main")
        with self.assertRaises(SystemExit):
            self.POOLS.check_branch("")
        with self.assertRaises(SystemExit):
            self.POOLS.check_branch(None, standalone=True)
        self.assertEqual(self.POOLS.check_branch("dc3"), "dc3")
        self.assertEqual(self.POOLS.check_branch("main", standalone=True), "main")

    def test_pools_standalone_refused_on_instance_with_other_sites(self):
        client = FakeGuardClient(self.ALL, ["SITE-A", "SITE-B"], 200, 0)
        with self.assertRaises(SystemExit) as cm:
            asyncio.run(self.POOLS.run(client, None, "main", standalone=True))
        self.assertIn("SITE-A", str(cm.exception))
        self.assertEqual(client.created, [])
        with self.assertRaises(SystemExit):                                                     # main without --standalone
            asyncio.run(self.POOLS.run(FakeGuardClient(self.ALL, ["DC3"], 0, 0), None, "main"))


class IdSnapshotTest(unittest.TestCase):
    ID = load_module("dc3_ids_t", ROOT / "scripts" / "dc3_id_snapshot.py")

    def raw(self, lo1_addr="a1"):
        n = lambda **kw: {k: ({"value": v} if k in ("name", "role", "address", "prefix") else v) for k, v in kw.items()}  # noqa: E731
        return {
            "devices": [n(id="d1", name="DC3-S1", role="spine", primary_address={"node": {"id": "a0"}}, asn={"node": None})],
            "interfaces": [n(id="i1", __typename="InterfaceVirtual", name="Loopback0", device={"node": {"name": {"value": "DC3-S1"}}},
                             ip_addresses={"edges": [{"node": {"id": lo1_addr}}]})],
            "addresses": [n(id="a1", address="10.27.0.1/32", interface={"node": {"id": "i1"}})],
            "pairs": [n(id="p1", __typename="FabricVpcDomain", name="DC3-MLAG-L1-L2", members={"edges": []}, anycast_vtep={"node": None}),
                      n(id="p9", __typename="FabricVpcDomain", name="SITE-A-VPC-L1-L2", members={"edges": []})],
            "attachments": [],
            "prefixes": [n(id="x1", prefix="10.27.4.0/31", site={"node": {"id": "s"}}), n(id="x2", prefix="10.16.0.0/16", site=None),
                         n(id="x3", prefix="10.250.32.0/24", site=None), n(id="x4", prefix="10.250.0.0/24", site=None)],
        }

    def test_build_and_compare(self):
        a = self.ID.build(self.raw())
        self.assertEqual(list(a["pairs"]), ["DC3-MLAG-L1-L2"])
        self.assertEqual(sorted(a["prefixes"]), ["10.250.32.0/24", "10.27.4.0/31"])
        self.assertEqual(self.ID.compare(a, self.ID.build(self.raw())), [])
        diff = self.ID.compare(a, self.ID.build(self.raw(lo1_addr="a2")))
        self.assertEqual(len(diff), 1)
        self.assertIn("DC3-S1 Loopback0", diff[0])

    def test_interface_query_uses_device_name_values(self):
        self.assertIn("DcimInterface(device__name__values: %s", self.ID.INTERFACES)
        self.assertTrue(self.ID.in_scope("10.24.10.0/24") and self.ID.in_scope("10.27.0.0/16") and self.ID.in_scope("10.250.32.1/32"))
        self.assertFalse(self.ID.in_scope("10.24.0.0/14") or self.ID.in_scope("10.250.0.0/16") or self.ID.in_scope("192.0.2.16/30"))


# ==================================================================================================== 6 renders
ARTIFACT_FILE = {"startup-config": "{d}.cfg", "frr-lab-init": "{d}.sh", "carrier-lab-init": "{d}.carrier.sh",
                 "branch-lab-init": "{d}.branch.sh", "partner-lab-init": "{d}.partner.sh"}


def render_all(graph) -> dict[str, str]:
    out = {}
    for name, dev in graph.dev.items():
        group = S.GROUPS[dev.data["role"]]
        for a in S.ARTIFACTS:
            if a["targets"] == group:
                t = S.TRANSFORMS[a["transformation"]]
                out[ARTIFACT_FILE[a["artifact_name"]].format(d=name)] = S.ENV.get_template(Path(t["template_path"]).name).render(
                    data=graph.response(t["query"], name))
    t = S.TRANSFORMS["dc3_frr_cloud_provider"]
    out["DC3.cloud-provider.sh"] = S.ENV.get_template(Path(t["template_path"]).name).render(
        data=graph.response(t["query"], "DC3"))
    return out


class RenderEquivalenceTest(unittest.TestCase):
    D = load_module("dc3_render_diff_t", ROOT / "scripts" / "dc3_render_diff.py")

    @classmethod
    def setUpClass(cls):
        cls.standalone = render_all(S.G)

    def test_standalone_renders_equal_expected_plus_recorded_delta(self):
        expected = self.D.render_files(ROOT / "tests" / "expected")
        self.assertEqual(len(self.standalone), 43)
        delta = (ROOT / "tests" / "expected-delta.diff").read_text()
        self.assertEqual(self.D.check(expected, self.standalone, delta), [])

    def test_delta_empty_after_rebaseline(self):
        # tests/expected is a render of the current templates, so no recorded change is left
        files = re.findall(r"^\+\+\+ b/(\S+)$", (ROOT / "tests" / "expected-delta.diff").read_text(), re.M)
        self.assertEqual(files, [])

    def test_render_diff_detects_a_change(self):
        expected = self.D.render_files(ROOT / "tests" / "expected")
        delta = (ROOT / "tests" / "expected-delta.diff").read_text()
        broken = dict(self.standalone)
        broken["DC3-L5.cfg"] = broken["DC3-L5.cfg"].replace("10.27.", "10.28.", 1)
        self.assertTrue(self.D.check(expected, broken, delta))
        missing = {k: v for k, v in self.standalone.items() if k != "DC3.cloud-provider.sh"}
        self.assertTrue(self.D.check(expected, missing, delta))


if __name__ == "__main__":
    unittest.main()
