# infrahub-arista-dc

An Arista EOS EVPN/VXLAN datacenter, DC3: 30 switches (2 spines and 28 leaves of 7 roles: server leaves as an MLAG pair,
EVPN ESI pairs and single leaves, a border-leaf pair, a service-leaf pair, an edge pair and a DMZ pair) with an eBGP underlay, EVPN / VXLAN overlay and tenant VRFs. It is
modelled in Infrahub, built by the Infrahub generator `dc3_site`, and rendered by Jinja2 templates into 30 EOS
startup-configs and 13 FRR stand-in scripts, then tested in a containerlab lab with cEOS-lab and FRR.

With this repo and Docker you start an empty Infrahub 1.11.2, load the schema and the data, run the generator and render
the same files as `tests/expected`. **Start here: [docs/QUICKSTART.md](docs/QUICKSTART.md)** (from an empty machine
to a running lab, step by step); every build step in detail: [docs/BUILD.md](docs/BUILD.md).

## Repository map

| Path | What it holds |
|---|---|
| `docs/QUICKSTART.md` | from an empty Ubuntu (Linux or WSL2) to a running, clickable lab: tools, images, Infrahub, renders, smoke or full lab, ZTP |
| `docs/DESIGN.md` | the design as built (topology, numbering, underlay, MLAG, overlay, tenants, pipeline, verification) |
| `docs/LAB-GUI.md` | clickable lab topology (containerlab VS Code extension): log in to any device, stop, start or restart it; `lab/dc3_lifecycle_test.sh` is written for it, but its scripted tests are not run yet |
| `docs/diagrams/DC3-diagrams.pdf` | 17-page A3 diagram set drawn from the renders: physical, spine-leaf links, underlay, overlay, EVPN route types, packet walks, MLAG, firewall and outside attachments, edge, VRF paths, policy, segments, management and ZTP, lab and tests (`scripts/dc3_diagrams.py`) |
| `docs/VXLAN-EVPN-CAPTURE.md` | how the control plane and data plane work, with captured packets |
| `ztp/` | zero-touch provisioning of the 30 switches from Infrahub (`ztp/README.md`) |
| `docs/BUILD.md` | the build, step by step: prerequisites, Infrahub, schema, objects, pools, generator, renders, lab, failure tests, teardown |
| `docker-compose.yml`, `.env.example` | the Infrahub 1.11.2 stack (compose project `infrahub-arista-dc`, host ports from `.env`, no built-in secrets) |
| `pyproject.toml`, `infrahubctl.toml.example` | the uv environment (`infrahub-sdk[ctl]==1.23.2`), infrahubctl settings |
| `schemas/`, `schemas-dc3/` | the Infrahub schema; `schemas-dc3/ztp.yml` adds the ZTP state fields |
| `objects/` | the DC3 data, loaded in file-name order; `dc3-01-bootstrap.yml` holds the shared objects DC3 references |
| `generators/` | generator `dc3_site` (`dc3_site.py`) and its pure-Python plan (`dc3_plan.py`) |
| `queries/`, `templates/`, `.infrahub.yml` | GraphQL queries, Jinja2 templates, transforms, artifact and generator definitions |
| `scripts/` | `bootstrap.sh` (build steps 4–10), `dc3_guard.py` (DC3-only instance guard), `dc3_create_pools.py`, `dc3_id_snapshot.py`, `dc3_render_diff.py`, `dc3_lab_topology.py` |
| `lab/` | containerlab topologies, cEOS interface mapping, render check, post-check, failure tests, smoke test |
| `tests/` | offline tests (no Infrahub, no Docker); `tests/expected/` = the 43 reference renders, `tests/expected-delta.diff` = the recorded changes since them (empty: `tests/expected` matches the current templates) |
| `plans/dc3-values.md` | all DC3 values in one page |


## License

MIT, see LICENSE.
