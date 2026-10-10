"""Standalone guard: refuse writes unless the Infrahub instance holds DC3 only.

The standalone build (scripts/bootstrap.sh) writes on the main branch of its own instance. An Infrahub instance that
also holds other sites must never receive those writes. The rule, checked on
branch main AND on the target branch (when it is another one):
  - every LocationSite is DC3 (no site at all is fine: a fresh instance), and
  - every DcimDevice is located in DC3 (none is fine).
Before the schema is loaded the kinds do not exist yet; that counts as empty. A partial schema (one of the two kinds
missing) is refused.

--schema also requires every kind of REQUIRED_KINDS (after the schema load).

Run (READ ONLY; INFRAHUB_ADDRESS / INFRAHUB_API_TOKEN from the environment, scripts/bootstrap.sh sets them):
  python3 scripts/dc3_guard.py                    exit 0 = DC3-only (or empty), 1 = refused
  python3 scripts/dc3_guard.py --schema           also require the kinds of REQUIRED_KINDS
  python3 scripts/dc3_guard.py --branch <name>    check <name> as well as main
Other scripts import check() (scripts/dc3_create_pools.py --standalone).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

STANDALONE_SITE = "DC3"
INSTANCE_BRANCH = "main"

# Kinds the object files, the pool script, the generator and the queries use (tests/test_dc3_standalone.py keeps this
# list complete against the code and checks every kind against schemas/ or the Infrahub core kinds).
REQUIRED_KINDS = (
    "CoreStandardGroup", "CoreIPAddressPool", "CoreIPPrefixPool", "IpamNamespace",
    "LocationCountry", "LocationMetro", "LocationSite",
    "OrganizationManufacturer", "OrganizationProvider", "DcimPlatform", "DcimDeviceType", "DcimDevice", "DcimInterface",
    "InterfacePhysical", "InterfaceVirtual", "InterfacePortChannel", "InterfaceLayer3", "IpamPrefix", "IpamIPAddress",
    "FabricAutonomousSystem", "FabricVRF", "FabricSegment", "FabricSwitchPair", "FabricVpcDomain", "FabricEsiPair",
    "FabricAttachment", "FabricL2Vlan", "FabricBranch", "FabricPartner",
)

STATE_QUERY = """
query Dc3GuardState($site: String!) {
  LocationSite { count edges { node { name { value } } } }
  all_devices: DcimDevice { count }
  site_devices: DcimDevice(location__name__value: $site) { count }
}
"""


def problems_from_state(sites: list[str] | None, devices_total: int | None, devices_site: int | None) -> list[str]:
    """Pure rule (tested without Infrahub). None = the kind does not exist yet (fresh instance)."""
    out = []
    others = sorted(s for s in (sites or []) if s != STANDALONE_SITE)
    if others:
        out.append(f"the instance has sites other than {STANDALONE_SITE}: {', '.join(others)} "
                   "(the instance holds other sites; the standalone build never writes there)")
    if devices_total is not None and devices_total != (devices_site or 0):
        out.append(f"the instance has {devices_total - (devices_site or 0)} device(s) outside {STANDALONE_SITE}")
    return out


def branches_to_check(branch: str | None) -> list[str]:
    """main always (the instance), plus the target branch when it is another one."""
    out = [INSTANCE_BRANCH]
    if branch and branch.strip() and branch.strip() != INSTANCE_BRANCH:
        out.append(branch.strip())
    return out


async def schema_kinds(client: InfrahubClient, branch: str) -> set[str]:
    return set(await client.schema.all(branch=branch, refresh=True))


async def branch_problems(client: InfrahubClient, branch: str) -> list[str]:
    """Empty list = the branch holds DC3 only (or nothing yet)."""
    kinds = await schema_kinds(client, branch)
    if "LocationSite" not in kinds and "DcimDevice" not in kinds:
        return []
    if not {"LocationSite", "DcimDevice"} <= kinds:
        return [f"partial schema: LocationSite {'present' if 'LocationSite' in kinds else 'missing'}, "
                f"DcimDevice {'present' if 'DcimDevice' in kinds else 'missing'}; not a DC3 standalone instance"]
    data = await client.execute_graphql(query=STATE_QUERY, variables={"site": STANDALONE_SITE}, branch_name=branch)
    sites = [e["node"]["name"]["value"] for e in data["LocationSite"]["edges"]]
    if data["LocationSite"]["count"] != len(sites):
        return [f"LocationSite count {data['LocationSite']['count']} but {len(sites)} returned; refusing"]
    return problems_from_state(sites, data["all_devices"]["count"], data["site_devices"]["count"])


async def missing_kinds(client: InfrahubClient, branch: str) -> list[str]:
    kinds = await schema_kinds(client, branch)
    return [k for k in REQUIRED_KINDS if k not in kinds]


async def check(client: InfrahubClient, branch: str | None = None, require_schema: bool = False) -> list[str]:
    """Every problem found, prefixed with its branch; empty list = writes allowed."""
    problems = []
    for b in branches_to_check(branch):
        problems += [f"branch {b}: {p}" for p in await branch_problems(client, b)]
        if require_schema:
            missing = await missing_kinds(client, b)
            if missing:
                problems.append(f"branch {b}: schema kinds missing: {', '.join(missing)}")
    return problems


async def main_async(branch: str | None, require_schema: bool) -> int:
    from infrahub_sdk import InfrahubClient     # imported here so the module loads without the SDK (tests)

    client = InfrahubClient()
    problems = await check(client, branch, require_schema)
    address = client.config.address
    if problems:
        for p in problems:
            print(f"REFUSED {address} {p}")
        return 1
    print(f"OK {address} branches {', '.join(branches_to_check(branch))}: {STANDALONE_SITE}-only instance"
          + (", schema kinds present" if require_schema else ""))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Refuse unless the Infrahub instance holds DC3 only (READ ONLY).")
    parser.add_argument("--branch", default=INSTANCE_BRANCH, help="target branch, checked together with main")
    parser.add_argument("--schema", action="store_true", help="also require every kind of REQUIRED_KINDS")
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args.branch, args.schema)))


if __name__ == "__main__":
    main()
