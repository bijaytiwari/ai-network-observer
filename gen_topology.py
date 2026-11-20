import json, random

# tier y-positions and fixed x-positions
core = {'s1': 400, 's2': 650}
dist = {'s3': 275, 's4': 775}
access = {'s5': 150, 's6': 400, 's7': 650, 's8': 900}
hosts_of = {'s5': ['h1', 'h2', 'h3'], 's6': ['h4', 'h5'],
            's7': ['h6', 'h7'], 's8': ['h8', 'h9', 'h10']}

nodes = []
for s, x in core.items():   nodes.append({'id': s, 'type': 'switch', 'x': x, 'y': 80})
for s, x in dist.items():   nodes.append({'id': s, 'type': 'switch', 'x': x, 'y': 230})
for s, x in access.items(): nodes.append({'id': s, 'type': 'switch', 'x': x, 'y': 380})
for s, hs in hosts_of.items():
    for i, h in enumerate(hs):
        off = (i - (len(hs) - 1) / 2) * 70
        nodes.append({'id': h, 'type': 'host', 'x': access[s] + off, 'y': 530})

pairs = [('s1', 's2'),
         ('s3', 's1'), ('s3', 's2'), ('s4', 's1'), ('s4', 's2'),
         ('s5', 's3'), ('s6', 's3'), ('s7', 's4'), ('s8', 's4')]
pairs += [(h, s) for s, hs in hosts_of.items() for h in hs]

ACTIVE = {'h1-s5', 's5-s3', 's3-s1', 's1-s4', 's4-s8', 's8-h10'}   # sample h1 -> h10
links = []
for a, b in pairs:
    lid = f'{a}-{b}'
    hi = 0.4 if lid.startswith('h') or lid.endswith(tuple(f'-h{i}' for i in range(1, 11))) else 0.95
    links.append({'id': lid, 'source': a, 'target': b,
                  'util': round(random.uniform(0.05, hi), 2),
                  'active': 1 if lid in ACTIVE else 0})

json.dump({'nodes': nodes, 'links': links}, open('topology.json', 'w'))
