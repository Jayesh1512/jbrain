#!/usr/bin/env python3
"""Evidence contract integration tests; arbitrary producer metadata, not only EPL."""
import concurrent.futures as futures
import contextlib
import json
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import unittest
import urllib.parse
from native_suite import BINARY, Service, http, md, write_input


def fact(name='Atlas', period='2024/25', value=12, aliases=None, basis='', extra='', body='A source note.', metric='points'):
    fields=f'title: {name} fact\nentity: {json.dumps(name)}\nperiod: {json.dumps(period)}\nfacts: {{{metric}: {value}}}\nbasis: {json.dumps(basis)}\nsources: [{{resource: "https://example.com/data"}}]\n'
    if aliases:
        fields += 'aliases: ' + json.dumps(aliases) + '\n'
    return md(body, fields + extra)


def query(service, question, **kwargs):
    status, value = http(service.url, '/v1/query', {'question': question, **kwargs})
    assert status == 200, (status, value)
    return value


class QueryTests(unittest.TestCase):
    def test_generic_facts_aliases_typos_and_provenance(self):
        with Service() as s:
            receipt = s.put('atlas', fact(aliases=['AX']))
            for question in ['How many points did Atlas earn in 2024/25?', 'AX 2024/25 points', 'Atals 2024/25 points']:
                result = query(s, question)
                self.assertTrue(result['plan_satisfied'], result)
                evidence = result['results'][0]
                self.assertEqual(evidence['revision'], receipt['revision'])
                self.assertEqual(evidence['facts'][0]['value'], 12)
                self.assertEqual(evidence['facts'][0]['pointer'], '/metadata/facts/points')
                self.assertEqual(evidence['sources'][0]['resource'], 'https://example.com/data')
                self.assertEqual(result['model_calls'], 0)
                self.assertFalse(result['complete'])
                self.assertFalse(result['answer_supported'])
            self.assertTrue(query(s, 'Atals 2024/25 points')['entities'][0]['corrected'])

    def test_comparison_reserves_each_clause_and_missing_scope(self):
        with Service() as s:
            s.put('a', fact())
            s.put('b', fact(period='2025/26', value=17))
            for i in range(15):
                s.put(f'noise-{i}', fact(name='Noise', value=999))
            value = query(s, 'Compare Atlas points in 2024/25 and 2025/26.', limit=2)
            self.assertTrue(value['plan_satisfied'], value)
            self.assertEqual({r['id'] for r in value['results']}, {'a', 'b'})
            self.assertEqual([c['evidence_ids'] for c in value['coverage']], [['a'], ['b']])
            missing = query(s, 'Compare Atlas points in 2024/25 and 2026/27.')
            self.assertFalse(missing['plan_satisfied'])
            self.assertEqual(missing['status'], 'partial')
            self.assertEqual(missing['coverage'][1]['support'], 'missing')
            s.put('other', fact(name='Beacon', value=21))
            same = query(s, 'Compare Atlas and Beacon points in 2024/25.')
            self.assertTrue(same['plan_satisfied'], same)
            self.assertEqual(len(same['coverage']), 2)

    def test_comparison_keeps_the_metric_of_each_clause(self):
        with Service() as s:
            s.put('chelsea', fact(name='Chelsea', period='2004/05', value=15, metric='goals_against'))
            s.put('city', fact(name='Manchester City', period='2017/18', value=106, metric='goals_for'))
            result=query(s, 'How many goals did Chelsea concede in 2004/05, and how many did Manchester City score in 2017/18?')
            self.assertTrue(result['plan_satisfied'], result)
            self.assertEqual([c['metrics'] for c in result['plan']['clauses']], [['goals_against'], ['goals_for']])
            self.assertEqual([r['facts'][0]['value'] for r in result['results']], [15, 106])

    def test_unsupported_qualifiers_and_ambiguous_entities(self):
        with Service() as s:
            s.put('a', fact(aliases=['shared'], basis='earned_before_adjustments'))
            s.put('b', fact(name='Beacon', aliases=['shared']))
            self.assertFalse(query(s, 'shared 2024/25 points')['plan_satisfied'])
            self.assertFalse(query(s, 'Atlas 2024/25 official points after deductions')['plan_satisfied'])
            self.assertTrue(query(s, 'Atlas 2024/25 points before deductions')['plan_satisfied'])
            self.assertFalse(query(s, 'Atlas points in 2026/27')['plan_satisfied'])
            self.assertFalse(query(s, 'Atlas 2024/25 points excluding penalties')['plan_satisfied'])
            self.assertFalse(query(s, 'Who managed Atlas in 2024/25?')['plan_satisfied'])
            self.assertFalse(query(s, 'Atlas points')['plan_satisfied'])
            s.put('dated', md('A dated score.', 'entity: Atlas\ndate: "2024-01-01"\nfacts: {score: "1-0"}\n'))
            self.assertFalse(query(s, 'Compare Atlas score on 2024-01-01 and 2024-02-01')['plan_satisfied'])

    def test_multi_metrics_conflicts_staleness_and_fact_basis(self):
        with Service() as s:
            s.put('a', md('Declared measures.', 'entity: Atlas\nperiod: "2024/25"\nfacts: {won: 12, drawn: 4}\n'))
            value=query(s, 'How many wins and draws did Atlas have in 2024/25?')
            self.assertTrue(value['plan_satisfied'], value)
            self.assertEqual({f['metric'] for f in value['results'][0]['facts']}, {'won', 'drawn'})
            s.put('conflict', md('Different measure.', 'entity: Atlas\nperiod: "2024/25"\nfacts: {won: 13, drawn: 4}\n'))
            self.assertFalse(query(s, 'Atlas wins in 2024/25')['plan_satisfied'])
            s.put('stale', fact(name='Stale', extra='stale_after: "2000-01-01T00:00:00Z"\n'))
            self.assertFalse(query(s, 'Stale points in 2024/25')['plan_satisfied'])
            s.put('basis', md('Measure with a fact qualification.', 'entity: Beacon\nperiod: "2024/25"\nfacts: {points: {value: 10, basis: earned_before_adjustments}}\n'))
            self.assertTrue(query(s, 'Beacon points before deductions in 2024/25')['plan_satisfied'])

    def test_caller_plan_filters_and_validation(self):
        with Service() as s:
            s.put('a', fact(metric='latency', extra='document_kind: Measurement\n'))
            value = query(s, 'Find the deployment measurement', plan={'clauses': [{'entities': ['Atlas'], 'period': '2024/25', 'metrics': ['latency'], 'kind': 'Measurement'}]})
            self.assertTrue(value['plan_satisfied'], value)
            self.assertEqual(value['planner'], 'caller_supplied')
            for changes in [dict(limit=0), dict(max_chars=10), dict(plan={'clauses': []}), dict(plan={'clauses': [{'sql': 'DROP TABLE concepts'}]}), dict(plan={'clauses': [{'date': '2024-02-30'}]}), dict(question='x '*97), dict(question='???')]:
                self.assertEqual(http(s.url, '/v1/query', {'question': 'Atlas', **changes})[0], 400)
            self.assertEqual(http(s.url, '/v1/query', {'question': 'Atlas', 'unexpected': True})[0], 400)
            self.assertEqual(http(s.url, '/v1/query', raw=b' ' * 17000)[0], 413)

    def test_browse_and_scoped_graph_have_limits_labels_and_headings(self):
        with Service() as s:
            s.put('root', md('# Evidence\n[Atlas](./data/a.md)\n[Beacon](./data/b.md)\n[Missing](./missing.md)'))
            s.put('data/a', fact(extra='document_kind: Measurement\n'))
            s.put('data/b', fact(name='Beacon', period='2025/26', extra='document_kind: Measurement\n'))
            status, value = http(s.url, '/v1/neighbors?' + urllib.parse.urlencode({'id': 'root', 'period': '2024/25', 'entity': 'Atlas', 'kind': 'Measurement', 'limit': 1}))
            self.assertEqual(status, 200, value)
            self.assertEqual([r['id'] for r in value['neighbors']], ['data/a'])
            self.assertEqual((value['neighbors'][0]['label'], value['neighbors'][0]['heading']), ('Atlas', 'Evidence'))
            self.assertFalse(value['has_more'])
            incoming = http(s.url, '/v1/neighbors?id=data/a&direction=incoming')[1]
            self.assertEqual(incoming['neighbors'][0]['id'], 'root')
            page = http(s.url, '/v1/browse?prefix=data/&limit=1')[1]
            self.assertEqual(page['next_offset'], 1)
            self.assertEqual(len(page['concepts']), 1)
            page2 = http(s.url, '/v1/browse?prefix=data/&period=2025%2F26')[1]
            self.assertEqual([r['id'] for r in page2['concepts']], ['data/b'])
            self.assertEqual(http(s.url, '/v1/neighbors?id=root&limit=1000')[0], 400)

    def test_revision_atomicity_updates_aliases_facts_and_deletion(self):
        with Service() as s:
            revision = s.put('a', fact(aliases=['oldalias']))['revision']
            def writer():
                nonlocal revision
                for i in range(12):
                    revision = s.put('a', fact(value=i, aliases=['newalias'], body=f'Version {i}.'), expectedRevision=revision)['revision']
            def reader():
                seen = []
                for _ in range(18):
                    result = query(s, 'Atlas 2024/25 points')
                    row = result['results'][0]
                    self.assertTrue(result['plan_satisfied'])
                    seen.append((row['revision'], row['facts'][0]['value']))
                return seen
            with futures.ThreadPoolExecutor(max_workers=5) as pool:
                write = pool.submit(writer)
                readers = [pool.submit(reader) for _ in range(4)]
                write.result()
                seen = [v for r in readers for v in r.result()]
            for rev, value in seen:
                if value != 12:
                    import hashlib
                    self.assertEqual(rev, hashlib.sha256(fact(value=value, aliases=['newalias'], body=f'Version {value}.').encode()).hexdigest())
            self.assertFalse(query(s, 'oldalias 2024/25 points')['plan_satisfied'])
            self.assertTrue(query(s, 'newalias 2024/25 points')['plan_satisfied'])
            self.assertEqual(http(s.url, '/v1/operations', {'requestId': 'delete', 'actor': 'test/1', 'action': 'delete', 'id': 'a', 'expectedRevision': revision})[0], 200)
            self.assertEqual(query(s, 'Atlas 2024/25 points')['results'], [])

    def test_recovery_and_derived_schema_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            with Service(directory) as s:
                receipt = s.put('a', fact())
            path = pathlib.Path(directory)
            before = (path / 'knowledge/a.md').read_bytes()
            with contextlib.closing(sqlite3.connect(path / 'index.sqlite')) as conn:
                conn.execute('PRAGMA user_version=3')
            with Service(directory) as s:
                value = query(s, 'Atlas 2024/25 points')
                self.assertTrue(value['plan_satisfied'])
                self.assertEqual(value['results'][0]['revision'], receipt['revision'])
            self.assertEqual((path / 'knowledge/a.md').read_bytes(), before)

    def test_cli_and_direct_mcp_query(self):
        with Service() as s:
            s.put('a', fact())
            proc = subprocess.run([BINARY, 'query', 'Atlas 2024/25 points'], env={**os.environ, 'JBRAIN_URL': s.url, 'JBRAIN_TOKEN': ''}, text=True, capture_output=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(json.loads(proc.stdout)['plan_satisfied'])
            headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'}
            for name, args in [('brain_query', {'question': 'Atlas 2024/25 points'}), ('brain_browse', {'prefix': 'a'}), ('brain_neighbors', {'id': 'a'})]:
                status, value = http(s.url, '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}, headers)
                self.assertEqual(status, 200, value)
                self.assertFalse(value['result'].get('isError', False), value)
                if name == 'brain_query':
                    self.assertTrue(json.loads(value['result']['content'][0]['text'])['plan_satisfied'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
