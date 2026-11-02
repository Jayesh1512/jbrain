#!/usr/bin/env python3
"""Freeze question-only inputs and separate gold for the real /ask baseline.

No model is called, no credential is read, and the live corpus is never modified.
Run evaluate_ask.py against cases.jsonl after configuring the daemon privately.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def prepare(fixture, trial, binary, output):
    fixture = Path(fixture)
    trial, binary, output = Path(trial), Path(binary), Path(output)
    original = fixture.read_bytes()
    cases = json.loads(original)
    if not cases or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Require a nonempty frozen challenge with unique case IDs.')
    categories = {
        'away_rank':'grouped_aggregation', 'calendar':'date_range', 'halftime':'conditional_aggregation',
        'unbeaten':'negation_subset', 'two_seasons':'multiple_periods', 'home_compare':'venue_comparison',
        'three_facts':'multiple_metrics', 'opponent':'linked_reference', 'away_losses':'venue_comparison',
        'split_draws':'venue_partition', 'negation':'negation_subset', 'zero':'venue_subset',
        'ties':'complete_comparator_set', 'sizes':'historical_grain', 'cross_compare':'multiple_periods',
        'premise':'false_premise',
    }
    inputs, gold = [], []
    for case in cases:
        if case['class'] not in ['answerable', 'unsupported', 'ambiguous']:
            raise ValueError('Invalid question class.')
        # Allowlist only; expected values and requirements cannot enter the evaluator's input file.
        inputs.append({'id':case['id'], 'question':case['question'], 'class':case['class'],
                       'domain':'epl', 'category':categories.get(case['id'],case['class']),
                       'development_only':True})
        gold.append({'id':case['id'], 'expected':case['expected'],
                     'source_requirements':case['requirements'], 'source_fixture_sha256':sha(original)})
    question_data=('\n'.join(json.dumps(c,ensure_ascii=False) for c in inputs)+'\n').encode()
    gold_data=('\n'.join(json.dumps(c,ensure_ascii=False) for c in gold)+'\n').encode()
    sources=json.loads((trial/'sources/manifest.json').read_text())
    for source in sources:
        if sha((trial/'sources'/source['file']).read_bytes()) != source['sha256']:
            raise ValueError('Frozen source CSV changed.')
    canonical=hashlib.sha256()
    knowledge=trial/'brain/knowledge'
    documents=0
    for path in sorted(knowledge.rglob('*.md')):
        canonical.update(str(path.relative_to(knowledge)).encode()+b'\0')
        canonical.update(hashlib.sha256(path.read_bytes()).digest())
        documents+=1
    session=json.loads((trial/'session.json').read_text())
    binary_hash=sha(binary.read_bytes())
    if binary_hash != session['binary_sha256']:
        raise ValueError('Built binary differs from the recorded running service.')
    manifest={
        'prepared_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'partition':'development', 'development_only':True,
        'source_fixture_sha256':sha(original), 'cases_sha256':sha(question_data), 'gold_sha256':sha(gold_data),
        'binary_sha256':binary_hash, 'canonical_corpus_sha256':canonical.hexdigest(),
        'source_manifest_sha256':sha((trial/'sources/manifest.json').read_bytes()),
        'canonical_documents':documents, 'cases':len(cases),
        'classes':{k:sum(c['class']==k for c in cases) for k in ['answerable','unsupported','ambiguous']},
        'daemon_url':session['url'], 'model_configuration':None,
        'real_model_run_status':'pending_provider_configuration',
        'accuracy':None, 'actual_model_calls':0,
        'protocol':{
            'request':'Only question is sent to /v1/ask. No gold, typed plan, source requirements or external calculations.',
            'grading':'Check all subjects, scope, requested fields, arithmetic and cited source support. Independent review is required.',
            'repetitions':'This first baseline is one pass. Later repeats measure stability, not additional independent questions.',
            'release_gate_eligible':False,
        },
    }
    # Exclusive directory creation preserves each prior frozen run.
    output.mkdir(parents=True, exist_ok=False)
    (output/'cases.jsonl').write_bytes(question_data)
    (output/'gold.jsonl').write_bytes(gold_data)
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',type=Path,default=ROOT/'.jbrain/challenge-v3/frozen-cases.json')
    parser.add_argument('--trial',type=Path,default=ROOT/'.jbrain/epl-trial')
    parser.add_argument('--binary',type=Path,default=ROOT/'target/release/jbrain')
    parser.add_argument('--output',type=Path,default=ROOT/'.jbrain/live-baseline-v1')
    args=parser.parse_args()
    print(json.dumps(prepare(args.fixture,args.trial,args.binary,args.output),indent=2))


if __name__=='__main__':
    main()
