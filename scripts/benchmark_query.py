#!/usr/bin/env python3
"""Evaluate the automatic query API against source-derived evidence, no prepared plans."""
import argparse
import collections
import concurrent.futures
import hashlib
import json
import random
import threading
import time
import urllib.request
from pathlib import Path
from benchmark_epl_retrieval import read_sources, load_documents, build_gold, evidence_score, distribution
from epl_trial import sample

ROOT = Path(__file__).resolve().parents[1]

def call(url, question):
    begun = time.perf_counter()
    req = urllib.request.Request(url + '/v1/query', data=json.dumps({'question': question, 'limit': 5}).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        data = response.read()
        result = json.loads(data)
    return result, (time.perf_counter() - begun) * 1000, len(data)


def evaluate(url, fixture, sources, docs):
    gold = build_gold(fixture, sources, docs)
    rows=[]
    for case in fixture['cases']:
        result, ms, size = call(url, case['question'])
        groups=gold[case['id']]
        ranked=[r['id'] for r in result['results']]
        for row in result['results']:
            if row['revision'] != docs[row['id']]['revision']:
                raise ValueError('Revision changed: ' + row['id'])
        verified=[]
        for group in groups:
            found=[]
            for row in result['results']:
                if row['id'] in group['candidates']:
                    # Assess actual response evidence, not just a relevant document ID.
                    quote = group['candidates'][row['id']][0]
                    if quote not in row['excerpt']:
                        continue
                    metric=group['need']['metric']
                    matching=[f for f in row['facts'] if f['metric']==metric]
                    expected=group['expected_value']
                    if isinstance(expected,dict):
                        if metric in ('score','half_time'):
                            expected=f"{expected['home_value']}-{expected['away_value']}"
                        else:
                            expected=f"{expected['home']} {expected['home_value']}, {expected['away']} {expected['away_value']}"
                    if matching and matching[0]['value']==expected:
                        found.append(row['id'])
            verified.append(found)
        rows.append({'id':case['id'],'category':case['category'],'question':case['question'],'metrics':evidence_score(ranked,groups),'verified_evidence':verified,'all_response_facts_correct':all(verified) if groups else None,'response_ms':ms,'bytes':size,'response':result})
    positive=[r for r in rows if r['metrics'] is not None]
    unknown=[r for r in rows if r['metrics'] is None]
    summary={'queries':len(rows),'answerable':len(positive),'unsupported':len(unknown),'complete_document_evidence_at_5':sum(r['metrics']['complete_evidence@5'] for r in positive),'complete_response_evidence':sum(r['all_response_facts_correct'] for r in positive),'reported_complete_positive':sum(r['response']['complete'] for r in positive),'unsupported_correctly_not_complete':sum(not r['response']['complete'] for r in unknown),'unsupported_with_related_candidates':sum(bool(r['response']['results']) for r in unknown)}
    return {'summary':summary,'rows':rows}


def timed(url,pid,cases,workers,repeats):
    for case in cases:
        call(url,case['question'])
    jobs=cases*repeats
    random.Random(1701).shuffle(jobs)
    samples=[sample(pid)]
    stop=threading.Event()
    def monitor():
        while not stop.wait(.1):
            samples.append(sample(pid))
    watcher=threading.Thread(target=monitor,daemon=True)
    watcher.start()
    begun=time.perf_counter()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            measured=list(pool.map(lambda case:call(url,case['question']),jobs))
        elapsed=time.perf_counter()-begun
    finally:
        stop.set()
        watcher.join()
    samples.append(sample(pid))
    return {'concurrent_clients':workers,'workflows':len(jobs),'http_calls_per_query':1,'errors':0,'elapsed_seconds':elapsed,'queries_per_second':len(jobs)/elapsed,'response_ms':distribution([r[1] for r in measured]),'mean_response_bytes':sum(r[2] for r in measured)/len(measured),'max_sampled_rss_mib':max(s['rss_mib'] for s in samples),'service_cpu_seconds':samples[-1]['cpu_seconds']-samples[0]['cpu_seconds']}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,default=ROOT/'.jbrain/epl-trial')
    p.add_argument('--url')
    p.add_argument('--pid',type=int)
    p.add_argument('--repeats',type=int,default=5)
    p.add_argument('--output',type=Path,default=ROOT/'.jbrain/epl-trial/query-eval/results.json')
    args=p.parse_args()
    session=json.loads((args.root/'session.json').read_text())
    url=args.url or session['url'];pid=args.pid or session['pid']
    docs=load_documents(args.root);sources=read_sources(args.root)
    names=['epl-retrieval-v1.json','epl-query-v2-validation.json']
    fixtures=[json.loads((ROOT/'tests/fixtures'/name).read_text()) for name in names]
    result={'protocol':'automatic deterministic POST /v1/query; limit 5; no plans, embeddings or model calls','binary_sha256':hashlib.sha256((ROOT/'target/release/jbrain').read_bytes()).hexdigest(),'corpus_sha256':hashlib.sha256((args.root/'documents.jsonl').read_bytes()).hexdigest(),'fixtures_sha256':{name:hashlib.sha256((ROOT/'tests/fixtures'/name).read_bytes()).hexdigest() for name in names},'quality':{name:evaluate(url,f,sources,docs) for name,f in zip(names,fixtures)},'latency':[]}
    print(json.dumps({k:v['summary'] for k,v in result['quality'].items()},indent=2),flush=True)
    for workers in [1,4,16]:
        row=timed(url,pid,fixtures[0]['cases'],workers,args.repeats)
        result['latency'].append(row)
        print(json.dumps(row),flush=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    compact={**result,'quality':{k:{'summary':v['summary'],'failures':[dict(id=r['id'],question=r['question'],plan=r['response']['plan'],status=r['response']['status'],warnings=r['response']['warnings']) for r in v['rows'] if r['metrics'] is not None and (not r['all_response_facts_correct'] or not r['response']['complete'])]} for k,v in result['quality'].items()}}
    (ROOT/'docs/evals/epl-query-v2-summary.json').write_text(json.dumps(compact,indent=2)+'\n')

if __name__=='__main__':
    main()
