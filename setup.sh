#!/usr/bin/env bash
set -e
sudo apt update && sudo apt install -y mininet openvswitch-switch iperf3 python3-networkx
curl -L -o ui/cytoscape.min.js https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.2/cytoscape.min.js
(cd ui && python3 gen_topology.py)