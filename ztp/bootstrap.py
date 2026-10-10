#!/usr/bin/env python3
# DC3 EOS ZTP bootstrap script.
# Served by ztp/ztp_server.py, which fills __SRV__. DHCP option 67 points EOS ZTP to this file; EOS runs an executable it
# downloads and reboots afterwards (github.com/arista-eosplus/ztpserver docs/overview.rst). During ZTP the switch has no config,
# so Management1 sits in the default VRF with its DHCP address until the first cEOS run.
# Steps: read the serial, download the config by serial to the startup-config, call back. Any error is reported to
# /callback/<serial>/failed/<reason> (ztp_state = failed) and the script exits non-zero.
import json
import re
import subprocess
import sys
import time
import urllib.request

SRV = "__SRV__"
LOG = "/mnt/flash/dc3_ztp.log"
STARTUP = "/mnt/flash/startup-config"


def log(msg):
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def get(path, timeout=120):
    with urllib.request.urlopen("http://%s/%s" % (SRV, path), timeout=timeout) as r:
        return r.read()


def report_failed(sn, step, err):
    log("FAILED at %s: %s" % (step, err))
    reason = re.sub(r"[^A-Za-z0-9_.-]", "_", "%s:%s" % (step, err))[:120]
    try:
        get("callback/%s/failed/%s" % (sn, reason), timeout=30)
    except Exception:
        pass
    sys.exit(1)


def main():
    sn, step = "UNKNOWN", "serial"
    try:
        out = subprocess.check_output(["FastCli", "-p", "15", "-c", "show version | json"], timeout=120)
        sn = json.loads(out.decode()).get("serialNumber") or "UNKNOWN"
        if sn == "UNKNOWN":
            raise RuntimeError("no serialNumber in show version")
        log("serial " + sn)
        step = "config-download"
        cfg = get("config/" + sn)
        if not cfg.strip():
            raise RuntimeError("empty config")
        with open(STARTUP, "wb") as f:
            f.write(cfg)
        log("startup-config written (%d bytes)" % len(cfg))
        step = "callback"
        get("callback/" + sn, timeout=30)
        log("callback sent; EOS ZTP reboots into the startup-config")
    except SystemExit:
        raise
    except Exception as e:
        report_failed(sn, step, e)


if __name__ == "__main__":
    main()
