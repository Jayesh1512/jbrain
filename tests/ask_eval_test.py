"""Prevent optimistic scoring of abstentions, unbound reviews and small development sets."""
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from evaluate_ask import summarize, wilson
from ask_suite import Provider
from native_suite import Service, md


def row(identity, complete=True):
    response = {'complete':complete,'status':'answered' if complete else 'needs_verification',
                'model_calls':1,'usage':[{'model':'m','usage':{'input_tokens':200,'output_tokens':50}}]}
    sha=hashlib.sha256(json.dumps(response,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'id':identity,'response':response,'response_sha256':sha,'class':'answerable','http_status':200,'elapsed_seconds':1.0}


def label(row):
    return {'id':row['id'],'correct':True,'citations_correct':True,'unsupported_claims':False,
            'reviewer':'independent-test-reviewer','notes':'Source values and requested scope compared.','response_sha256':row['response_sha256']}


class AskEvaluationTests(unittest.TestCase):
    def test_runner_sends_only_questions_and_binds_response_hashes(self):
        with Provider() as provider, provider.service() as service, tempfile.TemporaryDirectory() as directory:
            service.put('atlas',md('Atlas earned 12 points.','facts: {points: 12}\n'))
            path=Path(directory)
            cases=path/'cases.jsonl'; result=path/'results.jsonl'
            cases.write_text(json.dumps({'id':'a','question':'Atlas points?','domain':'test','class':'answerable','gold':'PRIVATE_GOLD_NEVER_SEND'})+'\n')
            process=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/evaluate_ask.py'),'run','--url',service.url,'--cases',str(cases),'--output',str(result)],capture_output=True,text=True,timeout=20)
            self.assertEqual(process.returncode,0,process.stderr)
            self.assertNotIn('PRIVATE_GOLD_NEVER_SEND',json.dumps(provider.requests))
            actual=json.loads(result.read_text())
            summary=summarize([actual],[label(actual)],json.loads(Path(str(result)+'.manifest.json').read_text()))
            self.assertEqual(summary['answerable_accuracy'],1)
            self.assertFalse(summary['release_gate_passed'])

    def test_runner_records_each_http_failure_as_a_result(self):
        with Service(OPENAI_API_KEY='',JBRAIN_API_KEY_FILE='',JBRAIN_MODEL='') as service, tempfile.TemporaryDirectory() as directory:
            path=Path(directory);cases=path/'cases.jsonl';result=path/'results.jsonl'
            cases.write_text('\n'.join(json.dumps({'id':str(i),'question':f'Missing question {i}?','domain':'test','class':'answerable'}) for i in range(2))+'\n')
            process=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/evaluate_ask.py'),'run','--url',service.url,'--cases',str(cases),'--output',str(result)],capture_output=True,text=True,timeout=20)
            self.assertEqual(process.returncode,0,process.stderr)
            self.assertEqual([json.loads(line)['http_status'] for line in result.read_text().splitlines()],[503,503])

    def test_abstention_is_a_miss_even_if_judge_marks_correct(self):
        result=row('a',False)
        summary=summarize([result],[label(result)],{'partition':'development'})
        self.assertEqual(summary['answerable_accuracy'],0)
        self.assertFalse(summary['release_gate_passed'])
        self.assertIsNone(summary['priced_api_cost_usd'])

    def test_costs_accumulate_all_calls_and_project_1000_per_day(self):
        result=row('a')
        summary=summarize([result],[label(result)],{}, {'m':{'input_per_million':2,'output_per_million':10}})
        self.assertAlmostEqual(summary['projected_1000_per_day_usd'],.9)
        self.assertAlmostEqual(summary['projected_30_day_usd'],27)
        self.assertFalse(summary['release_gate_passed'])

    def test_mismatched_review_or_changed_response_is_rejected(self):
        result=row('a'); judgment=label(result); judgment['response_sha256']='wrong'
        with self.assertRaises(ValueError):summarize([result],[judgment],{})
        judgment=label(result); result['response']['complete']=False
        with self.assertRaises(ValueError):summarize([result],[judgment],{})

    def test_cache_writes_have_their_own_rate(self):
        result=row('a')
        result['response']['usage'][0]['usage']['input_tokens_details']={'cached_tokens':80,'cache_write_tokens':60}
        result['response_sha256']=hashlib.sha256(json.dumps(result['response'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        price={'m':{'input_per_million':2,'cached_input_per_million':.2,'cache_write_per_million':2.5,'output_per_million':10}}
        summary=summarize([result],[label(result)],{},price)
        self.assertAlmostEqual(summary['priced_api_cost_usd'],.000786)
        del price['m']['cache_write_per_million']
        self.assertIsNone(summarize([result],[label(result)],{},price)['priced_api_cost_usd'])

    def test_missing_call_usage_preserves_subtotal_and_blocks_total_projection(self):
        result=row('a');result['response']['model_calls']=2
        result['response_sha256']=hashlib.sha256(json.dumps(result['response'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        summary=summarize([result],[label(result)],{}, {'m':{'input_per_million':2,'output_per_million':10}})
        self.assertIsNone(summary['priced_api_cost_usd'])
        self.assertIsNone(summary['projected_1000_per_day_usd'])
        self.assertEqual(summary['provider_calls_without_usage'],1)
        self.assertAlmostEqual(summary['priced_usage_subtotal_usd'],.0009)

    def test_wilson_threshold_is_stricter_than_point_accuracy(self):
        self.assertLess(wilson(970,1000),.97)
        self.assertGreater(wilson(985,1000),.97)
        self.assertLess(wilson(97,100),.97)


if __name__=='__main__':unittest.main()
