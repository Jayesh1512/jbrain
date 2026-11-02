#!/usr/bin/env python3
"""Fresh difficult development questions: automatic evidence vs caller-authored tool workflows.

Read-only. Expected answers come from hash-verified CSVs, never from retrieval outputs.
Caller-authored replay and Python arithmetic are explicitly NOT model answer accuracy.
"""
import argparse
import collections
import hashlib
import json
import math
from pathlib import Path
import statistics
import time
import urllib.error
import urllib.request
from benchmark_epl_retrieval import read_sources, independently_tally
from epl_trial import NAMES, season, slug, club_season_id

ROOT=Path(__file__).resolve().parents[1]
QUESTIONS=[
    ('away_rank','Which Premier League club scored the most away goals in 2022/23, and how many?'),
    ('calendar','In calendar year 2020, how many away Premier League wins did Manchester United record?'),
    ('halftime','How many Arsenal league matches in 2023/24 were level at half time but ended as Arsenal wins?'),
    ('unbeaten','How many away league matches did Newcastle avoid defeat in 2022/23, and what was their goal difference in those matches?'),
    ('two_seasons','Across 2021/22 and 2022/23 combined, how many goals did Liverpool concede in away league defeats?'),
    ('home_compare','Who won more home league games in 2022/23: Brighton or Aston Villa? Give both counts and the margin.'),
    ('three_facts','For Tottenham versus Manchester City on 2023-02-05, give the half-time score, full-time score and each team’s shots on target.'),
    ('opponent','Who did Brentford beat at home on 2021-08-13, and how many away league goals did that opponent score in 2021/22?'),
    ('away_losses','Did Nottingham Forest lose more away league games than Everton in 2022/23? Give each total.'),
    ('split_draws','In 2023/24, how many Premier League draws did Chelsea have at home and away separately?'),
    ('negation','How many league matches did Burnley fail to win in 2014/15, excluding home fixtures?'),
    ('zero','How many away Premier League wins did Southampton have in 2024/25?'),
    ('ties','Was the highest number of Premier League draws in 2016/17 held by one club or tied? Name every club with that total.'),
    ('sizes','Compare the league fixtures played by Newcastle in 1993/94 and 2024/25. How many clubs were in each league season?'),
    ('cross_compare','Which had more away league draws, Leicester in 2015/16 or Chelsea in 2014/15? Give both counts and the difference.'),
    ('premise','How many fewer league points did Manchester City earn away than at home in 2020/21, using three points per win and one per draw? Correct the premise if necessary.'),
    ('official','After all appeals and deductions, what were Everton’s official final league points in 2023/24?'),
    ('player','Who scored Liverpool’s second goal against Bournemouth on 2022-08-27, and in which minute?'),
    ('xg','What was Brighton’s average xG per away Premier League game in 2022/23?'),
    ('cup','How many FA Cup away wins did Arsenal have in 2022/23?'),
    ('europe','What was the exact attendance for Newcastle versus PSG on 2023-10-04?'),
    ('outside','How many Premier League home wins did Chelsea have in 1992/93?'),
    ('ambiguous_season','How many wins did United have that season?'),
    ('ambiguous_clubs','Compare City’s and United’s home records in 2023/24.'),
]
REASONS={
    'official':'Only earned points before administrative adjustments are stored; appeals/deductions are absent.',
    'player':'Match totals are stored, not individual scorers or goal-event minutes.',
    'xg':'Expected goals are absent; actual goals cannot substitute for xG.',
    'cup':'The corpus contains Premier League matches only, not FA Cup matches.',
    'europe':'This is a European match outside the competition; attendance is not imported either.',
    'outside':'The snapshot begins in 1993/94, not 1992/93.',
    'ambiguous_season':'Several United clubs exist; no season or prior referent is supplied.',
    'ambiguous_clubs':'City and United each match multiple clubs; canonical subjects need clarification.',
}


def canonical(raw):return NAMES.get(raw,raw)
def identity(row):
    r=row['raw'];return f"epl/matches/{row['year']}-{str(row['year']+1)[-2:]}/{row['date']}-{slug(canonical(r['HomeTeam']))}-vs-{slug(canonical(r['AwayTeam']))}"
def scope(kind=None,year=None,entities=None,date=None,filters=None):
    return {'kind':kind,'period':season(year) if year is not None else None,'date':date,'entities':entities or [],'filters':filters or []}


class Run:
    def __init__(self,url,sources):
        self.url=url;self.sources=sources;self.trace=[];self.requirements=[]
    def http(self,path,payload):
        request=urllib.request.Request(self.url+path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        start=time.perf_counter()
        try:response=urllib.request.urlopen(request,timeout=30)
        except urllib.error.HTTPError as error:response=error
        with response:value=json.load(response);status=response.status
        self.trace.append({'path':path,'request':payload,'http_status':status,'response':value,'elapsed_ms':(time.perf_counter()-start)*1000})
        return status,value
    def tool(self,name,**args):
        status,value=self.http('/v1/evidence',{'tool':name,'arguments':args})
        if status!=200:raise ValueError(f'{name}: {status}: {value}')
        return value
    def source_rows(self,club,year,venue='all'):
        def included(row):
            r=row['raw'];home=canonical(r['HomeTeam'])==club;away=canonical(r['AwayTeam'])==club
            return row['year']==year and (home if venue=='home' else away if venue=='away' else home or away)
        return [r for r in self.sources if included(r)]
    def require_matches(self,rows,metric='score'):
        # All scoped inputs must be visible. A related season total is not subset evidence.
        self.requirements.append({'kind':'matches','metric':metric,'rows':rows})
    def source_subset(self,club,year,venue='all',outcome='all',calendar=None):
        base=self.source_rows(club,year,venue)
        if calendar:base=[r for r in base if r['date'].startswith(str(calendar)+'-')]
        self.require_matches(base)
        selected=[]
        for row in base:
            r=row['raw'];gf,ga=(int(r['FTHG']),int(r['FTAG'])) if canonical(r['HomeTeam'])==club else (int(r['FTAG']),int(r['FTHG']))
            if {'all':True,'win':gf>ga,'draw':gf==ga,'loss':gf<ga,'not_loss':gf>=ga,'not_win':gf<=ga}[outcome]:selected.append(row)
        # Independent tally reads CSV fields; no retrieval result participates.
        raw=next((r['raw']['HomeTeam'] if canonical(r['raw']['HomeTeam'])==club else r['raw']['AwayTeam'] for r in base),club)
        stats=independently_tally(selected,raw)
        return {'count':stats['played'],'goals_for':stats['goals_for'],'goals_against':stats['goals_against']}
    def subset(self,club,year,venue='all',outcome='all'):
        return self.tool('compute',operation='matches',scope=scope('Match',year,[club],filters=[{'field':'dataset','value':'epl-results-v1'}]),metric=None,basis=None,club=club,venue=venue,outcome=outcome)
    def one(self,kind,year=None,entities=None,date=None,filters=None):
        found=self.tool('find',text='',scope=scope(kind,year,entities,date,filters),limit=2)
        assert len(found['results'])==1 and not found['has_more']
        return self.tool('read',id=found['results'][0]['id'],offset=0,length=16000)
    def league(self,year):
        found=self.tool('find',text='',scope=scope('ClubSeasonSummary',year,filters=[{'field':'dataset','value':'epl-results-v1'}]),limit=20)
        assert not found['has_more']
        return {r['id']:r['title'].split(' Premier League ')[0] for r in found['results']}


def solve(run,key,gold):
    sub=(lambda *a,**kw:run.source_subset(*a,**kw)) if gold else (lambda *a,**kw:run.subset(*a,**kw)['result'])
    if key=='away_rank':
        clubs=sorted({canonical(r['raw']['HomeTeam']) for r in run.sources if r['year']==2022}) if gold else list(run.league(2022).values())
        values={club:sub(club,2022,'away')['goals_for'] for club in clubs};top=max(values.values())
        return {'clubs':sorted(k for k,v in values.items() if v==top),'away_goals':top}
    if key=='calendar':
        if gold:return {'away_wins':sum(sub('Manchester United',y,'away','win',calendar=2020)['count'] for y in [2019,2020])}
        count=0
        for year in [2019,2020]:
            receipt=run.subset('Manchester United',year,'away','win')
            count+=sum(item['included'] and item['value']['date'].startswith('2020-') for item in receipt['inputs'])
        return {'away_wins':count}
    if key=='halftime':
        if gold:
            base=run.source_rows('Arsenal',2023);wins=[]
            for row in base:
                r=row['raw'];win=(int(r['FTHG'])>int(r['FTAG'])) if canonical(r['HomeTeam'])=='Arsenal' else (int(r['FTAG'])>int(r['FTHG']))
                if win:wins.append(row)
            run.require_matches(wins,'half_time')
            return {'wins_after_level_half_time':sum(int(r['raw']['HTHG'])==int(r['raw']['HTAG']) for r in wins)}
        receipt=run.subset('Arsenal',2023,'all','win');count=0
        for item in receipt['inputs']:
            if not item['included']:continue
            doc=run.tool('read',id=item['id'],offset=0,length=16000)
            a,b=next(f['value'] for f in doc['facts'] if f['metric']=='half_time').split('-');count+=a==b
        return {'wins_after_level_half_time':count}
    if key=='unbeaten':
        r=sub('Newcastle United',2022,'away','not_loss');return {'unbeaten_away':r['count'],'goal_difference_in_those_games':r['goals_for']-r['goals_against']}
    if key=='two_seasons':return {'goals_conceded_in_away_defeats':sum(sub('Liverpool',y,'away','loss')['goals_against'] for y in [2021,2022])}
    if key in ['home_compare','away_losses','cross_compare']:
        if key=='home_compare':a,b,ya,yb,venue,outcome='Brighton and Hove Albion','Aston Villa',2022,2022,'home','win'
        elif key=='away_losses':a,b,ya,yb,venue,outcome='Nottingham Forest','Everton',2022,2022,'away','loss'
        else:a,b,ya,yb,venue,outcome='Leicester City','Chelsea',2015,2014,'away','draw'
        x,y=sub(a,ya,venue,outcome)['count'],sub(b,yb,venue,outcome)['count']
        return {a:x,b:y,'larger_count':a if x>y else b if y>x else 'tie','difference':abs(x-y)}
    if key=='three_facts':
        if gold:
            row=next(r for r in run.sources if r['date']=='2023-02-05' and canonical(r['raw']['HomeTeam'])=='Tottenham Hotspur' and canonical(r['raw']['AwayTeam'])=='Manchester City');r=row['raw']
            expected={'half_time':f"{r['HTHG']}-{r['HTAG']}",'score':f"{r['FTHG']}-{r['FTAG']}",'shots_on_target':f"Tottenham Hotspur {r['HST']}, Manchester City {r['AST']}"}
            for metric,value in expected.items():run.requirements.append({'kind':'fact','id':identity(row),'metric':metric,'value':value})
            return expected
        doc=run.one('Match',entities=['Tottenham Hotspur','Manchester City'],date='2023-02-05')
        return {f['metric']:f['value'] for f in doc['facts'] if f['metric'] in ['half_time','score','shots_on_target']}
    if key=='opponent':
        if gold:
            row=next(r for r in run.sources if r['date']=='2021-08-13' and canonical(r['raw']['HomeTeam'])=='Brentford');other=canonical(row['raw']['AwayTeam'])
            run.requirements.append({'kind':'source','id':identity(row)})
        else:other=run.one('Match',date='2021-08-13',filters=[{'field':'home_team','value':'Brentford'}])['metadata']['away_team']
        return {'opponent':other,'opponent_away_goals':sub(other,2021,'away')['goals_for']}
    if key=='split_draws':return {'home_draws':sub('Chelsea',2023,'home','draw')['count'],'away_draws':sub('Chelsea',2023,'away','draw')['count']}
    if key=='negation':return {'away_non_wins':sub('Burnley',2014,'away','not_win')['count']}
    if key=='zero':return {'away_wins':sub('Southampton',2024,'away','win')['count']}
    if key=='ties':
        if gold:
            clubs=sorted({canonical(r['raw']['HomeTeam']) for r in run.sources if r['year']==2016})
            values={c:run.source_subset(c,2016,'all','draw')['count'] for c in clubs}
            # A complete league table is also sufficient; avoid requiring every fixture for this case.
            run.requirements=[{'kind':'fact','id':club_season_id(2016,c),'metric':'drawn','value':v} for c,v in values.items()]
        else:
            names=run.league(2016);sample=run.tool('read',id=next(iter(names)),offset=0,length=16000)
            basis=next(f['basis'] for f in sample['facts'] if f['metric']=='drawn')
            rank=run.tool('compute',operation='rank',scope=scope('ClubSeasonSummary',2016,filters=[{'field':'dataset','value':'epl-results-v1'}]),metric='drawn',basis=basis,club=None,venue=None,outcome=None)
            values={names[r['id']]:r['value'] for r in rank['result']}
        top=max(values.values());return {'clubs':sorted(k for k,v in values.items() if v==top),'draws':top}
    if key=='sizes':
        out={}
        for year in [1993,2024]:
            if gold:
                games=len(run.source_rows('Newcastle United',year));clubs=len({r['raw']['HomeTeam'] for r in run.sources if r['year']==year})
                run.requirements.append({'kind':'fact','id':club_season_id(year,'Newcastle United'),'metric':'played','value':games})
                run.requirements.append({'kind':'league_size','year':year,'value':clubs})
            else:
                doc=run.one('ClubSeasonSummary',year,['Newcastle United']);games=next(f['value'] for f in doc['facts'] if f['metric']=='played')
                neighbors=run.tool('neighbors',id=f'epl/seasons/{year}-{str(year+1)[-2:]}',direction='outgoing',kind='ClubSeasonSummary',period=season(year),entity='',limit=64)
                assert not neighbors['has_more'];clubs=len(neighbors['neighbors'])
            out[season(year)]={'fixtures':games,'clubs':clubs}
        return out
    if key=='premise':
        points={venue:3*sub('Manchester City',2020,venue,'win')['count']+sub('Manchester City',2020,venue,'draw')['count'] for venue in ['home','away']}
        return {'home_points':points['home'],'away_points':points['away'],'away_minus_home':points['away']-points['home'],'fewer_away_premise_correct':points['away']<points['home']}
    raise ValueError(key)


def evidence_score(response,requirements):
    """Strict sufficient-input check; not final-answer scoring or a complete reasoning judge."""
    results=response.get('results',[]);by_id={r['id']:r for r in results}
    texts={r['id']:r.get('excerpt','')+'\n'+'\n'.join(f.get('quote','') for f in r.get('facts',[])) for r in results}
    checks=[]
    for req in requirements:
        if req['kind']=='source':checks.append(req['id'] in by_id)
        elif req['kind']=='fact':
            row=by_id.get(req['id'],{});checks.append(any(f['metric']==req['metric'] and f['value']==req['value'] for f in row.get('facts',[])))
        elif req['kind']=='matches':
            found=[]
            for source in req['rows']:
                raw=source['raw'];identity_=identity(source);doc=by_id.get(identity_,{})
                value=f"{raw['HTHG']}-{raw['HTAG']}" if req['metric']=='half_time' else f"{raw['FTHG']}-{raw['FTAG']}"
                typed=any(f['metric']==req['metric'] and f['value']==value for f in doc.get('facts',[]))
                home,away=canonical(raw['HomeTeam']),canonical(raw['AwayTeam'])
                needle=f"{source['date']} {home} {raw['FTHG']}-{raw['FTAG']} {away}"
                visible=(req['metric']=='score' and any(needle in text for text in texts.values()))
                found.append(typed or visible)
            checks.append(bool(found) and all(found))
        elif req['kind']=='league_size':
            # Accept all club-season rows, or an explicit correctly scoped league-size statement.
            sid=f"epl/seasons/{req['year']}-{str(req['year']+1)[-2:]}"
            text=texts.get(sid,'');checks.append(f"{req['value']} clubs" in text or len([line for line in text.splitlines() if line.startswith('| [')])==req['value'])
    return {'complete_requested_evidence':bool(checks) and all(checks),'requirements_satisfied':sum(checks),'requirements':len(checks)}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=ROOT/'.jbrain/challenge-v3');parser.add_argument('--url',default='http://127.0.0.1:7331');args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    sources=read_sources(ROOT/'.jbrain/epl-trial');cases=[]
    for key,question in QUESTIONS:
        gold=Run(args.url,sources)
        expected=solve(gold,key,True) if key not in REASONS else {'reason':REASONS[key]}
        assert not gold.trace  # No service requests while building expected values.
        cases.append({'id':key,'question':question,'class':'ambiguous' if key.startswith('ambiguous') else 'unsupported' if key in REASONS else 'answerable','expected':expected,'requirements':gold.requirements,'development_only':True})
    fixture=json.dumps(cases,indent=2).encode();path=args.output/'frozen-cases.json'
    with path.open('xb') as output:output.write(fixture)
    existing=json.loads((ROOT/'tests/fixtures/hard-retrieval-v1.json').read_text())
    old_questions={c['question'].casefold() for domain in ['epl','nasa'] for c in existing[domain]}
    assert not any(c['question'].casefold() in old_questions for c in cases)
    output=[]
    for case in cases:
        run=Run(args.url,sources);status,response=run.http('/v1/query',{'question':case['question'],'limit':16,'max_chars':64000})
        automatic={'http_status':status,'response':response,'latency_ms':run.trace[-1]['elapsed_ms'],'strict_evidence':evidence_score(response,case['requirements']) if case['class']=='answerable' else None}
        replay=None
        if case['class']=='answerable':
            run=Run(args.url,sources);start=time.perf_counter()
            try:actual=solve(run,case['id'],False);error=None
            except Exception as exc:actual=None;error=str(exc)
            calls=len(run.trace)
            replay={'actual':actual,'correct':actual==case['expected'],'error':error,'http_tool_calls':calls,'elapsed_ms':(time.perf_counter()-start)*1000,'straightforward_sequential_plan_exceeds_16_model_calls':calls+3>16,'caller_authored_interpretation':True,'external_python_arithmetic':case['id'] not in ['zero','negation','three_facts'],'trace':run.trace}
        output.append({'id':case['id'],'question':case['question'],'class':case['class'],'expected':case['expected'],'automatic':automatic,'explicit_replay':replay})
    probe=Run(args.url,sources);base={'operation':'matches','scope':scope('Match',2023,['Arsenal'],filters=[{'field':'dataset','value':'epl-results-v1'}]),'metric':None,'basis':None,'club':'Arsenal','venue':'all','outcome':'win'}
    for field,value in [('date_start','2023-08-01'),('half_time_result','draw')]:
        args_={**base,'scope':{**base['scope'],field:value}};probe.http('/v1/evidence',{'tool':'compute','arguments':args_})
    probe.http('/v1/ask',{'question':cases[0]['question']})
    positives=[r for r in output if r['class']=='answerable'];times=sorted(r['automatic']['latency_ms'] for r in output)
    summary={'description':'New implementation-aware development challenge, not an independent holdout or real-model accuracy result. Interpretations and Python arithmetic in replay are caller authored. Automatic score is strict sufficient-evidence retrieval, not final-answer correctness.',
        'frozen_fixture_sha256':hashlib.sha256(fixture).hexdigest(),'binary_sha256':hashlib.sha256((ROOT/'target/release/jbrain').read_bytes()).hexdigest(),
        'questions':len(cases),'answerable':len(positives),'unsupported':sum(r['class']=='unsupported' for r in output),'ambiguous':sum(r['class']=='ambiguous' for r in output),
        'automatic_complete_requested_evidence':sum(r['automatic']['strict_evidence']['complete_requested_evidence'] for r in positives),
        'automatic_claimed_answer_completion':sum(r['automatic']['response'].get('complete') is True for r in output),
        'explicit_replay_correct':sum(r['explicit_replay']['correct'] for r in positives),
        'over_sequential_model_budget':[r['id'] for r in positives if r['explicit_replay']['straightforward_sequential_plan_exceeds_16_model_calls']],
        'automatic_latency_ms':{'p50':statistics.median(times),'p95':times[math.ceil(len(times)*.95)-1]},'model_calls':0,'probes':probe.trace}
    (args.output/'results.json').write_text(json.dumps({'summary':summary,'rows':output},indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k!='probes'},indent=2))
    for row in output:print(row['id'],json.dumps(row['expected']),None if row['explicit_replay'] is None else {'correct':row['explicit_replay']['correct'],'calls':row['explicit_replay']['http_tool_calls']})


if __name__=='__main__':main()
