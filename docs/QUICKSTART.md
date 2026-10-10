# Quickstart: from an empty machine to a running DC3 lab

This page takes you from a fresh Ubuntu machine to the DC3 fabric running in containerlab. You get your own Infrahub with the
DC3 data, the 43 rendered configs, a running lab you can log in to, and optionally zero-touch provisioning. Follow the steps
in order. Run each command block in one go, except where a step says to choose one of its lines.

Each step names the command that proves it worked. If one fails, see "When something fails" at the end.

## The host

| | Needed |
|---|---|
| OS | x86-64 Ubuntu 24.04: a Linux machine or VM, or Windows 10/11 with WSL2 |
| CPU | 4 cores for the smoke lab, 16+ for the full lab |
| Memory | build only: 4 GB free. Smoke lab (5 switches + 1 host): about 7 GB with Infrahub stopped. Full lab (52 nodes): about 45 GB for the 30 cEOS plus the FRR nodes; a 64 GB Linux host, not yet run on a laptop or desktop |
| Disk | 30 GB free |
| Account | a free arista.com account (to download cEOS-lab) |

Which lab to run is only a question of memory. Every step up to step 5 is the same for both.

**Windows only, before step 1.** In PowerShell, install a new Ubuntu 24.04 and give WSL enough memory:
```
wsl --install -d Ubuntu-24.04
```
Then create or edit `C:\Users\<you>\.wslconfig` (for the smoke lab on a 16 GB laptop, `memory=10GB`; Windows keeps the rest),
and run `wsl --shutdown` once so it takes effect:
```
[wsl2]
memory=10GB
swap=8GB
```
Open "Ubuntu-24.04" from the Start menu and continue there. If Docker Desktop is installed, turn off its WSL integration for
this distro (Docker Desktop → Settings → Resources → WSL integration), so the distro uses the Docker Engine of step 1.
WSL must run systemd: `ps -p 1 -o comm=` prints `systemd`. If not, add `[boot]` and `systemd=true` to `/etc/wsl.conf` and
run `wsl --shutdown` in PowerShell.

## 1. Install the tools

```
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git curl python3 python3-yaml openssl iproute2 openssh-client
sudo usermod -aG docker "$USER"
bash -c "$(curl -sL https://get.containerlab.dev)"
sudo groupadd -f clab_admins && sudo usermod -aG clab_admins "$USER"
curl -LsSf https://astral.sh/uv/install.sh | sh
```
Log out and in again (WSL: close the window, `wsl --shutdown` in PowerShell, open Ubuntu again), so the two groups apply.

Check:
```
docker run --rm hello-world | grep -m1 Hello
docker compose version
containerlab version | grep -m1 version
uv --version
python3 --version
```
Expected: `Hello from Docker!`, Compose v2, containerlab 0.75 or later, uv, Python 3.11 to 3.13 (Ubuntu 24.04 has 3.12).
All without `sudo`.

Optional tools: `tcpdump` and `tshark` (lab/dc3_capture.sh, docs/VXLAN-EVPN-CAPTURE.md), `graphviz` and Playwright
Chromium (only to rebuild docs/diagrams/DC3-diagrams.pdf), VS Code (step 7).

## 2. Get the images

cEOS-lab cannot be redistributed, so you download it yourself:
1. Log in at https://www.arista.com/en/support/software-download.
2. Open **EOS → cEOS-lab → 4.36.2F** and download `cEOS64-lab-4.36.2F.tar.xz`.
3. Copy it into your Ubuntu home folder. On Windows with WSL2, from your Downloads folder:
   `cp /mnt/c/Users/<windows user>/Downloads/cEOS64-lab-4.36.2F.tar.xz ~/`

```
docker import ~/cEOS64-lab-4.36.2F.tar.xz ceos:4.36.2F
docker pull quay.io/frrouting/frr:10.6.0
docker image inspect ceos:4.36.2F quay.io/frrouting/frr:10.6.0 >/dev/null && echo images OK
```

## 3. Get the code

```
cd ~
git clone https://github.com/cciedata/infrahub-arista-dc.git
cd infrahub-arista-dc
uv sync
uv run python -m pytest -q tests
```
Expected: pytest ends with `passed` and no `failed`.

Every later step runs from this folder.

## 4. Build Infrahub and DC3

This starts an empty Infrahub 1.11.2 from the repo's own `docker-compose.yml`, then loads the schema, the DC3 data and the
pools, runs the generator twice (the second run must change nothing), and renders the 43 configs into `lab/configs/`.

```
cp .env.example .env
python3 - <<'EOF'
import pathlib, secrets, uuid
p = pathlib.Path(".env"); out = []
for line in p.read_text().splitlines():
    k, sep, v = line.partition("=")
    if sep and v == "CHANGE_ME":
        v = str(uuid.uuid4()) if k.endswith(("_TOKEN", "_SECRET_KEY")) else secrets.token_urlsafe(24)
    out.append(k + sep + v)
p.write_text("\n".join(out) + "\n")
EOF
chmod 600 .env
docker compose up -d
uv run bash scripts/bootstrap.sh
```
Expected: PASS lines for steps 0 and 3 to 10, then a line starting with `DONE:`. The first `docker compose up` downloads
the Infrahub images (a few minutes). Then `ls lab/configs | wc -l` prints 43.

### Open Infrahub in the browser

Infrahub runs at http://localhost:8000 (on Windows with WSL2, open it in the Windows browser; WSL forwards the port).
Log in as `admin` with the password the `.env` step generated:
```
grep '^INFRAHUB_INITIAL_ADMIN_PASSWORD=' .env | cut -d= -f2-
```
It is not `infrahub`, the default of a plain Infrahub install; if your browser fills that in, replace it.

Worth a look after the build: **Devices** (30 switches and 8 stand-ins of site DC3), and on a device page its
**Interfaces** and **IP addresses**. The rendered configs are files in `lab/configs/`, not stored in Infrahub. ZTP
(step 8) writes `ztp_state` on the device page.

Each step of this command, with what it checks and how to do it by hand: docs/BUILD.md.

## 5. The switch password

The configs carry a local user `dc3-automation`. Its password hash is filled in when a switch gets its config, and never
stored in Git:
```
bash scripts/dc3_make_secret.sh
```
It asks for a password twice and writes `~/.dc3/secrets.env` (mode 600). Keep that password: it is the SSH and eAPI login
of every switch.

## 6a. Smoke lab (5 switches, about 7 GB)

The automatic test: stop Infrahub, deploy, check every BGP, BFD, MLAG and EVPN item, capture, destroy, start Infrahub again.
```
python3 scripts/dc3_lab_topology.py --subset smoke
bash lab/smoke.sh
```
Expected: `lab/results/smoke-<time>/summary.txt` shows `deploy: ok`, `postcheck: PASS`, `destroy: ok`, `infrahub start: ok`, and exit code 0 (exit 7: the post-check found FAIL or ERROR rows; see `postcheck.txt`
in the results folder).

To keep the lab running and work on it:
```
docker compose stop
python3 lab/dc3_secrets.py fill lab/configs
containerlab deploy -t lab/dc3-smoke.clab.yml --reconfigure
```
Wait about 3 minutes for the switches to boot, then:
```
docker exec -it clab-dc3-DC3-L11 Cli -c "show ip bgp summary"
```
Expected: 4 neighbors in state `Estab`.

## 6b. Full lab (30 switches, about 45 GB)

```
containerlab destroy -t lab/dc3-smoke.clab.yml --cleanup 2>/dev/null || true
python3 scripts/dc3_lab_topology.py
docker compose stop
python3 lab/dc3_secrets.py fill lab/configs
containerlab deploy -t lab/dc3.clab.yml --reconfigure
python3 lab/dc3_postcheck.py --wait 300
```
Expected: the post-check ends with no `FAIL` or `ERROR` row. Then the 16 failure tests (each breaks something, measures the
outage and restores it):
```
python3 lab/dc3_failtests.py --list
python3 lab/dc3_failtests.py --run 1-16
```
Results go to `lab/results/`.

## 7. Click on the lab

Open the running lab in VS Code with the Containerlab extension: a topology view, a CLI on any device, and start, stop or
restart for each device. Setup and use: docs/LAB-GUI.md. In short:
1. Install VS Code; add the extensions **WSL** (Windows only) and **Containerlab** (publisher srl-labs).
2. In the Ubuntu terminal: `cd ~/infrahub-arista-dc && code .`
3. Containerlab icon in the left bar → **Running Labs → dc3** → right-click a switch → **Attach shell** → `Cli`.

SSH works too: `ssh dc3-automation@clab-dc3-DC3-L11` with the password of step 5.

## 8. Zero-touch provisioning (optional)

A blank switch gets its config from Infrahub by itself: DHCP, a bootstrap script, a live render, a reboot, an eAPI check, and
its state recorded in Infrahub. This needs the lab down (it uses the same management network) and Infrahub up.

Destroy the lab that is running (choose the line of that lab):
```
containerlab destroy -t lab/dc3-smoke.clab.yml --cleanup
containerlab destroy -t lab/dc3.clab.yml --cleanup
```
Then free the management network (ZTP needs 10.250.32.0/24) and start Infrahub:
```
docker network rm dc3-mgmt 2>/dev/null || true
docker compose start
until curl -sf -o /dev/null http://localhost:8000/api/config; do sleep 5; done; echo Infrahub up
export INFRAHUB_PASSWORD="$(grep '^INFRAHUB_INITIAL_ADMIN_PASSWORD=' .env | cut -d= -f2-)"
uv run python ztp/setup_account.py
uv run python ztp/set_serial.py DC3-L11 DC3L11LAB01
PATH="$PWD/.venv/bin:$PATH" bash ztp/run-ztp.sh prep
PATH="$PWD/.venv/bin:$PATH" bash ztp/run-ztp.sh start DC3-L11
```
Then watch it with `bash ztp/run-ztp.sh status`. After about 1-3 minutes the device shows `ztp_state complete` in Infrahub
(Device DC3-L11). Clean up with `bash ztp/run-ztp.sh stop`. Details: ztp/README.md.

## 9. Clean up

Destroy the lab that is running (choose the line of that lab):
```
containerlab destroy -t lab/dc3-smoke.clab.yml --cleanup
containerlab destroy -t lab/dc3.clab.yml --cleanup
```
Then either keep Infrahub and its data:
```
docker compose start
```
or remove Infrahub. `down -v` deletes all Infrahub data; the next `docker compose up -d` starts empty:
```
docker compose down -v
```
ZTP runs leave root-owned folders in `~/.dc3-ztp/flash`: remove them with `sudo rm -rf ~/.dc3-ztp/flash`.

## When something fails

| Symptom | What to do |
|---|---|
| `permission denied` on docker or containerlab | the groups of step 1 apply only after a new login |
| `docker compose`: unknown command | `sudo apt-get install -y docker-compose-v2` |
| bootstrap step 3: no answer after 600 s | the first start pulls images: `HEALTH_TIMEOUT=1200 uv run bash scripts/bootstrap.sh` |
| bootstrap step 4: HTTP 500 on the schema check | a brand-new Infrahub can answer 500 for a minute; the script retries; if it still fails: `uv run bash scripts/bootstrap.sh --from 4` |
| port 8000 already used | another Infrahub runs here: set the alternate ports in `.env` (docs/BUILD.md step 3), then `export DC3_INFRAHUB_ADDRESS=http://localhost:8001` and `export INFRAHUB_ADDRESS=http://localhost:8001` in every terminal |
| deploy: `post-deploy: channel timeout sending input to device` | usually only slow; check with `docker exec clab-dc3-DC3-L11 Cli -c "show hostname"` after 3 minutes; if a switch does not answer, `containerlab deploy ... --reconfigure` again |
| `Cannot allocate memory`, switches restart | not enough memory for this lab: smoke lab with Infrahub stopped, or a bigger host |
| the extension's SSH asks for `admin` | DC3 has no `admin`: use `ssh dc3-automation@...` or **Attach shell** |
| anything else | docs/BUILD.md "Troubleshooting" |
