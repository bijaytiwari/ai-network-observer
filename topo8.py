#!/usr/bin/env python3
import json, os, time, threading, mimetypes, subprocess
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import networkx as nx
from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel
from topo_def import SWITCHES, HOSTS, SW_LINKS
import re
import csv

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui')
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
class Logger:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        stamp = time.strftime('%Y%m%d_%H%M%S')
        self.mfile = open(os.path.join(DATA_DIR, f'metrics_{stamp}.csv'), 'w', newline='')
        self.mw = csv.writer(self.mfile)
        self.mw.writerow(['ts', 'link', 'util', 'queue_pkts', 'drops', 'down'])
        self.efile = open(os.path.join(DATA_DIR, f'events_{stamp}.jsonl'), 'w')
        self.lock = threading.Lock()

    def metrics(self, ts, util, queue, down):
        with self.lock:
            for lid, u in util.items():
                q = queue.get(lid)
                self.mw.writerow([round(ts, 2), lid, u,
                                  q['backlog_pkts'] if q else '', q['drops'] if q else '',
                                  1 if lid in down else 0])
            self.mfile.flush()

    def event(self, kind, **kw):
        with self.lock:
            self.efile.write(json.dumps({'ts': round(time.time(), 2), 'event': kind, **kw}) + '\n')
            self.efile.flush()

hid = lambda h: int(h[1:])
MAC = lambda h: '00:00:00:00:00:%02x' % hid(h)
IP = lambda h: '10.0.0.%d' % hid(h)

def qstats(ifname):
    out = subprocess.run(['tc', '-s', 'class', 'show', 'dev', ifname],
                         capture_output=True, text=True).stdout
    m = re.search(r'dropped (\d+).*?backlog (\d+)b (\d+)p', out, re.S)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)

def if_drops(ifname):
    try:
        return int(open(f'/sys/class/net/{ifname}/statistics/tx_dropped').read())
    except OSError:
        return 0

def tree(core):
    other = 's2' if core == 's1' else 's1'
    g = nx.Graph()
    g.add_edges_from(SW_LINKS)
    g.add_edges_from(HOSTS.items())
    g.remove_node(other)                     # loop-free tree through one core
    return g

def port_of(net, a, b):
    sa = net[a]
    return sa.ports[sa.connectionsTo(net[b])[0][0]]

PRIO_OVERRIDE = 40000

def build_flows(net, core, overrides):
    rules = {sw: [] for sw in SWITCHES}
    g = tree(core)
    for sw in SWITCHES:
        if sw not in g:
            continue
        for h in HOSTS:
            nxt = nx.shortest_path(g, sw, h)[1]
            rules[sw].append(f'priority=100,dl_dst={MAC(h)},actions=output:{port_of(net, sw, nxt)}')
    for (src, dst), c in overrides.items():
        p = nx.shortest_path(tree(c), src, dst)
        for sw, nxt in zip(p[1:-1], p[2:]):
            rules[sw].append(f'priority={PRIO_OVERRIDE},dl_src={MAC(src)},dl_dst={MAC(dst)},'
                             f'actions=output:{port_of(net, sw, nxt)}')
    return rules

def install_routes(net, core, overrides=None):
    rules = build_flows(net, core, overrides or {})
    for sw in SWITCHES:
        path = f'/tmp/flows_{sw}.txt'
        with open(path, 'w') as f:
            f.write('\n'.join(rules[sw]) + ('\n' if rules[sw] else ''))
        subprocess.run(['ovs-ofctl', 'replace-flows', sw, path], check=True)


def dump_links(net):
    out = []
    for a, b in SW_LINKS + list(HOSTS.items()):
        i1, i2 = net[a].connectionsTo(net[b])[0]
        out.append({'id': f'{a}-{b}', 'a': a, 'b': b, 'a_if': str(i1), 'b_if': str(i2),
                    'cap_bps': (3e6 if (a, b) in SW_LINKS else 3e6)})
    json.dump(out, open('links.json', 'w'), indent=1)
    return out

def read_ctr(ifname):
    base = f'/sys/class/net/{ifname}/statistics/'
    try:
        return int(open(base + 'rx_bytes').read()), int(open(base + 'tx_bytes').read())
    except OSError:
        return None          # host-side interfaces live in the host's namespace

class Obs:
    def __init__(self, net, links):
        self.log = Logger()
        self.pair_core = {}
        self.q = {}
        self.net, self.links = net, links
        self.lid = {frozenset((l['a'], l['b'])): l['id'] for l in links}
        self.ends = {l['id']: (l['a'], l['b']) for l in links}
        self.core, self.util, self.prev = 's1', {}, {}
        self.down = set()
        self.flows, self.next_id = {}, 1
        self.lock = threading.Lock()

    def sampler(self):
        n = 0
        while True:
            t, util = time.time(), {}
            for l in self.links:
                best = 0.0
                for ifn in (l['a_if'], l['b_if']):
                    c = read_ctr(ifn)
                    if c is None:
                        continue
                    p = self.prev.get(ifn)
                    if p and t > p[0]:
                        rate = max(c[0] - p[1][0], c[1] - p[1][1]) * 8 / (t - p[0])
                        best = max(best, rate / l['cap_bps'])
                    self.prev[ifn] = (t, c)
                util[l['id']] = round(min(best, 1.0), 3)
            self.util = util
            n += 1
            if n % 2 == 0:      # every 2 s, switch-to-switch links only (keeps CPU low)
                q = {}
                for l in self.links:
                    if (l['a'], l['b']) in SW_LINKS:
                        d = [qstats(i) for i in (l['a_if'], l['b_if'])]
                        q[l['id']] = {'drops': sum(x[0] for x in d) + sum(if_drops(i) for i in (l['a_if'], l['b_if'])),
                                      'backlog_pkts': max(x[2] for x in d)}
                self.q = q
            with self.lock:
                self.prune()
            self.log.metrics(t, util, self.q, self.down)
            time.sleep(1)

    def path_links(self, src, dst):
        c = self.pair_core.get((src, dst), self.core)
        p = nx.shortest_path(tree(c), src, dst)
        return [self.lid[frozenset(e)] for e in zip(p, p[1:])]

    def prune(self):
        for fid in list(self.flows):
            f = self.flows[fid]
            if f['cli'].poll() is not None:
                if f['srv'].poll() is None:
                    f['srv'].terminate()
                self.log.event('flow_end', id=fid, src=f['src'], dst=f['dst'], proto=f['proto'])
                del self.flows[fid]
        live = {(f['src'], f['dst']) for f in self.flows.values()} | \
               {(f['dst'], f['src']) for f in self.flows.values()}
        stale = [k for k in self.pair_core if k not in live]
        if stale:
            for k in stale:
                del self.pair_core[k]
            install_routes(self.net, self.core, self.pair_core)

    def start_flow(self, src, dst, secs, proto='tcp', mbps=15, core=None):
        if src not in HOSTS or dst not in HOSTS or src == dst:
            raise ValueError('bad src/dst')
        if proto not in ('tcp', 'udp'):
            raise ValueError('proto must be tcp or udp')
        if core not in (None, 's1', 's2'):
            raise ValueError('core must be s1 or s2')
        secs = max(1, min(int(secs), 3600))
        mbps = max(0, min(int(mbps), 1000))
        with self.lock:
            if core:
                self.pair_core[(src, dst)] = core
                self.pair_core[(dst, src)] = core
                install_routes(self.net, self.core, self.pair_core)
            fid = self.next_id; self.next_id += 1
            port = str(5200 + fid)
            dn = subprocess.DEVNULL
            srv = self.net[dst].popen(['iperf3', '-s', '-1', '-p', port], stdout=dn, stderr=dn)
            time.sleep(0.5)
            cmd = ['iperf3', '-c', IP(dst), '-p', port, '-t', str(secs)]
            if proto == 'udp':
                cmd += ['-u', '-b', f'{mbps}M']
            elif mbps:
                cmd += ['-b', f'{mbps}M']
            cli = self.net[src].popen(cmd, stdout=dn, stderr=dn)
            self.flows[fid] = {'id': fid, 'src': src, 'dst': dst, 'proto': proto,
                               'srv': srv, 'cli': cli, 'end': time.time() + secs, 'mbps': mbps}
            self.log.event('flow_start', id=fid, src=src, dst=dst, proto=proto,
                           mbps=mbps, secs=secs, core=core or self.core)

    def steer(self, src, dst, core):
        if src not in HOSTS or dst not in HOSTS or src == dst or core not in ('s1', 's2'):
            raise ValueError('bad src/dst/core')
        with self.lock:
            self.pair_core[(src, dst)] = core
            self.pair_core[(dst, src)] = core
            install_routes(self.net, self.core, self.pair_core)
            self.log.event('steer', src=src, dst=dst, core=core)

    def set_link(self, lid, status):
        l = next((x for x in self.links if x['id'] == lid), None)
        if not l or status not in ('up', 'down'):
            raise ValueError('bad link or status')
        with self.lock:
            for ifn in (l['a_if'], l['b_if']):   # an end inside a host namespace just fails quietly
                subprocess.run(['ip', 'link', 'set', ifn, status], capture_output=True)
            if status == 'down':
                self.down.add(lid)
            else:
                self.down.discard(lid)

    def stop_all(self):
        with self.lock:
            for f in self.flows.values():
                f['cli'].terminate(); f['srv'].terminate()
            self.flows.clear()
            if self.pair_core:
                self.pair_core.clear()
                install_routes(self.net, self.core, self.pair_core)

    def divert(self, core):
        if core not in ('s1', 's2'):
            raise ValueError('core must be s1 or s2')
        with self.lock:
            self.log.event('stop_all')
            install_routes(self.net, core, self.pair_core)
            self.core = core
            self.log.event('divert', core=core)

    def mark(self, text):
        self.log.event('mark', text=str(text)[:200])

    def state(self):
        with self.lock:
            self.prune()
            active, flows = set(), []
            for f in self.flows.values():
                active.update(self.path_links(f['src'], f['dst']))
                flows.append({'id': f['id'], 'src': f['src'], 'dst': f['dst'], 'proto': f['proto'],
                              'core': self.pair_core.get((f['src'], f['dst']), self.core),
                              'mbps': f['mbps'], 'path': self.path_links(f['src'], f['dst']),
                              'left': max(0, int(f['end'] - time.time()))})
            return {'core': self.core, 'util': self.util, 'active': sorted(active),
                    'down': sorted(self.down), 'queue': self.q, 'flows': flows,
                    'cap': {l['id']: l['cap_bps'] for l in self.links}}

class H(BaseHTTPRequestHandler):
    obs = None
    def log_message(self, *a): pass
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/state':
            return self._json(self.obs.state())
        if path == '/':
            path = '/index.html'
        fp = os.path.normpath(os.path.join(UI_DIR, path.lstrip('/')))
        if not fp.startswith(UI_DIR + os.sep) or not os.path.isfile(fp):
            self.send_response(404); self.end_headers(); return
        data = open(fp, 'rb').read()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(fp)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            body = {}
        try:
            if self.path == '/api/iperf':
                self.obs.start_flow(body.get('src'), body.get('dst'), body.get('secs', 20),
                                    body.get('proto', 'tcp'), body.get('mbps', 15),
                                    body.get('core') or None)
            elif self.path == '/api/steer':
                self.obs.steer(body.get('src'), body.get('dst'), body.get('core'))
            elif self.path == '/api/mark':
                self.obs.mark(body.get('text', ''))
            elif self.path == '/api/link':
                self.obs.set_link(body.get('id'), body.get('status'))
            elif self.path == '/api/divert':
                self.obs.divert(body.get('core'))
            elif self.path == '/api/stop':
                self.obs.stop_all()
            else:
                return self._json({'error': 'not found'}, 404)
        except (ValueError, TypeError) as e:
            return self._json({'error': str(e)}, 400)
        except Exception as e:
            return self._json({'error': f'{type(e).__name__}: {e}'}, 500)
        self._json({'ok': True})

class ObsCLI(CLI):
    obs = None
    def do_divert(self, line):
        "divert s1|s2 : route all traffic via that core switch"
        self.obs.divert(line.strip())
    def set_link(self, lid, status):
        if lid not in self.ends or status not in ('up', 'down'):
            raise ValueError('bad link or status')
        l = next(x for x in self.links if x['id'] == lid)
        with self.lock:
            for ifn in (l['a_if'], l['b_if']):   # host-side ends live in host namespaces and just fail quietly
                subprocess.run(['ip', 'link', 'set', ifn, status], capture_output=True)
            if status == 'down':
                self.down.add(lid)
            else:
                self.down.discard(lid)
def run():
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink)
    for h in HOSTS:
        net.addHost(h, ip=f'{IP(h)}/24', mac=MAC(h))
    for s in SWITCHES:
        net.addSwitch(s, failMode='secure')
    for a, b in SW_LINKS:
        net.addLink(a, b, bw=3)
    for h, s in HOSTS.items():
        net.addLink(h, s, bw=100)
    net.start()
    net.staticArp()
    install_routes(net, 's1')
    obs = Obs(net, dump_links(net))
    H.obs = ObsCLI.obs = obs
    threading.Thread(target=obs.sampler, daemon=True).start()
    srv = ThreadingHTTPServer(('0.0.0.0', 8000), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ObsCLI(net)
    obs.stop_all()
    srv.shutdown()
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    run()
