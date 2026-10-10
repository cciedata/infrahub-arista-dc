"""Generator tests of generators/dc3_site.py against an in-memory fake Infrahub client (no SDK, no server).

On MLAG VTEP pairs (L1-L2, BL, SL) Loopback1 of both members has no address in the data (the anycast VTEP is only on
FabricVpcDomain.anycast_vtep) and Loopback2 holds the switch's own VTEP address. No address may ever sit on two
interfaces: the fake refuses it on every save (IpamIPAddress.interface has cardinality one on a running instance).
The tests check that refusal, the generator's claim guard, the scope of its holder check, and that an upsert without
addresses clears them.

The fake models only what the generator uses. Assumptions (not checked against Infrahub 1.11.2):
upsert matches InterfaceX on (device, name), IpamIPAddress on address, other kinds on name; an upsert REPLACES a
cardinality-many relationship with the list given; pool allocation with a known identifier returns the same address;
the reverse side of InterfaceVirtual.ip_addresses (IpamIPAddress.interface) has cardinality one (seen on a running instance).
"""
import asyncio
import copy
import importlib.util
import ipaddress
import os
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REL_KEYS = {"device", "location", "platform", "device_type", "asn", "primary_address", "ip_addresses", "members",
            "anycast_vtep", "ip_namespace", "site", "parent_interface", "switch_pair", "vrf", "devices"}
REL_KEYS_BY_KIND = {"FabricAttachment": {"prefix"}, "FabricL2Vlan": {"prefix"}}   # `prefix` is an attribute on IpamPrefix


class Attr:
    """Attribute bound to the node's data: `node.description.value = x` writes through, as on an SDK node."""
    def __init__(self, data, key):
        object.__setattr__(self, "_d", data)
        object.__setattr__(self, "_k", key)

    @property
    def value(self):
        return self._d.get(self._k)

    @value.setter
    def value(self, v):
        self._d[self._k] = v


class One:
    def __init__(self, id_):
        self.id = id_


class Many:
    def __init__(self, store, ids):
        self._store, self._ids = store, list(ids)

    @property
    def peers(self):
        return [One(i) for i in self._ids]

    async def fetch(self):
        return None

    def add(self, id_):
        self._ids.append(id_)

    def remove(self, id_):
        self._ids.remove(id_)


class Node:
    def __init__(self, client, kind, data, id_=None):
        object.__setattr__(self, "_c", client)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "data", dict(data))
        object.__setattr__(self, "id", id_)

    def __getattr__(self, key):
        d = self.data
        if self.kind == "IpamIPAddress" and key == "interface":      # reverse side, cardinality one (live schema)
            holders = [n.id for k in ("InterfaceVirtual", "InterfacePhysical") for n in self._c.store.get(k, {}).values()
                       if self.id in (n.data.get("ip_addresses") or [])]
            return One(holders[0] if holders else None)
        if key in REL_KEYS or key in REL_KEYS_BY_KIND.get(self.kind, ()):
            v = d.get(key)
            if isinstance(v, list) or key in ("ip_addresses", "members", "devices"):
                m = Many(self._c, [])
                m._ids = d.setdefault(key, []) if v is not None else d.setdefault(key, [])   # bound: remove()/add() write through
                return m
            return One(v)
        return Attr(d, key)

    def __setattr__(self, key, value):
        if key in ("id",):
            object.__setattr__(self, key, value)
        elif key == "site":
            self.data["site"] = value.id
        elif key == "interface" and self.kind == "IpamIPAddress":
            self.data["_move_to"] = value.id          # applied on save(): the one-cardinality side moves the address
        else:
            raise AttributeError(key)

    def key(self):
        d = self.data
        if self.kind.startswith("Interface"):
            return (d["device"], d["name"])
        if self.kind == "IpamIPAddress":
            return d["address"]
        if self.kind == "IpamPrefix":
            return d["prefix"]
        if self.kind == "FabricAutonomousSystem":
            return d["asn"]
        return d["name"]

    def _enforce_one_interface(self, target_id):
        """IpamIPAddress.interface has cardinality one on the running instance: refuse a second holder."""
        if not self.kind.startswith("Interface"):
            return
        for a in self.data.get("ip_addresses") or []:
            for k in ("InterfaceVirtual", "InterfacePhysical", "InterfacePortChannel"):
                for n in self._c.store.get(k, {}).values():
                    if n.id != target_id and a in (n.data.get("ip_addresses") or []):
                        raise ValueError(f"IpamIPAddress.interface has cardinality one: {a} is on {n.key()}, not {self.key()}")

    async def save(self, allow_upsert=False):
        c = self._c
        if self.id is None:
            existing = next((n for n in c.store.get(self.kind, {}).values() if n.key() == self.key()), None)
            if existing is not None:
                if allow_upsert:
                    if self.kind.startswith("Interface") and self.kind != "InterfacePortChannel":
                        # A real upsert that omits ip_addresses (or gives []) CLEARS the list (seen on a running instance)
                        self.data.setdefault("ip_addresses", [])
                    self._enforce_one_interface(existing.id)
                    for k, v in self.data.items():
                        if existing.data.get(k) != v:
                            existing.data[k] = v
                            c.changes.append(f"update {self.kind} {existing.key()} {k}")
                object.__setattr__(self, "id", existing.id)
                object.__setattr__(self, "data", existing.data)
                return
            if self.kind.startswith("Interface") and self.kind != "InterfacePortChannel":
                self.data.setdefault("ip_addresses", [])     # a created interface without addresses has an empty list
            self._enforce_one_interface(None)
            c.seq += 1
            object.__setattr__(self, "id", f"id-{c.seq}")
            c.store.setdefault(self.kind, {})[self.id] = self
            c.changes.append(f"create {self.kind} {self.key()}")
            return
        stored = c.store[self.kind][self.id]
        if self.kind == "IpamIPAddress" and "_move_to" in self.data:
            # IpamIPAddress.interface has cardinality one: assigning it takes the address off its previous holder
            target = self.data.pop("_move_to")
            for k in ("InterfaceVirtual", "InterfacePhysical", "InterfacePortChannel"):
                for n in c.store.get(k, {}).values():
                    lst = n.data.get("ip_addresses") or []
                    if self.id in lst and n.id != target:
                        lst.remove(self.id)
                        c.changes.append(f"unlink IpamIPAddress {self.key()} from {n.key()}")
                    if n.id == target and self.id not in lst:
                        n.data["ip_addresses"] = lst + [self.id]
        self._enforce_one_interface(self.id)
        if stored is not self:
            stored.data.update(self.data)
        c.changes.append(f"save {self.kind} {self.key()}")

    async def delete(self):
        del self._c.store[self.kind][self.id]
        self._c.changes.append(f"delete {self.kind} {self.key()}")


class GroupNode(Node):
    @property
    def members(self):
        if "_members" not in self.data:
            self.data["_members"] = []
        m = Many(self._c, [])
        m._ids = self.data["_members"]
        return m


class Rel:
    def __init__(self, name, peer, identifier, cardinality):
        self.name, self.peer, self.identifier, self.cardinality = name, peer, identifier, cardinality


class Schema:
    async def get(self, kind):
        if kind == "InterfaceVirtual":
            return types.SimpleNamespace(relationships=[Rel("ip_addresses", "IpamIPAddress", "ip_address__interface", "many")])
        return types.SimpleNamespace(relationships=[Rel("interfaces", "InterfaceVirtual", "ip_address__interface", "many")])


class FakeClient:
    POOLS = {"DC3-LOOPBACK0": "10.27.0.0/24", "DC3-LOOPBACK1-VTEP": "10.27.1.0/24", "DC3-MGMT0": "10.250.32.0/24",
             "DC3-ANYCAST-VTEP": "10.27.2.0/25", "DC3-FABRIC-P2P": "10.27.4.0/22", "DC3-VPC-BACKUP": "10.27.3.0/24",
             "DC3-VPC-KEEPALIVE": "10.27.17.0/24"}
    # Tenants and stand-ins: IP-plan prefixes the generator reads (objects/dc3-03-ip-plan.yml) and the VRFs that list DC3 (dc3-06-fabric.yml)
    PLAN_PREFIXES = ["10.27.14.0/24"] + [f"10.24.{128 + i}.0/24" for i in range(16)] + [
        "198.18.3.144/28", "198.18.3.128/28", "169.254.120.0/30", "169.254.121.0/30", "10.98.0.16/30", "10.98.0.20/30"]
    VRFS = {"PROD": 50001, "DEV": 50002, "CITRIX": 50022, "PARTNER": 50031, "REPLICATION": 50041, "TRANSIT": 50100}

    def __init__(self):
        self.store, self.seq, self.changes, self.alloc = {}, 0, [], {}
        self.schema = Schema()
        self._seed()

    def _seed(self):
        import yaml
        sys.path.insert(0, str(ROOT / "generators"))
        import dc3_plan as P
        add = lambda kind, data, cls=Node: self._put(cls(self, kind, data))  # noqa: E731
        add("LocationSite", {"name": "DC3", "vpc_leaf_pairs": 1, "esi_leaf_pairs": 4, "single_leaves": 10})
        add("DcimPlatform", {"name": P.PLATFORM})
        for m in set(P.DEVICE_TYPE.values()):
            add("DcimDeviceType", {"name": m})
        for a in [65300] + list(range(65371, 65392)):
            add("FabricAutonomousSystem", {"asn": a})
        # tenant and stand-in references
        site_id = next(iter(self.store["LocationSite"]))
        add("DcimPlatform", {"name": "FRR"})
        add("DcimDeviceType", {"name": "FRR stand-in"})
        for a in (65310, 65320, 65330, 65340, 65370, 65050):
            add("FabricAutonomousSystem", {"asn": a})
        for name, vni in self.VRFS.items():
            add("FabricVRF", {"name": name, "l3_vni": vni, "sites": [site_id]})
        for prefix in self.PLAN_PREFIXES:
            add("IpamPrefix", {"prefix": prefix, "description": "IP plan", "site": site_id})
        for g in ("dc3-internal-fws", "dc3-internet-fws", "dc3-sdwan-fws", "dc3-vpn-fws", "dc3-isp-routers", "dc3-cloud-routers"):
            add("CoreStandardGroup", {"name": g}, GroupNode)
        for name, prefix in self.POOLS.items():
            kind = "CoreIPPrefixPool" if name.endswith(("P2P", "BACKUP", "KEEPALIVE")) else "CoreIPAddressPool"
            add(kind, {"name": name, "prefix": prefix})
        add("IpamNamespace", {"name": "default"})
        for g in set(P.ARTIFACT_GROUPS.values()):
            add("CoreStandardGroup", {"name": g}, GroupNode)
        self.changes.clear()

    def _put(self, node):
        self.seq += 1
        node.id = f"id-{self.seq}"
        self.store.setdefault(node.kind, {})[node.id] = node

    async def create(self, kind, data):
        return Node(self, kind, data)

    async def execute_graphql(self, query):
        """Only the generator's site_vrfs query (FabricVRF with sites)."""
        assert "FabricVRF" in query, query
        sites = {n.id: n.data["name"] for n in self.store["LocationSite"].values()}
        return {"FabricVRF": {"edges": [{"node": {"id": n.id, "name": {"value": n.data["name"]}, "sites": {"edges": [
            {"node": {"name": {"value": sites[i]}}} for i in n.data.get("sites", [])]}}} for n in self.store["FabricVRF"].values()]}}

    def _match(self, node, filters):
        for k, v in filters.items():
            if k == "id":
                if node.id != v:
                    return False
            elif k == "device__ids":
                if node.data.get("device") not in v:
                    return False
            elif k in ("location__name__value", "site__name__value"):
                ref = node.data.get("location" if k.startswith("location") else "site")
                site = next((n for n in self.store["LocationSite"].values() if n.id == ref), None)
                if site is None or site.data["name"] != v:
                    return False
            elif k.endswith("__value"):
                if node.data.get(k[: -len("__value")]) != v:
                    return False
        return True

    async def get(self, kind, raise_when_missing=True, include=None, **filters):
        found = [n for n in self.store.get(kind, {}).values() if self._match(n, filters)]
        if not found:
            if raise_when_missing:
                raise LookupError(f"{kind} {filters}")
            return None
        return found[0]

    async def filters(self, kind, include=None, **filters):
        return [n for n in list(self.store.get(kind, {}).values()) if self._match(n, filters)]

    def _next(self, pool, identifier, size):
        key = (pool.data["name"], identifier)
        if key not in self.alloc:
            net = ipaddress.ip_network(pool.data["prefix"])
            used = sum(1 for k in self.alloc if k[0] == pool.data["name"])
            first = 1 if size == 1 and net.prefixlen < 31 else 0
            if pool.data["name"].endswith("MGMT0"):
                first = 2                                     # .1 is the gateway
            self.alloc[key] = int(net.network_address) + first + used * size
        return self.alloc[key]

    async def allocate_next_ip_address(self, resource_pool, identifier, data):
        n = self._next(resource_pool, identifier, 1)
        plen = 24 if resource_pool.data["name"].endswith("MGMT0") else 32
        node = Node(self, "IpamIPAddress", {"address": f"{ipaddress.ip_address(n)}/{plen}", **data})
        existing = next((x for x in self.store.get("IpamIPAddress", {}).values() if x.key() == node.key()), None)
        if existing is not None:
            return existing
        await node.save()
        return node

    async def allocate_next_ip_prefix(self, resource_pool, identifier, data):
        n = self._next(resource_pool, identifier, 2)
        node = Node(self, "IpamPrefix", {"prefix": f"{ipaddress.ip_address(n)}/31", **data})
        existing = next((x for x in self.store.get("IpamPrefix", {}).values() if x.key() == node.key()), None)
        if existing is not None:
            return existing
        await node.save()
        return node


def load_generator(directory: Path, tag: str):
    """Import dc3_site.py from `directory` with a stub infrahub_sdk.generator.InfrahubGenerator."""
    sdk = types.ModuleType("infrahub_sdk")
    gen = types.ModuleType("infrahub_sdk.generator")

    class InfrahubGenerator:
        def __init__(self, client):
            self.client = client
    gen.InfrahubGenerator = InfrahubGenerator
    sys.modules["infrahub_sdk"], sys.modules["infrahub_sdk.generator"] = sdk, gen
    os.environ["DC3_RECONCILE"], os.environ["DC3_DRY_RUN"] = "delete", "0"
    spec = importlib.util.spec_from_file_location(f"dc3_site_{tag}", directory / "dc3_site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def query_data(client):
    site = next(iter(client.store["LocationSite"].values())).data
    return {"LocationSite": {"edges": [{"node": {
        "name": {"value": site["name"]}, "shortname": {"value": "VA02"},
        **{k: {"value": site[k]} for k in ("vpc_leaf_pairs", "esi_leaf_pairs", "single_leaves")},
        "fabric_asn": {"node": {"asn": {"value": 65300}}}}}]}}


def run(module, client):
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()) as out:
        asyncio.run(module.Dc3SiteGenerator(client).generate(query_data(client)))
    return out.getvalue()


def snapshot(client):
    return {kind: {n.key() if not kind.startswith("Interface") else n.key(): copy.deepcopy(n.data) for n in nodes.values()}
            for kind, nodes in client.store.items()}


class ConvergeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.new = load_generator(ROOT / "generators", "work")
        sys.path.insert(0, str(ROOT / "generators"))
        import dc3_plan as P
        cls.P = P

    def intf(self, client, device, name):
        dev = next(n for n in client.store["DcimDevice"].values() if n.data["name"] == device)
        return next((n for n in client.store["InterfaceVirtual"].values()
                     if n.data["device"] == dev.id and n.data["name"] == name), None)

    def addr(self, client, id_):
        return client.store["IpamIPAddress"][id_].data["address"]

    def shared(client):
        use = {}
        for kind in ("InterfaceVirtual", "InterfacePhysical", "InterfacePortChannel"):
            for n in client.store.get(kind, {}).values():
                for a in n.data.get("ip_addresses") or []:
                    use.setdefault(a, []).append(n.key())
        return {a for a, ks in use.items() if len(ks) > 1}

    def test_fake_refuses_a_second_interface(self):
        """The fake enforces IpamIPAddress.interface cardinality one, as the live schema."""
        client = FakeClient()
        run(self.new, client)
        lo2 = self.intf(client, "DC3-L1", "Loopback2")
        lo1 = self.intf(client, "DC3-L1", "Loopback1")
        lo1.data["ip_addresses"] = list(lo2.data["ip_addresses"])
        with self.assertRaisesRegex(ValueError, "cardinality one"):
            asyncio.run(lo1.save())

    def test_claim_guard(self):
        gen = self.new.Dc3SiteGenerator(FakeClient())
        dev, other = types.SimpleNamespace(id="d1"), types.SimpleNamespace(id="d2")
        addr = types.SimpleNamespace(id="a1")
        gen.claim([addr], dev, "Loopback2")
        gen.claim([addr], dev, "Loopback2")                                      # same interface again: fine
        with self.assertRaisesRegex(ValueError, "two interfaces"):
            gen.claim([addr], other, "Loopback1")

    def test_check_holder_scope(self):
        client = FakeClient()
        run(self.new, client)
        gen = self.new.Dc3SiteGenerator(client)
        lo2 = self.intf(client, "DC3-L1", "Loopback2")
        addr = client.store["IpamIPAddress"][lo2.data["ip_addresses"][0]]
        dev = lambda n: next(x for x in client.store["DcimDevice"].values() if x.data["name"] == n)   # noqa: E731
        asyncio.run(gen.check_holder(addr, dev("DC3-L1"), "Loopback2"))         # its own holder
        asyncio.run(gen.check_holder(addr, dev("DC3-L1"), "Loopback1"))         # loopback -> loopback, same device: allowed
        with self.assertRaisesRegex(ValueError, "one interface per address"):
            asyncio.run(gen.check_holder(addr, dev("DC3-L2"), "Loopback2"))     # another device
        mgmt = next(n for n in client.store["InterfacePhysical"].values()
                    if n.data["device"] == dev("DC3-L1").id and n.data["name"] == "Management1")
        maddr = client.store["IpamIPAddress"][mgmt.data["ip_addresses"][0]]
        with self.assertRaisesRegex(ValueError, "one interface per address"):
            asyncio.run(gen.check_holder(maddr, dev("DC3-L1"), "Loopback2"))    # non-loopback holder

    def test_fake_upsert_without_addresses_clears(self):
        """The fake models the real upsert: omitting ip_addresses on an existing interface clears them."""
        client = FakeClient()
        run(self.new, client)
        gen = self.new.Dc3SiteGenerator(client)
        dev = next(x for x in client.store["DcimDevice"].values() if x.data["name"] == "DC3-L1")
        asyncio.run(gen.virtual(dev, "Loopback2", "x"))
        self.assertEqual(self.intf(client, "DC3-L1", "Loopback2").data["ip_addresses"], [])


if __name__ == "__main__":
    unittest.main()
