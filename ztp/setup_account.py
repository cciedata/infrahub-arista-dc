#!/usr/bin/env python3
"""DC3 ZTP: create the dc3-ztp-callback account on the Infrahub instance and its API token (idempotent).

  setup_account.py [--new-token]        run with the repo venv; admin login from INFRAHUB_USERNAME / INFRAHUB_PASSWORD (default admin)

own account dc3-ztp-callback, account type Script, token with no expiry; Dcim:Device view on all branches
(allow_all).: in this repo's own Infrahub (DC3_ZTP_BRANCH unset or main) DC3 lives on main, so update is allowed on
every branch (allow_all) and the "refused on main" test is skipped. Option: in an Infrahub instance that also holds other
sites, DC3 devices live on a branch (DC3_ZTP_BRANCH, for example dc3) and update is allowed on branches other than main only
(allow_other).
plus view on every object kind (*:*, all branches), because the live render reads them all.
Creates, when missing: the two object permissions, role and group "DC3 ZTP Callback", the account (random password, never
printed), member of the group. Then logs in as the account, creates the token (only if ~/.dc3/secrets.env has none, or
--new-token) and stores it there as DC3_ZTP_CALLBACK_TOKEN (mode 600).
Last, a permission test with the token: write DC3-L11's current serial_number back unchanged on the DC3 branch (allowed) and a
main device's serial_number back unchanged on main (must be refused). serial_number is used because ztp_state exists only after
the schema change.
Infrahub permissions are per object kind, not per attribute: the account can update any DcimDevice attribute on main.
"""
import os
import re
import secrets
import sys

import httpx

ADDR = os.environ.get("INFRAHUB_ADDRESS", "http://localhost:8000")
ADMIN = (os.environ.get("INFRAHUB_USERNAME", "admin"), os.environ.get("INFRAHUB_PASSWORD", "infrahub"))
SECRETS = os.path.expanduser(os.environ.get("DC3_SECRETS", "~/.dc3/secrets.env"))
NAME, ROLE, GROUP, TOKEN_NAME, TEST_DEV = "dc3-ztp-callback", "DC3 ZTP Callback", "DC3 ZTP Callback", "dc3-ztp-server", "DC3-L11"
BRANCH = os.environ.get("DC3_ZTP_BRANCH", "main")
# a standalone instance (docs/BUILD.md) holds only DC3, on main; an instance that also holds other sites keeps DC3
# on a branch (DC3_ZTP_BRANCH=dc3) and main must stay read-only for the account there
STANDALONE = BRANCH == "main"


def login(user, pw):
    r = httpx.post(f"{ADDR}/api/auth/login", json={"username": user, "password": pw}, timeout=30)
    r.raise_for_status(); return {"Authorization": f"Bearer {r.json()['access_token']}"}


def gql(h, q, branch="main"):
    r = httpx.post(f"{ADDR}/graphql/{branch}", json={"query": q}, headers=h, timeout=60).json()
    if r.get("errors"):
        raise RuntimeError(str(r["errors"])[:500])
    return r["data"]


def find(h, kind, field, value, branch="main"):
    e = gql(h, '{ %s(%s__value: "%s") { edges { node { id } } } }' % (kind, field, value), branch)[kind]["edges"]
    return e[0]["node"]["id"] if e else None


def save_secrets(new):
    """Replace these keys in ~/.dc3/secrets.env (mode 600), keeping every other line."""
    os.makedirs(os.path.dirname(SECRETS), mode=0o700, exist_ok=True)
    old = open(SECRETS).read().splitlines() if os.path.exists(SECRETS) else []
    keep = [l for l in old if not any(l.startswith(k + "=") for k in new)]
    with open(SECRETS + ".tmp", "w") as f:
        f.write("\n".join(keep + [f"{k}='{v}'" for k, v in new.items()]) + "\n")
    os.chmod(SECRETS + ".tmp", 0o600); os.replace(SECRETS + ".tmp", SECRETS)
    print("secrets file updated:", SECRETS, "(" + ", ".join(new) + ")")


def main():
    A = login(*ADMIN)
    # decision encoding, read from the instance's own permissions (never assumed)
    perms = gql(A, "{ CoreObjectPermission { edges { node { id identifier { value } decision { value } } } } }")["CoreObjectPermission"]["edges"]
    enc = {}
    for p in perms:
        m = re.search(r":(deny|allow_default|allow_other|allow_all)$", p["node"]["identifier"]["value"] or "")
        if m: enc[m.group(1)] = p["node"]["decision"]["value"]
    print("decision encoding on this instance:", enc)
    known = {"deny": 1, "allow_default": 2, "allow_other": 4, "allow_all": 6}
    if any(known[k] != v for k, v in enc.items()) or "allow_all" not in enc:
        sys.exit(f"STOP: decision values differ from the expected encoding {known}")
    ids = []
    # the live render reads every kind its query touches, so view on all kinds (*:*);
    # writes: Dcim:Device update on branches other than main (instance with other sites), or on every branch including main
    # on a standalone instance where DC3 lives on main
    for ns, nm, action, decision in (("*", "*", "view", "allow_all"), ("Dcim", "Device", "view", "allow_all"),
                                     ("Dcim", "Device", "update", "allow_all" if STANDALONE else "allow_other")):
        ident = f"{ns}:{nm} {action} {decision}"
        # look up by the fields of the uniqueness constraint 'namespace-name-action-decision' (the identifier string format
        # of Infrahub 1.11.2 did not match the expected format)
        allp = gql(A, '{ CoreObjectPermission { edges { node { id identifier { value } namespace { value } name { value } '
                      'action { value } decision { value } } } } }')["CoreObjectPermission"]["edges"]
        found = [e for e in allp if (e["node"]["namespace"]["value"], e["node"]["name"]["value"], e["node"]["action"]["value"],
                                     e["node"]["decision"]["value"]) == (ns, nm, action, known[decision])]
        if found:
            pid = found[0]["node"]["id"]; print("permission exists:", found[0]["node"]["identifier"]["value"])
        else:
            pid = gql(A, 'mutation { CoreObjectPermissionCreate(data: {namespace: {value: "%s"}, name: {value: "%s"}, '
                         'action: {value: "%s"}, decision: {value: %d}, description: {value: "DC3 ZTP callback"}}) '
                         '{ ok object { id identifier { value } } } }' % (ns, nm, action, known[decision]))["CoreObjectPermissionCreate"]["object"]
            print("permission created:", pid["identifier"]["value"]); pid = pid["id"]
        ids.append(pid)
    plist = ", ".join('{id: "%s"}' % i for i in ids)
    rid = find(A, "CoreAccountRole", "name", ROLE)
    if not rid:  # an existing role gets the full permission list again below (adds to a role)
        rid = gql(A, 'mutation { CoreAccountRoleCreate(data: {name: {value: "%s"}, permissions: [%s]}) { ok object { id } } }'
                  % (ROLE, plist))["CoreAccountRoleCreate"]["object"]["id"]; print("role created:", ROLE)
    else:
        gql(A, 'mutation { CoreAccountRoleUpdate(data: {id: "%s", permissions: [%s]}) { ok } }' % (rid, plist)); print("role exists:", ROLE)
    gid = find(A, "CoreAccountGroup", "name", GROUP)
    if not gid:
        gid = gql(A, 'mutation { CoreAccountGroupCreate(data: {name: {value: "%s"}, roles: [{id: "%s"}]}) { ok object { id } } }'
                  % (GROUP, rid))["CoreAccountGroupCreate"]["object"]["id"]; print("group created:", GROUP)
    else:
        print("group exists:", GROUP)
    vals = {}
    if os.path.exists(SECRETS):
        for line in open(SECRETS):
            m = re.match(r"([A-Z0-9_]+)=(.*)$", line.strip())
            if m: vals[m.group(1)] = m.group(2).strip("'\"")
    aid = find(A, "CoreAccount", "name", NAME)
    pw = vals.get("DC3_ZTP_CALLBACK_PASSWORD")
    if not aid:
        pw = secrets.token_urlsafe(24)
        aid = gql(A, 'mutation { CoreAccountCreate(data: {name: {value: "%s"}, password: {value: "%s"}, account_type: {value: "Script"}, '
                     'description: {value: "DC3 ZTP server: writes ztp_state"}, member_of_groups: [{id: "%s"}]}) '
                     '{ ok object { id } } }' % (NAME, pw, gid))["CoreAccountCreate"]["object"]["id"]
        print("account created:", NAME, "(Script)")
    else:
        print("account exists:", NAME)
        if not pw:
            pw = secrets.token_urlsafe(24)
            gql(A, 'mutation { CoreAccountUpdate(data: {id: "%s", password: {value: "%s"}}) { ok } }' % (aid, pw)); print("password reset (not stored before)")
    save_secrets({"DC3_ZTP_CALLBACK_PASSWORD": pw})
    tok = vals.get("DC3_ZTP_CALLBACK_TOKEN")
    if not tok or "--new-token" in sys.argv:
        U = login(NAME, pw)
        # Infrahub 1.11.2: token is a ValueType ({ value })
        r = gql(U, 'mutation { InfrahubAccountTokenCreate(data: {name: "%s"}) { ok object { token { value } } } }' % TOKEN_NAME)
        t = r["InfrahubAccountTokenCreate"]["object"]["token"]
        tok = t["value"] if isinstance(t, dict) else t; print("token created:", TOKEN_NAME, "(no expiry)")
    save_secrets({"DC3_ZTP_CALLBACK_PASSWORD": pw, "DC3_ZTP_CALLBACK_TOKEN": tok})
    # permission test with the token: allowed on the DC3 branch, refused on main
    T = {"X-INFRAHUB-KEY": tok}
    d = gql(A, '{ DcimDevice(name__value: "%s") { edges { node { id serial_number { value } } } } }' % TEST_DEV, BRANCH)["DcimDevice"]["edges"]
    if not d:
        sys.exit(f"STOP: {TEST_DEV} not found on branch {BRANCH}")
    d = d[0]["node"]; cur = d["serial_number"]["value"]; val = '"%s"' % cur if cur else "null"
    try:
        gql(T, 'mutation { DcimDeviceUpdate(data: {id: "%s", serial_number: {value: %s}}) { ok } }' % (d["id"], val), BRANCH)
        print(f"TEST branch {BRANCH}: {NAME} may update {TEST_DEV} (serial_number {cur}, unchanged): OK")
    except RuntimeError as e:
        print(f"TEST branch {BRANCH}: REFUSED {e}"); sys.exit(4)
    try:
        n_sites = gql(T, "{ LocationSite { count } }", BRANCH)["LocationSite"]["count"]
        print(f"TEST read with the token on {BRANCH}: LocationSite count {n_sites} (view on all kinds): OK")
    except RuntimeError as e:
        print(f"TEST read with the token: REFUSED {str(e)[:160]}"); sys.exit(6)
    m = [] if STANDALONE else gql(A, "{ DcimDevice(limit: 1) { edges { node { id name { value } serial_number { value } } } } }")["DcimDevice"]["edges"]
    if STANDALONE:
        print("TEST main: skipped (standalone instance, DC3 lives on main)")
    if m:
        n = m[0]["node"]; cur = n["serial_number"]["value"]; val = '"%s"' % cur if cur else "null"
        try:
            gql(T, 'mutation { DcimDeviceUpdate(data: {id: "%s", serial_number: {value: %s}}) { ok } }' % (n["id"], val))
            print(f"TEST main: {NAME} may update {n['name']['value']} on main (NOT expected)"); sys.exit(5)
        except RuntimeError as e:
            print(f"TEST main: refused as expected ({str(e)[:120]})")
    print("SETUP DONE")


if __name__ == "__main__":
    main()
