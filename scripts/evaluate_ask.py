#!/usr/bin/env python3
"""Run final-answer evaluations; release scoring requires independent, bound human labels.

Gold/labels are NEVER sent to /ask. Run this on development cases for model selection;
freeze code/model/corpus before a fresh, independently authored holdout run.
"""
import argparse
import collections
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import time
import urllib.error
import urllib.request


def digest(data):
    return hashlib.sha256(data).hexdigest()


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def wilson(correct, total):
    if not total:
        return 0.0
    z = 1.959963984540054
    p = correct / total
    return (p + z*z/(2*total) - z*math.sqrt(p*(1-p)/total + z*z/(4*total*total))) / (1+z*z/total)


def summarize(results, judgments, manifest, prices=None):
    actual = {r['id']: r for r in results}
    labels = {j['id']: j for j in judgments}
    if len(actual) != len(results) or len(labels) != len(judgments) or actual.keys() != labels.keys():
        raise ValueError('Require exactly one independent judgment per result, with identical IDs')
    counts = collections.Counter()
    breakdown = collections.defaultdict(collections.Counter)
    usage = collections.defaultdict(collections.Counter)
    latencies = []
    costs_complete = True
    cost = 0.0
    missing_usage_calls = 0
    unpriced_usage_records = 0
    for identity, row in actual.items():
        if digest(json.dumps(row['response'],sort_keys=True,separators=(',',':')).encode()) != row['response_sha256']:
            raise ValueError(f'Response changed after run: {identity}')
        label = labels[identity]
        if label.get('response_sha256') != row['response_sha256']:
            raise ValueError(f'Judgment not bound to this exact response: {identity}')
        if label.get('reviewer', '').strip() == '' or any(type(label.get(k)) is not bool for k in ['correct', 'citations_correct', 'unsupported_claims']):
            raise ValueError(f'Require reviewer and boolean correct/citations_correct/unsupported_claims: {identity}')
        if not isinstance(label.get('notes'), str):
            raise ValueError(f'Require notes explaining source checks: {identity}')
        response = row['response']
        answered = row['http_status'] == 200 and response.get('complete') is True and response.get('status') == 'answered'
        # An answerable question's abstention, provider error or truncation is always a miss.
        correct = label['correct'] and (answered if row['class'] == 'answerable' else not answered)
        counts['total'] += 1
        counts['correct'] += correct
        counts[row['class']] += 1
        counts[row['class']+'_correct'] += correct
        counts['answered'] += answered
        counts['citation_correct_answers'] += answered and label['citations_correct']
        counts['answers_without_unsupported_claims'] += answered and not label['unsupported_claims']
        for group in ['domain:'+row.get('domain','unspecified'), 'category:'+row.get('category','unspecified')]:
            breakdown[group]['total']+=1
            breakdown[group]['correct']+=correct
            if row['class']=='answerable':
                breakdown[group]['answerable']+=1
                breakdown[group]['answerable_correct']+=correct
        latencies.append(row['elapsed_seconds'])
        for call in response.get('usage', []):
            model = call.get('resolved_model') or call['model']
            tokens = call.get('usage') or {}
            incoming, outgoing = tokens.get('input_tokens', 0), tokens.get('output_tokens', 0)
            cached = (tokens.get('input_tokens_details') or {}).get('cached_tokens', 0)
            writes = (tokens.get('input_tokens_details') or {}).get('cache_write_tokens', 0)
            usage[model].update(input_tokens=incoming, cached_input_tokens=cached, cache_write_input_tokens=writes, output_tokens=outgoing)
            rate = (prices or {}).get(model)
            if (rate is None or 'input_tokens' not in tokens or 'output_tokens' not in tokens
                    or min(incoming, outgoing, cached, writes) < 0 or cached+writes > incoming
                    or writes and 'cache_write_per_million' not in rate):
                costs_complete = False
                unpriced_usage_records += 1
            else:
                cost += ((incoming-cached-writes)*rate['input_per_million']
                         + cached*rate.get('cached_input_per_million', rate['input_per_million'])
                         + writes*rate.get('cache_write_per_million', 0)
                         + outgoing*rate['output_per_million'])/1e6
        if response.get('model_calls', 0) != len(response.get('usage', [])):
            costs_complete = False
            missing_usage_calls += max(0,response.get('model_calls',0)-len(response.get('usage',[])))
    n = counts['answerable']
    accuracy = counts['answerable_correct']/n if n else 0
    lower = wilson(counts['answerable_correct'], n)
    overall = counts['correct']/counts['total'] if counts['total'] else 0
    citation_rate = counts['citation_correct_answers']/counts['answered'] if counts['answered'] else 0
    supported_rate = counts['answers_without_unsupported_claims']/counts['answered'] if counts['answered'] else 0
    refusal_rate = counts['unsupported_correct']/counts['unsupported'] if counts['unsupported'] else 0
    ambiguity_rate = counts['ambiguous_correct']/counts['ambiguous'] if counts['ambiguous'] else 0
    provenance = manifest.get('provenance', {})
    # A gate is not evidence of independent authorship: the signed protocol/review is also required.
    prerequisites = (manifest.get('partition') == 'holdout' and manifest.get('independent_holdout_attestation') is True
                     and len(set(manifest.get('domains', []))) >= 2
                     and all(provenance.get(k) for k in ['binary_sha256', 'corpus_manifest_sha256', 'model_configuration', 'holdout_author', 'review_protocol']))
    passed = (prerequisites and n >= 1000 and counts['unsupported'] >= 300 and counts['ambiguous'] >= 200
              and accuracy >= .985 and lower > .97 and overall >= .985
              and citation_rate >= .99 and supported_rate >= .99 and refusal_rate >= .99 and ambiguity_rate >= .99)
    latencies.sort()
    mean_cost = cost/counts['total'] if costs_complete and counts['total'] else None
    return {'counts': dict(counts), 'breakdown':{k:dict(v) for k,v in breakdown.items()},'answerable_accuracy': accuracy, 'answerable_wilson_95_lower': lower,
            'overall_accuracy': overall, 'citation_accuracy_on_answers': citation_rate,
            'answers_without_unsupported_claims': supported_rate, 'unsupported_correct_rate': refusal_rate,
            'ambiguous_correct_rate': ambiguity_rate, 'release_gate_passed': bool(passed),
            'release_gate_prerequisites_present': bool(prerequisites), 'usage_by_resolved_model': dict(usage),
            'latency_seconds': {'p50': latencies[len(latencies)//2] if latencies else None,
                                'p95': latencies[min(len(latencies)-1, math.ceil(len(latencies)*.95)-1)] if latencies else None},
            'priced_api_cost_usd': cost if costs_complete else None,
            'priced_usage_subtotal_usd': cost,
            'provider_calls_without_usage': missing_usage_calls,
            'usage_records_without_complete_pricing': unpriced_usage_records,
            'projected_1000_per_day_usd': mean_cost*1000 if mean_cost is not None else None,
            'projected_30_day_usd': mean_cost*30000 if mean_cost is not None else None,
            'cost_note': 'Projection uses measured calls and supplied dated rates; excludes storage, hosting, ingestion and evaluation. Null means incomplete usage/pricing, not free.'}


def run(args):
    cases = rows(args.cases)
    questions = [' '.join(c['question'].lower().split()) for c in cases]
    if len({c['id'] for c in cases}) != len(cases) or len(set(questions)) != len(cases):
        raise ValueError('Case IDs and normalized questions must be unique')
    if not cases or any(c.get('class') not in ['answerable','unsupported','ambiguous'] or not c.get('domain') or not c.get('question') for c in cases):
        raise ValueError('Each case requires id, question, domain and class answerable/unsupported/ambiguous')
    if args.partition == 'holdout' and not args.provenance:
        raise ValueError('Holdout run requires --provenance documenting frozen code/corpus/models and independent authorship')
    if args.partition == 'holdout' and any(c.get('development_only') for c in cases):
        raise ValueError('Previously inspected development cases cannot be relabeled as an independent holdout')
    provenance = json.loads(Path(args.provenance).read_text()) if args.provenance else {}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'partition':args.partition,
                'cases_sha256':digest(Path(args.cases).read_bytes()), 'domains': sorted({c['domain'] for c in cases}),
                'provenance':provenance, 'independent_holdout_attestation':provenance.get('independent_holdout_attestation', False)}
    # Exclusive creation prevents accidentally replacing an existing run or reusing old labels.
    with path.open('x') as output:
        manifest_path = Path(str(path)+'.manifest.json')
        with manifest_path.open('x') as handle:
            handle.write(json.dumps(manifest,indent=2)+'\n')
        for case in cases:
            headers = {'Content-Type':'application/json'}
            if os.environ.get('JBRAIN_TOKEN'):
                headers['Authorization'] = 'Bearer '+os.environ['JBRAIN_TOKEN']
            request = urllib.request.Request(args.url.rstrip('/')+'/v1/ask',
                      data=json.dumps({'question':case['question']}).encode(), headers=headers)
            start = time.perf_counter()
            response = None
            status, value = None, None
            try:
                response = urllib.request.urlopen(request, timeout=200)
            except urllib.error.HTTPError as error:
                response = error
            except OSError:
                status, value = 0, {'error':{'code':'CONNECTION_FAILED'}}
            if response is not None:
                with response:
                    status, value = response.status, json.load(response)
            # The source gold stays in the cases file. Reports only include the model's actual result.
            serialized = json.dumps(value, sort_keys=True, separators=(',',':')).encode()
            row = {'id':case['id'],'question':case['question'],'domain':case['domain'],'class':case['class'],
                   'category':case.get('category','unspecified'),'http_status':status,'response':value,
                   'response_sha256':digest(serialized),'elapsed_seconds':time.perf_counter()-start}
            output.write(json.dumps(row)+'\n'); output.flush()
            print(f"{case['id']}: HTTP {status}, {value.get('status', 'error')}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    execute = commands.add_parser('run')
    execute.add_argument('--cases',required=True)
    execute.add_argument('--output',required=True)
    execute.add_argument('--url',default=os.environ.get('JBRAIN_URL','http://127.0.0.1:7331'))
    execute.add_argument('--partition',choices=['development','holdout'],default='development')
    execute.add_argument('--provenance')
    score = commands.add_parser('score')
    score.add_argument('--results',required=True)
    score.add_argument('--judgments',required=True)
    score.add_argument('--prices',help='Dated per-model USD rates, keyed by resolved model ID')
    args = parser.parse_args()
    if args.command == 'run':
        run(args)
    else:
        manifest=json.loads(Path(args.results+'.manifest.json').read_text())
        summary=summarize(rows(args.results),rows(args.judgments),manifest,json.loads(Path(args.prices).read_text()) if args.prices else None)
        print(json.dumps(summary,indent=2))


if __name__ == '__main__':
    main()
