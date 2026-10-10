#!/usr/bin/env python3
"""DC3 ZTP: record a switch's serial number before racking and mark it pending.

  set_serial.py <device> <serial> [--branch main]

Writes DcimDevice serial_number and, when the schema has it, ztp_state = pending
and ztp_updated, on the branch the device lives on. Runs with an account that may write there (INFRAHUB_USERNAME /
INFRAHUB_PASSWORD, or DC3_ZTP_CALLBACK_TOKEN from ~/.dc3/secrets.env). Refuses a serial already used by another device.
It writes the branch directly, not through a proposed change.
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lab"))
import dc3_secrets  # noqa: E402

ADDR = os.environ.get("DC3_INFRAHUB_ADDRESS", "http://localhost:8000").rstrip("/")


def headers():
    if os.environ.get("INFRAHUB_USERNAME"):
        req = urllib.request.Request(f"{ADDR}/api/auth/login", data=json.dumps(
            {"username": os.environ["INFRAHUB_USERNAME"], "password": os.environ.get("INFRAHUB_PASSWORD", "")}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"Authorization": "Bearer " + json.loads(r.read())["access_token"]}
    tok = dc3_secrets.load().get("DC3_ZTP_CALLBACK_TOKEN")
    if not tok:
        sys.exit("STOP: no INFRAHUB_USERNAME and no DC3_ZTP_CALLBACK_TOKEN")
    return {"X-INFRAHUB-KEY": tok}


def gql(h, q, branch):
    req = urllib.request.Request(f"{ADDR}/graphql/{branch}", data=json.dumps({"query": q}).encode(),
                                 headers={"Content-Type": "application/json", **h})
    with urllib.request.urlopen(req, timeout=60) as r:
        doc = json.loads(r.read())
    if doc.get("errors"):
        raise RuntimeError(str(doc["errors"])[:300])
    return doc["data"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("device"); ap.add_argument("serial"); ap.add_argument("--branch", default=os.environ.get("DC3_ZTP_BRANCH", "main"))
    a = ap.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", a.serial):
        sys.exit("STOP: serial must be 1-64 characters of A-Z a-z 0-9 . _ -")
    h = headers()
    dev = gql(h, '{ DcimDevice(name__value: "%s") { edges { node { id serial_number { value } } } } }' % a.device, a.branch)["DcimDevice"]["edges"]
    if len(dev) != 1:
        sys.exit(f"STOP: {a.device}: {len(dev)} devices on branch {a.branch}")
    used = gql(h, '{ DcimDevice(serial_number__value: "%s") { edges { node { name { value } } } } }' % a.serial, a.branch)["DcimDevice"]["edges"]
    others = [e["node"]["name"]["value"] for e in used if e["node"]["name"]["value"] != a.device]
    if others:
        sys.exit(f"STOP: serial {a.serial} already on {others}")
    did, old = dev[0]["node"]["id"], dev[0]["node"]["serial_number"]["value"]
    gql(h, 'mutation { DcimDeviceUpdate(data: {id: "%s", serial_number: {value: "%s"}}) { ok } }' % (did, a.serial), a.branch)
    print(f"{a.device}: serial_number {old!r} -> {a.serial!r} on branch {a.branch}")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        gql(h, 'mutation { DcimDeviceUpdate(data: {id: "%s", ztp_state: {value: "pending"}, ztp_updated: {value: "%s"}}) { ok } }'
            % (did, now), a.branch)
        print(f"{a.device}: ztp_state pending")
    except RuntimeError as e:
        print(f"WARNING: ztp_state not written (schema without ztp_state?): {str(e)[:160]}")


if __name__ == "__main__":
    main()
