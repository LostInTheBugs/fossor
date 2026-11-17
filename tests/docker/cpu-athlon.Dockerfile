# Cible jetable 2026.11.003-c7 : faux cpuminer (log TTF @ 237.8 Mh/s) + faux
# *-cli de nœud (getters seulement).  Aucune machine réelle, aucun rig.
FROM fossor-sshd:test

COPY fake_cpuminer.sh /opt/fake-cpuminer
COPY fake_node_cli.sh /usr/local/bin/fakecoin-cli
RUN chmod +x /opt/fake-cpuminer /usr/local/bin/fakecoin-cli \
 && mkdir -p /var/log /root/fakecoin/data
