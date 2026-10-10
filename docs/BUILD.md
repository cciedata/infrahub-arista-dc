# Build DC3 from this repository alone

From an empty machine with Docker to a running DC3 lab: an empty Infrahub 1.11.2 of this repo, the DC3 data, the
`dc3_site` generator, 43 rendered files equal to `tests/expected`, then the cEOS / FRR
lab and its tests.

Commands marked READ ONLY change nothing; RUN changes something.
`scripts/bootstrap.sh` runs the checks of step 3 and steps 4–10 in one go, with the same step numbers; each step below also
gives the manual commands. The standalone build does not register this repository in Infrahub: the generator and the
transforms run locally through `infrahubctl` (they read `.infrahub.yml`, `queries/`, `templates/` from the repo).

Verified from scratch: a fresh Linux host with Docker ran steps 2-10 from a `git archive` of this repository.
Results: unit tests passed, an empty Infrahub 1.11.2, the schema, 9 object files, 7 pools and the generator (38 devices,
idempotent rerun), and 43 renders equal to `tests/expected`.

## 1. Prerequisites

| Item | Version | Where it is pinned / from |
|---|---|---|
| Infrahub (server, task-manager, task-worker) | 1.11.2 | `docker-compose.yml` (`registry.opsmill.io/opsmill/infrahub:1.11.2`) |
| Neo4j / RabbitMQ / Redis / PostgreSQL | 2026.05.0-community / 4.2.1-management / 8.4.0 / 18-alpine | `docker-compose.yml` |
| infrahub-sdk (with `infrahubctl`, Jinja2, PyYAML; netutils is a core dependency) | 1.23.2 | `pyproject.toml` (`infrahub-sdk[ctl]==1.23.2`) github.com/opsmill/infrahub-sdk-python v1.23.2 pyproject.toml |
| Python | 3.11 – 3.13 | `pyproject.toml` `requires-python` |
| uv | any current | astral.sh/uv |
| Docker Engine + Compose v2, `docker` usable without sudo | no minimum tested | Ubuntu's `docker.io` package has no Compose v2: add `docker-compose-v2` |
| containerlab, usable without sudo (group `clab_admins`) | ≥ 0.75 (node start / stop, docs/LAB-GUI.md) | containerlab.dev |
| Ubuntu packages `git`, `curl`, `python3`, `python3-yaml`, `openssl`, `iproute2`, `openssh-client` | Ubuntu 24.04 | `apt-get install` (docs/QUICKSTART.md step 1) |
| Arista cEOS-lab | 4.36.2F, image tag `ceos:4.36.2F` | you download it yourself from arista.com (Software Downloads, cEOS-lab; free account; not redistributable) |
| FRR | 10.6.0, public image `quay.io/frrouting/frr:10.6.0` | stand-ins, hosts and lab-only nodes |

Memory:

| What | Memory |
|---|---|
| Infrahub stack (7 services, 8 containers) | about 3.7 GiB (measured on a 16 GB laptop) |
| one cEOS node | 1.35 GiB (measured on a 16 GB laptop) |
| smoke lab (step 12): 5 cEOS + 1 FRR host, Infrahub stopped by the script | about 6.8 GiB + the FRR host |
| full lab (step 13): 30 cEOS + 22 FRR containers | about 45 GB for the 30 cEOS plus the FRR nodes; a 64 GB Linux host; stop Infrahub first |

RUN: import the cEOS image (file name as downloaded; arista.com names it `cEOS64-lab-4.36.2F.tar.xz` or `cEOS-lab-4.36.2F.tar.xz`)
```
docker import cEOS64-lab-4.36.2F.tar.xz ceos:4.36.2F
docker pull quay.io/frrouting/frr:10.6.0
```
Success: `docker image inspect ceos:4.36.2F quay.io/frrouting/frr:10.6.0 >/dev/null && echo OK` prints `OK`.

## 2. Get the code and the uv environment

RUN: clone, create `.venv` with the pinned SDK, run the offline tests
```
git clone https://github.com/cciedata/infrahub-arista-dc.git
cd infrahub-arista-dc
uv sync
uv run pytest -q tests
```
Success: `uv run infrahubctl --help` answers, and pytest ends with `passed` and no `failed`. The same 135 tests run without
uv: `python3 -m unittest discover -s tests` ends with `Ran 135 tests` and `OK`.

## 3. Start an empty Infrahub

RUN: create `.env` with fresh secrets (every `CHANGE_ME` replaced), then start the stack (compose project `infrahub-arista-dc`)
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
```
- Another Infrahub already uses 8000 / 2004 / 6362 / 15692 on this machine: set `DC3_INFRAHUB_PORT=8001`,
  `DC3_NEO4J_METRICS_PORT=2005`, `DC3_NEO4J_BACKUP_PORT=6363`, `DC3_RABBITMQ_METRICS_PORT=15693` in `.env` before `up`.
- Do not export `INFRAHUB_ADDRESS` in the terminal that runs `docker compose` (see Known limits).

READ ONLY: success check
```
docker compose ps
curl -sf -o /dev/null http://localhost:8000/api/config && echo OK
```
Success: `infrahub-server`, `database`, `message-queue`, `cache`, `task-manager`, `task-manager-db` are `healthy`, two
`task-worker` replicas run, and `OK` is printed (use your port if you changed it).

Client terminal for steps 4–10 (run once per terminal; not the terminal you use for `docker compose`):
```
export INFRAHUB_ADDRESS=http://localhost:8000
export INFRAHUB_API_TOKEN="$(grep '^INFRAHUB_INITIAL_ADMIN_TOKEN=' .env | cut -d= -f2-)"
source .venv/bin/activate
```
If you also keep an `infrahubctl.toml` (copy of `infrahubctl.toml.example`), its `server_address` must be the same address.

**Step 3 checks and steps 4–10 in one command** (stops at the first FAIL; resume with `--from <step>`):
```
uv run bash scripts/bootstrap.sh
```
Success: every step prints `PASS  step N`, the last line starts with `DONE:`. Log: `lab/results/bootstrap-<UTC>/`.

## 4. Schema

RUN: guard, then load the schema
```
python3 scripts/dc3_guard.py
infrahubctl schema check schemas/base-schemas/*.yml schemas/locations.yml schemas/fabric.yml schemas-dc3/ztp.yml --branch main
infrahubctl schema load schemas/base-schemas/*.yml schemas/locations.yml schemas/fabric.yml schemas-dc3/ztp.yml --branch main --wait 120
python3 scripts/dc3_guard.py --schema
```
Success: the guard prints `OK ... DC3-only instance` before and `..., schema kinds present` after the load.
`schemas-dc3/ztp.yml` is DC3's own addition: the ZTP state fields `ztp_state` and `ztp_updated` on DcimDevice.
If `schema check` answers `500 Internal Server Error` right after a fresh start, wait a minute and run it again (server
log: `Schema 'DcimDeviceType' on branch main has incorrect hash`). `scripts/bootstrap.sh` retries it on its own (6 tries, 30 s apart).

## 5. Objects, in this order

RUN: one file at a time (references always point to objects loaded before; tests/test_dc3_standalone.py checks it)
```
for f in objects/dc3-00-groups.yml objects/dc3-01-bootstrap.yml objects/dc3-02-site.yml objects/dc3-03-ip-plan.yml \
         objects/dc3-04-hardware.yml objects/dc3-05-asns.yml objects/dc3-06-fabric.yml objects/dc3-07-segments.yml \
         objects/dc3-08-standins.yml; do
  infrahubctl object load "$f" --branch main || break
done
```
| File | What it loads |
|---|---|
| dc3-00-groups.yml | generator target group `dc3-sites`, EOS artifact groups |
| dc3-01-bootstrap.yml | the shared objects DC3 references: United States / Ashburn, providers, shared ASNs, shared prefixes, branch LANs |
| dc3-02-site.yml | site DC3 (parent Ashburn) |
| dc3-03-ip-plan.yml | DC3 prefixes and the Management1 gateway |
| dc3-04-hardware.yml | Arista manufacturer, platform, device types |
| dc3-05-asns.yml | DC3 ASNs |
| dc3-06-fabric.yml | VRFs (DC3 only) and their summaries |
| dc3-07-segments.yml | segments and their /24s |
| dc3-08-standins.yml | FRR platform / type, stand-in groups, PARTNER01, branches BR001 / BR002 |

Success: every load ends without an error and the loop does not `break`.

## 6. Pools

RUN: the 7 DC3 pools on main (allowed only with `--standalone` and only when the guard passes)
```
python3 scripts/dc3_create_pools.py --standalone --branch main
```
Success: `guard: DC3-only instance ...` and 7 lines `CoreIPAddressPool DC3-...` / `CoreIPPrefixPool DC3-...`.

## 7. Generator dry run

READ ONLY: plan printed, every referenced object read, nothing written
```
DC3_DRY_RUN=1 infrahubctl generator dc3_site --branch main site=DC3
```
Success: the plan tables and the last line `DRY RUN: nothing written`.

## 8. Write, then rerun with identical ids

RUN: write; id snapshot; rerun; id snapshot; compare
```
DC3_RECONCILE=report infrahubctl generator dc3_site --branch main site=DC3
python3 scripts/dc3_id_snapshot.py --branch main --out lab/results/ids-1.json
DC3_RECONCILE=report infrahubctl generator dc3_site --branch main site=DC3
python3 scripts/dc3_id_snapshot.py --branch main --out lab/results/ids-2.json
python3 scripts/dc3_id_snapshot.py --compare lab/results/ids-1.json lab/results/ids-2.json
```
Success: both runs end with `reconcile (report) done` and print no `WOULD DELETE` and no `MOVED` line; the snapshot shows
`devices 38`; the compare prints `IDENTICAL (...)`.

## 9. Host ports

RUN: the three test-host access ports on DC3-L11 (needs the devices of step 8)
```
infrahubctl object load objects/dc3-10-lab-host-ports.yml --branch main
```
Success: the load ends without an error.

## 10. Render the 43 files, check them, compare with tests/expected

RUN: render into `lab/configs/` (git-ignored)
```
mkdir -p lab/configs
for d in DC3-S1 DC3-S2; do infrahubctl render dc3_spine_config device=$d --branch main --out lab/configs/$d.cfg; done
for n in $(seq 1 20); do infrahubctl render dc3_leaf_config device=DC3-L$n --branch main --out lab/configs/DC3-L$n.cfg; done
for d in DC3-BL1 DC3-BL2 DC3-SL1 DC3-SL2; do infrahubctl render dc3_border_service_leaf_config device=$d --branch main --out lab/configs/$d.cfg; done
for d in DC3-EDGE1 DC3-EDGE2 DC3-DMZ1 DC3-DMZ2; do infrahubctl render dc3_l2_pair_config device=$d --branch main --out lab/configs/$d.cfg; done
infrahubctl render dc3_frr_internal_fw device=DC3-INTFW1 --branch main --out lab/configs/DC3-INTFW1.sh
infrahubctl render dc3_frr_internet_fw device=DC3-INETFW1 --branch main --out lab/configs/DC3-INETFW1.sh
infrahubctl render dc3_frr_sdwan_fw device=DC3-SDWANFW1 --branch main --out lab/configs/DC3-SDWANFW1.sh
infrahubctl render dc3_frr_branch device=DC3-SDWANFW1 --branch main --out lab/configs/DC3-SDWANFW1.branch.sh
infrahubctl render dc3_frr_vpn_fw device=DC3-VPNFW1 --branch main --out lab/configs/DC3-VPNFW1.sh
infrahubctl render dc3_frr_partner device=DC3-VPNFW1 --branch main --out lab/configs/DC3-VPNFW1.partner.sh
for n in 1 2; do infrahubctl render dc3_frr_isp_router device=DC3-ISP$n --branch main --out lab/configs/DC3-ISP$n.sh; done
for n in 1 2; do infrahubctl render dc3_frr_carrier device=DC3-ISP$n --branch main --out lab/configs/DC3-ISP$n.carrier.sh; done
for n in 1 2; do infrahubctl render dc3_frr_cloud_router device=DC3-CLOUD$n --branch main --out lab/configs/DC3-CLOUD$n.sh; done
infrahubctl render dc3_frr_cloud_provider site=DC3 --branch main --out lab/configs/DC3.cloud-provider.sh
```
READ ONLY: count, static render check, comparison
```
ls lab/configs/*.cfg lab/configs/*.sh | wc -l
python3 lab/dc3_render_check.py --configs lab/configs
python3 scripts/dc3_render_diff.py lab/configs
```
Success:
- `43`;
- the render check exits 0 (no blocker or major finding; minor and info rows may remain);
- `PASS: 43 renders = tests/expected (43) + the recorded delta (0 files)`.

`tests/expected` is the reference render; `tests/expected-delta.diff` records changed lines since it (empty now), and the
comparison passes only if nothing else differs. `python3 scripts/dc3_render_diff.py lab/configs --show` prints the full diff.
`lab/dc3_secrets.py fill` writes the password hash into `lab/configs`, so after a lab deploy re-render
(`uv run bash scripts/bootstrap.sh --from 10`) before comparing with `tests/expected`.

## 11. Topology

RUN: write the containerlab topologies from the plan, with the Management1 addresses read from `lab/configs`
```
python3 scripts/dc3_lab_topology.py
python3 scripts/dc3_lab_topology.py --subset smoke
```
Success: `lab/dc3.clab.yml: 52 nodes, 151 links, mgmt-ipv4 on 52 nodes` and
`lab/dc3-smoke.clab.yml: 6 nodes, 16 links, mgmt-ipv4 on 6 nodes`.
Both files are tracked; with `lab/configs` present the regenerated files are identical to the ones in Git.

## 12. Smoke lab (6 nodes)

RUN: the `dc3-automation` password (once; asks twice, writes `~/.dc3/secrets.env`, mode 600; smoke.sh fills it into
`lab/configs` itself)
```
bash scripts/dc3_make_secret.sh
```
READ ONLY: what the post-check will judge (no lab needed)
```
python3 lab/dc3_postcheck.py --topology lab/dc3-smoke.clab.yml --list | grep -vc SKIP
```
Success: `87`.
RUN: stop this repo's Infrahub, deploy, check, capture, destroy, start Infrahub again (always, trap)
```
bash lab/smoke.sh
```
Success: `lab/results/smoke-<UTC>/summary.txt` shows `deploy: ok`, every node with `Cli answers`, `postcheck: PASS (no FAIL / ERROR)`,
`destroy: ok`, `infrahub start: ok`. Exit codes: 3 pre-flight failed (nothing stopped or deployed), 4 Infrahub stop failed,
5 topology write failed, 6 deploy failed, 7 the post-check found FAIL or ERROR rows (see `postcheck.txt` in the
results folder).

## 13. Full lab (52 nodes)

RUN: the password, if step 12 did not set it (`~/.dc3/secrets.env`)
```
bash scripts/dc3_make_secret.sh
```
RUN: stop Infrahub (memory), fill the secrets into `lab/configs` (needed before any manual `containerlab deploy`), deploy,
wait, post-check
```
docker compose stop
python3 lab/dc3_secrets.py fill lab/configs
containerlab deploy -t lab/dc3.clab.yml --reconfigure
python3 lab/dc3_postcheck.py --wait 300
```
Success: the post-check ends with no `FAIL` / `ERROR` row (exit 0); results in `lab/results/postcheck-<UTC>.json`.

## 14. Failure tests 1–16

READ ONLY: the plan of every test
```
python3 lab/dc3_failtests.py --list
python3 lab/dc3_failtests.py --run 1-16 --dry-run
```
RUN: on the running full lab (tests break and restore links; test 16 adds and removes a probe /32)
```
python3 lab/dc3_failtests.py --run 1-16
```
Success: every test `PASS` in the summary; results in `lab/results/failtests-<UTC>/`. Smoke lab only: `--topology
lab/dc3-smoke.clab.yml --run 3,5,15 --ping-target 10.24.10.1`.

## 15. Teardown

RUN: remove the lab that is running (choose the line of that lab)
```
containerlab destroy -t lab/dc3.clab.yml --cleanup
containerlab destroy -t lab/dc3-smoke.clab.yml --cleanup
```
Then either stop Infrahub and keep its data:
```
docker compose down
```
or remove Infrahub with its data. `down -v` deletes all Infrahub data; the next `up` is empty (fresh build: back to step 3):
```
docker compose down -v
```
Success: `docker ps --filter name=clab-dc3- -q` and `docker compose ps -q` print nothing.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `docker compose up`: `set INFRAHUB_... in .env` | a credential is missing in `.env` (step 3) |
| bootstrap step 0: `still CHANGE_ME` | run the `.env` generator of step 3 |
| bootstrap step 3: no answer after 600 s | `docker compose ps`, `docker compose logs infrahub-server`; first start pulls images and can take long (`HEALTH_TIMEOUT=1200 uv run bash scripts/bootstrap.sh`) |
| bootstrap step 3: other Infrahub version | another Infrahub answers on that port: use the alternate ports (step 3) and `DC3_INFRAHUB_ADDRESS` in `.env` |
| guard `REFUSED ... sites other than DC3` | this Infrahub already holds other sites. Nothing was written. Point `DC3_INFRAHUB_ADDRESS` / `INFRAHUB_ADDRESS` at this repo's instance |
| step 0: `infrahubctl.toml server_address ... differs` | fix or remove `infrahubctl.toml` (infrahubctl reads it; which source wins over the environment is not documented) |
| `object load` error naming a missing object | an earlier file was skipped; load in the step 5 order (or `--from 4`) |
| generator: `VRFs [...] do not list the site` | `objects/dc3-06-fabric.yml` not loaded |
| step 8: `the rerun changed ids` | a real idempotency problem: keep `lab/results/bootstrap-*/ids-*.json` and the logs, do not continue |
| step 10: render diff `FAIL` | a template, object file or pool allocation differs; `--show` prints the diff; do not edit `tests/expected*` to make it pass |
| smoke / full lab: `containerlab is not usable without sudo` | add the user to `clab_admins` once (smoke.sh prints the commands), log in again |
| cEOS node never answers `Cli` | memory (table in step 1), image tag `ceos:4.36.2F` |

## Known limits

- `infrahubctl schema load --wait` exists (docs.infrahub.app/infrahubctl/infrahubctl-schema); its exact behaviour on 1.11.2 with an empty instance is unverified.
- Precedence between `infrahubctl.toml` and `INFRAHUB_ADDRESS` / `INFRAHUB_API_TOKEN` is not documented; bootstrap refuses when they differ.
- GraphQL `InfrahubInfo { version }` (version check of bootstrap step 3) and the `device__name__values` filter on `DcimInterface` (id snapshot) on 1.11.2 are unverified.
- `client.schema.all()` returns generics as well as nodes (the guard's kind check).
- The pool allocation order of a fresh instance gives the addresses of `tests/expected`: shown in the fake store and in the rebuild from scratch (top of this page).
- Docker Compose passes `.env` values into a container for keys listed without a value (`INFRAHUB_ADDRESS:` in the server's config block); the client address therefore has its own name (`DC3_INFRAHUB_ADDRESS`).
- The cEOS download file name on arista.com and the containerlab minimum version for non-root use are unverified.
