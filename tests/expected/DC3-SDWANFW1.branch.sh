#!/bin/sh
# SD-WAN branch stand-ins behind DC3-SDWANFW1 (DC3): BR001, BR002. Rendered by Infrahub, do not edit.
set -eu
sysctl -qw net.ipv4.ip_forward=1
case "$(hostname)" in
DC3-BR001)
ip link set eth1 up
ip addr replace 10.79.16.2/21 dev eth1
ip link add lan type dummy 2>/dev/null || true
ip addr replace 10.64.1.1/24 dev lan
ip link set lan up
pgrep -x bgpd >/dev/null || /usr/lib/frr/bgpd -d -A 127.0.0.1
sleep 2
cat > /etc/frr/frr.conf <<'FRREOF'
hostname DC3-BR001
service integrated-vtysh-config
!
router bgp 65400
 bgp router-id 10.79.16.2
 neighbor 10.79.16.1 remote-as 65400
 neighbor 10.79.16.1 description DC3-SDWANFW1 ADVPN-HUB
 address-family ipv4 unicast
  network 10.64.1.0/24
 exit-address-family
exit
FRREOF
vtysh -f /etc/frr/frr.conf
echo "DC3-BR001 stand-in ready"
;;
DC3-BR002)
ip link set eth1 up
ip addr replace 10.79.16.3/21 dev eth1
ip link add lan type dummy 2>/dev/null || true
ip addr replace 10.64.2.1/24 dev lan
ip link set lan up
pgrep -x bgpd >/dev/null || /usr/lib/frr/bgpd -d -A 127.0.0.1
sleep 2
cat > /etc/frr/frr.conf <<'FRREOF'
hostname DC3-BR002
service integrated-vtysh-config
!
router bgp 65400
 bgp router-id 10.79.16.3
 neighbor 10.79.16.1 remote-as 65400
 neighbor 10.79.16.1 description DC3-SDWANFW1 ADVPN-HUB
 address-family ipv4 unicast
  network 10.64.2.0/24
 exit-address-family
exit
FRREOF
vtysh -f /etc/frr/frr.conf
echo "DC3-BR002 stand-in ready"
;;
*)
echo "$(hostname): not a branch of DC3-SDWANFW1" >&2
exit 1
;;
esac