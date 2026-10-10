#!/bin/sh
# Partner stand-ins behind DC3-VPNFW1 (DC3): PARTNER01. Rendered by Infrahub, do not edit.
set -eu
case "$(hostname)" in
DC3-PARTNER01)
ip link set eth1 up
ip addr replace 198.18.100.1/31 dev eth1
ip link add lan type dummy 2>/dev/null || true
ip addr replace 192.168.100.1/24 dev lan
ip link set lan up
ip route replace default via 198.18.100.0 dev eth1
echo "DC3-PARTNER01 stand-in ready (sources 192.168.100.0/24, seen by the fabric as 10.24.160.0/28)"
;;
*)
echo "$(hostname): not a partner of DC3-VPNFW1" >&2
exit 1
;;
esac