"""Write the DC3 containerlab topology (cEOS-lab) and the cEOS interface mapping files from the generator plan (generators/dc3_plan.py).

No Infrahub needed: devices, ports and links come from generators/dc3_plan.py (the same plan the dc3_site
generator writes), leaf counts from objects/dc3-02-site.yml.

Writes:
  lab/intf-mapping/<model>.json   one cEOS mapping file per model: eth0 -> Management1,
                                  ethN -> the EOS name of front-panel port N (PORT_NAMING)
  lab/dc3.clab.yml                30 cEOS nodes (kind arista_ceos, image ceos:4.36.2F), startup-config
                                  configs/<device>.cfg, the model's mapping file bound read-only on each node,
                                  88 spine-leaf links + MLAG peer-link members + keepalive links.
                                  Plus the 8 FRR stand-ins (kind linux, FRR_IMAGE), each running
                                  configs/<device>.sh (artifact frr-lab-init) via `exec`; the lab-only nodes (carriers,
                                  internet hosts, branches, partner, AWS / Azure, cloud-exchange bridges, one test host per
                                  server VRF); every attach link of the generator plus the stand-in links.

Paths inside the topology are relative to lab/ (containerlab resolves them from the topology file's folder ).
Stand-in scripts are rendered with infrahubctl render into lab/configs/ (names in STANDIN_SCRIPTS / LAB_ONLY_NODES).
If lab/configs/<device>.cfg already exists (rendered with infrahubctl render), its Management1 address is used
as the node's mgmt-ipv4 so the containerlab management address equals the Infrahub one.

Smoke subset: --subset smoke writes lab/dc3-smoke.clab.yml with only the SUBSETS["smoke"] nodes
(spines, the MLAG pair L1-L2, single leaf L11, the PROD test host) and the links among them; the lab name stays "dc3"
so the container names (clab-dc3-<node>) are the same as in the full lab. The full topology is unchanged.

Run: python3 scripts/dc3_lab_topology.py                  (from the repo root; full lab, lab/dc3.clab.yml)
     python3 scripts/dc3_lab_topology.py --subset smoke   (lab/dc3-smoke.clab.yml)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LAB = ROOT / "lab"

# Lab values
LAB_NAME = "dc3"
KIND = "arista_ceos"                         # containerlab kind for cEOS-lab
IMAGE = "ceos:4.36.2F"                       # (cEOS-lab 4.36.2F); local image tag (as imported on the laptop)
MGMT_NETWORK = "dc3-mgmt"
MGMT_SUBNET = "10.250.32.0/24"               # DC3 Management1 /24; gateway 10.250.32.1 = containerlab bridge
MGMT_VRF = "MGMT"                            # CLAB_MGMT_VRF env
MAPPING_DIR = "intf-mapping"
CONFIG_DIR = "configs"
MAPPING_TARGET = "/mnt/flash/EosIntfMapping.json"

# FRR stand-ins and lab-only nodes
LINUX_KIND = "linux"
FRR_IMAGE = "quay.io/frrouting/frr:10.6.0"    # FRR 10.6.0; also used for hosts/bridges
INIT_TARGET = "/frr-init.sh"                  # where a node's rendered script is bound; run by `exec`
INIT_EXEC = ["sh " + INIT_TARGET]
# Lab-only nodes: name -> (script file in configs/ or None, management address). Addresses at the top of the
# Management1 /24 (.240-.253), not used by Infrahub pools.
LAB_ONLY_NODES = {
    "DC3-ISP-A": ("DC3-ISP1.carrier.sh", "10.250.32.240"),
    "DC3-ISP-B": ("DC3-ISP2.carrier.sh", "10.250.32.241"),
    "DC3-BR001": ("DC3-SDWANFW1.branch.sh", "10.250.32.242"),
    "DC3-BR002": ("DC3-SDWANFW1.branch.sh", "10.250.32.243"),
    "DC3-PARTNER01": ("DC3-VPNFW1.partner.sh", "10.250.32.244"),
    "DC3-HOST-PROD": (None, "10.250.32.245"),
    "DC3-HOST-ISP-A": (None, "10.250.32.246"),
    "DC3-HOST-ISP-B": (None, "10.250.32.247"),
    "DC3-HOST-DEV": (None, "10.250.32.248"),
    "DC3-HOST-CITRIX": (None, "10.250.32.249"),
    "DC3-AWS": ("DC3.cloud-provider.sh", "10.250.32.250"),
    "DC3-AZURE": ("DC3.cloud-provider.sh", "10.250.32.251"),
    "DC3-XC1": (None, "10.250.32.252"),
    "DC3-XC2": (None, "10.250.32.253"),
}
INTERNET_HOSTS = {"DC3-HOST-ISP-A": "198.18.1.0/24", "DC3-HOST-ISP-B": "198.18.2.0/24"}   # internet test prefixes
HOST_ADDRESS, HOST_GATEWAY = 10, 1             # host .10, gateway .1 (segment anycast gateway / carrier)

# Smoke subset
SUBSETS = {"smoke": ("DC3-S1", "DC3-S2", "DC3-L1", "DC3-L2", "DC3-L11", "DC3-HOST-PROD")}
SUBSET_FILE = "{lab}-{subset}.clab.yml"


def load_plan():
    path = ROOT / "generators" / "dc3_plan.py"
    spec = importlib.util.spec_from_file_location("dc3_plan_lab", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


P = load_plan()


def site_counts(path: Path = ROOT / "objects" / "dc3-02-site.yml") -> tuple[str, int, int, int]:
    doc = next(d for d in yaml.safe_load_all(path.read_text()) if d)
    site = doc["spec"]["data"][0]
    return site["name"], site["vpc_leaf_pairs"], site["esi_leaf_pairs"], site["single_leaves"]


def port_number(eos_name: str) -> int:
    m = re.fullmatch(r"Ethernet(\d+)(/1)?", eos_name)
    if not m:
        raise ValueError(f"{eos_name}: not a front-panel Ethernet name")
    return int(m.group(1))


def eth(eos_name: str) -> str:
    """EOS front-panel name -> containerlab interface (ethN = front-panel port N)."""
    return f"eth{port_number(eos_name)}"


def mapping(model: str) -> dict:
    """cEOS EosIntfMapping.json for one model."""
    ports = {}
    for first, last, _kind in P.PORT_NAMING[model]:
        for n in range(first, last + 1):
            ports[f"eth{n}"] = P.eos_port(model, n)
    return {"ManagementIntf": {"eth0": P.MGMT_IF}, "EthernetIntf": ports}


def mapping_file(model: str) -> str:
    return f"{MAPPING_DIR}/{model}.json"


def mgmt_address(device: str, lab_dir: Path) -> str | None:
    """Management1 address (without the mask) from a rendered startup-config, if present."""
    cfg = lab_dir / CONFIG_DIR / f"{device}.cfg"
    if not cfg.exists():
        return None
    m = re.search(rf"^interface {P.MGMT_IF}\n(?:   .*\n)*?   ip address (\S+)/\d+$", cfg.read_text(), re.M)
    return m.group(1) if m else None


def stage3_objects() -> dict:
    """Branches, partners, segments and lab host ports from the tenant and stand-in object files (what the templates read from Infrahub)."""
    out = {"branches": [], "partners": [], "segments": [], "hosts": []}
    for name, kind, key in (("dc3-08-standins.yml", "FabricBranch", "branches"), ("dc3-08-standins.yml", "FabricPartner", "partners"),
                            ("dc3-07-segments.yml", "FabricSegment", "segments"),
                            ("dc3-10-lab-host-ports.yml", "InterfacePhysical", "hosts")):
        for doc in yaml.safe_load_all((ROOT / "objects" / name).read_text()):
            if doc and doc["spec"]["kind"] == kind:
                out[key] += doc["spec"]["data"]
    return out


def standin_rows(site: str = "DC3") -> list[tuple]:
    """(name, role) of the FRR stand-in devices the generator writes (generators/dc3_plan.py STANDINS)."""
    return [(s["name"], s["role"]) for s in P.build_standins(site)]


def host_node_name(site: str, description: str) -> str:
    """Host node = first word of the access-port description ( `<peer device> <peer interface>`, review fix R-4)."""
    name = description.split(" ")[0]
    if not name.startswith(f"{site}-HOST-"):
        raise ValueError(f"access port description {description!r} does not name a {site}-HOST- node")
    return name


def script_mgmt_address(path: Path) -> str | None:
    """Management address from a rendered stand-in script header (`# Management <addr>/<len>`)."""
    if not path.exists():
        return None
    m = re.search(r"^# Management (\S+)/\d+ ", path.read_text(), re.M)
    return m.group(1) if m else None


def stage3_links(site: str, plan: list[dict], obj: dict) -> list[dict]:
    """Attach links (switch ethN <-> stand-in port), stand-in cross-links, carrier / internet host, branch, partner,
    cloud exchange and test host links."""
    standins = P.build_standins(site)
    links = [{"endpoints": [f"{ap['device']}:{eth(ap['port'])}", f"{ap['far']}:{ap['far_port']}"]}
             for ap in P.build_attach_ports(site, plan, standins)]
    links.append({"endpoints": [f"{site}-ISP1:{P.ISP_CROSSLINK_PORT}", f"{site}-ISP2:{P.ISP_CROSSLINK_PORT}"]})
    links.append({"endpoints": [f"{site}-CLOUD1:{P.CLOUD_XLINK_PORT}", f"{site}-CLOUD2:{P.CLOUD_XLINK_PORT}"]})
    for n, carrier in P.CARRIER.items():
        links.append({"endpoints": [f"{site}-ISP{n}:{P.ISP_CARRIER_PORT}", f"{site}-{carrier}:isp"]})
        links.append({"endpoints": [f"{site}-{carrier}:host", f"{site}-HOST-{carrier}:eth1"]})
    for b in sorted(obj["branches"], key=lambda x: x["branch_id"]):
        if site in b["hubs"]:
            links.append({"endpoints": [f"{site}-SDWANFW1:{b['name']}", f"{site}-{b['name']}:eth1"]})
    for p in obj["partners"]:
        if any(t[1].startswith(P.SITE_TENANT[site] + ".") for t in p["translated_prefixes"]):
            links.append({"endpoints": [f"{site}-VPNFW1:{p['name']}", f"{site}-{p['name']}:eth1"]})
    for n in (1, 2):
        links.append({"endpoints": [f"{site}-CLOUD{n}:{P.CLOUD_XC_PORT}", f"{site}-XC{n}:eth1"]})
        links.append({"endpoints": [f"{site}-AWS:eth{n}", f"{site}-XC{n}:eth2"]})
        links.append({"endpoints": [f"{site}-AZURE:eth{n}", f"{site}-XC{n}:eth3"]})
    for h in obj["hosts"]:
        links.append({"endpoints": [f"{h['device']}:{eth(h['name'])}", f"{host_node_name(site, h['description'])}:eth1"]})
    return links


def lab_only_exec(site: str, name: str, obj: dict) -> list[str] | None:
    """Commands for lab-only nodes without an Infrahub script: test hosts, internet hosts, exchange bridges."""
    if name in INTERNET_HOSTS:
        net = INTERNET_HOSTS[name].split("/")
        o = net[0].split(".")
        base = ".".join(o[:3])
        return [f"ip addr add {base}.{HOST_ADDRESS}/{net[1]} dev eth1", "ip link set eth1 up",
                f"ip route replace default via {base}.{HOST_GATEWAY} dev eth1"]
    if name.startswith(f"{site}-XC"):
        return ["ip link add br0 type bridge", "ip link set br0 up"] + \
               [f"ip link set eth{k} master br0" for k in (1, 2, 3)] + [f"ip link set eth{k} up" for k in (1, 2, 3)]
    for h in obj["hosts"]:
        if host_node_name(site, h["description"]) == name:
            seg = next(s for s in obj["segments"] if s["name"] == h["access_segment"])
            net, plen = seg["prefix"][1].split("/")
            base = ".".join(net.split(".")[:3])
            return [f"ip addr add {base}.{HOST_ADDRESS}/{plen} dev eth1", "ip link set eth1 up",
                    f"ip route replace default via {base}.{HOST_GATEWAY} dev eth1"]
    return None


def build_links(site: str, plan: list[dict], vpc: int, esi: int) -> list[dict]:
    """Spine-leaf links (plan order), then per MLAG pair the peer-link members and the keepalive link."""
    links = [{"endpoints": [f"{l['spine']}:{eth(l['spine_port'])}", f"{l['leaf']}:{eth(l['leaf_port'])}"]}
             for l in P.build_links(site, plan)]
    domains = P.mlag_domains(plan, P.build_pairs(site, plan, vpc, esi))
    for d in domains:
        a, b = d["members"]
        for port in (*d["peer_link"], d["keepalive"]):
            links.append({"endpoints": [f"{a}:{eth(port)}", f"{b}:{eth(port)}"]})
    return links


def topology(lab_dir: Path = LAB) -> dict:
    site, vpc, esi, single = site_counts()
    plan = P.build_plan(site, vpc, esi, single)
    nodes = {}
    for r in plan:
        node = {"kind": KIND, "startup-config": f"{CONFIG_DIR}/{r['name']}.cfg",
                "binds": [f"{mapping_file(r['model'])}:{MAPPING_TARGET}:ro"]}
        addr = mgmt_address(r["name"], lab_dir)
        if addr:
            node["mgmt-ipv4"] = addr
        nodes[r["name"]] = node
    # ---- FRR stand-ins (Infrahub devices) and lab-only nodes
    obj = stage3_objects()
    for name, _role in standin_rows(site):
        node = {"kind": LINUX_KIND, "binds": [f"{CONFIG_DIR}/{name}.sh:{INIT_TARGET}:ro"], "exec": list(INIT_EXEC)}
        addr = script_mgmt_address(lab_dir / CONFIG_DIR / f"{name}.sh")
        if addr:
            node["mgmt-ipv4"] = addr
        nodes[name] = node
    for name, (script, addr) in LAB_ONLY_NODES.items():
        node = {"kind": LINUX_KIND, "mgmt-ipv4": addr}
        if script:
            node["binds"] = [f"{CONFIG_DIR}/{script}:{INIT_TARGET}:ro"]
            node["exec"] = list(INIT_EXEC)
        else:
            node["exec"] = lab_only_exec(site, name, obj)
        nodes[name] = node
    seen: dict = {}
    for n, x in nodes.items():
        if x.get("mgmt-ipv4"):
            seen.setdefault(x["mgmt-ipv4"], []).append(n)
    dupes = {a: ns for a, ns in seen.items() if len(ns) > 1}
    if dupes:                                        # stop if a lab-only address is already in use
        raise ValueError(f"management address used twice: {dupes}")
    links = build_links(site, plan, vpc, esi) + stage3_links(site, plan, obj)
    for l in links:
        for e in l["endpoints"]:
            if e.split(":")[0] not in nodes:
                raise ValueError(f"link endpoint {e}: no such node")
    return {
        "name": LAB_NAME,
        "mgmt": {"network": MGMT_NETWORK, "ipv4-subnet": MGMT_SUBNET},
        "topology": {
            "kinds": {KIND: {"image": IMAGE, "env": {"CLAB_MGMT_VRF": MGMT_VRF}}, LINUX_KIND: {"image": FRR_IMAGE}},
            "nodes": nodes,
            "links": links,
        },
    }


def subset_topology(topo: dict, names) -> dict:
    """The topology restricted to some nodes: those nodes (definitions unchanged) and only the links among them."""
    names = list(names)
    missing = [n for n in names if n not in topo["topology"]["nodes"]]
    if missing:
        raise ValueError(f"subset nodes not in the topology: {missing}")
    t = topo["topology"]
    nodes = {n: x for n, x in t["nodes"].items() if n in names}
    links = [l for l in t["links"] if all(e.split(":")[0] in nodes for e in l["endpoints"])]
    kinds = {k: v for k, v in t["kinds"].items() if any(x["kind"] == k for x in nodes.values())}
    return {"name": topo["name"], "mgmt": topo["mgmt"], "topology": {"kinds": kinds, "nodes": nodes, "links": links}}


def topology_path(lab_dir: Path = LAB, subset: str | None = None) -> Path:
    return lab_dir / (SUBSET_FILE.format(lab=LAB_NAME, subset=subset) if subset else f"{LAB_NAME}.clab.yml")


def write(lab_dir: Path = LAB, subset: str | None = None) -> dict:
    (lab_dir / MAPPING_DIR).mkdir(parents=True, exist_ok=True)
    (lab_dir / CONFIG_DIR).mkdir(parents=True, exist_ok=True)
    for model in sorted(set(P.DEVICE_TYPE.values())):
        (lab_dir / mapping_file(model)).write_text(json.dumps(mapping(model), indent=2) + "\n")
    topo = topology(lab_dir)
    header = ("# DC3 cEOS lab (containerlab). Written by scripts/dc3_lab_topology.py from the generator plan (generators/dc3_plan.py); do not edit.\n"
              "# Startup configs: lab/configs/<device>.cfg (infrahubctl render ... --out); mapping files: lab/intf-mapping/.\n")
    if subset:
        topo = subset_topology(topo, SUBSETS[subset])
        header += f"# Subset '{subset}': {', '.join(SUBSETS[subset])}; links among them only.\n"
    topology_path(lab_dir, subset).write_text(header + yaml.safe_dump(topo, sort_keys=False, width=200))
    return topo


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Write the DC3 containerlab topology and the cEOS mapping files.")
    ap.add_argument("--subset", choices=sorted(SUBSETS), help="write only a subset topology (lab/dc3-<subset>.clab.yml)")
    a = ap.parse_args(argv)
    topo = write(subset=a.subset)
    t = topo["topology"]
    with_mgmt = sum(1 for n in t["nodes"].values() if "mgmt-ipv4" in n)
    print(f"{topology_path(LAB, a.subset).relative_to(ROOT)}: {len(t['nodes'])} nodes, {len(t['links'])} links, "
          f"mgmt-ipv4 on {with_mgmt} nodes (cEOS and stand-ins: from lab/configs when rendered; lab-only nodes: fixed)")
    print(f"lab/{MAPPING_DIR}/: " + ", ".join(f"{m}.json" for m in sorted(set(P.DEVICE_TYPE.values()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
