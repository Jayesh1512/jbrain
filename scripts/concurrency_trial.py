#!/usr/bin/env python3
"""Isolated storage integrity at 1/4/16 concurrent HTTP clients, without model inference."""
import argparse
import concurrent.futures
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
import native_suite
from native_suite import http,md,write_input
from benchmark import sample


def document(number):
    return md(f'Counter: {number}',f'entity: Shared counter\nfacts: {{count: {number}}}\n')


def read(service):
    status,value=http(service.url,'/v1/concept?id=counter')
    assert status==200,(status,value)
    number=value['metadata']['facts']['count']
    assert value['body'].strip()==f'Counter: {number}'
    assert hashlib.sha256(value['markdown'].encode()).hexdigest()==value['revision']
    return number,value['revision']


def block(clients,increments):
    # Independent disposable brain: the EPL corpus and its owning process are untouched.
    with native_suite.Service(OPENAI_API_KEY='',JBRAIN_API_KEY_FILE='',JBRAIN_MODEL='') as service:
        service.put('counter',document(0))
        barrier=threading.Barrier(clients)
        def worker(index):
            conflicts=0;duplicates=0;latencies=[]
            barrier.wait(timeout=20)
            for iteration in range(increments):
                for _ in range(2000):
                    old,revision=read(service)
                    payload=write_input('counter',document(old+1),actor=f'concurrency/{index}',expectedRevision=revision)
                    start=time.perf_counter();status,receipt=http(service.url,'/v1/operations',payload)
                    latencies.append((time.perf_counter()-start)*1000)
                    if status==409:
                        conflicts+=1
                        time.sleep((index%4+1)*.0005)
                        continue  # A new read/merge and fresh request ID, never a stale overwrite.
                    assert status==200,(status,receipt)
                    current,_=read(service)
                    assert current>=old+1  # Other commits may already have advanced the value.
                    if iteration%3==0:
                        again_status,again=http(service.url,'/v1/operations',payload)
                        assert again_status==200 and again==receipt
                        duplicates+=1
                    break
                else:
                    raise AssertionError('CAS retries exhausted')
            return {'successes':increments,'conflicts':conflicts,'duplicate_receipts_checked':duplicates,'write_attempt_latency_ms':latencies}
        started=time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as pool:
            rows=list(pool.map(worker,range(clients)))
        elapsed=time.perf_counter()-started
        actual,_=read(service);expected=clients*increments
        assert actual==expected,(actual,expected)
        status,health=http(service.url,'/health')
        assert status==200 and health['pendingWrites']==0
        latencies=sorted(t for row in rows for t in row['write_attempt_latency_ms'])
        return {'concurrent_http_clients':clients,'expected_final_count':expected,'actual_final_count':actual,
                'integrity_passed':True,'successful_increments':sum(r['successes'] for r in rows),
                'cas_conflicts_safely_retried':sum(r['conflicts'] for r in rows),
                'duplicate_receipts_checked':sum(r['duplicate_receipts_checked'] for r in rows),
                'wall_seconds':elapsed,'write_attempt_latency_ms':{'p50':statistics.median(latencies),'p95':latencies[math.ceil(len(latencies)*.95)-1]},
                'service_after_workload':sample(service.proc.pid),'model_calls':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',type=Path,default=ROOT/'target/release/jbrain')
    parser.add_argument('--increments',type=int,default=10)
    parser.add_argument('--output',type=Path,default=ROOT/'.jbrain/live-baseline-v1/storage-concurrency.json')
    args=parser.parse_args()
    if not 1<=args.increments<=100:parser.error('Use 1..100 increments per client.')
    native_suite.BINARY=str(args.binary.resolve())
    results=[block(clients,args.increments) for clients in [1,4,16]]
    output={'scope':'Storage only: concurrent HTTP clients, not full LLM agents. Independent temporary brain per block. Model inference, model queues and answer accuracy untested.',
            'binary_sha256':hashlib.sha256(args.binary.read_bytes()).hexdigest(),'blocks':results}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as handle:handle.write(json.dumps(output,indent=2)+'\n')
    print(json.dumps(output,indent=2))


if __name__=='__main__':main()
