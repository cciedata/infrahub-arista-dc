"""DC3 id snapshot (READ ONLY): the ids of the objects the dc3_site generator writes, to prove that a rerun keeps them.

Sections (one line per object, sorted by key):
  devices      DcimDevice in site DC3: id, role, primary address id, ASN id
  interfaces   DcimInterface of those devices (filter device__name__values): id, kind, address ids
  addresses    IpamIPAddress: id, interface id
  pairs        FabricSwitchPair whose name starts with DC3-: id, kind, member ids, anycast VTEP id
  attachments  FabricAttachment of site DC3: id, prefix id, device ids
  prefixes     IpamPrefix inside 10.24.0.0/16, 10.27.0.0/16 or 10.250.32.0/20: id, site id

Run (INFRAHUB_ADDRESS / INFRAHUB_API_TOKEN from the environment; scripts/bootstrap.sh does both steps):
  python3 scripts/dc3_id_snapshot.py --branch main --out lab/results/ids-1.json
  python3 scripts/dc3_id_snapshot.py --compare lab/results/ids-1.json lab/results/ids-2.json   exit 0 = identical
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import sys
from pathlib import Path

SITE = "DC3"
PAIR_PREFIX = "DC3-"
PREFIX_SCOPES = tuple(ipaddress.ip_network(p) for p in ("10.24.0.0/16", "10.27.0.0/16", "10.250.32.0/20"))
PAGE = 500

DEVICES = """query { DcimDevice(location__name__value: "%s", offset: %d, limit: %d) { count edges { node {
  id name { value } role { value } primary_address { node { id } } asn { node { id } } } } } }"""
INTERFACES = """query { DcimInterface(device__name__values: %s, offset: %d, limit: %d) { count edges { node {
  id __typename name { value } device { node { name { value } } }
  ... on InterfaceLayer3 { ip_addresses { edges { node { id } } } } } } } }"""
ADDRESSES = """query { IpamIPAddress(offset: %d, limit: %d) { count edges { node {
  id address { value } interface { node { id } } } } } }"""
PAIRS = """query { FabricSwitchPair(offset: %d, limit: %d) { count edges { node {
  id __typename name { value } members { edges { node { id } } }
  ... on FabricVpcDomain { anycast_vtep { node { id } } } } } } }"""
ATTACHMENTS = """query { FabricAttachment(site__name__value: "%s", offset: %d, limit: %d) { count edges { node {
  id name { value } prefix { node { id } } devices { edges { node { id } } } } } } }"""
PREFIXES = """query { IpamPrefix(offset: %d, limit: %d) { count edges { node {
  id prefix { value } site { node { id } } } } } }"""


def _one(rel):
    return rel["node"]["id"] if rel and rel.get("node") else None


def _many(rel):
    return sorted(e["node"]["id"] for e in (rel or {}).get("edges", []))


def in_scope(prefix: str) -> bool:
    net = ipaddress.ip_network(prefix, strict=False)
    return any(net.version == s.version and net.subnet_of(s) for s in PREFIX_SCOPES)


def build(raw: dict) -> dict:
    """Pure: raw query nodes per section -> snapshot (tested without Infrahub)."""
    snap = {
        "devices": {n["name"]["value"]: {"id": n["id"], "role": n["role"]["value"], "primary_address": _one(n.get("primary_address")),
                                         "asn": _one(n.get("asn"))} for n in raw["devices"]},
        "interfaces": {f'{n["device"]["node"]["name"]["value"]} {n["name"]["value"]}': {
            "id": n["id"], "kind": n["__typename"], "ip_addresses": _many(n.get("ip_addresses"))} for n in raw["interfaces"]},
        "addresses": {n["address"]["value"]: {"id": n["id"], "interface": _one(n.get("interface"))} for n in raw["addresses"]},
        "pairs": {n["name"]["value"]: {"id": n["id"], "kind": n["__typename"], "members": _many(n.get("members")),
                                       "anycast_vtep": _one(n.get("anycast_vtep"))}
                  for n in raw["pairs"] if n["name"]["value"].startswith(PAIR_PREFIX)},
        "attachments": {n["name"]["value"]: {"id": n["id"], "prefix": _one(n.get("prefix")), "devices": _many(n.get("devices"))}
                        for n in raw["attachments"]},
        "prefixes": {n["prefix"]["value"]: {"id": n["id"], "site": _one(n.get("site"))}
                     for n in raw["prefixes"] if in_scope(n["prefix"]["value"])},
    }
    return {k: dict(sorted(v.items())) for k, v in snap.items()}


def compare(a: dict, b: dict) -> list[str]:
    """Differences between two snapshots, one line each (empty = identical)."""
    out = []
    for section in sorted(set(a) | set(b)):
        sa, sb = a.get(section, {}), b.get(section, {})
        for key in sorted(set(sa) - set(sb)):
            out.append(f"{section}: {key} only in the first snapshot")
        for key in sorted(set(sb) - set(sa)):
            out.append(f"{section}: {key} only in the second snapshot")
        for key in sorted(set(sa) & set(sb)):
            if sa[key] != sb[key]:
                out.append(f"{section}: {key} changed: {sa[key]} -> {sb[key]}")
    return out


async def paged(client, template: str, key: str, args: tuple, branch: str) -> list[dict]:
    nodes, offset = [], 0
    while True:
        data = await client.execute_graphql(query=template % (*args, offset, PAGE), branch_name=branch)
        part = data[key]
        nodes += [e["node"] for e in part["edges"]]
        offset += PAGE
        if offset >= part["count"] or not part["edges"]:
            if len(nodes) != part["count"]:
                raise SystemExit(f"{key}: count {part['count']} but {len(nodes)} read")
            return nodes


async def take(branch: str) -> dict:
    from infrahub_sdk import InfrahubClient     # imported here so the module loads without the SDK (tests)

    client = InfrahubClient()
    raw = {"devices": await paged(client, DEVICES, "DcimDevice", (SITE,), branch)}
    names = json.dumps(sorted(n["name"]["value"] for n in raw["devices"]))       # a JSON list of strings is a GraphQL list
    raw["interfaces"] = await paged(client, INTERFACES, "DcimInterface", (names,), branch) if raw["devices"] else []
    raw["addresses"] = await paged(client, ADDRESSES, "IpamIPAddress", (), branch)
    raw["pairs"] = await paged(client, PAIRS, "FabricSwitchPair", (), branch)
    raw["attachments"] = await paged(client, ATTACHMENTS, "FabricAttachment", (SITE,), branch)
    raw["prefixes"] = await paged(client, PREFIXES, "IpamPrefix", (), branch)
    return build(raw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="DC3 id snapshot (READ ONLY) and comparison.")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--out", help="write the snapshot (JSON) to this file")
    ap.add_argument("--compare", nargs=2, metavar=("FIRST", "SECOND"), help="compare two snapshot files; exit 1 if they differ")
    a = ap.parse_args(argv)
    if a.compare:
        first, second = (json.loads(Path(p).read_text()) for p in a.compare)
        diff = compare(first, second)
        for line in diff:
            print(line)
        counts = ", ".join(f"{k} {len(v)}" for k, v in first.items())
        print(("IDENTICAL " if not diff else f"DIFFERENT ({len(diff)} lines) ") + f"({counts})")
        return 1 if diff else 0
    if not a.out:
        ap.error("--out or --compare is required")
    snap = asyncio.run(take(a.branch))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n")
    print(f"{a.out}: " + ", ".join(f"{k} {len(v)}" for k, v in snap.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
