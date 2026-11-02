#!/usr/bin/env python3
"""Typed backend diagnostic on the OLD development EPL cases, using oracle interpretations.

This tests evidence availability/computation, NOT NLP, model selection, final answers, or >97%.
Gold values remain outside the service. Existing frozen v1 reports are never overwritten.
"""
import argparse
import collections
import contextlib
import hashlib
import json
from pathlib import Path
import time
import urllib.request
import sys
from epl_trial import NAMES, season, slug

ROOT=Path(__file__).resolve().parents[1]


class Diagnostic:
    def __init__(self,url):
        self.url=url
        self.calls=0
        self.times=[]

    def call(self,tool,**arguments):
        request=urllib.request.Request(self.url+'/v1/evidence',data=json.dumps({'tool':tool,'arguments':arguments}).encode(),headers={'Content-Type':'application/json'})
        start=time.perf_counter()
        with urllib.request.urlopen(request,timeout=120) as response:value=json.load(response)
        self.times.append((time.perf_counter()-start)*1000); self.calls+=1
        return value

    def find_read(self,kind,period=None,date=None,entities=None,filters=None):
        found=self.call('find',text='',scope={'kind':kind,'period':period,'date':date,'entities':entities or [],'filters':filters or []},limit=2)
        assert len(found['results'])==1 and not found['has_more'],found
        return self.call('read',id=found['results'][0]['id'],offset=0,length=16000)

    def match(self,need):
        return self.find_read('Match',date=need['date'],entities=[NAMES.get(need['source_home'],need['source_home']),NAMES.get(need['source_away'],need['source_away'])])

    def stat(self,need,club=None):
        doc=self.find_read('ClubSeasonSummary',period=season(need['year']),entities=[club or need['club']])
        return next(f['value'] for f in doc['facts'] if f['metric']==need['metric'])

    def subset(self,need,outcome=None):
        request={'operation':'matches','scope':{'kind':'Match','period':season(need['year']),'date':None,'entities':[need['club']],
                 'filters':[{'field':'dataset','value':'epl-results-v1'}]},'metric':None,'basis':None,'club':need['club'],
                 'venue':need.get('side','all'),'outcome':outcome or {'won':'win','drawn':'draw','lost':'loss'}.get(need['metric'],'all')}
        result=self.call('compute',**request)
        return result['result']['count' if need['metric'] in ['won','drawn','lost'] or outcome else need['metric']]

    def league(self,need):
        scope={'kind':'ClubSeasonSummary','period':season(need['year']),'date':None,'entities':[],
               'filters':[{'field':'dataset','value':'epl-results-v1'}]}
        found=self.call('find',text='',scope=scope,limit=1)
        sample=self.call('read',id=found['results'][0]['id'],offset=0,length=16000)
        basis=next(f['basis'] for f in sample['facts'] if f['metric']==need['metric'])
        result=self.call('compute',operation='rank',scope=scope,metric=need['metric'],basis=basis,club=None,venue=None,outcome=None)
        return {row['id'].rsplit('/',1)[-1]:row['value'] for row in result['result']}

    def case(self,case,gold):
        category=case['category']
        if category=='implicit_entity':
            first=case['needs'][0]
            # Only the explicit host/date are supplied. The opponent for the second tool is read from evidence.
            doc=self.find_read('Match',date=first['date'],filters=[{'field':'home_team','value':NAMES.get(first['source_home'],first['source_home'])}])
            opponent=doc['metadata']['away_team']
            return [opponent,self.stat(case['needs'][1],club=opponent)],[gold[0]['expected_value']['away'],gold[1]['expected_value']]
        if category=='negation':
            return self.subset(case['needs'][0],outcome='not_loss'),sum(g['expected_value'] for g in gold)
        actual=[];expected=[]
        for need,group in zip(case['needs'],gold):
            if need['kind']=='season_stat':value=self.stat(need)
            elif need['kind']=='planet':
                doc=self.find_read('PlanetFact',entities=[need['body']])
                fact=next(f for f in doc['facts'] if f['metric']==need['metric'])
                actual.append({'value':fact['value'],'basis':fact['basis']})
                expected.append({'value':group['expected_value'],'basis':group['basis']});continue
            elif need['kind']=='match':
                doc=self.match(need); score=next(f['value'] for f in doc['facts'] if f['metric']=='score').split('-')
                value={'home':doc['metadata']['home_team'],'away':doc['metadata']['away_team'],'home_value':int(score[0]),'away_value':int(score[1])}
            elif need['kind']=='subset':value=self.subset(need)
            elif need['kind']=='league_table':
                value=self.league(need)
                actual.append(value);expected.append({slug(k):v for k,v in group['expected_value'].items()});continue
            else:raise ValueError(need['kind'])
            actual.append(value);expected.append(group['expected_value'])
        return actual,expected


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:7331')
    parser.add_argument('--gold',type=Path,default=ROOT/'.jbrain/hard-eval-v1/gold.json')
    parser.add_argument('--domain',choices=['epl','nasa'],default='epl')
    parser.add_argument('--nasa-html',type=Path,default=ROOT/'.jbrain/hard-eval-v1/sources/planet-facts.html')
    parser.add_argument('--output',type=Path,default=ROOT/'.jbrain/evidence-v2/typed-epl.json')
    args=parser.parse_args()
    fixture=ROOT/'tests/fixtures/hard-retrieval-v1.json'
    cases=json.loads(fixture.read_text())[args.domain];gold=json.loads(args.gold.read_text())
    results=[]
    with contextlib.ExitStack() as stack:
        url=args.url
        if args.domain=='nasa':
            from hard_eval import planet_data,planet_documents
            sys.path.insert(0,str(ROOT/'tests'))
            import native_suite
            native_suite.BINARY=str(ROOT/'target/release/jbrain')
            service=stack.enter_context(native_suite.Service(OPENAI_API_KEY='',JBRAIN_API_KEY_FILE='',JBRAIN_MODEL=''))
            for identity,doc in planet_documents(planet_data(args.nasa_html.read_bytes())).items():service.put(identity,doc['markdown'])
            url=service.url
        diagnostic=Diagnostic(url)
        for case in cases:
            if not case['needs']:continue
            start=time.perf_counter()
            try:
                actual,expected=diagnostic.case(case,gold[case['id']]);correct=actual==expected;error=None
            except Exception as exc:
                actual,expected,correct,error=None,None,False,str(exc)
            results.append({'id':case['id'],'category':case['category'],'correct':correct,'actual':actual,'expected':expected,'error':error,'elapsed_ms':(time.perf_counter()-start)*1000})
    categories=collections.defaultdict(list)
    for row in results:categories[row['category']].append(row)
    output={'diagnostic':'Oracle-interpreted typed backend only. Old development cases, not end-to-end accuracy.','domain':args.domain,
            'release_binary_sha256':hashlib.sha256((ROOT/'target/release/jbrain').read_bytes()).hexdigest(),
            'fixture_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),'cases':len(results),'correct':sum(r['correct'] for r in results),
            'categories':{k:{'correct':sum(r['correct'] for r in v),'cases':len(v)} for k,v in categories.items()},
            'http_calls':diagnostic.calls,'model_calls':0,'rows':results}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as handle:handle.write(json.dumps(output,indent=2)+'\n')
    print(json.dumps({k:v for k,v in output.items() if k!='rows'},indent=2))


if __name__=='__main__':main()
