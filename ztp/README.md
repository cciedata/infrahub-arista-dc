# ZTP: EOS zero-touch provisioning from Infrahub

A blank DC3 switch provisions itself. Kea DHCP hands out an address from the DC3 OOB /24 and, in option 67, the URL of
`bootstrap.py`. EOS ZTP downloads and runs it. The script reads the switch serial and asks the ZTP server for its config. The
server finds the device with that `serial_number` in Infrahub, renders its startup-config live with the DC3 transforms, fills
the secrets, and sends it. The script writes it as the startup-config and calls back, then EOS reboots into it. The server
checks the switch over eAPI and records the result in `DcimDevice.ztp_state` (branch main in this repo's own Infrahub).

Scope: all 30 DC3 switches.
Stand-ins are not ZTP devices.

| State | Written by | When |
|---|---|---|
| `pending` | `set_serial.py` | the serial is recorded, before racking |
| `served` | ZTP server | the config was sent to that serial |
| `complete` | ZTP server | after the reboot, eAPI on the device's Management1 address answers `show hostname` = device name |
| `failed` | ZTP server | the script reported an error, or no verified answer within 30 min of the callback |

Every write also sets `ztp_updated`. The server writes as `dc3-ztp-callback` (Script account, API token, no expiry):
`Dcim:Device` view on all branches and update on every branch including main, plus view on every object kind for the
live render. On an Infrahub that also holds other sites, update is allowed on branches other than main only
(see "Dependencies" item 1).

## Files
| File | What |
|---|---|
| `bootstrap.py` | EOS ZTP script: serial, config by serial into the startup-config, callback; errors to `/callback/<serial>/failed/<reason>` |
| `ztp_server.py` | HTTP server: `/bootstrap`, `/config/<serial>`, `/callback/<serial>[/failed/<reason>]`; live render; state writes; eAPI check |
| `setup_account.py` | permissions, role/group "DC3 ZTP Callback", account `dc3-ztp-callback`, token; permission test |
| `set_serial.py` | records a serial on the device's branch, `ztp_state` pending |
| `kea-dhcp4.json` | DHCP: pool 10.250.32.200-.239 on `br-dc3-ztp`, option 67 `http://10.250.32.1:8080/bootstrap` |
| `run-ztp.sh` | laptop test: prep / start / status / stop with one cEOS-lab container |
| `run-ztp-all.sh` | all 30 switches one after another, result table per switch |
| `run-ztp-par.sh` | all 30 switches in parallel; needs memory for 30 cEOS at once |
| `../lab/dc3_secrets.py`, `../scripts/dc3_make_secret.sh` | delivery-time secret fill, `dc3-automation` password and hash |

## Secrets (never in Git)
Rendered configs carry `username dc3-automation privilege 15 role network-admin secret sha512 __DC3_AUTOMATION_HASH__`. The ZTP
server and the lab deploy fill the placeholder from `~/.dc3/secrets.env`. A missing hash
drops the line with a warning, and delivery goes on.

From the repo folder, with Infrahub up and no lab running (docs/QUICKSTART.md step 8):
```
bash scripts/dc3_make_secret.sh                       # DC3_AUTOMATION_PASSWORD, DC3_AUTOMATION_HASH
export INFRAHUB_PASSWORD="$(grep '^INFRAHUB_INITIAL_ADMIN_PASSWORD=' .env | cut -d= -f2-)"
uv run python ztp/setup_account.py                    # DC3_ZTP_CALLBACK_PASSWORD, DC3_ZTP_CALLBACK_TOKEN (+ permission test)
uv run python ztp/set_serial.py DC3-L11 DC3L11LAB01   # serial_number + ztp_state pending (branch main)
PATH="$PWD/.venv/bin:$PATH" bash ztp/run-ztp.sh prep
PATH="$PWD/.venv/bin:$PATH" bash ztp/run-ztp.sh start DC3-L11
PATH="$PWD/.venv/bin:$PATH" bash ztp/run-ztp.sh status
PATH="$PWD/.venv/bin:$PATH" bash ztp/run-ztp.sh stop
```
`prep` prints `infrahubctl: <path>`, or `infrahubctl: MISSING` when infrahubctl is not in PATH (the live render needs it).

## Dependencies and open
1. Branch: in this repo's own Infrahub (docs/BUILD.md) DC3 lives on `main`, the default of every ZTP script.
   Option: in an Infrahub instance that also holds other sites, DC3 lives on a branch: set `DC3_ZTP_BRANCH=<branch>`
   (for example `dc3`) for all of them (setup_account.py then allows updates on branches other than main only). `ztp_state` / `ztp_updated` come from
   `schemas-dc3/ztp.yml`; without them the server logs `state ... written: false` and goes on.
2. cEOS-lab ZTP, tested: with an empty host flash folder cEOS runs ZTP, DHCP works next to the Docker
   address, the ceos-config serial is the one `show version` reports, and DC3-L11 provisioned itself to `complete` in 66 s.
3. `FastCli -p 15 -c "show version | json"` field `serialNumber` inside the ZTP environment (tested).
4. The ZTP test and the smoke/full lab cannot run at the same time: both use 10.250.32.0/24.
5. The eAPI check does not verify the switch certificate: a switch fresh from ZTP has a self-signed certificate.

Sources: EOS ZTP runs only while flash has no startup-config, and the switch reboots after it
https://www.arista.com/en/um-eos/eos-initial-switch-access ; option 67 names the file, and an executable file is run
https://github.com/arista-eosplus/ztpserver/blob/v1.3.2/docs/overview.rst.
