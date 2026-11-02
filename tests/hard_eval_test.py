"""Protect scoring against wrong-metric, partial-input and unknown-as-zero credit."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from hard_eval import score_response, planet_data

class HardEvalTests(unittest.TestCase):
    def group(self):
        return {'need':{'metric':'goals_for'},'expected_value':80,'candidates':{'summary':['Goals scored: 80.']}}

    def test_right_document_with_wrong_metric_is_false_complete(self):
        result=score_response({'complete':True,'results':[{'id':'summary','excerpt':'Goals scored: 80.','facts':[{'metric':'goals_against','value':20}]}]},[self.group()])
        self.assertTrue(result['complete_documents_at_5'])
        self.assertTrue(result['complete_visible_evidence'])
        self.assertFalse(result['complete_requested_facts_or_inputs'])
        self.assertTrue(result['false_complete'])

    def test_correct_fact_requires_correct_basis(self):
        group={'need':{'metric':'rotation_period'},'expected_value':-5832.5,'basis':'hours_signed_sidereal','candidates':{'venus':['Rotation']}}
        result=score_response({'complete':True,'results':[{'id':'venus','excerpt':'Rotation','facts':[{'metric':'rotation_period','value':-5832.5,'basis':'hours_solar'}]}]},[group])
        self.assertTrue(result['false_complete'])

    def test_derived_statistics_require_all_inputs(self):
        group={'need':{'metric':'home_wins'},'expected_value':3,'requires_computation':True,'candidates':{'fixtures':['game1','game2','game3']}}
        result=score_response({'complete':True,'results':[{'id':'fixtures','excerpt':'game1 game2','facts':[]}]},[group])
        self.assertFalse(result['complete_requested_facts_or_inputs'])
        self.assertTrue(result['false_complete'])

    def test_unsupported_related_candidate_is_safe_only_if_not_complete(self):
        for complete in [True,False]:
            result=score_response({'complete':complete,'results':[{'id':'related','excerpt':'text','facts':[]}]},[])
            self.assertEqual(result['false_complete'],complete)
            self.assertEqual(result['safe_noncompletion'],not complete)

    def test_table_parser_preserves_negative_values_unknown_and_zero(self):
        path=Path(__file__).resolve().parents[1]/'.jbrain/hard-eval-v1/sources/planet-facts.html'
        if not path.exists():self.skipTest('Fetch the NASA source snapshot first')
        data=planet_data(path.read_bytes())
        self.assertEqual(data['Venus']['rotation_period']['value'],-5832.5)
        self.assertEqual(data['Mercury']['surface_pressure']['value'],0)
        self.assertIsNone(data['Jupiter']['surface_pressure']['value'])
        self.assertEqual(data['Moon']['distance_from_sun']['basis'],'million_km_from_Earth')
        self.assertEqual(data['Jupiter']['diameter']['value'],142984)

if __name__=='__main__':unittest.main(verbosity=2)
