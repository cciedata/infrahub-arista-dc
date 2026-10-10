# Clickable lab topology: log in to a device, start and stop it

This page shows how to run the DC3 lab with a clickable topology, like EVE-NG. You click a device and get its CLI, and you
can stop, start or restart single devices or the whole lab. It uses containerlab's own GUI, the **containerlab VS Code
extension**: its TopoViewer draws the running lab, and every node has actions. Nothing here is DC3 code. DC3 provides the
lab files, the steps and a test (`lab/dc3_lifecycle_test.sh`) showing what works.

The PDF diagrams (`docs/diagrams/DC3-diagrams.pdf`) are for reading. This page is for working on a running lab.

## What you get

| Where | Action (extension command) | What it does |
|---|---|---|
| Lab | Deploy, Redeploy, Destroy (each also "(cleanup)"), Start nodes, Stop nodes | the whole lab |
| Node | Attach shell, Connect (SSH), Connect (Telnet), View logs | open a terminal on that device |
| Node | Start, Stop, Restart, Pause, Unpause | one device; the rest of the lab keeps running |
| Interface | Capture interface (Wireshark / Edgeshark) | packet capture on one link |

Source: the extension's command list (`package.json` of srl-labs/vscode-containerlab, version 0.26.3) and
https://containerlab.dev/manual/gui/vsc-extension/. In TopoViewer the right-click (radial) menu of a running node has SSH,
container shell, logs and properties. Start, stop and restart are in the extension's tree view (right-click the node)
(https://hack.srexperts.net/tools/tools-clab-vscode-plugin/).

Node start, stop and restart are the containerlab commands `containerlab stop|start|restart -t <topology> --node <name>`.
containerlab keeps the stopped device's links ("parks the veth interfaces") so they come back on start, for nodes with
veth links and a single container (https://containerlab.dev/cmd/stop/). Subsets of nodes need containerlab 0.75 or
later (https://containerlab.dev/rn/0.75/). Every DC3 node meets these conditions: all are `arista_ceos` or `linux`, and
every link is a veth link (lab/dc3.clab.yml).

## Which lab fits your machine

| Machine | Lab | Memory |
|---|---|---|
| Laptop, 12-16 GB | smoke subset: S1, S2, L1, L2, L11 and the PROD host (`lab/dc3-smoke.clab.yml`) | about 6.8 GB, Infrahub stopped (laptop smoke runs) |
| 64 GB Linux host | full lab, 52 nodes (`lab/dc3.clab.yml`) | about 45 GB for the 30 cEOS plus the FRR nodes; not yet run on a laptop or desktop |

cEOS-lab is an x86-64 image. An Apple-silicon Mac is not tested.

## Set up (once)

1. Build the renders and the images as in docs/QUICKSTART.md steps 1-5. You need `lab/configs/` (43
   files), `ceos:4.36.2F`, `quay.io/frrouting/frr:10.6.0`, and containerlab 0.75 or later.
2. Install VS Code and the **Containerlab** extension (publisher srl-labs; source https://github.com/srl-labs/vscode-containerlab).
3. Open the repo folder on the machine where the lab runs. The extension runs containerlab in the same place as the VS Code
   window (extension docs):
   - **Windows laptop:** connect VS Code to WSL ("WSL: Connect to WSL"), then open `~/infrahub-arista-dc`. The extension supports
     Linux and WSL.
   - **Remote Linux host:** install the Remote-SSH extension, connect to the host and open `~/infrahub-arista-dc` there
     ("connect to that host with the Remote SSH extension and open the Containerlab view there").
4. Docker and containerlab must work without sudo for your user, for example through the `clab_admins` group (docs/BUILD.md
   prerequisites).

## Run the lab and click on it

1. Write the topology and fill the secrets (the deploy needs the filled configs):
   ```
   python3 scripts/dc3_lab_topology.py --subset smoke      # laptop; for the full lab: python3 scripts/dc3_lab_topology.py
   python3 lab/dc3_secrets.py fill lab/configs
   ```
2. In VS Code, open `lab/dc3-smoke.clab.yml` (or `lab/dc3.clab.yml`) and choose **Deploy**. On the laptop, stop Infrahub
   first so the switches have memory: `docker compose stop` in the repo folder.
3. Open the topology: click the Containerlab icon in the activity bar, or **Graph with TopoViewer** (`Ctrl+Alt+G`) on the
   topology file. After the deploy the view shows the running nodes. Drag them to match `docs/diagrams/DC3-diagrams.pdf`
   page 1 if you like.
4. Wait about 2-3 minutes for the cEOS switches to boot. The CLI answers after about 80-100 s and BGP is up about 80 s
   later (laptop smoke runs).

## Log in to a device

| Device | Click | Then |
|---|---|---|
| Arista switch (DC3-S*, L*, BL*, SL*, EDGE*, DMZ*) | Attach shell | `Cli`, then `enable`. This is the same login the DC3 checks use (`docker exec ... Cli`) |
| Arista switch over SSH | Connect (SSH) | user `dc3-automation`, your password from `~/.dc3/secrets.env`. The extension asks for `admin` by default: type `ssh dc3-automation@clab-dc3-<node>` instead |
| Firewall, ISP and cloud stand-ins (FRR) | Attach shell | `vtysh` for the routing CLI (`show bgp vrf all summary`) |
| Test hosts, branches, partner | Attach shell | a Linux shell (`ip addr`, `ping`, `traceroute`) |

Management addresses (VRF MGMT, Management1) are on page 15 of the PDF. eAPI answers over HTTPS on them as
`dc3-automation` (ZTP check).

## Start and stop devices

- One device: right-click it in the extension's tree view → **Stop**, **Start** or **Restart**. Same from a terminal:
  ```
  containerlab stop    -t lab/dc3-smoke.clab.yml --node DC3-L11
  containerlab start   -t lab/dc3-smoke.clab.yml --node DC3-L11
  containerlab restart -t lab/dc3-smoke.clab.yml --node DC3-L1
  ```
- Whole lab: **Stop nodes** / **Start nodes** on the lab (`containerlab stop|start -t <topology>`).
- Done for the day: **Destroy**. Start Infrahub again: `docker compose start` in the repo folder.

What comes back after a start is listed under "Tested" below.

## Test it yourself

```
bash lab/dc3_lifecycle_test.sh                                  # laptop: deploys the smoke lab, tests, destroys
LIFECYCLE_KEEP=1 bash lab/dc3_lifecycle_test.sh                 # same, lab stays up afterwards to click on
LIFECYCLE_LAB=up TOPO=lab/dc3.clab.yml bash lab/dc3_lifecycle_test.sh   # a lab you already deployed: tests only
```
| Test | What it does | Pass when |
|---|---|---|
| A | stop, then start DC3-L11 | stopped: the spines lose its sessions; started: same hostname (startup-config kept), its 5 links present, all its BGP sessions back, the PROD host reaches its gateway |
| E | TCP 22 and 443 to L11's Management1 from the lab machine | information: shows whether the SSH action can work |
| B | stop, then start the PROD host | its address and default route are still there. If not, the test re-applies them (see Known limits) |
| C | restart DC3-L1 (MLAG member) | MLAG active on L1 and L2, BGP back |
| D | stop and start the whole lab | every switch back with all its sessions, host reaches its gateway |

Results: `lab/results/lifecycle-<UTC>/summary.txt` and one file per step.

## Tested

Smoke lab on a 16 GB laptop (WSL2 capped at 8 GB, Infrahub stopped, containerlab 0.79.0, extension v0.26.3):
- Deploy: containerlab reported `post-deploy: channel timeout sending input to device` on every cEOS, yet all 6 containers
  were up and all 5 switches answered `show hostname` five minutes later.
- Memory with the lab up: 6.9 GB for the 6 containers (1.29-1.48 GB per cEOS), about 250 MB left in WSL.
- The Containerlab panel lists lab `dc3` with its 6 nodes as running (after a refresh of Running Labs). The lab menu has
  Destroy, Redeploy, Start Nodes, Stop Nodes, Restart Nodes, Save Configs, Inspect and Topology → Edit Topology.
- **Attach shell** on DC3-L11 opens `sh` in the container; `Cli` gives the EOS prompt.
- **Stop**, then **Start** on DC3-L11 (test A, by hand): 3 minutes after the start all 4 underlay sessions and both EVPN
  sessions (168 prefixes each) were Established, and Et49/1-Et52/1 were connected with their descriptions.
- **Connect (SSH)** reaches L11, but logs in as `admin`, which DC3 does not have (see Known limits).
- Not tested yet: the scripted tests A-E (`LIFECYCLE_LAB=up bash lab/dc3_lifecycle_test.sh` runs them on a running lab),
  tests B, C and D by hand, and the TopoViewer drawing (Topology → Edit Topology asks for a permission on first use).

## Known limits

- **Laptop memory:** with WSL2 capped at 8 GB the smoke lab leaves about 250 MB free, and VS Code's WSL helper also needs
  memory. Raise the cap in `%USERPROFILE%\.wslconfig` (`memory=`) if the machine has room, or run fewer nodes.

- **Lab-only Linux nodes and FRR stand-ins** get their addresses from containerlab `exec` lines or `/frr-init.sh` at deploy
  time. Whether containerlab runs those again on `start` is test B. If it does not, re-apply them by hand (the `exec`
  lines of the node in the topology file) or redeploy the lab.
- **SSH to the switches:** SSH answers on Management1 (laptop), but the extension's SSH action uses the user
  `admin`, and DC3 has only `dc3-automation`. Log in as `dc3-automation`, or use **Attach shell** → `Cli`.
- **Positions:** TopoViewer places the nodes itself. Dragged positions are saved by the extension; its file format is not
  checked here.

## Sources
- containerlab GUI overview: https://containerlab.dev/manual/gui/
- VS Code extension: https://containerlab.dev/manual/gui/vsc-extension/, https://github.com/srl-labs/vscode-containerlab (package.json command list)
- TopoViewer node menu: https://hack.srexperts.net/tools/tools-clab-vscode-plugin/
- Node lifecycle: https://containerlab.dev/cmd/stop/, https://containerlab.dev/cmd/start/, https://containerlab.dev/cmd/restart/, https://containerlab.dev/rn/0.75/
- DC3: docs/QUICKSTART.md, docs/BUILD.md, lab/smoke.sh (laptop memory)
