#!/usr/bin/env python3
"""Start flows through the observer API (topo8.py must be running).
Spec: src,dst,proto,mbps,duration   e.g. h1,h2,tcp,25,600s
"""
import argparse, json, re, sys, urllib.request, urllib.error

UNIT = {'': 1, 's': 1, 'm': 60, 'h': 3600}

def post(base, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(),
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)

def parse(spec):
    p = [x.strip() for x in spec.split(',')]
    if len(p) not in (5, 6):
        raise ValueError('need src,dst,proto,mbps,duration[,core]')
    src, dst, proto, mbps, dur = p[:5]
    core = p[5].lower() if len(p) == 6 else ''
    if core not in ('', 's1', 's2'):
        raise ValueError('core must be s1 or s2')
    if proto.lower() not in ('tcp', 'udp'):
        raise ValueError('proto must be tcp or udp')
    m = re.fullmatch(r'(\d+)([smh]?)', dur.lower())
    if not m:
        raise ValueError('duration like 600, 600s, 10m, 1h')
    return {'src': src, 'dst': dst, 'proto': proto.lower(), 'mbps': int(mbps),
            'secs': int(m.group(1)) * UNIT[m.group(2)], 'core': core}

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('specs', nargs='*', help='flow specs, e.g. h1,h10,udp,15,600s')
    ap.add_argument('-f', '--file', help='file with one spec per line (# comments allowed)')
    ap.add_argument('--stop', action='store_true', help='stop all flows')
    ap.add_argument('--steer', metavar='SRC,DST,CORE', help='move a flow, e.g. h5,h7,s2')
    ap.add_argument('--link', metavar='ID,STATUS', help='set a link up/down, e.g. s3-s1,down')
    ap.add_argument('--mark', metavar='TEXT', help='write a label into the events log')
    ap.add_argument('--url', default='http://localhost:8000')
    a = ap.parse_args()
    if a.link:
        i, st = [x.strip() for x in a.link.split(',')]
        print(post(a.url, '/api/link', {'id': i, 'status': st}))
        return
    if a.mark:
        print(post(a.url, '/api/mark', {'text': a.mark}))
        return
    if a.steer:
        s, d, c = [x.strip() for x in a.steer.split(',')]
        print(post(a.url, '/api/steer', {'src': s, 'dst': d, 'core': c}))
        return
    if a.stop:
        print(post(a.url, '/api/stop'))
        return
    specs = list(a.specs)
    if a.file:
        specs += [l.split('#')[0].strip() for l in open(a.file)]
    specs = [s for s in specs if s]
    if not specs:
        ap.error('give at least one spec, -f FILE, or --stop')
    rc = 0
    for s in specs:
        try:
            body = parse(s)
        except ValueError as e:
            print(f'SKIP  {s}: {e}'); rc = 1; continue
        r = post(a.url, '/api/iperf', body)
        ok = r.get('ok')
        print(f"{'OK  ' if ok else 'FAIL'}  {s}" + ('' if ok else f"  -> {r.get('error')}"))
        rc |= 0 if ok else 1
    sys.exit(rc)

if __name__ == '__main__':
    main()
