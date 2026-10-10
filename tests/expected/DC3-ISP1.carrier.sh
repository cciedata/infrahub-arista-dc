#!/bin/sh
# DC3-ISP-A: lab stand-in for carrier ISP-A (AS 64496) facing DC3-ISP1 Gi0-0-0-3.
# Rendered by Infrahub from DC3-ISP1's data, do not edit. Interfaces: "isp" toward DC3-ISP1, "host" toward the internet test host.
set -eu
sysctl -qw net.ipv4.ip_forward=1
ip link set isp mtu 9214 up
ip addr replace 192.0.2.17/30 dev isp
ip link set host up
ip addr replace 198.18.1.1/24 dev host
pgrep -x bfdd >/dev/null || /usr/lib/frr/bfdd -d -A 127.0.0.1
pgrep -x bgpd >/dev/null || /usr/lib/frr/bgpd -d -A 127.0.0.1
sleep 2
cat > /etc/frr/frr.conf <<'FRREOF'
hostname DC3-ISP-A
service integrated-vtysh-config
!
bfd
 profile CARRIER-BFD
  transmit-interval 999
  receive-interval 999
  detect-multiplier 3
  no echo-mode
 exit
exit
!
router bgp 64496
 bgp router-id 192.0.2.17
 no bgp ebgp-requires-policy
 neighbor 192.0.2.18 remote-as 65050
 neighbor 192.0.2.18 description DC3-ISP1
 neighbor 192.0.2.18 bfd profile CARRIER-BFD
 address-family ipv4 unicast
  network 198.18.1.0/24
  neighbor 192.0.2.18 default-originate
 exit-address-family
exit
FRREOF
vtysh -f /etc/frr/frr.conf
echo "DC3-ISP-A stand-in ready"