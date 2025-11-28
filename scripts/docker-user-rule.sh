
#docker-user-rule.sh — set iptables rules in DOCKER-USER chain to allow satellite container traffic
# enable container to forward traffic for the SAT_HOST_CIDR
iptables -I DOCKER-USER -s 172.0.0.0/8 -d 172.0.0.0/8 -j ACCEPT

# enable container to access internet via NAT
iptables -t nat -A POSTROUTING -s 172.0.0.0/8 ! -d 172.0.0.0/8 -j MASQUERADE
