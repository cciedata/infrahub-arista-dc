"""Generator dc3_site: DC3 (Arista EOS) fabric core; standalone Infrahub: branch main; an Infrahub that also holds other sites: a named branch such as dc3.

Stage 1: devices, Management1, loopbacks, spine-leaf links, MLAG and ESI pairs, MLAG plumbing, artifact groups.
Loopback2 with a fallback (the running schema has IpamIPAddress.interface cardinality one): on MLAG VTEP pairs
(L1-L2, BL, SL) the anycast VTEP is only on FabricVpcDomain.anycast_vtep; Loopback1 of both members has no address in
the data (the templates render the anycast there) and Loopback2 holds the switch's own VTEP address (same pool
identifier <device>-Loopback1 as before). A rerun on a stage-1 state creates Loopback2 without addresses and moves the
own address there by saving the ADDRESS with IpamIPAddress.interface = Loopback2 (cardinality one: Infrahub takes it off
Loopback1), then checks that Loopback1 holds nothing. Guards: claim() within a run; check_holder() stops when an
address is held by an interface of another device or by a non-loopback interface.
Stage 3: FRR stand-in devices (one per role), their ports, aggregates, sub-interfaces and loopbacks, the attach
port-channels on BL / SL / EDGE / DMZ, FabricAttachments (TRANSIT, Internal FW tenant VRFs, VPN-partner PARTNER), the
L2-only VLANs on EDGE / DMZ, ISP router and cloud router links (slot tables with DC3 blocks).
Pool allocation by identifier, idempotent upserts, reconciler with report/delete mode. Plan functions live in generators/dc3_plan.py (no SDK).

Environment:
  DC3_DRY_RUN=1          print the plan and exit before any write
  DC3_RECONCILE=report   (default) print what the reconciler would delete; =delete to delete it
"""
import hashlib
import importlib.util
import ipaddress
import os
import sys
from pathlib import Path

from infrahub_sdk.generator import InfrahubGenerator


def _load_plan():
    """Import generators/dc3_plan.py by path: works whatever import root Infrahub or infrahubctl uses (which).

    The module name carries a hash of the file path, so a checkout of another commit (other directory) never
    reuses a dc3_plan module loaded from an older checkout in the same worker process.
    """
    path = Path(__file__).resolve().parent / "dc3_plan.py"
    name = f"dc3_plan_{hashlib.sha1(str(path).encode()).hexdigest()[:12]}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


P = _load_plan()

DRY_RUN = os.environ.get("DC3_DRY_RUN", "0") == "1"
RECONCILE = os.environ.get("DC3_RECONCILE", "report")      # default report: print, delete nothing
if RECONCILE not in ("delete", "report"):
    raise ValueError(f"DC3_RECONCILE must be 'delete' or 'report', got {RECONCILE!r}")

OWNED_VIRTUAL = {P.LOOPBACK0, P.LOOPBACK1, P.LOOPBACK2, "loopback0"}         # loopback0: ISP / cloud stand-ins (stage 3)
OWNED_VIRTUAL_ROLES = {P.ROLE_BACKUP, P.ROLE_PEERING, P.ROLE_CLOUD}           # Vlan3900; stage 3: attachment SVIs, stand-in sub-interfaces
OWNED_PHYSICAL_ROLES = {P.MGMT_ROLE, P.FABRIC_ROLE, P.ROLE_KEEPALIVE, P.ROLE_PEER_LINK,
                        P.ROLE_PEERING, P.ROLE_UPSTREAM, P.ROLE_CLOUD}          # stage 3: attach ports, carrier and exchange ports
OWNED_PORTCHANNEL_ROLES = {P.ROLE_PEER_LINK, P.ROLE_PEERING}
PREFIX_ROLES = {P.FABRIC_ROLE, P.ROLE_KEEPALIVE, P.ROLE_BACKUP, P.ROLE_UPSTREAM}   # interfaces whose link prefix the generator owns
# Not reconciled on their own: standalone addresses inside IP-plan prefixes (edge VLAN VIP); L2 VLAN objects.
# Attachment /29s go with their attachment; carrier /30s and cross-link /31s with their interface.
RECONCILED_ROLES = set(P.DEVICE_TYPE) | P.STANDIN_ROLES


class Dc3SiteGenerator(InfrahubGenerator):
    def claim(self, addresses, device, ifname: str) -> None:
        """Guard within one run: an address goes to one interface only (IpamIPAddress.interface has cardinality one)."""
        claimed = self.__dict__.setdefault("_claimed", {})
        for a in addresses or ():
            owner = claimed.setdefault(a.id, (device.id, ifname))
            if owner != (device.id, ifname):
                raise ValueError(f"address {a.id} would sit on two interfaces: {owner} and {(device.id, ifname)}")

    async def holder(self, address):
        """The interface that holds an address in Infrahub (IpamIPAddress.interface, cardinality one live), or None."""
        addr = await self.client.get(kind="IpamIPAddress", id=address.id, include=["interface"])
        holder_id = addr.interface.id
        if not holder_id:
            return None
        for kind in ("InterfaceVirtual", "InterfacePhysical"):
            intf = await self.client.get(kind=kind, id=holder_id, raise_when_missing=False)
            if intf is not None:
                return intf
        return None

    async def check_holder(self, address, device, ifname: str, holder=None) -> None:
        """Guard on the stored data: an address may move only between loopbacks of the same device (loopback move);
        held by an interface of ANOTHER device or by a non-loopback interface -> stop."""
        if holder is None:
            holder = await self.holder(address)
        if holder is None or (holder.device.id, holder.name.value) == (device.id, ifname):
            return
        if holder.device.id != device.id or not holder.name.value.startswith("Loopback"):
            raise ValueError(f"{address.address.value} is on {holder.name.value} of device {holder.device.id}; "
                             f"not moving it to {ifname} (one interface per address)")

    async def move_address(self, address, device, device_name: str, intf, ifname: str, holder=None) -> bool:
        """loopback fallback move: put an address on `intf` by setting the address's own one-cardinality relationship
        (IpamIPAddress.interface) and saving the ADDRESS; Infrahub then takes it off the previous loopback.
        Does not depend on removing it from the old interface's ip_addresses. Returns True when it moved."""
        if holder is None:
            holder = await self.holder(address)
        self.claim([address], device, ifname)
        if holder is not None and holder.id == intf.id:
            return False
        await self.check_holder(address, device, ifname, holder)
        address.interface = intf
        await address.save()
        print(f"MOVED         {address.address.value} {device_name} "
              f"{holder.name.value if holder is not None else '(no interface)'} -> {ifname}")
        return True

    async def allocate(self, pool, identifier: str, data: dict):
        """Same identifier -> same address on every run."""
        address = await self.client.allocate_next_ip_address(resource_pool=pool, identifier=identifier, data=data)
        if address is None:
            raise ValueError(f"{identifier}: pool {pool.name.value} returned nothing")
        return address

    async def allocate_p31(self, pool, identifier: str, description: str, site):
        """Same identifier -> same /31 on every run; `site` set after allocation."""
        prefix = await self.client.allocate_next_ip_prefix(
            resource_pool=pool, identifier=identifier, data={"description": description, "status": P.LINK_STATUS})
        if prefix is None:
            raise ValueError(f"{identifier}: pool {pool.name.value} returned nothing")
        prefix.site = site
        await prefix.save()
        network = ipaddress.ip_network(str(prefix.prefix.value))
        if network.prefixlen != 31:
            raise ValueError(f"{identifier}: expected a /31, got {network}")
        return network

    async def upsert(self, kind: str, data: dict):
        obj = await self.client.create(kind=kind, data=data)
        await obj.save(allow_upsert=True)
        return obj

    async def p31_address(self, ip, device_name: str, ifname: str, namespace):
        return await self.upsert("IpamIPAddress", {
            "address": f"{ip}/31", "description": f"{device_name} {ifname}", "status": P.LINK_STATUS,
            "ip_namespace": namespace.id})

    async def physical(self, device, name: str, description: str, role: str, mtu: int | None = None, addresses=None,
                       l2_mode: str | None = None):
        data = {"name": name, "device": device.id, "description": description, "role": role, "status": P.INTERFACE_STATUS}
        if mtu:
            data["mtu"] = mtu
        if addresses:
            self.claim(addresses, device, name)
            data["ip_addresses"] = [a.id for a in addresses]
        if l2_mode:
            data["l2_mode"] = l2_mode
        return await self.upsert("InterfacePhysical", data)

    async def virtual(self, device, name: str, description: str, role: str | None = None, mtu: int | None = None,
                      addresses=None, parent=None):
        data = {"name": name, "device": device.id, "description": description, "status": P.INTERFACE_STATUS}
        if role:
            data["role"] = role
        if mtu:
            data["mtu"] = mtu
        if addresses:
            self.claim(addresses, device, name)
            data["ip_addresses"] = [a.id for a in addresses]
        if parent is not None:
            data["parent_interface"] = parent.id
        return await self.upsert("InterfaceVirtual", data)

    async def port_channel(self, device, name: str, description: str, role: str, mtu: int, members, l2_mode: str | None = None):
        data = {"name": name, "device": device.id, "description": description, "role": role, "mtu": mtu,
                "status": P.INTERFACE_STATUS, "members": [m.id for m in members]}
        if l2_mode:
            data["l2_mode"] = l2_mode
        return await self.upsert("InterfacePortChannel", data)

    async def address(self, ip: str, plen: int, description: str, namespace):
        """An address by value (upsert): attachments, edge VLAN, carriers, stand-in loopbacks."""
        return await self.upsert("IpamIPAddress", {
            "address": f"{ip}/{plen}", "description": description, "status": P.LINK_STATUS, "ip_namespace": namespace.id})

    async def fixed_prefix(self, prefix: str, description: str, site, namespace, role: str | None = None):
        """A prefix by value, get-or-create, saved on every run: attachment /29s, cross-link /31s,
        carrier /30s, the SD-WAN overlay."""
        existing = await self.client.get(kind="IpamPrefix", prefix__value=prefix, raise_when_missing=False)
        if existing is not None:
            for attr, value in (("description", description), ("status", P.LINK_STATUS), ("member_type", "address"), ("role", role)):
                if value is not None and getattr(existing, attr).value != value:
                    getattr(existing, attr).value = value
            if existing.site.id != site.id:
                existing.site = site
            await existing.save()
            return existing
        data = {"prefix": prefix, "description": description, "status": P.LINK_STATUS, "member_type": "address",
                "ip_namespace": namespace.id, "site": site.id}
        if role:
            data["role"] = role
        obj = await self.client.create(kind="IpamPrefix", data=data)
        await obj.save()
        return obj

    # ---------------------------------------------------------------- reconciliation
    async def owned_interfaces(self, device) -> list[tuple]:
        """(interface, owns_prefix) for the interfaces this generator owns on a device; nothing else."""
        owned = []
        for intf in await self.client.filters(kind="InterfaceVirtual", device__ids=[device.id], include=["ip_addresses"]):
            if intf.name.value in OWNED_VIRTUAL or intf.role.value in OWNED_VIRTUAL_ROLES:
                owned.append((intf, intf.role.value in PREFIX_ROLES))
        for intf in await self.client.filters(kind="InterfacePhysical", device__ids=[device.id], include=["ip_addresses"]):
            if intf.role.value in OWNED_PHYSICAL_ROLES:
                owned.append((intf, intf.role.value in PREFIX_ROLES))
        for intf in await self.client.filters(kind="InterfacePortChannel", device__ids=[device.id], include=["ip_addresses"]):
            if intf.role.value in OWNED_PORTCHANNEL_ROLES:
                owned.append((intf, False))
        return owned

    async def remove(self, label: str, node) -> None:
        if RECONCILE == "delete":
            await node.delete()
            print(f"DELETED       {label}")
        else:
            print(f"WOULD DELETE  {label}")

    async def remove_prefix_with_addresses(self, network: str, done_prefixes: set) -> None:
        if network in done_prefixes:
            return
        done_prefixes.add(network)
        prefix = await self.client.get(kind="IpamPrefix", prefix__value=network, raise_when_missing=False, include=["ip_addresses"])
        if prefix is None:
            return
        for peer in prefix.ip_addresses.peers:
            addr = await self.client.get(kind="IpamIPAddress", id=peer.id, raise_when_missing=False)
            if addr is not None:
                await self.remove(f"IpamIPAddress {addr.address.value} (in {network})", addr)
        await self.remove(f"IpamPrefix    {network}", prefix)

    async def remove_interface(self, device_name: str, intf, owns_prefix: bool, done_prefixes: set) -> None:
        addresses, networks = [], []
        for peer in intf.ip_addresses.peers:
            address = await self.client.get(kind="IpamIPAddress", id=peer.id, raise_when_missing=False)
            if address is None:
                continue
            addresses.append(address)
            if owns_prefix:
                networks.append(str(ipaddress.ip_interface(str(address.address.value)).network))
        await self.remove(f"Interface     {device_name} {intf.name.value}", intf)
        for address in addresses:
            await self.remove(f"IpamIPAddress {address.address.value} ({device_name} {intf.name.value})", address)
        for network in networks:
            await self.remove_prefix_with_addresses(network, done_prefixes)

    async def reconcile(self, site_name: str, plan, links, pairs, domains, standins=(), expected_extra=None,
                        attachments=()) -> None:
        """Delete (or report) what exists in this generator's scope on the site but is not in the plan."""
        expected_devices = {r["name"] for r in plan} | {s["name"] for s in standins}
        expected_if = P.merge_expected(P.expected_interfaces(plan, links, domains), expected_extra or {})
        expected_attachments = {a["name"] for a in attachments}
        expected_pairs = {p["name"] for p in pairs}
        done_prefixes: set = set()
        pair_prefixes = (f"{site_name}-MLAG-", f"{site_name}-ESI-")

        # 1. Pairs first; a removed MLAG pair takes its anycast VTEP.
        for kind in ("FabricVpcDomain", "FabricEsiPair"):
            for pair in await self.client.filters(kind=kind):
                n = pair.name.value
                if n.startswith(pair_prefixes) and n not in expected_pairs:
                    anycast = None
                    if kind == "FabricVpcDomain" and pair.anycast_vtep.id:
                        anycast = await self.client.get(kind="IpamIPAddress", id=pair.anycast_vtep.id)
                    await self.remove(f"Pair          {n}", pair)
                    if anycast is not None:
                        await self.remove(f"IpamIPAddress {anycast.address.value} ({n} anycast VTEP)", anycast)

        devices = [d for d in await self.client.filters(kind="DcimDevice", location__name__value=site_name)
                   if d.role.value in RECONCILED_ROLES]

        # 2. Orphan interfaces on devices that stay.
        for d in devices:
            name = d.name.value
            if name in expected_devices:
                for intf, owns_prefix in await self.owned_interfaces(d):
                    if intf.name.value not in expected_if[name]:
                        await self.remove_interface(name, intf, owns_prefix, done_prefixes)

        # 3. Orphan devices, their owned interfaces first.
        for d in devices:
            name = d.name.value
            if name not in expected_devices:
                for intf, owns_prefix in await self.owned_interfaces(d):
                    await self.remove_interface(name, intf, owns_prefix, done_prefixes)
                await self.remove(f"Device        {name}", d)

        # 4. Orphan attachments of this site: the object, then its /29 with every address in it.
        for att in await self.client.filters(kind="FabricAttachment", site__name__value=site_name, include=["prefix"]):
            n = att.name.value
            if n not in expected_attachments:
                network = None
                if att.prefix.id:
                    pfx = await self.client.get(kind="IpamPrefix", id=att.prefix.id, raise_when_missing=False)
                    network = str(pfx.prefix.value) if pfx is not None else None
                await self.remove(f"Attachment    {n}", att)
                if network:
                    await self.remove_prefix_with_addresses(network, done_prefixes)

        print(f"reconcile ({RECONCILE}) done")

    # ---------------------------------------------------------------- stage 3
    async def site_vrfs(self, site_name: str) -> dict:
        """VRF name -> id for every VRF that lists this site (GraphQL, one request)."""
        r = await self.client.execute_graphql(
            query="query { FabricVRF { edges { node { id name { value } sites { edges { node { name { value } } } } } } } }")
        out = {}
        for e in r["FabricVRF"]["edges"]:
            n = e["node"]
            if site_name in [s["node"]["name"]["value"] for s in n["sites"]["edges"]]:
                out[n["name"]["value"]] = n["id"]
        return out

    async def write_stage3(self, site_name, site, namespace, devices, pair_objs, vrfs, s3) -> None:
        """Attach ports, attachments, edge VLAN addresses, L2 VLANs, ISP routers, FW stand-ins, cloud routers."""
        # ---- switch ports toward the stand-ins: one member port + its MLAG port-channel (mlag id = Po number)
        for ap in s3["attach_ports"]:
            device = devices[ap["device"]]
            member = await self.physical(device, ap["port"], ap["port_desc"], P.ROLE_PEERING, mtu=P.EXT_MTU, l2_mode="trunk")
            await self.port_channel(device, ap["po"], ap["po_desc"], P.ROLE_PEERING, P.EXT_MTU, [member], l2_mode="trunk")
            print(f"{ap['device']:<10} {ap['port']:<13} -> {ap['far']:<13} {ap['far_port']:<10} {ap['po']}")

        # ---- attachments: /29 by slot, leaf SVIs (.1 lower member, .2 upper), stand-in .3 on its sub-interface (below)
        for a in s3["attachments"]:
            pfx = await self.fixed_prefix(a["prefix"], f"{a['name']} attachment /29 (VLAN {a['vlan']})", site, namespace, role="link")
            for m in a["members"]:
                addr = await self.address(a["addrs"][m], 29, f"{m} Vlan{a['vlan']} ({a['name']})", namespace)
                await self.virtual(devices[m], f"Vlan{a['vlan']}", a["name"], role=P.ROLE_PEERING, mtu=P.EXT_MTU, addresses=[addr])
            await self.upsert("FabricAttachment", {
                "name": a["name"], "slot": a["slot"], "vlan_id": a["vlan"],
                "description": f"{' + '.join(a['devices'])} in {a['vrf']} on {a['pair']}",
                "vrf": vrfs[a["vrf"]], "prefix": pfx.id, "switch_pair": pair_objs[a["pair"]].id,
                "devices": [devices[d].id for d in a["devices"]], "site": site.id})
            m1, m2 = a["members"]
            print(f"{a['name']:<22} VLAN {a['vlan']} {a['prefix']:<16} {m1}={a['addrs'][m1]} {m2}={a['addrs'][m2]} "
                  f"standin={a['addrs']['standin']}")

        # ---- edge VLAN standalone address: the ISP routers' VRRP VIP (not on any interface)
        edge = P.edge_vlan_addresses(site_name)
        await self.address(edge["VIP"], 24, f"{site_name} edge VLAN {P.EDGE_VLAN} VRRP VIP (VRID 1)", namespace)

        # ---- L2-only VLANs: edge VLAN on the EDGE pair, DMZ VLANs on the DMZ pair (/24s owned by the IP plan)
        for v in s3["l2vlans"]:
            pfx = await self.client.get(kind="IpamPrefix", prefix__value=v["prefix"])   # raises if missing; never saved
            await self.upsert("FabricL2Vlan", {"name": v["name"], "vlan_id": v["vlan"], "description": v["description"],
                                               "switch_pair": pair_objs[v["pair"]].id, "site": site.id, "prefix": pfx.id})
            print(f"{v['name']:<22} VLAN {v['vlan']} {v['prefix']} on {v['pair']}")

        # ---- ISP router stand-ins: loopback0, edge bundle + tagged sub-interface, iBGP cross-link /31, carrier /30
        for r in s3["isp"]:
            device = devices[r["name"]]
            lo = await self.address(r["loopback0"], 32, f"{r['name']} loopback0", namespace)
            await self.virtual(device, "loopback0", P.LOOPBACK0_DESCRIPTION, addresses=[lo])
            members = [await self.physical(device, port, f"{far} {far_port}", P.ROLE_PEERING, mtu=P.EXT_MTU)
                       for port, far, far_port in r["edge_ports"]]
            bundle = await self.port_channel(device, r["bundle"], r["bundle_desc"], P.ROLE_PEERING, P.EXT_MTU, members)
            sub_addr = await self.address(r["subif_addr"], 24, f"{r['name']} {r['subif']}", namespace)
            await self.virtual(device, r["subif"], f"Edge VLAN {P.EDGE_VLAN}", role=P.ROLE_PEERING, mtu=P.EXT_MTU,
                               addresses=[sub_addr], parent=bundle)
            x = r["crosslink"]
            await self.fixed_prefix(x["prefix"], f"{site_name} ISP router iBGP cross-link", site, namespace, role="link")
            x_addr = await self.address(x["addr"], 31, f"{r['name']} {x['port']}", namespace)
            await self.physical(device, x["port"], f"{x['peer']} {x['port']}", P.FABRIC_ROLE, mtu=P.EXT_MTU, addresses=[x_addr])
            c = r["carrier"]
            await self.fixed_prefix(c["prefix"], f"{r['name']} <-> {c['provider']} handoff", site, namespace, role="link")
            ours = await self.address(c["ours"], 30, f"{r['name']} {c['port']}", namespace)
            await self.address(c["theirs"], 30, f"{c['provider']} side of the {r['name']} handoff", namespace)
            await self.physical(device, c["port"], f"{c['provider']} carrier handoff", P.ROLE_UPSTREAM, mtu=P.EXT_MTU, addresses=[ours])
            print(f"{r['name']:<9} lo0={r['loopback0']} {r['subif']}={r['subif_addr']} xlink={x['addr']}/31 "
                  f"carrier={c['ours']}/30 ({c['provider']} {c['theirs']})")

        # ---- FW stand-ins: ports, aggregates, VLAN sub-interfaces, LO-WAN / ADVPN-HUB (WAN FWs)
        for fw in s3["fws"]:
            device = devices[fw["name"]]
            members: dict = {}
            for p, desc, agg in fw["ports"]:
                members.setdefault(agg, []).append(await self.physical(device, p, desc, P.ROLE_PEERING, mtu=P.EXT_MTU))
            aggs = {agg: await self.port_channel(device, agg, desc, P.ROLE_PEERING, P.EXT_MTU, members[agg])
                    for agg, desc in fw["aggs"].items()}
            for sub in fw["subifs"]:
                addr = await self.address(sub["addr"][0], sub["addr"][1], f"{fw['name']} {sub['name']}", namespace)
                await self.virtual(device, sub["name"], sub["desc"], role=P.ROLE_PEERING, mtu=P.EXT_MTU, addresses=[addr],
                                   parent=aggs[sub["parent"]])
            for x in fw["virtuals"]:
                if x["plan_prefix"]:
                    await self.client.get(kind="IpamPrefix", prefix__value=x["plan_prefix"])   # IP plan; raises if missing
                if x["own_prefix"]:
                    await self.fixed_prefix(x["own_prefix"], x["own_prefix_desc"], site, namespace)
                addr = await self.address(x["addr"][0], x["addr"][1], f"{fw['name']} {x['name']}", namespace)
                await self.virtual(device, x["name"], x["desc"], role=P.ROLE_PEERING, addresses=[addr])
            print(f"{fw['name']:<13} ports {len(fw['ports'])} aggs {len(fw['aggs'])} subifs {len(fw['subifs'])} "
                  f"virtuals {len(fw['virtuals'])}")

        # ---- cloud router stand-ins: loopback0, fabric bundle + TRANSIT sub-interface, cross-link, exchange handoffs
        for r in s3["cloud"]:
            device = devices[r["name"]]
            lo = await self.address(r["loopback0"], 32, f"{r['name']} loopback0", namespace)
            await self.virtual(device, "loopback0", P.LOOPBACK0_DESCRIPTION, addresses=[lo])
            members = [await self.physical(device, port, f"{far} {far_port}", P.ROLE_PEERING, mtu=P.EXT_MTU)
                       for port, far, far_port in r["fabric_ports"]]
            bundle = await self.port_channel(device, r["bundle"], r["bundle_desc"], P.ROLE_PEERING, P.EXT_MTU, members)
            sub_addr = await self.address(r["subif_addr"], 29, f"{r['name']} {r['subif']} ({r['attachment']})", namespace)
            await self.virtual(device, r["subif"], r["attachment"], role=P.ROLE_PEERING, mtu=P.EXT_MTU, addresses=[sub_addr],
                               parent=bundle)
            x = r["crosslink"]
            await self.fixed_prefix(x["prefix"], f"{site_name} cloud router iBGP link", site, namespace, role="link")
            x_addr = await self.address(x["addr"], 31, f"{r['name']} {x['port']}", namespace)
            await self.physical(device, x["port"], f"{x['peer']} {x['port']}", P.FABRIC_ROLE, mtu=P.EXT_MTU, addresses=[x_addr])
            xc = await self.physical(device, r["xc"]["port"], r["xc"]["desc"], P.ROLE_CLOUD, mtu=P.EXT_MTU)
            for h in r["handoffs"]:
                await self.client.get(kind="IpamPrefix", prefix__value=h["prefix"])   # IP plan; raises if missing
                ours = await self.address(h["ours"], 30, f"{r['name']} {h['subif']}", namespace)
                await self.address(h["theirs"], 30, f"{h['provider']} side of the {r['name']} {h['subif']} handoff", namespace)
                await self.virtual(device, h["subif"], h["desc"], role=P.ROLE_CLOUD, mtu=P.CLOUD_HANDOFF_MTU, addresses=[ours],
                                   parent=xc)
            print(f"{r['name']:<9} lo0={r['loopback0']} {r['subif']}={r['subif_addr']} xlink={x['addr']}/31 "
                  + " ".join(f"{h['subif']}={h['ours']}/30" for h in r["handoffs"]))

    # ---------------------------------------------------------------- generation
    async def generate(self, data: dict) -> None:
        self._claimed = {}                            # claim() guard: address id -> (device id, interface), this run
        edges = data["LocationSite"]["edges"]
        if len(edges) != 1:
            raise ValueError(f"expected exactly 1 site, got {len(edges)}")
        node = edges[0]["node"]
        site_name = node["name"]["value"]

        counts = {k: node[k]["value"] for k in ("vpc_leaf_pairs", "esi_leaf_pairs", "single_leaves")}
        if any(v is None for v in counts.values()):
            raise ValueError(f"{site_name}: leaf counts not set: {counts}")
        if node["fabric_asn"]["node"] is None:
            raise ValueError(f"{site_name}: fabric_asn not set")
        fabric_asn = node["fabric_asn"]["node"]["asn"]["value"]
        if fabric_asn != P.SPINE_ASN:
            raise ValueError(f"{site_name}: fabric_asn {fabric_asn} differs from the decided spine ASN {P.SPINE_ASN}")

        # Plans and guards before anything is written.
        plan = P.build_plan(site_name, counts["vpc_leaf_pairs"], counts["esi_leaf_pairs"], counts["single_leaves"], fabric_asn)
        links = P.build_links(site_name, plan)
        pairs = P.build_pairs(site_name, plan, counts["vpc_leaf_pairs"], counts["esi_leaf_pairs"])
        domains = P.mlag_domains(plan, pairs)
        expected = P.expected_interfaces(plan, links, domains)

        # Stage 3 plan (needs the site's VRFs, objects/dc3-06-fabric.yml)
        vrfs = await self.site_vrfs(site_name)
        missing = P.DC3_VRFS - set(vrfs)
        if missing:
            raise ValueError(f"{site_name}: VRFs {sorted(missing)} do not list the site (load objects/dc3-06-fabric.yml first)")
        standins = P.build_standins(site_name, fabric_asn)
        attach_ports = P.build_attach_ports(site_name, plan, standins)
        attachments = P.build_attachments(site_name, standins, list(vrfs))
        l2vlans = P.build_l2_vlans(site_name)
        isp = P.build_isp(site_name)
        fws = P.build_fw(site_name, standins, attach_ports, attachments, l2vlans)
        cloud = P.build_cloud(site_name, attachments)
        expected_s3 = P.expected_standin_interfaces(standins, attach_ports, attachments, isp, fws, cloud)
        s3 = {"attach_ports": attach_ports, "attachments": attachments, "l2vlans": l2vlans, "isp": isp, "fws": fws, "cloud": cloud}

        # Resolve every reference once; get() raises if the object is missing.
        site = await self.client.get(kind="LocationSite", name__value=site_name)
        platform = await self.client.get(kind="DcimPlatform", name__value=P.PLATFORM)
        types = {m: await self.client.get(kind="DcimDeviceType", name__value=m) for m in {r["model"] for r in plan}}
        asns = {a: await self.client.get(kind="FabricAutonomousSystem", asn__value=a)
                for a in sorted({r["asn"] for r in plan + standins if r["asn"] is not None})}
        standin_platform = await self.client.get(kind="DcimPlatform", name__value=P.STANDIN_PLATFORM)
        standin_type = await self.client.get(kind="DcimDeviceType", name__value=P.STANDIN_DEVICE_TYPE)
        pools = {k: await self.client.get(kind="CoreIPAddressPool", name__value=P.pool_name(site_name, k))
                 for k in ("loopback0", "loopback1", "mgmt", "anycast")}
        p2p_pool = await self.client.get(kind="CoreIPPrefixPool", name__value=P.pool_name(site_name, "p2p"))
        keepalive_pool = await self.client.get(kind="CoreIPPrefixPool", name__value=P.pool_name(site_name, "keepalive"))
        backup_pool = await self.client.get(kind="CoreIPPrefixPool", name__value=P.pool_name(site_name, "backup"))
        namespace = await self.client.get(kind="IpamNamespace", name__value="default")
        mlag_vtep = P.mlag_vtep_members(pairs)
        members_by_group = {**P.group_members(plan), **P.standin_group_members(standins)}
        groups = {g: await self.client.get(kind="CoreStandardGroup", name__value=g) for g in members_by_group}

        if DRY_RUN:     # after the reads, so a dry run also proves every referenced object exists
            P.print_plan(site_name, plan, links, pairs, domains, expected)
            P.print_plan_stage3(site_name, standins, attach_ports, attachments, l2vlans, isp, fws, cloud, list(vrfs))
            print("DRY RUN: nothing written")
            return

        # ---- devices, Management1, loopbacks
        devices = {}
        for row in plan:
            name, role = row["name"], row["role"]
            mgmt_address = await self.allocate(pools["mgmt"], f"{name}-{P.MGMT_IF}",
                                               {"description": f"{name} {P.MGMT_IF}", "status": P.MGMT_ADDRESS_STATUS})
            payload = {"name": name, "description": row["description"], "status": P.STATUS, "role": role,
                       "location": site.id, "platform": platform.id, "device_type": types[row["model"]].id,
                       "os_version": P.OS_VERSION, "primary_address": mgmt_address.id}
            if row["asn"] is not None:
                payload["asn"] = asns[row["asn"]].id
            device = await self.upsert("DcimDevice", payload)
            devices[name] = device
            await self.physical(device, P.MGMT_IF, P.MGMT_DESCRIPTION, P.MGMT_ROLE, addresses=[mgmt_address])
            report = [f"{P.MGMT_IF}={mgmt_address.address.value}"]
            for pool_key, ifname, description, identifier in P.device_loopbacks(row, mlag_vtep):
                address = await self.allocate(pools[pool_key], identifier, {"description": f"{name} {ifname}"})
                if address.description.value != f"{name} {ifname}":     # address moved Loopback1 -> Loopback2
                    address.description.value = f"{name} {ifname}"
                    await address.save()
                if ifname == P.LOOPBACK2:
                    # loopback fallback. A real upsert that omits ip_addresses CLEARS them (seen in a test run), so Loopback2 is
                    # always written WITH the own address, except while the address still sits on Loopback1 (stage-1
                    # state): then Loopback2 is created empty and the address is moved by saving the address.
                    holder = await self.holder(address)
                    on_lo1 = holder is not None and (holder.device.id, holder.name.value) == (device.id, P.LOOPBACK1)
                    if on_lo1:
                        lo2 = await self.virtual(device, ifname, description)
                        await self.move_address(address, device, name, lo2, ifname, holder)
                    else:
                        await self.check_holder(address, device, ifname, holder)
                        await self.virtual(device, ifname, description, addresses=[address])
                else:
                    await self.check_holder(address, device, ifname)
                    await self.virtual(device, ifname, description, addresses=[address])
                report.append(f"{ifname}={address.address.value}")
            print(f"{name:<10} {role:<13} asn={str(row['asn'] or '-'):<6} " + " ".join(report))

        # ---- stage 3: stand-in devices (after the switches, so the switches keep their Management1 allocations)
        for row in standins:
            name = row["name"]
            mgmt_address = await self.allocate(pools["mgmt"], f"{name}-{P.STANDIN_MGMT_IF}",
                                               {"description": f"{name} {P.STANDIN_MGMT_IF}", "status": P.MGMT_ADDRESS_STATUS})
            device = await self.upsert("DcimDevice", {
                "name": name, "description": row["description"], "status": P.STATUS, "role": row["role"], "location": site.id,
                "platform": standin_platform.id, "device_type": standin_type.id, "os_version": P.STANDIN_OS_VERSION,
                "primary_address": mgmt_address.id, "asn": asns[row["asn"]].id})
            devices[name] = device
            await self.physical(device, P.STANDIN_MGMT_IF, P.MGMT_DESCRIPTION, P.MGMT_ROLE, addresses=[mgmt_address])
            print(f"{name:<13} {row['role']:<13} asn={row['asn']:<6} {P.STANDIN_MGMT_IF}={mgmt_address.address.value}")

        # ---- artifact target groups: add this site's devices; other members are kept (fetch, then add)
        for gname, group in groups.items():
            await group.members.fetch()
            have = {peer.id for peer in group.members.peers}
            new = [devices[n].id for n in members_by_group[gname] if devices[n].id not in have]
            for node_id in new:
                group.members.add(node_id)
            if new:
                await group.save()
            print(f"group {gname:<26} +{len(new)} (members now {len(have) + len(new)})")

        # ---- spine-leaf links: /31 per link, description <peer device> <peer interface>
        for link in links:
            network = await self.allocate_p31(p2p_pool, f"{link['spine']}-{link['spine_port']}",
                                              f"{link['spine']} {link['spine_port']} link", site)
            ends = ((link["spine"], link["spine_port"], link["leaf"], link["leaf_port"], network[0]),
                    (link["leaf"], link["leaf_port"], link["spine"], link["spine_port"], network[1]))
            for device_name, ifname, peer, peer_if, ip in ends:
                address = await self.p31_address(ip, device_name, ifname, namespace)
                await self.physical(devices[device_name], ifname, f"{peer} {peer_if}", P.FABRIC_ROLE,
                                    mtu=P.FABRIC_MTU, addresses=[address])
            print(f"{link['spine']:<7} {link['spine_port']:<13} <-> {link['leaf']:<9} {link['leaf_port']:<13} {network}")

        # ---- switch pairs; MLAG pairs with VTEPs get an anycast VTEP on the pair only; Loopback1 of both members without
        #      an address (loopback fallback; the templates render the pair's anycast VTEP on Loopback1)
        pair_objs = {}
        for pair in pairs:
            m1, m2 = pair["members"]
            data = {"name": pair["name"], "members": [devices[m1].id, devices[m2].id]}
            anycast = "-"
            anycast_address = None
            if pair["domain_id"] is not None:
                data["domain_id"] = pair["domain_id"]
                if pair["nve"]:
                    anycast_address = await self.allocate(pools["anycast"], pair["name"], {"description": f"{pair['name']} anycast VTEP"})
                    data["anycast_vtep"] = anycast_address.id
                    anycast = str(anycast_address.address.value)
            pair_objs[pair["name"]] = await self.upsert(pair["kind"], data)
            if anycast_address is not None:
                for member in (m1, m2):
                    lo1 = await self.virtual(devices[member], P.LOOPBACK1, P.LOOPBACK1_ANYCAST_DESCRIPTION.format(pair=pair["name"]))
                    # loopback fallback: Loopback1 must carry no address (the own address moved to Loopback2 above)
                    check = await self.client.get(kind="InterfaceVirtual", id=lo1.id, include=["ip_addresses"])
                    if check.ip_addresses.peers:
                        raise ValueError(f"{member} {P.LOOPBACK1} still holds {len(check.ip_addresses.peers)} address(es) "
                                         "after the move to Loopback2; stopping (loopback fallback)")
            print(f"{pair['name']:<20} {pair['kind']:<16} id={str(pair['domain_id']):<5} {m1} + {m2}  anycast={anycast}")

        # ---- MLAG plumbing on every MLAG pair: keepalive /31, Vlan3900 /31, peer-link Port-Channel1000
        for d in domains:
            m1, m2 = d["members"]
            ka_net = await self.allocate_p31(keepalive_pool, f"{m1}-{d['keepalive']}", f"{d['name']} MLAG peer-keepalive", site)
            bk_net = await self.allocate_p31(backup_pool, f"{m1}-{P.MLAG_SVI}", f"{d['name']} MLAG {P.MLAG_SVI}", site)
            for device_name, peer, i in ((m1, m2, 0), (m2, m1, 1)):
                device = devices[device_name]
                ka_addr = await self.p31_address(ka_net[i], device_name, d["keepalive"], namespace)
                await self.physical(device, d["keepalive"], f"{peer} {d['keepalive']}", P.ROLE_KEEPALIVE,
                                    mtu=P.MLAG_MTU, addresses=[ka_addr])
                bk_addr = await self.p31_address(bk_net[i], device_name, P.MLAG_SVI, namespace)
                await self.virtual(device, P.MLAG_SVI, f"{peer} {P.MLAG_SVI}", role=P.ROLE_BACKUP, mtu=P.MLAG_MTU,
                                   addresses=[bk_addr])
                members = [await self.physical(device, ifname, f"{peer} {ifname}", P.ROLE_PEER_LINK, mtu=P.MLAG_MTU)
                           for ifname in d["peer_link"]]
                await self.port_channel(device, P.PEER_LINK, f"{peer} {P.PEER_LINK}", P.ROLE_PEER_LINK, P.MLAG_MTU, members)
                print(f"{device_name:<10} {d['keepalive']}={ka_net[i]}/31 {P.MLAG_SVI}={bk_net[i]}/31 "
                      f"{P.PEER_LINK}=[{', '.join(d['peer_link'])}] peer={peer}")

        await self.write_stage3(site_name, site, namespace, devices, pair_objs, vrfs, s3)

        await self.reconcile(site_name, plan, links, pairs, domains, standins, expected_s3, attachments)
