#!/usr/bin/env python3
"""Network observer agent: local LLM (Ollama) + tools over the observer API. Recommends only."""
import json, os, sys, threading, time, urllib.request
from collections import deque
import networkx as nx
from topo_def import HOSTS, SW_LINKS

API = 'http://localhost:8000'
OLLAMA = os.environ.get('OLLAMA_URL', 'http://10.0.2.2:11434')
MODEL = os.environ.get('OLLAMA_MODEL', 'qwen2.5:1.5b')
UTIL_HI, QUEUE_HI = 0.8, 20

LID = {frozenset(p): f'{p[0]}-{p[1]}' for p in SW_LINKS + list(HOSTS.items())}

def tree(core):
    other = 's2' if core == 's1' else 's1'
    g = nx.Graph()
    g.add_edges_from(SW_LINKS)
    g.add_edges_from(HOSTS.items())
    g.remove_node(other)
    return g

def path_ids(src, dst, core):
    p = nx.shortest_path(tree(core), src, dst)
    return [LID[frozenset(e)] for e in zip(p, p[1:])]

# ---- state history (polled once a second) ----
hist = deque(maxlen=120)

def poll():
    while True:
        try:
            with urllib.request.urlopen(API + '/state', timeout=20) as r:
                hist.append((time.time(), json.load(r)))
        except Exception:
            pass
        time.sleep(1)

# ---- tools ----
def get_network_state():
    if not hist:
        return {'error': 'no data yet; is topo8.py running?'}
    s = hist[-1][1]
    busy = {}
    for lid, u in s['util'].items():
        q = s['queue'].get(lid, {})
        if u > 0.02 or q.get('backlog_pkts', 0) > 0 or lid in s['down']:
            busy[lid] = {'util': u, 'queue_pkts': q.get('backlog_pkts'),
                         'dropped_total': q.get('drops'), 'down': lid in s['down']}
    return {'default_core': s['core'], 'busy_links': busy, 'flows': s['flows'],
            'down_links': s['down']}

def find_congested_links(window_s=10):
    window_s = max(5, min(int(window_s), 60))
    now = time.time()
    w = [st for t, st in hist if now - t <= window_s]
    if len(w) < min(window_s, 5):
        return {'error': f'only {len(w)} samples of history; try again in a few seconds'}
    out = []
    for lid in w[-1]['queue']:                 # switch-to-switch links only
        utils = [st['util'].get(lid, 0) for st in w]
        qs = [st['queue'].get(lid, {}).get('backlog_pkts', 0) for st in w]
        d0 = w[0]['queue'].get(lid, {}).get('drops', 0)
        d1 = w[-1]['queue'].get(lid, {}).get('drops', 0)
        why = []
        if min(utils) >= UTIL_HI:
            why.append(f'utilization >= {UTIL_HI:.0%} for the whole window')
        if min(qs) >= QUEUE_HI:
            why.append(f'queue >= {QUEUE_HI} packets for the whole window')
        if d1 > d0:
            why.append(f'{d1 - d0} packets dropped during the window')
        if why:
            users = [{'src': f['src'], 'dst': f['dst'], 'proto': f['proto'],
                      'core': f['core'], 'mbps': f['mbps']}
                     for f in w[-1]['flows'] if lid in f['path']]
            out.append({'link': lid, 'reasons': why,
                        'avg_util': round(sum(utils) / len(utils), 2),
                        'max_queue_pkts': max(qs), 'flows_using_link': users})
    return {'window_s': window_s, 'samples': len(w), 'congested': out}

def evaluate_move(src, dst, target_core):
    if not hist:
        return {'error': 'no data yet'}
    s = hist[-1][1]
    if target_core not in ('s1', 's2'):
        return {'error': 'target_core must be s1 or s2'}
    f = next((x for x in s['flows'] if x['src'] == src and x['dst'] == dst), None)
    if not f:
        return {'error': f'no running flow {src}->{dst}'}
    cur, alt = f['path'], path_ids(src, dst, target_core)
    if cur == alt:
        return {'error': f'flow already uses {target_core}'}
    cap = s['cap']
    rate = min([f['mbps'] * 1e6 if f['mbps'] else 1e18] + [cap[l] for l in cur])
    new = {l: {'util_now': s['util'].get(l, 0),
               'util_after': round(s['util'].get(l, 0) + rate / cap[l], 2),
               'queue_now': s['queue'].get(l, {}).get('backlog_pkts')}
           for l in alt if l not in cur}
    freed = {l: {'util_now': s['util'].get(l, 0),
                 'util_after': round(max(0, s['util'].get(l, 0) - rate / cap[l]), 2)}
             for l in cur if l not in alt}
    down = [l for l in alt if l in s['down']]
    ok = not down and all(v['util_after'] <= UTIL_HI for v in new.values())
    return {'flow': f'{src}->{dst} ({f["proto"]})', 'est_rate_mbps': round(rate / 1e6, 1),
            'current_path': cur, 'new_path': alt,
            'new_links_after_move': new, 'freed_links_after_move': freed,
            'down_links_on_new_path': down,
            'verdict': 'safe' if ok else 'not recommended',
            'command': f'python3 flows.py --steer {src},{dst},{target_core}'}

FUNCS = {'get_network_state': get_network_state,
         'find_congested_links': find_congested_links,
         'evaluate_move': evaluate_move}

def _t(name, desc, props=None, req=None):
    return {'type': 'function', 'function': {'name': name, 'description': desc,
            'parameters': {'type': 'object', 'properties': props or {}, 'required': req or []}}}

TOOLS = [
    _t('get_network_state', 'Current snapshot: busy links (utilization 0-1, queue packets, total dropped), running flows with paths and core, down links.'),
    _t('find_congested_links', 'Switch-to-switch links congested for a whole recent window (utilization >= 80%, queue >= 20 packets, or drops), with the flows using each.',
       {'window_s': {'type': 'integer', 'description': 'seconds of history, 5-60, default 10'}}),
    _t('evaluate_move', 'Estimate moving one running flow to core switch s1 or s2: load on the new path, relief on the old one, and a verdict.',
       {'src': {'type': 'string'}, 'dst': {'type': 'string'},
        'target_core': {'type': 'string', 'enum': ['s1', 's2']}}, ['src', 'dst', 'target_core']),
]

SYSTEM = """You are a network observer for a Mininet lab: two core switches (s1, s2), distribution s3/s4, access s5-s8, hosts h1-h10. Each flow can be steered through s1 or s2.
Rules:
- Get every number from the tools; never invent values.
- To judge congestion, call find_congested_links. For a fix, call evaluate_move for candidate flows and core switches, and compare.
- You only recommend. Give the exact command from the tool result and let the user run it. Never claim you applied a change.
- The estimates are rough, especially for TCP, which adapts its rate. Say so briefly.
- If nothing is congested, say so. If no move helps, say that too.
- Be brief and concrete: which link, why, which flow to move, expected effect."""


# ---- recommendation pipeline: code decides, the local LLM explains ----
SYSTEM_PIPE = ("You are a network observer. Using only the facts given, explain in at most "
               "3 short sentences which flow should move and why, and include its command. "
               "You only recommend; the user runs the command. Do not invent numbers.")

def analyze():
    cong = find_congested_links(10)
    if 'error' in cong or not cong['congested']:
        return {'congestion': cong, 'candidate_moves': []}
    moves, seen = [], set()
    for c in cong['congested']:
        for f in c['flows_using_link']:
            for core in ('s1', 's2'):
                key = (f['src'], f['dst'], core)
                if core == f['core'] or key in seen:
                    continue
                seen.add(key)
                moves.append(evaluate_move(f['src'], f['dst'], core))
    return {'congestion': cong, 'candidate_moves': moves}

def best_move(data):
    safe = [m for m in data['candidate_moves'] if m.get('verdict') == 'safe']
    if not safe:
        return None
    return max(safe, key=lambda m: sum(v['util_now'] - v['util_after']
                                       for v in m['freed_links_after_move'].values()))

def plain_report(data):
    cong = data['congestion']
    if 'error' in cong:
        return cong['error']
    if not cong['congested']:
        return 'No sustained congestion in the last 10 s.'
    lines = []
    for c in cong['congested']:
        lines.append(f"Congested: {c['link']} ({'; '.join(c['reasons'])}), "
                     f"avg util {c['avg_util']:.0%}, max queue {c['max_queue_pkts']} pkts")
        lines.append('  Flows: ' + ', '.join(f"{f['src']}->{f['dst']} {f['proto']} via {f['core']}"
                                              for f in c['flows_using_link']))
    best = best_move(data)
    if best:
        lines.append(f"Recommend: move {best['flow']}  ->  {best['command']}")
        lines.append(f"  New path {best['new_path']} stays under {UTIL_HI:.0%} (rough estimate).")
    else:
        lines.append('No safe move found among the candidates.')
    return '\n'.join(lines)

def compact_facts(data):
    cong = data['congestion']
    if 'error' in cong or not cong['congested']:
        return None
    c = max(cong['congested'], key=lambda x: x['avg_util'])
    lines = [f"Congested link {c['link']}: util {c['avg_util']:.0%}, queue {c['max_queue_pkts']} pkts. Flows: "
             + ', '.join(f"{f['src']}->{f['dst']} {f['proto']}" for f in c['flows_using_link'])]
    best = best_move(data)
    if best:
        lines.append(f"Best safe move: {best['flow']}, command: {best['command']}")
    else:
        lines.append('No safe move exists.')
    return '\n'.join(lines)

def explain(facts):
    payload = {'model': MODEL, 'stream': True, 'keep_alive': '5m',
               'messages': [{'role': 'system', 'content': SYSTEM_PIPE},
                            {'role': 'user', 'content': facts}],
               'options': {'temperature': 0, 'num_ctx': 1024, 'num_predict': 100, 'num_thread': 2}}
    req = urllib.request.Request(OLLAMA + '/api/chat', data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    t0, first = time.time(), True
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            if not line.strip():
                continue
            d = json.loads(line)
            tok = d.get('message', {}).get('content', '')
            if tok and first:
                print(f'[first token after {time.time() - t0:.0f}s]')
                first = False
            print(tok, end='', flush=True)
            if d.get('done'):
                break
    print()

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui')

def explain_text(facts):
    payload = {'model': MODEL, 'stream': False, 'keep_alive': '5m',
               'messages': [{'role': 'system', 'content': SYSTEM_PIPE},
                            {'role': 'user', 'content': facts}],
               'options': {'temperature': 0, 'num_ctx': 1024, 'num_predict': 100, 'num_thread': 2}}
    req = urllib.request.Request(OLLAMA + '/api/chat', data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)['message']['content'].strip()

def build_recommendation(data):
    cong, rec = data['congestion'], {'ts': time.time()}
    if 'error' in cong:
        rec.update(state='waiting', summary=cong['error'])
    elif not cong['congested']:
        rec.update(state='ok', summary='No sustained congestion in the last 10 s.')
    else:
        best = best_move(data)
        rec.update(state='congested', summary=plain_report(data),
                   move=None if not best else {'flow': best['flow'], 'command': best['command'],
                                               'before': best.get('max_overload_before'),
                                               'after': best.get('max_overload_after')})
    return rec

_expl = {'key': None, 'text': '', 'busy': False}

def _explain_bg(key, facts):
    try:
        text = explain_text(facts)
    except Exception:
        text = ''
    _expl.update(key=key, text=text, busy=False)

def serve(use_llm=True):
    threading.Thread(target=poll, daemon=True).start()
    time.sleep(6)
    print('Autonomous mode: updating ui/recommendation.json every 5 s (Ctrl+C to stop)')
    path, last = os.path.join(UI_DIR, 'recommendation.json'), None
    while True:
        try:
            data = analyze()
            rec = build_recommendation(data)
            key = (rec.get('move') or {}).get('command')
            if use_llm and key and key != _expl['key'] and not _expl['busy']:
                _expl['busy'] = True
                threading.Thread(target=_explain_bg, args=(key, compact_facts(data)), daemon=True).start()
            if key and _expl['key'] == key and _expl['text']:
                rec['explanation'] = _expl['text']
            with open(path + '.tmp', 'w') as f:
                json.dump(rec, f)
            os.replace(path + '.tmp', path)
            if (rec['state'], key) != last:
                print(time.strftime('%H:%M:%S'), rec['summary'])
                last = (rec['state'], key)
        except Exception as e:
            print('error:', e)
        time.sleep(5)

def main():
    if '--serve' in sys.argv:
        serve(use_llm='--no-llm' not in sys.argv)
        return
    threading.Thread(target=poll, daemon=True).start()
    time.sleep(6)                                   # let a few samples accumulate
    data = analyze()
    print(plain_report(data))                       # instant, no LLM
    if '--plain' in sys.argv:
        return
    facts = compact_facts(data)
    if not facts:
        return
    print('\nAsking the local model for an explanation (slow; Ctrl+C to skip)...')
    try:
        explain(facts)
    except KeyboardInterrupt:
        print('\n(skipped)')
    except Exception as e:
        print(f'\n[LLM unavailable: {e}]')

if __name__ == '__main__':
    main()
