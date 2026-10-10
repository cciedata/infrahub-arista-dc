"""DC3 (Arista EOS) plan: devices, ports, links, ASNs, MLAG/ESI pairs.

Pure Python, no infrahub_sdk import, so tests/test_dc3_plan.py runs without Infrahub.
generators/dc3_site.py imports everything from here and only adds the Infrahub reads and writes.

The values are listed in plans/dc3-values.md and docs/DESIGN.md.
"""

SITE = "DC3"

# ---------------------------------------------------------------- devices
PLATFORM = "Arista EOS"                 # DcimPlatform in objects/dc3-04-hardware.yml
OS_VERSION = "4.36.2F"                  # (cEOS-lab 4.36.2F; same release on hardware)
STATUS = "provisioning"                 # device status written by the generator
MAX_SERVER_LEAVES = 20                  # spine port map: 2 x 20 server-leaf ports per spine
DEVICE_TYPE = {
    "spine": "DCS-7060DX5-64S",
    "leaf": "DCS-7050SX3-48YC8C",
    "dmz": "DCS-7050SX3-48YC8C",
    "border_leaf": "DCS-7280DR3-24",
    "service_leaf": "DCS-7280DR3-24",
    "edge": "DCS-7050DX4-32S",
}

# Fixed pairs, no BGW and no DCI; the FWs, ISP and cloud routers are stand-ins outside the fabric
# and are built in stage 3. Device names carry the DC3- prefix.
FIXED_PAIRS = (
    ("BL", "border_leaf", "Border leaf {n}"),
    ("SL", "service_leaf", "Service leaf {n}"),
    ("EDGE", "edge", "Edge switch {n}, untrusted side, L2 only"),
    ("DMZ", "dmz", "DMZ switch {n}, L2 only, behind the Internet firewall"),
)

# ---------------------------------------------------------------- ASNs
SPINE_ASN = 65300                       # one ASN for both spines (= LocationSite.fabric_asn)
LEAF_ASN_FIRST = 65371                  # leaves/pairs 65371-65391, spare 65392-65399
LEAF_ASN_LAST = 65391
NO_ASN_ROLES = {"edge", "dmz"}          # edge/DMZ have no BGP, no ASN
# order by role: BL pair, SL pair, (BGW: none in DC3), then server leaves in name order.
# an MLAG pair shares one ASN; ESI members and single leaves get one ASN each.
ASN_ROLE_ORDER = ("border_leaf", "service_leaf")

# ---------------------------------------------------------------- interfaces
# ONE table for the port-number -> EOS-name rule.
# (to be confirmed in the lab): SFP-type ports are EthernetN, QSFP/QSFP-DD ports are EthernetN/1.
# 7050DX4-32S SFP+ ports numbered 33 and 34 (datasheet: 32 x QSFP-DD + 2 x SFP+; the numbers 33/34 are unverified).
# Port counts from the Arista datasheets of each model.
# Spine SFP+ ports (2 x SFP+ on 7060DX5-64S) are not used by DC3 and not listed.
PORT_NAMING = {
    "DCS-7060DX5-64S": ((1, 64, "qsfp"),),                       # 64 x 400G QSFP-DD
    "DCS-7050SX3-48YC8C": ((1, 48, "sfp"), (49, 56, "qsfp")),    # 48 x SFP28 + 8 x QSFP28
    "DCS-7280DR3-24": ((1, 24, "qsfp"),),                        # 24 x 400G QSFP-DD
    "DCS-7050DX4-32S": ((1, 32, "qsfp"), (33, 34, "sfp")),       # 32 x 400G QSFP-DD + 2 x SFP+ (33/34 )
}

MGMT_IF = "Management1"                 # (Management1 via cEOS interface mapping)
MGMT_ROLE = "management"                # interface role of Management1
MGMT_DESCRIPTION = "OOB management"     # Management1 description
MGMT_ADDRESS_STATUS = "active"          # status of management addresses
INTERFACE_STATUS = "provisioning"       # status of interfaces written by the generator
LINK_STATUS = "active"                  # status of link prefixes and addresses
LOOPBACK0 = "Loopback0"                 # router-ID / EVPN source
LOOPBACK1 = "Loopback1"                 # VTEP source; on MLAG VTEP pairs the shared anycast VTEP
LOOPBACK2 = "Loopback2"                 # per-switch VTEP address on MLAG VTEP pairs
LOOPBACK0_DESCRIPTION = "Router ID, BGP peering"
LOOPBACK1_DESCRIPTION = "VTEP source (Vxlan1)"
LOOPBACK2_DESCRIPTION = "VTEP per-switch address"
# loopback fallback (the running schema has IpamIPAddress.interface with cardinality one): on MLAG VTEP members Loopback1
# has no address in the data; the pair's anycast VTEP lives only on FabricVpcDomain.anycast_vtep and the templates
# render it on Loopback1.
LOOPBACK1_ANYCAST_DESCRIPTION = "VTEP source (Vxlan1), anycast from {pair}"
NO_LOOPBACK0_ROLES = {"edge", "dmz"}    # edge/DMZ are L2 only, no loopback0
VTEP_ROLES = {"leaf", "border_leaf", "service_leaf"}  # Loopback1 and anycast VTEP on MLAG pairs

# ---------------------------------------------------------------- spine-leaf links (spine port map)
FABRIC_ROLE = "core"
FABRIC_MTU = 9214
SERVER_LEAF_UPLINKS = {"S1": (49, 50), "S2": (51, 52)}  # server-leaf ports 49-52
FIXED_LEAF_UPLINKS = {"S1": 1, "S2": 2}                 # BL/SL ports 1, 2
SPINE_FIXED_PORTS = (("BL1", 41), ("BL2", 42), ("SL1", 43), ("SL2", 44))  # BGW ports 45/46 unused

# ---------------------------------------------------------------- pairs (domain ID = site base + slot)
MLAG_DOMAIN_BASE = {"DC3": 300}         # site base; MLAG domain ID = base + slot
# Slots: 1 = first MLAG server pair, 2 BL, 3 SL, 4 EDGE, 5 DMZ, further MLAG server pairs from 6.
MLAG_FIXED_SLOTS = (("BL", 2), ("SL", 3), ("EDGE", 4), ("DMZ", 5))
FIRST_EXTRA_SERVER_SLOT = 6
MLAG_PAIR_NAME = "{site}-MLAG-{a}-{b}"  # (also the EOS domain-id string)
ESI_PAIR_NAME = "{site}-ESI-{a}-{b}"    # ESI pair name pattern

# ---------------------------------------------------------------- MLAG plumbing
PEER_LINK = "Port-Channel1000"
MLAG_SVI = "Vlan3900"                   # on every MLAG pair, /31 from <site>-VPC-BACKUP
MLAG_MTU = 9214                         # (SVI); peer-link and keepalive too: 9214 is the EOS L3 maximum
ROLE_PEER_LINK = "vpc_peer_link"
ROLE_KEEPALIVE = "vpc_keepalive"
ROLE_BACKUP = "vpc_backup"
# role: peer-link member port numbers and keepalive port number
MLAG_PORTS = {
    "leaf":         {"peer_link": (53, 54), "keepalive": 48},
    "dmz":          {"peer_link": (53, 54), "keepalive": 48},
    "border_leaf":  {"peer_link": (3, 4, 5, 6), "keepalive": 24},      # 100G QSFP DAC
    "service_leaf": {"peer_link": (3, 4, 5, 6), "keepalive": 24},
    "edge":         {"peer_link": (1, 2, 3, 4), "keepalive": 34},      # second SFP+ port (34)
}

# ---------------------------------------------------------------- artifact target groups (add only)
ARTIFACT_GROUPS = {
    "spine": "dc3-spines",
    "leaf": "dc3-server-leaves",
    "border_leaf": "dc3-border-service-leaves",
    "service_leaf": "dc3-border-service-leaves",
    "edge": "dc3-l2-pairs",
    "dmz": "dc3-l2-pairs",
}

# ---------------------------------------------------------------- pools (scripts/dc3_create_pools.py)
POOL_SUFFIX = {
    "loopback0": "LOOPBACK0",
    "loopback1": "LOOPBACK1-VTEP",
    "mgmt": "MGMT0",
    "anycast": "ANYCAST-VTEP",
    "p2p": "FABRIC-P2P",
    "keepalive": "VPC-KEEPALIVE",
    "backup": "VPC-BACKUP",
}


def pool_name(site: str, key: str) -> str:
    return f"{site}-{POOL_SUFFIX[key]}"


def eos_port(model: str, n: int) -> str:
    """Front-panel port number -> EOS interface name for a model (PORT_NAMING )."""
    if model not in PORT_NAMING:
        raise ValueError(f"{model}: no port naming rule")
    for first, last, kind in PORT_NAMING[model]:
        if first <= n <= last:
            return f"Ethernet{n}" if kind == "sfp" else f"Ethernet{n}/1"
    raise ValueError(f"{model}: no port {n}")


def mlag_vtep_members(pairs: list[dict]) -> set:
    """Members of MLAG pairs with VTEPs (L1-L2, BL, SL): Loopback1 = shared anycast VTEP, Loopback2 = own address ."""
    return {m for p in pairs if p["kind"] == "FabricVpcDomain" and p["nve"] for m in p["members"]}


def device_loopbacks(row: dict, mlag_vtep: set) -> list[tuple]:
    """Loopbacks allocated from a per-device pool: (pool key, interface, description, allocation identifier).

    The per-switch VTEP address keeps the identifier <device>-Loopback1 on every device, so the address allocated by
    a stage-1 run is reused when it moves to Loopback2 on an MLAG VTEP member . Loopback1 of an MLAG VTEP
    member (anycast VTEP) is written with the switch pair, after the anycast allocation.
    """
    name, role = row["name"], row["role"]
    out = []
    if role not in NO_LOOPBACK0_ROLES:
        out.append(("loopback0", LOOPBACK0, LOOPBACK0_DESCRIPTION, f"{name}-{LOOPBACK0}"))
    if role in VTEP_ROLES:
        ifname, description = (LOOPBACK2, LOOPBACK2_DESCRIPTION) if name in mlag_vtep else (LOOPBACK1, LOOPBACK1_DESCRIPTION)
        out.append(("loopback1", ifname, description, f"{name}-{LOOPBACK1}"))
    return out


def build_plan(site: str, vpc_pairs: int, esi_pairs: int, single: int, spine_asn: int = SPINE_ASN) -> list[dict]:
    """One dict per device: name, short, role, description, model, platform, asn (int or None).

    Order (= pool allocation order): S1-S2, L1..L(2*vpc) MLAG, next 2*esi ESI, next `single` single-homed,
    then the fixed pairs in FIXED_PAIRS order.
    """
    server_leaves = 2 * vpc_pairs + 2 * esi_pairs + single
    if server_leaves > MAX_SERVER_LEAVES:
        raise ValueError(f"{site}: {server_leaves} server leaves exceed the spine port map ({MAX_SERVER_LEAVES})")

    plan: list[dict] = []

    def add(short: str, role: str, description: str, kind: str | None = None) -> None:
        plan.append({"name": f"{site}-{short}", "short": short, "role": role, "description": description,
                     "model": DEVICE_TYPE[role], "platform": PLATFORM, "leaf_kind": kind, "asn": None})

    for n in (1, 2):
        add(f"S{n}", "spine", f"Spine {n}")
    n = 1
    for _ in range(2 * vpc_pairs):
        add(f"L{n}", "leaf", f"Server leaf {n}, MLAG pair", "mlag"); n += 1
    for _ in range(2 * esi_pairs):
        add(f"L{n}", "leaf", f"Server leaf {n}, ESI pair", "esi"); n += 1
    for _ in range(single):
        add(f"L{n}", "leaf", f"Server leaf {n}, single-homed", "single"); n += 1
    for prefix, role, description in FIXED_PAIRS:
        for k in (1, 2):
            add(f"{prefix}{k}", role, description.format(n=k))

    assign_asns(plan, spine_asn)
    return plan


def assign_asns(plan: list[dict], spine_asn: int = SPINE_ASN) -> None:
    """ in place: spines spine_asn; BL pair, SL pair, then server leaves in name order
    (an MLAG pair shares one ASN, ESI members and single leaves one each); edge/DMZ none."""
    nxt = LEAF_ASN_FIRST

    def take() -> int:
        nonlocal nxt
        if nxt > LEAF_ASN_LAST:
            raise ValueError(f"leaf ASN range {LEAF_ASN_FIRST}-{LEAF_ASN_LAST} exhausted")
        nxt += 1
        return nxt - 1

    for r in plan:
        if r["role"] == "spine":
            r["asn"] = spine_asn
    for role in ASN_ROLE_ORDER:            # pairs: one ASN per pair
        members = [r for r in plan if r["role"] == role]
        if members:
            asn = take()
            for r in members:
                r["asn"] = asn
    leaves = [r for r in plan if r["role"] == "leaf"]   # plan order = name order L1..L20
    i = 0
    while i < len(leaves):
        if leaves[i]["leaf_kind"] == "mlag":
            asn = take()
            leaves[i]["asn"] = leaves[i + 1]["asn"] = asn
            i += 2
        else:
            leaves[i]["asn"] = take()
            i += 1
    for r in plan:
        if r["role"] in NO_ASN_ROLES:
            r["asn"] = None


def build_links(site: str, plan: list[dict]) -> list[dict]:
    """One dict per spine-leaf link (port plan): spine, spine_port, leaf, leaf_port (EOS names).
    Order decides the /31."""
    rows = {r["name"]: r for r in plan}
    spines = [r for r in plan if r["role"] == "spine"]
    server_leaves = [r for r in plan if r["role"] == "leaf"]

    links: list[dict] = []
    for spine in spines:
        short = spine["short"]
        for n, leaf in enumerate(server_leaves, start=1):
            for i, leaf_port in enumerate(SERVER_LEAF_UPLINKS[short]):
                links.append({"spine": spine["name"], "spine_port": eos_port(spine["model"], 2 * n - 1 + i),
                              "leaf": leaf["name"], "leaf_port": eos_port(leaf["model"], leaf_port)})
        for leaf_short, port in SPINE_FIXED_PORTS:
            leaf_name = f"{site}-{leaf_short}"
            if leaf_name not in rows:
                raise ValueError(f"{site}: link to unknown device {leaf_name}")
            links.append({"spine": spine["name"], "spine_port": eos_port(spine["model"], port),
                          "leaf": leaf_name, "leaf_port": eos_port(rows[leaf_name]["model"], FIXED_LEAF_UPLINKS[short])})
    return links


def build_pairs(site: str, plan: list[dict], vpc_pairs: int, esi_pairs: int) -> list[dict]:
    """One dict per switch pair: kind, name, members, domain_id (MLAG only), role, nve."""
    if site not in MLAG_DOMAIN_BASE:
        raise ValueError(f"{site}: no MLAG domain base in MLAG_DOMAIN_BASE")
    base = MLAG_DOMAIN_BASE[site]
    roles = {r["name"]: r["role"] for r in plan}
    leaves = [r["name"] for r in plan if r["role"] == "leaf"]
    mlag_leaves = leaves[: 2 * vpc_pairs]
    esi_leaves = leaves[2 * vpc_pairs: 2 * vpc_pairs + 2 * esi_pairs]

    pairs: list[dict] = []

    def add(kind: str, m1: str, m2: str, domain_id: int | None) -> None:
        pattern = MLAG_PAIR_NAME if kind == "FabricVpcDomain" else ESI_PAIR_NAME
        s1, s2 = m1.removeprefix(f"{site}-"), m2.removeprefix(f"{site}-")
        role = roles.get(m1)
        pairs.append({"kind": kind, "name": pattern.format(site=site, a=s1, b=s2), "members": (m1, m2),
                      "domain_id": domain_id, "role": role, "nve": role in VTEP_ROLES})

    for k in range(vpc_pairs):
        slot = 1 if k == 0 else FIRST_EXTRA_SERVER_SLOT + k - 1
        add("FabricVpcDomain", mlag_leaves[2 * k], mlag_leaves[2 * k + 1], base + slot)
    for prefix, slot in MLAG_FIXED_SLOTS:
        add("FabricVpcDomain", f"{site}-{prefix}1", f"{site}-{prefix}2", base + slot)
    for k in range(esi_pairs):
        add("FabricEsiPair", esi_leaves[2 * k], esi_leaves[2 * k + 1], None)

    seen_devices, seen_ids = set(), set()
    for pair in pairs:
        for member in pair["members"]:
            if member not in roles:
                raise ValueError(f"{pair['name']}: unknown member {member}")
            if member in seen_devices:
                raise ValueError(f"{member} is in two pairs")
            seen_devices.add(member)
        if roles[pair["members"][0]] != roles[pair["members"][1]]:
            raise ValueError(f"{pair['name']}: members have different roles")
        did = pair["domain_id"]
        if did is not None:
            if not base + 1 <= did <= base + 99:
                raise ValueError(f"{pair['name']}: domain ID {did} out of range")
            if did in seen_ids:
                raise ValueError(f"{pair['name']}: domain ID {did} used twice")
            seen_ids.add(did)
    return pairs


def mlag_domains(plan: list[dict], pairs: list[dict]) -> list[dict]:
    """Every MLAG pair: peer-link members, keepalive port (EOS names) and Vlan3900 (all pairs)."""
    models = {r["name"]: r["model"] for r in plan}
    out = []
    for p in pairs:
        if p["kind"] != "FabricVpcDomain":
            continue
        if p["role"] not in MLAG_PORTS:
            raise ValueError(f"{p['name']}: no MLAG port map for role {p['role']}")
        ports = MLAG_PORTS[p["role"]]
        model = models[p["members"][0]]
        if models[p["members"][1]] != model:
            raise ValueError(f"{p['name']}: members are different models")
        out.append({**p, "peer_link": tuple(eos_port(model, n) for n in ports["peer_link"]),
                    "keepalive": eos_port(model, ports["keepalive"]), "backup": True})
    return out


def expected_interfaces(plan: list[dict], links: list[dict], domains: list[dict]) -> dict:
    """device -> set of interface names the generator owns. Guard: no physical port used twice on a device."""
    exp: dict = {}
    used: list[tuple] = []

    def port(device: str, name: str) -> None:
        used.append((device, name)); exp[device].add(name)

    for r in plan:
        names = {MGMT_IF}
        if r["role"] not in NO_LOOPBACK0_ROLES:
            names.add(LOOPBACK0)
        if r["role"] in VTEP_ROLES:
            names.add(LOOPBACK1)
        exp[r["name"]] = names
    for link in links:
        port(link["spine"], link["spine_port"]); port(link["leaf"], link["leaf_port"])
    for d in domains:
        for m in d["members"]:
            if d["nve"]:
                exp[m].add(LOOPBACK2)                       # per-switch VTEP address
            exp[m].add(PEER_LINK)
            port(m, d["keepalive"])
            for p in d["peer_link"]:
                port(m, p)
            if d["backup"]:
                exp[m].add(MLAG_SVI)
    dupes = {u for u in used if used.count(u) > 1}
    if dupes:
        raise ValueError(f"port used twice: {sorted(dupes)}")
    return exp


def group_members(plan: list[dict]) -> dict:
    """artifact group name -> device names (plan order)."""
    out: dict = {}
    for r in plan:
        g = ARTIFACT_GROUPS.get(r["role"])
        if g:
            out.setdefault(g, []).append(r["name"])
    return out


def print_plan(site: str, plan, links, pairs, domains, expected) -> None:
    """Every object a write run creates or updates (plan-only mode, DC3_DRY_RUN=1)."""
    print(f"=== PLAN {site}: {len(plan)} devices, platform {PLATFORM} {OS_VERSION}")
    print(f"--- devices: name | role | model | asn | {MGMT_IF} (pool {pool_name(site, 'mgmt')}) | loopbacks")
    mlag_vtep = mlag_vtep_members(pairs)
    for r in plan:
        lo = [f"{ifname} (pool {pool_name(site, key)}, identifier {ident})" for key, ifname, _d, ident in device_loopbacks(r, mlag_vtep)]
        if r["name"] in mlag_vtep:
            lo.append(f"{LOOPBACK1} (anycast VTEP of the pair, pool {pool_name(site, 'anycast')})")
        print(f"  {r['name']:<10} {r['role']:<13} {r['model']:<19} {str(r['asn'] or '-'):<6} {MGMT_IF:<12} {', '.join(lo)}")
    print(f"--- spine-leaf links ({len(links)}): /31 from pool {pool_name(site, 'p2p')}, MTU {FABRIC_MTU}, identifier <spine>-<spine port>")
    for l in links:
        print(f"  {l['spine']:<7} {l['spine_port']:<13} <-> {l['leaf']:<9} {l['leaf_port']}")
    print(f"--- switch pairs ({len(pairs)}): name | kind | domain id | members | anycast VTEP")
    for p in pairs:
        anycast = f"pool {pool_name(site, 'anycast')}" if p["nve"] and p["domain_id"] else "-"
        print(f"  {p['name']:<20} {p['kind']:<16} {str(p['domain_id']):<5} {p['members'][0]} + {p['members'][1]}  anycast={anycast}")
    print(f"--- MLAG plumbing ({len(domains)} pairs): {PEER_LINK} members | keepalive (/31 pool {pool_name(site, 'keepalive')}) | "
          f"{MLAG_SVI} (/31 pool {pool_name(site, 'backup')})")
    for d in domains:
        print(f"  {d['name']:<20} {', '.join(d['peer_link']):<52} {d['keepalive']:<13} {MLAG_SVI}")
    for g, members in group_members(plan).items():
        print(f"--- group {g}: {', '.join(members)}")
    total_if = sum(len(v) for v in expected.values())
    print(f"--- totals: devices {len(plan)}, links {len(links)}, pairs {len(pairs)}, MLAG pairs {len(domains)}, interfaces owned {total_if}")


# =====================================================================================================================
# Stage 3: tenant VRFs and segments (data in objects/dc3-06/07), FW / ISP / cloud stand-ins (FRR),
# FabricAttachments, attach port-channels, L2 VLANs on EDGE / DMZ: slot tables, VLAN bases, host numbers and name
# patterns, with the DC3 blocks and one stand-in per role (active unit only) instead of a pair.
# =====================================================================================================================
import ipaddress as _ip

SITE_INFRA = {"DC3": "10.27"}           # infrastructure block 10.27.0.0/16
SITE_TENANT = {"DC3": "10.24"}          # tenant block 10.24.0.0/16
DC_SUMMARY = {"DC3": "10.24.0.0/16"}    # site summary for the cloud routers: the tenant /16

# ---------------------------------------------------------------- stand-in devices
STANDIN_PLATFORM = "FRR"                # DcimPlatform of the stand-ins (objects/dc3-08-standins.yml)
STANDIN_OS_VERSION = "10.6.0"           # FRR release of the lab image quay.io/frrouting/frr:10.6.0
STANDIN_DEVICE_TYPE = "FRR stand-in"    # DcimDeviceType of the stand-ins
STANDIN_MGMT_IF = "eth0"                # containerlab management interface of a linux node
FABRIC_ASN_OFFSET = {"internal_fw": 10, "sdwan_fw": 20, "vpn_fw": 30, "internet_fw": 40, "cloud_router": 70}  # ASN = fabric ASN + offset
ISP_ROUTER_ASN = 65050
# Order INETFW, INTFW, SDWANFW, VPNFW, ISP, CLOUD; FWs: unit 1 only.
STANDINS = (
    ("INETFW1", "internet_fw", "Internet FW stand-in (FRR), one per role"),
    ("INTFW1", "internal_fw", "Internal FW stand-in (FRR), one per role"),
    ("SDWANFW1", "sdwan_fw", "SD-WAN hub FW stand-in (FRR), one per role"),
    ("VPNFW1", "vpn_fw", "VPN-partner FW stand-in (FRR), one per role"),
    ("ISP1", "isp_router", "ISP router 1 stand-in (FRR), peers with ISP-A"),
    ("ISP2", "isp_router", "ISP router 2 stand-in (FRR), peers with ISP-B"),
    ("CLOUD1", "cloud_router", "Cloud router 1 stand-in (FRR)"),
    ("CLOUD2", "cloud_router", "Cloud router 2 stand-in (FRR)"),
)
STANDIN_ROLES = {r for _s, r, _d in STANDINS}
FW_ROLES = {"internal_fw", "internet_fw", "sdwan_fw", "vpn_fw"}
WAN_FW_ROLES = {"sdwan_fw", "vpn_fw"}
STANDIN_GROUPS = {                      # artifact target groups of the stand-ins
    "internal_fw": "dc3-internal-fws", "internet_fw": "dc3-internet-fws", "sdwan_fw": "dc3-sdwan-fws",
    "vpn_fw": "dc3-vpn-fws", "isp_router": "dc3-isp-routers", "cloud_router": "dc3-cloud-routers",
}

# ---------------------------------------------------------------- attach ports (unit 1 of each FW pair)
ROLE_PEERING = "peering"
ROLE_UPSTREAM = "upstream"
ROLE_CLOUD = "cloud_handoff"
EXT_MTU = 9214                          # the EOS L3 maximum
PORT_CHANNEL = "Port-Channel{n}"        # EOS name; MLAG id = port-channel number
# switch role -> (front-panel port number, far device short name, port-channel number)
ATTACH_PORTS = {
    "border_leaf":  ((7, "INETFW1", 11), (9, "SDWANFW1", 13), (11, "VPNFW1", 15), (13, "CLOUD1", 31), (14, "CLOUD2", 32)),
    "service_leaf": ((7, "INTFW1", 11),),
    "edge":         ((5, "ISP1", 21), (6, "ISP2", 22), (7, "INETFW1", 11), (9, "SDWANFW1", 13), (11, "VPNFW1", 15)),
    "dmz":          ((49, "INETFW1", 11),),
}
# Stand-in side of every attach link (the firewall port names are the Linux interface names):
# FW role -> switch role -> (aggregate, (port toward switch 1, port toward switch 2))
FW_PORTS = {
    "internal_fw": {"service_leaf": ("agg-inside", ("port19", "port20"))},
    "internet_fw": {"border_leaf": ("agg-inside", ("port19", "port20")), "edge": ("agg-outside", ("port21", "port22")),
                    "dmz": ("agg-dmz", ("port15", "port16"))},
    "sdwan_fw": {"border_leaf": ("agg-inside", ("port37", "port38")), "edge": ("agg-outside", ("port39", "port40"))},
    "vpn_fw": {"border_leaf": ("agg-inside", ("port37", "port38")), "edge": ("agg-outside", ("port39", "port40"))},
}
# ISP and cloud routers: router port names shortened for Linux (FourHundredGigE0/0/0/<2k> -> Gi0-0-0-<k>;
# Bundle-Ether<n> -> BE<n>; the cloud-exchange port HundredGigE0/0/0/16 -> Hu0-0-0-16).
ISP_EDGE_PORTS = (("Gi0-0-0-0", "EDGE1"), ("Gi0-0-0-1", "EDGE2"))
ISP_CROSSLINK_PORT, ISP_CARRIER_PORT = "Gi0-0-0-2", "Gi0-0-0-3"
ISP_BUNDLE = "BE21"
CLOUD_FABRIC_PORTS = (("Gi0-0-0-0", "BL1"), ("Gi0-0-0-1", "BL2"))
CLOUD_XLINK_PORT, CLOUD_XC_PORT = "Gi0-0-0-2", "Hu0-0-0-16"
CLOUD_BUNDLE = "BE31"

# ---------------------------------------------------------------- attachments (slot tables)
# /29 by fixed slot: .1 lower member, .2 upper member, .3 the attached stand-in
TRANSIT_ATTACHMENTS = (("INTFW", "SL", 0), ("INETFW", "BL", 1), ("SDWANFW", "BL", 2), ("CLOUD1", "BL", 3), ("CLOUD2", "BL", 4))
TRANSIT_BLOCK_THIRD, TRANSIT_VLAN_BASE = 16, 3000
INTFW_VRF_SLOT = {"PROD": 0, "DEV": 1, "CITRIX": 5, "PARTNER": 6}   # slots 2 and 3 unused
TENANT_BLOCK_THIRD, TENANT_VLAN_BASE = 8, 3100
TENANT_EXCLUDED = {"REPLICATION", "TRANSIT"}
VPN_ATTACHMENTS = (("VPNFW", "BL", "PARTNER", 0),)
VPN_BLOCK_THIRD, VPN_VLAN_BASE = 9, 3300
ATTACHMENT_NAME = "{site}-{vrf}-{pair}"
ATTACH_HOST = {"lower": 1, "upper": 2, "standin": 3}
DC3_VRFS = {"PROD", "DEV", "CITRIX", "PARTNER", "REPLICATION", "TRANSIT"}

# ---------------------------------------------------------------- L2 VLANs on EDGE / DMZ
EDGE_VLAN = 3200
EDGE_VLAN_NAME = "{site}-EDGE-OUTSIDE"
EDGE_VLAN_HOST = {"VIP": 1, "ISP1": 2, "ISP2": 3, "INETFW": 10, "SDWANFW": 11, "VPNFW": 12}
DMZ_VLAN_BASE, DMZ_THIRD_OCTET_BASE = 1400, 128
DMZ_VLAN_NAMES = {1400: "LB-VIP", 1401: "LB-SELF", **{x: f"SRV-{x - DMZ_VLAN_BASE:02d}" for x in range(1402, 1416)}}
DMZ_VLAN_PURPOSE = {1400: "load balancer virtual servers", 1401: "load balancer self IPs and SNAT"}
FW_OUTSIDE_SUBIF, FW_DMZ_SUBIF, FW_DMZ_GW_HOST = "OUTSIDE-{vlan}", "DMZ-{vlan}", 1

# ---------------------------------------------------------------- ISP routers, carriers (DC3 blocks)
ISP_LOOPBACK0_HOST = {1: 17, 2: 18}     # <infra>.15.16/28
ISP_CROSSLINK_HOST = {1: 0, 2: 1}       # <infra>.15.0/31
CARRIER_BLOCK = "192.0.2"
SITE_CARRIER_BASE = {"DC3": 4}          # /30 index in 192.0.2.0/24: carriers 192.0.2.16/30 and 192.0.2.20/30
CARRIER = {1: "ISP-A", 2: "ISP-B"}
CARRIER_SIDE_HOST, OUR_SIDE_HOST = 1, 2

# ---------------------------------------------------------------- cloud routers (DC3 values)
CLOUD_LOOPBACK0_HOST = {1: 1, 2: 2}     # <infra>.13.0/28
CLOUD_XLINK_HOST = 16                   # <infra>.13.16/31
CLOUD_AWS_VLAN = {"DC3": {1: 120, 2: 121}}   # AWS private VIF VLANs
CLOUD_ER_VLAN = {"DC3": 220}                 # ExpressRoute VLAN
CLOUD_ER_NET = {"DC3": {1: "10.98.0.16/30", 2: "10.98.0.20/30"}}          # objects/dc3-03-ip-plan.yml (stage 1)
CLOUD_AWS_NET = {"DC3": {1: "169.254.120.0/30", 2: "169.254.121.0/30"}}   # objects/dc3-03-ip-plan.yml (stage 1)
ER_CUSTOMER_HOST, ER_MICROSOFT_HOST = 1, 2
AWS_PEER_HOST, AWS_CUSTOMER_HOST = 1, 2
ER_LEG = {1: "primary", 2: "secondary"}
CLOUD_PROVIDER = {"aws": "AWS", "azure": "Microsoft"}
CLOUD_HANDOFF_MTU = 1500                # MTU of the cloud hand-off sub-interfaces

# ---------------------------------------------------------------- WAN FWs (DC3 values)
FW_WAN_LOOPBACK, FW_WAN_HOST = "LO-WAN", 1
FW_WAN_ENDPOINT = {("DC3", "sdwan_fw"): "198.18.3.144/28", ("DC3", "vpn_fw"): "198.18.3.128/28"}   # rule
SDWAN_OVERLAY = {"DC3": "10.79.16.0/21"}     # SD-WAN overlay of the site
SDWAN_TUNNEL, SDWAN_HUB_HOST = "ADVPN-HUB", 1


def _net(p: str):
    return _ip.ip_network(p)


def standin_asn(role: str, fabric_asn: int = SPINE_ASN) -> int:
    return ISP_ROUTER_ASN if role == "isp_router" else fabric_asn + FABRIC_ASN_OFFSET[role]


def build_standins(site: str, fabric_asn: int = SPINE_ASN) -> list[dict]:
    """One dict per stand-in device (STANDINS order, unit 1 of each FW pair; plan order = mgmt allocation order)."""
    return [{"name": f"{site}-{short}", "short": short, "role": role, "description": desc, "asn": standin_asn(role, fabric_asn),
             "model": STANDIN_DEVICE_TYPE, "platform": STANDIN_PLATFORM} for short, role, desc in STANDINS]


def build_attach_ports(site: str, plan: list[dict], standins: list[dict]) -> list[dict]:
    """Switch side of every attach link: device, port, far device, far port, port-channel (+ number), descriptions."""
    rows = {r["name"]: r for r in plan}
    far_roles = {s["name"]: s["role"] for s in standins}
    out = []
    for r in plan:
        for port_n, far_short, po in ATTACH_PORTS.get(r["role"], ()):
            far = f"{site}-{far_short}"
            if far not in far_roles:
                raise ValueError(f"{site}: attach port to unknown stand-in {far}")
            k = int(r["short"][-1])                       # switch 1 or 2 of the MLAG pair
            far_role = far_roles[far]
            if far_role == "isp_router":
                far_port, far_po = ISP_EDGE_PORTS[k - 1][0], ISP_BUNDLE
            elif far_role == "cloud_router":
                far_port, far_po = CLOUD_FABRIC_PORTS[k - 1][0], CLOUD_BUNDLE
            else:
                far_po, ports = FW_PORTS[far_role][r["role"]]
                far_port = ports[k - 1]
            port = eos_port(rows[r["name"]]["model"], port_n)
            out.append({"device": r["name"], "role": r["role"], "port": port, "far": far, "far_role": far_role,
                        "far_port": far_port, "far_po": far_po, "po": PORT_CHANNEL.format(n=po), "po_number": po,
                        "port_desc": f"{far} {far_port}", "po_desc": f"{far} {far_po}"})
    return out


def build_attachments(site: str, standins: list[dict], site_vrfs) -> list[dict]:
    """One dict per FabricAttachment: name, vrf, pair (MLAG pair name), members, devices, slot, vlan, prefix, addrs."""
    infra = SITE_INFRA[site]
    names = {s["name"] for s in standins}
    out = []

    def add(pair_short: str, device_short: str, leaf_short: str, vrf: str, slot: int, third: int, vlan: int, router: bool):
        members = (f"{site}-{leaf_short}1", f"{site}-{leaf_short}2")
        device = f"{site}-{device_short}"
        if device not in names:
            raise ValueError(f"{site}: attachment to unknown stand-in {device}")
        net = _net(f"{infra}.{third}.{8 * slot}/29")
        out.append({"name": ATTACHMENT_NAME.format(site=site, vrf=vrf, pair=pair_short), "vrf": vrf,
                    "pair": MLAG_PAIR_NAME.format(site=site, a=f"{leaf_short}1", b=f"{leaf_short}2"),
                    "members": members, "devices": (device,), "slot": slot, "vlan": vlan, "prefix": str(net), "router": router,
                    "addrs": {members[0]: str(net[ATTACH_HOST["lower"]]), members[1]: str(net[ATTACH_HOST["upper"]]),
                              "standin": str(net[ATTACH_HOST["standin"]])}})

    for pair_short, leaf_short, slot in TRANSIT_ATTACHMENTS:
        router = pair_short.startswith("CLOUD")
        add(pair_short, pair_short if router else f"{pair_short}1", leaf_short, "TRANSIT", slot, TRANSIT_BLOCK_THIRD,
            TRANSIT_VLAN_BASE + slot, router)
    for vrf in sorted(x for x in site_vrfs if x not in TENANT_EXCLUDED):
        if vrf not in INTFW_VRF_SLOT:
            raise ValueError(f"{site}: VRF {vrf} is in the site but has no Internal FW slot (INTFW_VRF_SLOT); decide it first")
        slot = INTFW_VRF_SLOT[vrf]
        add("INTFW", "INTFW1", "SL", vrf, slot, TENANT_BLOCK_THIRD, TENANT_VLAN_BASE + slot, False)
    for pair_short, leaf_short, vrf, slot in VPN_ATTACHMENTS:
        if vrf not in site_vrfs:
            raise ValueError(f"{site}: VRF {vrf} is not in the site; the VPN-partner FW attachment needs it")
        add(pair_short, f"{pair_short}1", leaf_short, vrf, slot, VPN_BLOCK_THIRD, VPN_VLAN_BASE + slot, False)

    seen_vlan, seen_prefix, seen_name = set(), set(), set()
    for a in out:
        key = (a["pair"], a["vlan"])
        if key in seen_vlan or a["prefix"] in seen_prefix or a["name"] in seen_name:
            raise ValueError(f"{a['name']}: duplicate VLAN, prefix or name")
        seen_vlan.add(key); seen_prefix.add(a["prefix"]); seen_name.add(a["name"])
    return out


def edge_vlan_addresses(site: str) -> dict:
    infra = SITE_INFRA[site]
    return {k: f"{infra}.14.{h}" for k, h in EDGE_VLAN_HOST.items()}


def build_l2_vlans(site: str) -> list[dict]:
    """The edge VLAN on the EDGE pair, the DMZ VLANs on the DMZ pair (prefixes belong to the IP plan)."""
    out = [{"name": EDGE_VLAN_NAME.format(site=site), "vlan": EDGE_VLAN,
            "pair": MLAG_PAIR_NAME.format(site=site, a="EDGE1", b="EDGE2"), "prefix": f"{SITE_INFRA[site]}.14.0/24",
            "description": "Edge VLAN: VRRP VIP, ISP routers, Internet FW outside leg"}]
    for vlan, suffix in DMZ_VLAN_NAMES.items():
        out.append({"name": f"{site}-DMZ-{suffix}", "vlan": vlan, "pair": MLAG_PAIR_NAME.format(site=site, a="DMZ1", b="DMZ2"),
                    "prefix": f"{SITE_TENANT[site]}.{DMZ_THIRD_OCTET_BASE + vlan - DMZ_VLAN_BASE}.0/24",
                    "description": f"DMZ VLAN: {DMZ_VLAN_PURPOSE.get(vlan, 'DMZ servers')}"})
    return out


def build_isp(site: str) -> list[dict]:
    infra = SITE_INFRA[site]
    edge = edge_vlan_addresses(site)
    out = []
    for n in (1, 2):
        idx = SITE_CARRIER_BASE[site] + (n - 1)
        carrier = _net(f"{CARRIER_BLOCK}.{4 * idx}/30")
        po = next(p for _port, short, p in ATTACH_PORTS["edge"] if short == f"ISP{n}")
        out.append({
            "name": f"{site}-ISP{n}", "n": n, "loopback0": f"{infra}.15.{ISP_LOOPBACK0_HOST[n]}",
            "edge_ports": [(port, f"{site}-{sw}", eos_port(DEVICE_TYPE["edge"], next(p for p, s, _ in ATTACH_PORTS["edge"] if s == f"ISP{n}")))
                           for port, sw in ISP_EDGE_PORTS],
            "bundle": ISP_BUNDLE, "bundle_desc": f"{site}-EDGE1/EDGE2 {PORT_CHANNEL.format(n=po)}",
            "subif": f"{ISP_BUNDLE}.{EDGE_VLAN}", "subif_addr": edge[f"ISP{n}"],
            "crosslink": {"port": ISP_CROSSLINK_PORT, "prefix": f"{infra}.15.0/31", "addr": f"{infra}.15.{ISP_CROSSLINK_HOST[n]}",
                          "peer": f"{site}-ISP{3 - n}"},
            "carrier": {"port": ISP_CARRIER_PORT, "prefix": str(carrier), "ours": str(carrier[OUR_SIDE_HOST]),
                        "theirs": str(carrier[CARRIER_SIDE_HOST]), "provider": CARRIER[n]},
        })
    return out


def build_cloud(site: str, attachments: list[dict]) -> list[dict]:
    infra = SITE_INFRA[site]
    out = []
    for n in (1, 2):
        name = f"{site}-CLOUD{n}"
        att = [a for a in attachments if a["devices"] == (name,)]
        if len(att) != 1:
            raise ValueError(f"{name}: expected 1 TRANSIT attachment, got {len(att)}")
        a = att[0]
        po = next(p for _port, short, p in ATTACH_PORTS["border_leaf"] if short == f"CLOUD{n}")
        bl_port = eos_port(DEVICE_TYPE["border_leaf"], next(p for p, s, _ in ATTACH_PORTS["border_leaf"] if s == f"CLOUD{n}"))
        xnet = _net(f"{infra}.13.{CLOUD_XLINK_HOST}/31")
        aws_net, er_net = _net(CLOUD_AWS_NET[site][n]), _net(CLOUD_ER_NET[site][n])
        aws_vlan, er_vlan = CLOUD_AWS_VLAN[site][n], CLOUD_ER_VLAN[site]
        out.append({
            "name": name, "n": n, "loopback0": f"{infra}.13.{CLOUD_LOOPBACK0_HOST[n]}",
            "fabric_ports": [(port, f"{site}-{bl}", bl_port) for port, bl in CLOUD_FABRIC_PORTS],
            "bundle": CLOUD_BUNDLE, "bundle_desc": f"{site}-BL1/BL2 {PORT_CHANNEL.format(n=po)}",
            "subif": f"{CLOUD_BUNDLE}.{a['vlan']}", "subif_addr": a["addrs"]["standin"], "attachment": a["name"],
            "crosslink": {"port": CLOUD_XLINK_PORT, "prefix": str(xnet), "addr": str(xnet[n - 1]), "peer": f"{site}-CLOUD{3 - n}"},
            "xc": {"port": CLOUD_XC_PORT, "desc": f"Cloud exchange port {n} (AWS VIF {n}, ExpressRoute {ER_LEG[n]})"},
            "handoffs": [
                {"provider": CLOUD_PROVIDER["aws"], "subif": f"{CLOUD_XC_PORT}.{aws_vlan}", "vlan": aws_vlan, "prefix": str(aws_net),
                 "ours": str(aws_net[AWS_CUSTOMER_HOST]), "theirs": str(aws_net[AWS_PEER_HOST]),
                 "desc": f"{CLOUD_PROVIDER['aws']} private VIF {n} to the Direct Connect Gateway, VLAN {aws_vlan}"},
                {"provider": CLOUD_PROVIDER["azure"], "subif": f"{CLOUD_XC_PORT}.{er_vlan}", "vlan": er_vlan, "prefix": str(er_net),
                 "ours": str(er_net[ER_CUSTOMER_HOST]), "theirs": str(er_net[ER_MICROSOFT_HOST]),
                 "desc": f"{CLOUD_PROVIDER['azure']} ExpressRoute {ER_LEG[n]} private peering, VLAN {er_vlan}"},
            ],
        })
    return out


def build_fw(site: str, standins: list[dict], attach_ports: list[dict], attachments: list[dict], l2vlans: list[dict]) -> list[dict]:
    """FW stand-in interfaces (unit 1): ports toward the switches, aggregates, VLAN sub-interfaces with
    the unit-1 addresses, and on the WAN FWs the outside leg, LO-WAN and (SD-WAN) the ADVPN hub address.
    No HA ports and no NPU vlinks: one unit per role, FRR leaks with `import vrf`."""
    edge = edge_vlan_addresses(site)
    out = []
    for s in standins:
        if s["role"] not in FW_ROLES:
            continue
        fw = {"name": s["name"], "role": s["role"], "ports": [], "aggs": {}, "subifs": [], "virtuals": []}
        for ap in attach_ports:
            if ap["far"] != s["name"]:
                continue
            fw["ports"].append((ap["far_port"], f"{ap['device']} {ap['port']}", ap["far_po"]))
            sw_short = ap["device"].removeprefix(f"{site}-")[:-1]
            fw["aggs"][ap["far_po"]] = f"{site}-{sw_short}1/{sw_short}2 {ap['po']}"
        for a in attachments:
            if s["name"] in a["devices"]:
                fw["subifs"].append({"name": f"{a['vrf']}-{a['vlan']}", "parent": "agg-inside", "desc": a["name"],
                                     "addr": (a["addrs"]["standin"], 29)})
        short = s["short"][:-1]
        if s["role"] == "internet_fw":
            fw["subifs"].append({"name": FW_OUTSIDE_SUBIF.format(vlan=EDGE_VLAN), "parent": "agg-outside",
                                 "desc": f"Edge VLAN {EDGE_VLAN}", "addr": (edge["INETFW"], 24)})
            for v in l2vlans:
                if v["pair"].endswith("DMZ1-DMZ2"):
                    gw = str(_net(v["prefix"])[FW_DMZ_GW_HOST])
                    fw["subifs"].append({"name": FW_DMZ_SUBIF.format(vlan=v["vlan"]), "parent": "agg-dmz", "desc": v["name"],
                                         "addr": (gw, 24)})
        if s["role"] in WAN_FW_ROLES:
            fw["subifs"].append({"name": FW_OUTSIDE_SUBIF.format(vlan=EDGE_VLAN), "parent": "agg-outside",
                                 "desc": f"Edge VLAN {EDGE_VLAN}", "addr": (edge[short], 24)})
            wan = _net(FW_WAN_ENDPOINT[(site, s["role"])])
            fw["virtuals"].append({"name": FW_WAN_LOOPBACK, "desc": f"WAN endpoint (public {wan})",
                                   "addr": (str(wan[FW_WAN_HOST]), wan.prefixlen), "plan_prefix": str(wan), "own_prefix": None})
            if s["role"] == "sdwan_fw":
                ov = _net(SDWAN_OVERLAY[site])
                fw["virtuals"].append({"name": SDWAN_TUNNEL, "desc": f"ADVPN 2.0 hub tunnel, overlay {ov}",
                                       "addr": (str(ov[SDWAN_HUB_HOST]), ov.prefixlen), "plan_prefix": None, "own_prefix": str(ov),
                                       "own_prefix_desc": f"{site} SD-WAN overlay (ADVPN hub tunnel)"})
        for x in fw["subifs"] + fw["virtuals"]:
            if len(x["name"]) > 15:
                raise ValueError(f"{s['name']}: Linux interface name {x['name']} longer than 15 characters")
        out.append(fw)
    return out


def expected_standin_interfaces(standins, attach_ports, attachments, isp, fws, cloud) -> dict:
    """Stand-in device -> interface names the generator owns; switch device -> extra names (attach ports, SVIs)."""
    exp: dict = {s["name"]: {STANDIN_MGMT_IF} for s in standins}
    used: list[tuple] = []

    def port(device: str, name: str) -> None:
        used.append((device, name)); exp.setdefault(device, set()).add(name)

    for ap in attach_ports:
        port(ap["device"], ap["port"]); exp[ap["device"]].add(ap["po"])
    for a in attachments:
        for m in a["members"]:
            exp.setdefault(m, set()).add(f"Vlan{a['vlan']}")
    for fw in fws:
        for p, _d, _a in fw["ports"]:
            port(fw["name"], p)
        exp[fw["name"]] |= set(fw["aggs"]) | {x["name"] for x in fw["subifs"]} | {x["name"] for x in fw["virtuals"]}
    for r in isp:
        exp[r["name"]].add("loopback0")
        for p, _f, _fp in r["edge_ports"]:
            port(r["name"], p)
        port(r["name"], r["crosslink"]["port"]); port(r["name"], r["carrier"]["port"])
        exp[r["name"]] |= {r["bundle"], r["subif"]}
    for r in cloud:
        exp[r["name"]].add("loopback0")
        for p, _f, _fp in r["fabric_ports"]:
            port(r["name"], p)
        port(r["name"], r["crosslink"]["port"]); port(r["name"], r["xc"]["port"])
        exp[r["name"]] |= {r["bundle"], r["subif"]} | {h["subif"] for h in r["handoffs"]}
    dupes = {u for u in used if used.count(u) > 1}
    if dupes:
        raise ValueError(f"port used twice: {sorted(dupes)}")
    return exp


def merge_expected(*maps: dict) -> dict:
    out: dict = {}
    for m in maps:
        for k, v in m.items():
            out.setdefault(k, set()).update(v)
    return out


def standin_group_members(standins: list[dict]) -> dict:
    out: dict = {}
    for s in standins:
        out.setdefault(STANDIN_GROUPS[s["role"]], []).append(s["name"])
    return out


def print_plan_stage3(site, standins, attach_ports, attachments, l2vlans, isp, fws, cloud, site_vrfs) -> None:
    print(f"=== STAGE 3 {site}: site VRFs {sorted(site_vrfs)}")
    print(f"--- stand-ins ({len(standins)}): name | role | asn | platform | {STANDIN_MGMT_IF} (pool {pool_name(site, 'mgmt')})")
    for s in standins:
        print(f"  {s['name']:<13} {s['role']:<13} {s['asn']:<6} {s['platform']} {STANDIN_OS_VERSION}")
    print(f"--- attach ports ({len(attach_ports)}): switch | port | port-channel (mlag id) | far end")
    for ap in attach_ports:
        print(f"  {ap['device']:<10} {ap['port']:<12} {ap['po']:<16} {ap['port_desc']} ({ap['far_po']})")
    print(f"--- attachments ({len(attachments)}): name | VLAN | /29 | MLAG pair | .1 | .2 | .3 stand-in")
    for a in attachments:
        m1, m2 = a["members"]
        print(f"  {a['name']:<22} {a['vlan']} {a['prefix']:<16} {a['pair']:<20} {a['addrs'][m1]} {a['addrs'][m2]} "
              f"{a['addrs']['standin']} {'+'.join(a['devices'])}")
    print(f"--- L2-only VLANs ({len(l2vlans)}): name | VLAN | pair | prefix (IP plan)")
    for v in l2vlans:
        print(f"  {v['name']:<22} {v['vlan']:<5} {v['pair']:<22} {v['prefix']}")
    for r in isp:
        x, c = r["crosslink"], r["carrier"]
        print(f"  {r['name']:<9} lo0={r['loopback0']}/32 {r['subif']}={r['subif_addr']}/24 {x['port']}={x['addr']}/31 "
              f"{c['port']}={c['ours']}/30 ({c['provider']} {c['theirs']})")
    for fw in fws:
        subs = ", ".join(f"{x['name']}={x['addr'][0]}/{x['addr'][1]}" for x in fw["subifs"] + fw["virtuals"])
        print(f"  {fw['name']:<13} {', '.join(p for p, _d, _a in fw['ports'])} | {', '.join(fw['aggs'])} | {subs}")
    for r in cloud:
        hs = ", ".join(f"{h['subif']}={h['ours']}/30" for h in r["handoffs"])
        print(f"  {r['name']:<9} lo0={r['loopback0']}/32 {r['subif']}={r['subif_addr']}/29 "
              f"{r['crosslink']['port']}={r['crosslink']['addr']}/31 {hs}")
    exp = expected_standin_interfaces(standins, attach_ports, attachments, isp, fws, cloud)
    n_if = sum(len(exp[s["name"]]) for s in standins)
    print(f"--- stage-3 totals: stand-ins {len(standins)}, attach ports {len(attach_ports)} (+ {len(attach_ports)} port-channels), "
          f"attachments {len(attachments)} (+ {2 * len(attachments)} leaf SVIs), L2-only VLANs {len(l2vlans)}, "
          f"stand-in interfaces {n_if}")
