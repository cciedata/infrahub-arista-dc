"""DC3 resource pools.

Pool names DC3-..., source prefixes from objects/dc3-03-ip-plan.yml (which must be loaded on the same branch first).

Branch rule:
  - an Infrahub instance that also holds other sites: a named branch (for example dc3), never main;
  - standalone DC3 instance: --standalone allows main, and only when scripts/dc3_guard.py passes (the instance
    holds no site other than DC3 and no device outside DC3). Without --standalone, main stays refused.

Run (INFRAHUB_ADDRESS / INFRAHUB_API_TOKEN from the environment):
  python3 scripts/dc3_create_pools.py --branch dc3                      instance with other sites
  python3 scripts/dc3_create_pools.py --standalone --branch main        standalone instance (scripts/bootstrap.sh)
  infrahubctl run scripts/dc3_create_pools.py --branch dc3              instance with other sites, through infrahubctl
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

FORBIDDEN_BRANCHES = {"", "main"}

ADDRESS = {"default_address_type": "IpamIPAddress"}
P2P = {
    "default_prefix_length": 31,
    "default_member_type": "address",
    "default_prefix_type": "IpamPrefix",
}

POOLS = [
    {
        "kind": "CoreIPAddressPool",
        "name": "DC3-LOOPBACK0",
        "prefix": "10.27.0.0/24",
        "description": "DC3 Loopback0: router ID, BGP peering",
        "settings": {**ADDRESS, "default_prefix_length": 32},
    },
    {
        "kind": "CoreIPAddressPool",
        "name": "DC3-LOOPBACK1-VTEP",
        "prefix": "10.27.1.0/24",
        "description": "DC3 Loopback1: VTEP source (server, border and service leaves)",
        "settings": {**ADDRESS, "default_prefix_length": 32},
    },
    {
        "kind": "CoreIPPrefixPool",
        "name": "DC3-FABRIC-P2P",
        "prefix": "10.27.4.0/22",
        "description": "DC3 fabric p2p links: one /31 per link",
        "settings": P2P,
    },
    {
        "kind": "CoreIPAddressPool",
        "name": "DC3-MGMT0",
        "prefix": "10.250.32.0/24",
        "description": "DC3 Management1 on the OOB network",
        "settings": {**ADDRESS, "default_prefix_length": 24},
    },
    {
        "kind": "CoreIPAddressPool",
        "name": "DC3-ANYCAST-VTEP",
        "prefix": "10.27.2.0/25",
        "description": "DC3 MLAG anycast VTEPs: one per MLAG pair with VTEP (server pair, BL, SL)",
        "settings": {**ADDRESS, "default_prefix_length": 32},
    },
    {
        "kind": "CoreIPPrefixPool",
        "name": "DC3-VPC-BACKUP",
        "prefix": "10.27.3.0/24",
        "description": "DC3 MLAG Vlan3900 /31: one per MLAG pair (server, BL, SL, EDGE, DMZ)",
        "settings": P2P,
    },
    {
        "kind": "CoreIPPrefixPool",
        "name": "DC3-VPC-KEEPALIVE",
        "prefix": "10.27.17.0/24",
        "description": "DC3 MLAG peer-keepalive link: one /31 per MLAG pair (server, BL, SL, EDGE, DMZ)",
        "settings": P2P,
    },
]


def check_branch(branch: str | None, standalone: bool = False) -> str:
    """Instance with other sites: a named branch only. Standalone: main allowed too (the guard runs before any write)."""
    b = (branch or "").strip()
    if b == "" or (b == "main" and not standalone):
        raise SystemExit(f"refused: branch {branch!r}; DC3 pools are created on a named branch (for example dc3), never main "
                         "(standalone DC3 instance: --standalone, which allows main only when scripts/dc3_guard.py passes)")
    return b


def load_guard():
    """scripts/dc3_guard.py by file path (works from any working directory)."""
    path = Path(__file__).resolve().parent / "dc3_guard.py"
    spec = importlib.util.spec_from_file_location("dc3_guard_for_pools", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def guard_or_exit(client: InfrahubClient, branch: str) -> None:
    problems = await load_guard().check(client, branch, require_schema=True)
    if problems:
        raise SystemExit("refused (--standalone): the instance is not a DC3-only instance:\n  " + "\n  ".join(problems))
    print(f"guard: DC3-only instance, branch {branch} allowed (--standalone)")


async def run(client: InfrahubClient, log: logging.Logger, branch: str, standalone: bool = False, **kwargs) -> None:
    """Entry point for `infrahubctl run` and for main() below."""
    standalone = standalone is True or str(standalone).lower() in ("1", "true", "yes")
    branch = check_branch(branch, standalone)
    if standalone:
        await guard_or_exit(client, branch)
    namespace = await client.get(kind="IpamNamespace", name__value="default", branch=branch)
    for item in POOLS:
        prefix = await client.get(kind="IpamPrefix", prefix__value=item["prefix"], branch=branch)   # raises if missing
        pool = await client.create(
            kind=item["kind"],
            branch=branch,
            name=item["name"],
            description=item["description"],
            resources=[prefix],
            ip_namespace=namespace,
            **item["settings"],
        )
        await pool.save(allow_upsert=True)
        print(f"{item['kind']} {pool.name.value}: id={pool.id} source={item['prefix']} branch={branch} {item['settings']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the DC3 pools (instance with other sites: named branch; standalone: --standalone).")
    parser.add_argument("--branch", default=None, help="Infrahub branch: a named branch such as dc3, or main (with --standalone)")
    parser.add_argument("--standalone", action="store_true",
                        help="standalone DC3 instance: allow main, only when scripts/dc3_guard.py passes")
    args = parser.parse_args()
    branch = check_branch(args.branch, args.standalone)
    from infrahub_sdk import InfrahubClient     # imported here so the POOLS table loads without the SDK (tests)

    client = InfrahubClient()
    asyncio.run(run(client, logging.getLogger("dc3_create_pools"), branch, standalone=args.standalone))


if __name__ == "__main__":
    sys.exit(main())
