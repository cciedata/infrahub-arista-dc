#!/bin/sh
# Cloud provider stand-ins for DC3: AWS, AZURE. Rendered by Infrahub, do not edit.
set -eu
sysctl -qw net.ipv4.ip_forward=1
case "$(hostname)" in
DC3-AWS)
ip link set eth1 up
ip link add link eth1 name eth1.120 type vlan id 120 2>/dev/null || true
ip addr replace 169.254.120.1/30 dev eth1.120
ip link set eth1.120 up
ip link set eth2 up
ip link add link eth2 name eth2.121 type vlan id 121 2>/dev/null || true
ip addr replace 169.254.121.1/30 dev eth2.121
ip link set eth2.121 up
pgrep -x bfdd >/dev/null || /usr/lib/frr/bfdd -d -A 127.0.0.1
pgrep -x bgpd >/dev/null || /usr/lib/frr/bgpd -d -A 127.0.0.1
sleep 2
cat > /etc/frr/frr.conf <<'FRREOF'
hostname DC3-AWS
service integrated-vtysh-config
!
ip route 172.16.0.0/13 blackhole
!
bfd
 profile CLOUD-BFD
  transmit-interval 300
  receive-interval 300
  detect-multiplier 3
  no echo-mode
 exit
exit
!
router bgp 65500
 bgp router-id 169.254.120.1
 no bgp ebgp-requires-policy
 neighbor 169.254.120.2 remote-as 65370
 neighbor 169.254.120.2 description DC3-CLOUD1 Hu0-0-0-16.120
 neighbor 169.254.120.2 bfd profile CLOUD-BFD
 neighbor 169.254.121.2 remote-as 65370
 neighbor 169.254.121.2 description DC3-CLOUD2 Hu0-0-0-16.121
 neighbor 169.254.121.2 bfd profile CLOUD-BFD
 address-family ipv4 unicast
  network 172.16.0.0/13
 exit-address-family
exit
FRREOF
vtysh -f /etc/frr/frr.conf
echo "DC3-AWS stand-in ready"
;;
DC3-AZURE)
ip link set eth1 up
ip link add link eth1 name eth1.220 type vlan id 220 2>/dev/null || true
ip addr replace 10.98.0.18/30 dev eth1.220
ip link set eth1.220 up
ip link set eth2 up
ip link add link eth2 name eth2.220 type vlan id 220 2>/dev/null || true
ip addr replace 10.98.0.22/30 dev eth2.220
ip link set eth2.220 up
pgrep -x bfdd >/dev/null || /usr/lib/frr/bfdd -d -A 127.0.0.1
pgrep -x bgpd >/dev/null || /usr/lib/frr/bgpd -d -A 127.0.0.1
sleep 2
cat > /etc/frr/frr.conf <<'FRREOF'
hostname DC3-AZURE
service integrated-vtysh-config
!
ip route 172.30.0.0/15 blackhole
!
bfd
 profile CLOUD-BFD
  transmit-interval 300
  receive-interval 300
  detect-multiplier 3
  no echo-mode
 exit
exit
!
router bgp 12076
 bgp router-id 10.98.0.18
 no bgp ebgp-requires-policy
 neighbor 10.98.0.17 remote-as 65370
 neighbor 10.98.0.17 description DC3-CLOUD1 Hu0-0-0-16.220
 neighbor 10.98.0.17 bfd profile CLOUD-BFD
 neighbor 10.98.0.21 remote-as 65370
 neighbor 10.98.0.21 description DC3-CLOUD2 Hu0-0-0-16.220
 neighbor 10.98.0.21 bfd profile CLOUD-BFD
 address-family ipv4 unicast
  network 172.30.0.0/15
 exit-address-family
exit
FRREOF
vtysh -f /etc/frr/frr.conf
echo "DC3-AZURE stand-in ready"
;;
*)
echo "$(hostname): not a cloud provider stand-in of DC3" >&2
exit 1
;;
esac