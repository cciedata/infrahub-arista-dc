#!/usr/bin/env python3
"""DC3 ZTP server.

Serves the EOS bootstrap script, serves each switch's startup-config rendered LIVE from Infrahub for the device with that
serial number, and keeps DcimDevice.ztp_state current on the branch the device lives on through the
dc3-ztp-callback account.

  ztp_server.py <listen ip> <port>

Routes
  GET /bootstrap                       the EOS ZTP script (ztp/bootstrap.py with __SRV__ filled)
  GET /config/<serial>                 render the device's config, fill secrets, state served
  GET /callback/<serial>               start the check after the ZTP reboot (state stays served until it ends)
  GET /callback/<serial>/failed/<why>  the script reported an error: state failed

States: pending is set before racking (ztp/set_serial.py). This server writes
  served    config sent to that serial
  complete  after the reboot, eAPI over HTTPS on the device's Management1 address answers show hostname = device name
  failed    the script reported an error, or no verified answer within 30 min of the callback
Every write also sets ztp_updated (UTC).

Config source: `infrahubctl render <transform> device=<name> --branch <branch>`, the same transforms as
the render of the lab configs; the transform follows the device name (spine, server leaf, border/service leaf, edge/DMZ).

Secrets (~/.dc3/secrets.env; scripts/dc3_make_secret.sh, ztp/setup_account.py): DC3_ZTP_CALLBACK_TOKEN (Infrahub API token,
also used for the reads and the render), DC3_AUTOMATION_HASH (filled into the config, lab/dc3_secrets.py),
DC3_AUTOMATION_PASSWORD (the eAPI login of the check). Without the hash the username line is dropped (warning event) and the
check cannot log in, so the state ends failed after 30 min.
env: DC3_INFRAHUB_ADDRESS (default http://localhost:8000), DC3_ZTP_BRANCH (default main: standalone instance; a branch such as dc3 on an
instance that also holds other sites), INFRAHUBCTL (default infrahubctl)
"""
from __future__ import annotations

import base64
import datetime
import http.server
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lab"))
import dc3_secrets  # noqa: E402

LOG = os.path.join(HERE, "ztp-events.jsonl")
INFRAHUB = os.environ.get("DC3_INFRAHUB_ADDRESS", "http://localhost:8000").rstrip("/")
BRANCH = os.environ.get("DC3_ZTP_BRANCH", "main")
INFRAHUBCTL = os.environ.get("INFRAHUBCTL", "infrahubctl")
VERIFY_MAX_S = 1800        # 30 min after the callback, then failed
VERIFY_EVERY_S = 30
USER = "dc3-automation"

TRANSFORMS = [             # device name -> transform; stand-ins are not ZTP devices
    (re.compile(r"^DC3-S\d+$"), "dc3_spine_config"),
    (re.compile(r"^DC3-L\d+$"), "dc3_leaf_config"),
    (re.compile(r"^DC3-(BL|SL)\d+$"), "dc3_border_service_leaf_config"),
    (re.compile(r"^DC3-(EDGE|DMZ)\d+$"), "dc3_l2_pair_config"),
]


def event(kind, **kw):
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": kind, **kw}
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)


def transform_for(name: str) -> str | None:
    for rx, t in TRANSFORMS:
        if rx.match(name):
            return t
    return None


def bootstrap_script(listen: str, port: int) -> bytes:
    return open(os.path.join(HERE, "bootstrap.py")).read().replace("__SRV__", f"{listen}:{port}").encode()


def token() -> str | None:
    return dc3_secrets.load().get("DC3_ZTP_CALLBACK_TOKEN")


def gql(query: str, branch: str = BRANCH) -> dict:
    req = urllib.request.Request(f"{INFRAHUB}/graphql/{branch}", data=json.dumps({"query": query}).encode(),
                                 headers={"Content-Type": "application/json", "X-INFRAHUB-KEY": token() or ""})
    with urllib.request.urlopen(req, timeout=60) as r:
        doc = json.loads(r.read())
    if doc.get("errors"):
        raise RuntimeError(str(doc["errors"])[:300])
    return doc["data"]


def device_by_serial(sn: str, q=gql) -> list[tuple[str, str]]:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", sn):
        return []
    d = q('{ DcimDevice(serial_number__value: "%s") { edges { node { id name { value } } } } }' % sn)
    return [(e["node"]["id"], e["node"]["name"]["value"]) for e in d["DcimDevice"]["edges"]]


def render(name: str) -> str:
    """: live render with the same transform the render loop uses."""
    t = transform_for(name)
    if not t:
        raise RuntimeError(f"{name} is not a ZTP device (no EOS transform)")
    env = dict(os.environ, INFRAHUB_ADDRESS=INFRAHUB, INFRAHUB_API_TOKEN=token() or "", COLUMNS="100000")
    p = subprocess.run([INFRAHUBCTL, "render", t, f"device={name}", "--branch", BRANCH], capture_output=True, text=True,
                       env=env, timeout=300, cwd=os.path.join(HERE, ".."))
    if p.returncode != 0 or not p.stdout.startswith("hostname "):
        raise RuntimeError(f"render {t} {name} rc {p.returncode}: {(p.stderr or p.stdout)[:300]}")
    return p.stdout


def set_state(dev_id: str, name: str, state: str, q=gql, **kw):
    """: ztp_state + ztp_updated on the device's branch, as dc3-ztp-callback."""
    if not token():
        event("state-not-written", device=name, state=state, error="DC3_ZTP_CALLBACK_TOKEN not set"); return False
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    m = ('mutation { DcimDeviceUpdate(data: {id: "%s", ztp_state: {value: "%s"}, ztp_updated: {value: "%s"}}) { ok } }'
         % (dev_id, state, now))
    try:
        ok = bool(q(m)["DcimDeviceUpdate"]["ok"])
        event("state", device=name, state=state, branch=BRANCH, written=ok, **kw)
        return ok
    except Exception as e:
        event("state", device=name, state=state, branch=BRANCH, written=False, error=repr(e)[:300], **kw)
        return False


def mgmt_address(cfg: str) -> str | None:
    """Management1 address of the rendered config (VRF MGMT)."""
    m = re.search(r"^interface Management1\n(?:   .*\n)*?   ip address (\d+\.\d+\.\d+\.\d+)/\d+", cfg, re.M)
    return m.group(1) if m else None


def eapi_hostname(ip: str, password: str) -> str:
    """: eAPI over HTTPS, show hostname. A switch fresh from ZTP has a self-signed certificate, so the certificate is not
    checked (lab and first boot only; the address is the one Infrahub assigned)."""
    body = json.dumps({"jsonrpc": "2.0", "method": "runCmds", "id": "dc3-ztp",
                       "params": {"version": 1, "cmds": ["show hostname"], "format": "json"}}).encode()
    auth = base64.b64encode(f"{USER}:{password}".encode()).decode()
    req = urllib.request.Request(f"https://{ip}/command-api", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"})
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        doc = json.loads(r.read())
    if "error" in doc:
        raise RuntimeError(str(doc["error"])[:200])
    return doc["result"][0]["hostname"]


def verify(dev_id, name, sn, ip, ask=eapi_hostname, every=VERIFY_EVERY_S, max_s=VERIFY_MAX_S, q=gql):
    pw = dc3_secrets.load().get("DC3_AUTOMATION_PASSWORD") or ""
    event("verify-start", serial=sn, device=name, ip=ip, user=USER, max_s=max_s)
    start, last = time.time(), ""
    while time.time() - start < max_s:
        try:
            host = ask(ip, pw)
            if host.split(".")[0] == name:
                return set_state(dev_id, name, "complete", q=q, serial=sn, after_s=round(time.time() - start))
            last = f"hostname {host!r}"
        except Exception as e:
            last = type(e).__name__
        time.sleep(every)
    return set_state(dev_id, name, "failed", q=q, serial=sn, reason=f"not verified in {max_s} s ({last})")


SERVED: dict[str, tuple[str, str, str | None]] = {}    # serial -> (device id, name, Management1 address)


def handle(path: str, listen: str, port: int, q=gql, rnd=render, start_verify=None) -> tuple[int, bytes]:
    parts = path.strip("/").split("/")
    if parts == ["bootstrap"]:
        event("script"); return 200, bootstrap_script(listen, port)
    if len(parts) < 2 or parts[0] not in ("config", "callback"):
        event("unknown", path=path); return 404, b"not found\n"
    sn = parts[1]
    devs = device_by_serial(sn, q)
    if len(devs) != 1:
        event(parts[0] + "-refused", serial=sn, matches=[d[1] for d in devs])
        return 404, f"serial {sn}: {len(devs)} devices\n".encode()
    dev_id, name = devs[0]
    if parts[0] == "config":
        body, warns = dc3_secrets.fill(rnd(name))
        for w in warns:
            event("secret-warning", serial=sn, device=name, warning=w)
        SERVED[sn] = (dev_id, name, mgmt_address(body))
        event("config", serial=sn, device=name, transform=transform_for(name), bytes=len(body))
        set_state(dev_id, name, "served", q=q, serial=sn)
        return 200, body.encode()
    if len(parts) >= 3 and parts[2] == "failed":
        reason = "/".join(parts[3:])[:200]
        event("callback-failed", serial=sn, device=name, reason=reason)
        set_state(dev_id, name, "failed", q=q, serial=sn, reason=reason)
        return 200, b"ok\n"
    ip = SERVED.get(sn, (None, None, None))[2]
    event("callback", serial=sn, device=name, verify_ip=ip)
    if not ip:
        set_state(dev_id, name, "failed", q=q, serial=sn, reason="callback without a served config / no Management1 address")
    else:
        (start_verify or (lambda *a: threading.Thread(target=verify, args=a, daemon=True).start()))(dev_id, name, sn, ip)
    return 200, b"ok\n"


def serve(listen: str, port: int):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                code, body = handle(self.path, listen, port)
            except Exception as e:
                event("error", path=self.path, error=repr(e)[:300]); code, body = 500, b"error\n"
            self.send_response(code); self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass

    event("start", listen=listen, port=port, infrahub=INFRAHUB, branch=BRANCH)
    http.server.ThreadingHTTPServer((listen, port), H).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__.split("\n\n")[1]); sys.exit(2)
    serve(sys.argv[1], int(sys.argv[2]))
