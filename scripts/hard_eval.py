#!/usr/bin/env python3
"""Frozen adversarial retrieval diagnostic. Never changes/tunes the Rust service.
Source snapshots and every API response are retained; expected values are not sent.
"""
import argparse
import collections
import concurrent.futures
import datetime
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from benchmark_epl_retrieval import read_sources, load_documents, build_group, independently_tally, distribution, slug
from epl_trial import sample

ROOT=Path(__file__).resolve().parents[1]
NASA='https://nssdc.gsfc.nasa.gov/planetary/factsheet/'
NOTES=NASA+'planetfact_notes.html'
BINARY=ROOT/'target/release/jbrain'


def sha(data): return hashlib.sha256(data).hexdigest()
def http(url,path,payload=None):
    req=urllib.request.Request(url+path,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=40) as r:return r.status,json.load(r)
    except urllib.error.HTTPError as e:return e.code,json.load(e)


class Table(HTMLParser):
    def __init__(self):
        super().__init__();self.rows=[];self.row=None;self.cell=None
    def handle_starttag(self,tag,attrs):
        if tag=='tr': self.row=[]
        if tag=='td' and self.row is not None:self.cell=[]
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag=='td' and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()));self.cell=None
        if tag=='tr' and self.row is not None:
            self.rows.append(self.row);self.row=None


# Stable numerical fields; no inference of unknown pressure or magnetic field.
FIELDS=[('Mass','mass','10^24 kg'),('Diameter','diameter','km_equatorial'),('Density','density','kg_m3'),('Gravity','gravity','m_s2_surface_or_1bar'),('Escape Velocity','escape_velocity','km_s'),('Rotation Period','rotation_period','hours_signed_sidereal'),('Length of Day','length_of_day','hours_solar'),('Distance from Sun','distance_from_sun','million_km'),('Orbital Period','orbital_period','days_tropical_or_sidereal'),('Mean Temperature','mean_temperature','degrees_C'),('Surface Pressure','surface_pressure','bar'),('Number of Moons','number_of_moons','confirmed_as_of_2025_03_18')]


def planet_data(data):
    table=Table();table.feed(data.decode())
    rows=[r for r in table.rows if len(r)==11]
    if not rows or rows[0][1:]!=['MERCURY','VENUS','EARTH','MOON','MARS','JUPITER','SATURN','URANUS','NEPTUNE','PLUTO']:
        raise ValueError('NASA source columns changed')
    planets={name.title():{} for name in rows[0][1:]}
    for label,metric,unit in FIELDS:
        candidates=[r for r in rows if r[0].startswith(label)]
        if len(candidates)!=1:raise ValueError('Unexpected NASA row: '+label)
        row=candidates[0]
        for name,text in zip(planets,row[1:]):
            cleaned=text.replace(',','').replace('*','').strip()
            value=None if cleaned.lower()=='unknown' else float(cleaned)
            if value is not None and value.is_integer():value=int(value)
            actual_unit=unit
            if name=='Moon' and metric=='distance_from_sun':actual_unit='million_km_from_Earth'
            if name=='Moon' and metric=='orbital_period':actual_unit='days_sidereal_around_Earth'
            planets[name][metric]={'value':value,'basis':actual_unit,'raw':text,'label':label}
    return planets


def planet_documents(planets):
    docs={}
    for name,facts in planets.items():
        meta={'type':'Reference','title':name+' NASA planetary facts','entity':name,'document_kind':'PlanetFact','snapshot_date':'2025-03-18','facts':{k:{'value':v['value'],'basis':v['basis']} for k,v in facts.items()},'sources':[{'resource':NASA},{'resource':NOTES}]}
        body='# '+name+'\n\nSnapshot: NASA table last updated 18 March 2025, not a live measurement.\n\n'
        body+='\n'.join(f"{v['label']}: {v['raw']} [{v['basis']}]." for v in facts.values())
        body+='\n\nDiameter is equatorial. Rotation period is signed sidereal rotation; length of day is the solar day. Gravity for gas giants is at 1 bar, not at a solid surface. Unknown pressure is not zero. Moon distance and orbit refer to Earth.\n\n[Definitions](/planetary/definitions.md)\n'
        markdown='---\n'+'\n'.join(f'{k}: {json.dumps(v,ensure_ascii=False)}' for k,v in meta.items())+'\n---\n'+body
        docs['planetary/'+slug(name)]={'markdown':markdown,'revision':sha(markdown.encode()),'body':body,'metadata':meta}
    # Notes are short paraphrases with source links, not model-generated extra facts.
    body='# Definitions and limitations\n\nRotation period is sidereal; length of day is solar. Negative rotation denotes retrograde motion. The Moon distance in this table is from Earth, not from the Sun. Unknown gas-giant surface pressure is not zero. Measurements use table units and rounding; the table is dated 18 March 2025.\n\n'+ '\n'.join(f'[{n}](/planetary/{slug(n)}.md)' for n in planets)
    markdown='---\ntype: Reference\ntitle: NASA table definitions and limitations\nsources: [{resource: '+NOTES+'}]\n---\n'+body
    docs['planetary/definitions']={'markdown':markdown,'body':body,'revision':sha(markdown.encode())}
    return docs


def season(club,source,year,metric):return {'kind':'season_stat','club':club,'source_club':source,'year':year,'metric':metric}
def match(row,metric='score'):
    return {'kind':'match','source_home':row['raw']['HomeTeam'],'source_away':row['raw']['AwayTeam'],'date':row['date'],'metric':metric}
def case(identity,category,question,needs=(),**extra):return dict(id=identity,category=category,question=question,needs=list(needs),**extra)


def epl_cases(sources):
    c=[]
    specs=[('West Bromwich Albion','West Brom',2010,'won'),('Hull City','Hull',2013,'drawn'),('Middlesbrough','Middlesbrough',2016,'goals_against'),('Watford','Watford',2018,'points')]
    for i,(club,raw,year,metric) in enumerate(specs):
        c.append(case(f'control-{i}','control',f'{club} {year}/{str(year+1)[-2:]} '+{'won':'wins','drawn':'draws','goals_against':'goals conceded','points':'points'}[metric],[season(club,raw,year,metric)]))
    for i,(q,n) in enumerate([
        ('How many goals did Stoke City bag in 2014/15?',season('Stoke City','Stoke',2014,'goals_for')),
        ('How often did Newcastle United taste defeat during 2002/03?',season('Newcastle United','Newcastle',2002,'lost')),
        ('Count Blackburn Rovers losses in 2011/12.',season('Blackburn Rovers','Blackburn',2011,'lost')),
        ('Birminghma City victories in 2010/11?',season('Birmingham City','Birmingham',2010,'won')),
        ('In the 2013-2014 campaign, how many points did the Hammers earn?',season('West Ham United','West Ham',2013,'points')),
        ('How many times did Swansea City let one in across 2014/15?',season('Swansea City','Swansea',2014,'goals_against'))]):
        c.append(case(f'language-{i}','language',q,[n]))
    compounds=[
        ('How many goals did Swansea City concede in 2015/16 and how many did West Ham United score in 2020/21?',[season('Swansea City','Swansea',2015,'goals_against'),season('West Ham United','West Ham',2020,'goals_for')]),
        ('Give wins and draws for Norwich City, Watford and Bournemouth in 2015/16.',[season(cl,raw,2015,m) for cl,raw in [('Norwich City','Norwich'),('Watford','Watford'),('Bournemouth','Bournemouth')] for m in ['won','drawn']]),
        ('For 2020/21, Fulham wins; for 2018/19, Huddersfield Town goals scored.',[season('Fulham','Fulham',2020,'won'),season('Huddersfield Town','Huddersfield',2018,'goals_for')]),
        ('Compare Crystal Palace wins and Aston Villa losses in 2019/20, respectively.',[season('Crystal Palace','Crystal Palace',2019,'won'),season('Aston Villa','Aston Villa',2019,'lost')]),
        ('West Bromwich Albion goals scored in 2010/11, goals conceded in 2011/12, and points in 2012/13.',[season('West Bromwich Albion','West Brom',y,m) for y,m in [(2010,'goals_for'),(2011,'goals_against'),(2012,'points')]]),
        ('Compare Norwich City 2011/12 points, Swansea City 2012/13 wins, Stoke City 2013/14 draws, and Sunderland 2014/15 losses.',[season(cl,raw,y,m) for cl,raw,y,m in [('Norwich City','Norwich',2011,'points'),('Swansea City','Swansea',2012,'won'),('Stoke City','Stoke',2013,'drawn'),('Sunderland','Sunderland',2014,'lost')]])]
    for i,(q,ns) in enumerate(compounds):c.append(case(f'compound-{i}','composition',q,ns))
    pairs=[('Arsenal','Tottenham',2012),('Chelsea','Man City',2014),('Liverpool','Everton',2016),('Man United','West Ham',2017)]
    from epl_trial import NAMES
    for i,(a,b,year) in enumerate(pairs):
        games=sorted([r for r in sources if r['year']==year and set([r['raw']['HomeTeam'],r['raw']['AwayTeam']])=={a,b}],key=lambda r:r['date'])
        if len(games)!=2:raise ValueError('Expected two head-to-head fixtures')
        an,bn=NAMES.get(a,a),NAMES.get(b,b)
        c.append(case(f'dates-{i}','multiple_dates',f'Compare the full-time scores in {an} versus {bn} on {games[0]["date"]} and {games[1]["date"]}.',[match(g) for g in games]))
        g=games[0];host=g['raw']['HomeTeam'];opponent=g['raw']['AwayTeam'];host_name=NAMES.get(host,host);other=NAMES.get(opponent,opponent)
        c.append(case(f'implicit-{i}','implicit_entity',f'Who did {host_name} host on {g["date"]}, and how many points did that opponent earn in {year}/{str(year+1)[-2:]}?',[match(g),season(other,opponent,year,'points')]))
    for i,(cl,raw,y,side,metric) in enumerate([('Bournemouth','Bournemouth',2016,'home','won'),('Watford','Watford',2017,'away','goals_for'),('Fulham','Fulham',2013,'home','lost'),('Southampton','Southampton',2019,'away','drawn')]):
        c.append(case(f'split-{i}','subset_statistics',f'How many {dict(won="wins",goals_for="goals",lost="defeats",drawn="draws")[metric]} did {cl} have {"at home" if side=="home" else "away from home"} in {y}/{str(y+1)[-2:]}?',[{'kind':'subset','club':cl,'source_club':raw,'year':y,'side':side,'metric':metric}]))
    for i,(q,y,metric) in enumerate([
        ('Which club had the fewest defeats in 2008/09?',2008,'lost'),
        ('Did Watford have the fewest wins of all clubs in 2019/20?',2019,'won'),
        ('Which club earned the most points in 2000/01?',2000,'points'),
        ('Did Liverpool score more goals than every other club in 2013/14?',2013,'goals_for')]):
        c.append(case(f'ranking-{i}','league_comparison',q,[{'kind':'league_table','year':y,'metric':metric}]))
    for i,(cl,raw,y) in enumerate([('West Ham United','West Ham',2016),('Stoke City','Stoke',2012),('Swansea City','Swansea',2015),('Norwich City','Norwich',2011)]):
        c.append(case(f'negation-{i}','negation',f'How many matches did {cl} not lose in {y}/{str(y+1)[-2:]}?',[season(cl,raw,y,m) for m in ['won','drawn']]))
    unknown=[
        'How many FA Cup goals did Chelsea score in 2020/21?',
        'How many Champions League points did Manchester City earn in 2022/23?',
        'Which player scored the most goals for Liverpool in 2013/14?',
        'How many goals did Arsenal score from penalties in 2010/11?',
        'How many goals did Tottenham score in the first 15 minutes of games in 2016/17?',
        'How many league points did Everton officially finish with after deductions in 2023/24?',
        'How many offside calls did Sunderland receive in 2014/15?',
        'Who was Stoke City manager in 2013/14?',
        'How many points did Wrexham earn in 2020/21?',
        'How many goals did Liverpool score in the second division in 2019/20?',
        'What was Manchester United expected goals total in 2012/13?',
        'How many points did Bournemouth earn in 1992/93?']
    for i,q in enumerate(unknown):c.append(case(f'unsupported-{i}','unsupported',q,reason='Requested competition, granularity, entity, field or season is absent from the corpus.'))
    assert len(c)==48,len(c)
    return c


def nasa_cases():
    p=lambda body,metric:{'kind':'planet','body':body,'metric':metric}
    rows=[('n-0','control','Mars gravity',[p('Mars','gravity')]),('n-1','control','Earth diameter',[p('Earth','diameter')]),('n-2','control','Saturn mass',[p('Saturn','mass')]),('n-3','control','Neptune density',[p('Neptune','density')]),
          ('n-4','generic_comparison','Compare Venus rotation period and length of day in hours.',[p('Venus','rotation_period'),p('Venus','length_of_day')]),
          ('n-5','generic_comparison','Compare gravity on Mercury and Mars.',[p('Mercury','gravity'),p('Mars','gravity')]),
          ('n-6','generic_language','How wide is Jupiter at its equator?',[p('Jupiter','diameter')]),
          ('n-7','generic_language','Neptuen mean temperature in Celsius',[p('Neptune','mean_temperature')]),
          ('n-8','generic_language','How long does Venus take to spin once relative to the stars?',[p('Venus','rotation_period')]),
          ('n-9','generic_comparison','Which is larger across the equator, Uranus or Neptune?',[p('Uranus','diameter'),p('Neptune','diameter')]),
          ('n-10','generic_scope','What is the Moon orbital period around Earth in days?',[p('Moon','orbital_period')]),
          ('n-11','generic_comparison','Compare Earth and Venus surface pressure.',[p('Earth','surface_pressure'),p('Venus','surface_pressure')])]
    cases=[case(i,cat,q,ns,manual_control=i in ['n-0','n-1','n-2','n-3','n-4','n-5','n-7','n-11']) for i,cat,q,ns in rows]
    for i,q in enumerate(['Give a known numerical solid-surface pressure for Jupiter.','How many confirmed moons does Saturn have as of 2026-10-26?','What is the gravity of Titan?','What is the measured magnetic field strength of Pluto in teslas?'],12):
        cases.append(case(f'n-{i}','unsupported',q,reason='Unknown, stale relative to requested date, body absent, or measure absent from the frozen table.'))
    return cases


def gold_group(need,sources,docs,planets):
    if need['kind'] in ['season_stat','match']:return build_group(need,sources,docs)
    if need['kind']=='planet':
        f=planets[need['body']][need['metric']];identity='planetary/'+slug(need['body'])
        if f['value'] is None:raise ValueError('Unknown fact cannot be positive gold')
        quote=f"{f['label']}: {f['raw']} [{f['basis']}]."
        assert quote in docs[identity]['body']
        return {'need':need,'expected_value':f['value'],'basis':f['basis'],'candidates':{identity:[quote]}}
    if need['kind']=='subset':
        raw=need['source_club'];side='HomeTeam' if need['side']=='home' else 'AwayTeam'
        rows=[r for r in sources if r['year']==need['year'] and r['raw'][side]==raw]
        if len(rows) not in (19,21):raise ValueError('Incomplete home/away inputs')
        expected=independently_tally(rows,raw)[need['metric']]
        identity=f'epl/club-seasons/{need["year"]}-{str(need["year"]+1)[-2:]}/{slug(need["club"])}'
        quotes=[]
        from epl_trial import NAMES
        for r in rows:
            home=NAMES.get(r['raw']['HomeTeam'],r['raw']['HomeTeam']);away=NAMES.get(r['raw']['AwayTeam'],r['raw']['AwayTeam'])
            text=f"{r['date']} {home} {r['raw']['FTHG']}-{r['raw']['FTAG']} {away}"
            quotes.append(next(line for line in docs[identity]['body'].splitlines() if text in line))
        return {'need':need,'expected_value':expected,'requires_computation':True,'candidates':{identity:quotes},'input_rows':rows}
    if need['kind']=='league_table':
        identity=f'epl/seasons/{need["year"]}-{str(need["year"]+1)[-2:]}'
        # Require the complete comparator set, not just the named team's own total.
        clubs=sorted({r['raw']['HomeTeam'] for r in sources if r['year']==need['year']})
        from epl_trial import NAMES
        expected={NAMES.get(raw,raw):independently_tally([r for r in sources if r['year']==need['year']],raw)[need['metric']] for raw in clubs}
        quotes=[line for line in docs[identity]['body'].splitlines() if line.startswith('| [')]
        if len(quotes)!=len(expected):raise ValueError('Incomplete league table')
        # Validate every table against separately source-derived complete stat rows.
        for raw in clubs:build_group(season(NAMES.get(raw,raw),raw,need['year'],need['metric']),sources,docs)
        return {'need':need,'expected_value':expected,'requires_computation':True,'candidates':{identity:quotes}}
    raise ValueError(need)


def serialize_expected(group):
    expected=group['expected_value'];metric=group['need']['metric']
    if isinstance(expected,dict) and 'home_value' in expected:
        if metric in ['score','half_time']:return f"{expected['home_value']}-{expected['away_value']}"
        return f"{expected['home']} {expected['home_value']}, {expected['away']} {expected['away_value']}"
    return expected


def score_response(response,groups):
    """Score source-visible evidence separately from requested structured facts."""
    docs=response.get('results',[]);doc_groups=[];visible=[];facts=[]
    for g in groups:
        doc_groups.append(any(d['id'] in g['candidates'] for d in docs))
        texts=[];structured=[]
        for d in docs:
            quotes=g['candidates'].get(d['id'])
            if quotes is None:continue
            text=d.get('excerpt','')+'\n'+'\n'.join(f.get('quote','') for f in d.get('facts',[]))
            visible_here=all(q in text for q in quotes)
            texts.append(visible_here)
            if g.get('requires_computation'):
                structured.append(visible_here) # complete arithmetic inputs count; no final-answer scoring
            else:
                structured.append(any(f['metric']==g['need']['metric'] and f['value']==serialize_expected(g) and ('basis' not in g or f['basis']==g['basis']) for f in d.get('facts',[])))
        visible.append(any(texts));facts.append(any(structured))
    return {'complete_documents_at_5':all(doc_groups) if groups else None,
            'complete_visible_evidence':all(visible) if groups else None,
            'complete_requested_facts_or_inputs':all(facts) if groups else None,
            'per_group':{'documents':doc_groups,'visible':visible,'facts_or_inputs':facts},
            'false_complete':bool(response.get('complete')) and (not groups or not all(facts)),
            'safe_noncompletion':not bool(response.get('complete'))}


def manual_plan(case):
    # Supplementary NASA control uses explicit names/measures only; never target IDs or values.
    clauses=[]
    by_body=collections.defaultdict(list)
    for need in case['needs']:by_body[need['body']].append(need['metric'])
    for body,metrics in by_body.items():clauses.append({'entities':[body],'kind':'PlanetFact','metrics':metrics})
    return {'clauses':clauses}


def run_cases(url,cases,gold,docs,manual=False):
    result=[]
    for case in cases:
        if manual and not case.get('manual_control'):continue
        request={'question':case['question'],'limit':5}
        if manual:request['plan']=manual_plan(case)
        started=time.perf_counter();status,response=http(url,'/v1/query',request);ms=(time.perf_counter()-started)*1000
        if status!=200:raise ValueError((case['id'],status,response))
        for d in response['results']:
            assert d['revision']==docs[d['id']]['revision'],(case['id'],'revision mismatch')
        result.append({'id':case['id'],'category':case['category'],'question':case['question'],'positive':bool(gold[case['id']]),'manual_control':manual,'response_ms':ms,'score':score_response(response,gold[case['id']]),'response':response})
    return result


def summarize(rows):
    positive=[r for r in rows if r['positive']];negative=[r for r in rows if not r['positive']]
    complete=[r for r in rows if r['response']['complete']]
    return {'cases':len(rows),'answerable':len(positive),'unsupported':len(negative),
            **{name:sum(bool(r['score'][name]) for r in positive) for name in ['complete_documents_at_5','complete_visible_evidence','complete_requested_facts_or_inputs']},
            'false_complete':sum(r['score']['false_complete'] for r in rows),
            'reported_complete':len(complete),
            'complete_precision':sum(not r['score']['false_complete'] for r in complete)/len(complete) if complete else None,
            'unsupported_safely_not_complete':sum(r['score']['safe_noncompletion'] for r in negative)}


def timed(url,pid,cases):
    # Report error count, latency, bytes and service CPU; no extra reads in the measured path.
    result=[]
    for workers in [1,4,16]:
        for c in cases:http(url,'/v1/query',{'question':c['question'],'limit':5})
        before=sample(pid);started=time.perf_counter()
        def one(c):
            t=time.perf_counter();s,r=http(url,'/v1/query',{'question':c['question'],'limit':5});return s,(time.perf_counter()-t)*1000,len(json.dumps(r).encode())
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:rows=list(pool.map(one,cases*3))
        elapsed=time.perf_counter()-started;after=sample(pid)
        result.append({'clients':workers,'requests':len(rows),'errors':sum(s!=200 for s,_,_ in rows),'response_ms':distribution([t for _,t,_ in rows]),'queries_per_second':len(rows)/elapsed,'cpu_seconds':after['cpu_seconds']-before['cpu_seconds'],'rss_before_mib':before['rss_mib'],'rss_after_mib':after['rss_mib'],'mean_bytes':sum(b for _,_,b in rows)/len(rows)})
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=ROOT/'.jbrain/hard-eval-v1');p.add_argument('--nasa-html',type=Path,default=Path('/tmp/jbrain-hard-eval/planet-facts.html'));p.add_argument('--nasa-notes',type=Path,default=Path('/tmp/jbrain-hard-eval/planet-notes.txt'));p.add_argument('--prepare-only',action='store_true');args=p.parse_args()
    root=args.root;root.mkdir(parents=True,exist_ok=True)
    (root/'sources').mkdir(exist_ok=True)
    if not (root/'sources/planet-facts.html').exists():
        shutil.copyfile(args.nasa_html,root/'sources/planet-facts.html');shutil.copyfile(args.nasa_notes,root/'sources/planet-notes.txt')
    source_html=(root/'sources/planet-facts.html').read_bytes();planets=planet_data(source_html);ndocs=planet_documents(planets)
    epl_root=ROOT/'.jbrain/epl-trial';sources=read_sources(epl_root);edocs=load_documents(epl_root)
    cases=epl_cases(sources);ncases=nasa_cases()
    fixture={'name':'hard-retrieval-v1','description':'Adversarial, hand-authored with implementation knowledge. Frozen before first requests; no tuning of production Rust code. Not an independent blind benchmark.','epl':cases,'nasa':ncases}
    fixture_path=ROOT/'tests/fixtures/hard-retrieval-v1.json'
    data=(json.dumps(fixture,indent=2)+'\n').encode()
    if fixture_path.exists() and fixture_path.read_bytes()!=data:raise ValueError('Frozen questions changed; create a new version')
    fixture_path.write_bytes(data)
    docs={**edocs,**ndocs}
    gold={c['id']:[gold_group(n,sources,docs,planets) for n in c['needs']] for c in cases+ncases}
    manifest={'frozen_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'fixture_sha256':sha(data),'binary_sha256':sha(BINARY.read_bytes()),'epl_corpus_sha256':sha((epl_root/'documents.jsonl').read_bytes()),'nasa_html_sha256':sha(source_html),'nasa_notes_sha256':sha((root/'sources/planet-notes.txt').read_bytes()),'nasa_source':NASA,'nasa_notes':NOTES,'nasa_table_date':'2025-03-18','nasa_documents':len(ndocs),'cases':{'epl':len(cases),'nasa':len(ncases)}}
    path=root/'manifest.json'
    if path.exists():
        original=json.loads(path.read_text())
        assert all(original[k]==v for k,v in manifest.items() if k!='frozen_at'),'Frozen source/binary changed'
    else:path.write_text(json.dumps(manifest,indent=2)+'\n')
    (root/'gold.json').write_text(json.dumps(gold,indent=2)+'\n')
    if args.prepare_only:
        print(json.dumps(manifest,indent=2));return
    session=json.loads((epl_root/'session.json').read_text())
    assert session['binary_sha256']==manifest['binary_sha256'],'EPL running binary differs'
    print('Running frozen EPL cases',flush=True)
    epl=run_cases(session['url'],cases,gold,docs)
    print('EPL',json.dumps(summarize(epl)),flush=True)
    latency=timed(session['url'],session['pid'],[c for c in cases if c['category']!='control'])
    # New domain uses a separate, temporary brain; no changes to the EPL corpus.
    sys.path.insert(0,str(ROOT/'tests'))
    import native_suite
    native_suite.BINARY=str(BINARY)
    with native_suite.Service() as service:
        for identity,doc in ndocs.items():service.put(identity,doc['markdown'])
        nasa=run_cases(service.url,ncases,gold,docs)
        controls=run_cases(service.url,ncases,gold,docs,manual=True)
        nasa_resource=sample(service.proc.pid)
    output={'manifest':json.loads(path.read_text()),'epl':{'summary':summarize(epl),'rows':epl},'nasa':{'summary':summarize(nasa),'rows':nasa},'nasa_manual_plan_control':{'summary':summarize(controls),'rows':controls},'epl_latency':latency,'nasa_after_quality_resource':nasa_resource,'model_calls':0}
    (root/'results.json').write_text(json.dumps(output,indent=2)+'\n')
    compact={**output}
    for key in ['epl','nasa','nasa_manual_plan_control']:
        rows=output[key]['rows'];cats=collections.defaultdict(list)
        for row in rows:cats[row['category']].append(row)
        compact[key]={'summary':output[key]['summary'],'categories':{k:summarize(v) for k,v in cats.items()},'failures':[{'id':r['id'],'question':r['question'],'score':r['score'],'status':r['response']['status'],'plan':r['response']['plan'],'facts':[{'id':d['id'],'facts':d['facts']} for d in r['response']['results']]} for r in rows if r['score']['false_complete'] or r['positive'] and not r['score']['complete_requested_facts_or_inputs']]}
    (ROOT/'docs/evals/hard-retrieval-v1-summary.json').write_text(json.dumps(compact,indent=2)+'\n')
    print(json.dumps({k:v['summary'] for k,v in output.items() if isinstance(v,dict) and 'summary' in v},indent=2))
    print('Results:',root/'results.json')

if __name__=='__main__':main()
