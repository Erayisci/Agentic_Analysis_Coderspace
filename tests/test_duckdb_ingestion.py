"""Typed imports, migration and PDF validation must survive API/session lifetimes."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import duckdb
import pandas as pd
import pytest

from backend.agent.evidence_store import EvidenceStore, EvidenceStorageError
from backend.tools.bist_precious_metals import COLUMNS, MONTHS, parse_gold_pdf
from backend.tools.external_series import ingest_external_series


def gold_text(year=2026):
    lines = [f'PRECIOUS METALS MARKET GOLD TRADING DATA ({year})',
             'AY/MONTH TL USD EUR TOPLAM / TOTAL',
             'Hacim/Volume (TL) Hacim/Volume (USD) Hacim/Volume (EUR)',
             '(KG) (KG) (KG) (KG)',
             'Number of Trans. Number of Trans. Number of Trans.',
             'Ocak / January 93.824.682.381 13.837 1017 3.051.079.209 19.747 825 0 0 0 33.584',
             'Şubat / February 90.534.914.196 12.649 839 3.324.117.453 20.070 1.008 0 0 0 32.719']
    lines += [f'Month / {month} 0' for month in MONTHS[2:]]
    lines += ['TOPLAM / TOTAL 999 999 999']
    return '\n'.join(lines)


def parse_mock_pdf(monkeypatch, text):
    monkeypatch.setattr('backend.tools.bist_precious_metals.PdfReader', lambda data: SimpleNamespace(
        pages=[SimpleNamespace(extract_text=lambda **kw: text)]))
    return parse_gold_pdf(b'%PDF-test-input')


def test_gold_adapter_preserves_year_units_and_does_not_invent_future_zeros(monkeypatch):
    frame = parse_mock_pdf(monkeypatch, gold_text(2025))
    assert frame.columns.tolist() == ['period', *COLUMNS]
    assert frame.period.tolist() == ['2025-01-01', '2025-02-01']
    assert frame.total_quantity_kg.tolist() == [33584, 32719]
    assert frame.tl_volume.tolist() == [93824682381, 90534914196]
    result = ingest_external_series('https://example.com/gold.pdf', 'total_quantity_kg', frame=frame)
    assert result.unit == 'kg'
    assert result.temporal_semantics == 'flow'
    assert result.citation()['unit_verified'] is True
    assert result.citation()['document_year'] == 2025
    assert result.citation()['page'] == 1
    with pytest.raises(ValueError, match='Source unit'):
        ingest_external_series('https://example.com/gold.pdf', 'total_quantity_kg', unit='TL', frame=frame)


@pytest.mark.parametrize('replace', [
    ('TL USD EUR', 'USD TL EUR'),
    ('(2026)', '(unknown)'),
    ('33.584', '93.584'),
    ('13.837', '13.?37'),
    ('Ocak / January', 'Ocak / March'),
    ('(KG) (KG)', '(OZ) (KG)'),
])
def test_gold_adapter_refuses_changed_or_ambiguous_schema(monkeypatch, replace):
    with pytest.raises(ValueError):
        parse_mock_pdf(monkeypatch, gold_text().replace(*replace))


def output(values=None):
    return {'status': 'ok', 'citation': {
        'url': 'https://example.com/gold.pdf', 'value_column': 'total_quantity_kg',
        'period_column': 'period', 'unit': 'kg', 'temporal_semantics': 'flow',
        'monthly_rule': 'last', 'document_year': 2026, 'page': 1},
        'values': values if values is not None else [
            {'period': '2026-01-01', 'value': 33584.0}, {'period': '2026-02-01', 'value': 32719.0}]}


def test_observations_are_typed_queryable_versioned_and_survive_restart(tmp_path):
    path = tmp_path / 'research.duckdb'
    store = EvidenceStore(path)
    run = store.start('session', 'question', 'auto')
    store.record(run, 'ingest_external', {}, output())
    store.finish(run, {'summary': 'saved'}, 'ok')
    restarted = EvidenceStore(path)
    assert len(restarted.get_run('session', run)['tool_results']) == 1
    with duckdb.connect(str(path), read_only=True) as conn:
        assert conn.execute('SELECT period, value, unit FROM external_observations_latest ORDER BY period').fetchall() == [
            (pd.Timestamp('2026-01-01').date(), 33584.0, 'kg'),
            (pd.Timestamp('2026-02-01').date(), 32719.0, 'kg')]
        types = dict((r[0], r[1]) for r in conn.execute('DESCRIBE external_observations').fetchall())
        assert types['period'] == 'DATE' and types['value'] == 'DOUBLE'
    # A revised source retains the prior extraction while the latest view is one snapshot.
    run2 = restarted.start('session2', 'revision', 'auto')
    restarted.record(run2, 'ingest_external', {}, output([{'period': '2026-01-01', 'value': 33585.0}]))
    with duckdb.connect(str(path), read_only=True) as conn:
        assert conn.execute('SELECT count(*) FROM external_observations').fetchone()[0] == 3
        assert conn.execute('SELECT value FROM external_observations_latest').fetchall() == [(33585.0,)]


def test_bad_observations_roll_back_evidence_and_series_atomically(tmp_path):
    store = EvidenceStore(tmp_path / 'research.duckdb')
    run = store.start('s', 'q', 'auto')
    bad = output([{'period': 'not-a-date', 'value': 1.0}])
    with pytest.raises(EvidenceStorageError):
        store.record(run, 'ingest_external', {}, bad)
    assert store.get_run('s', run)['tool_results'] == []
    with duckdb.connect(str(store.path), read_only=True) as conn:
        assert conn.execute('SELECT count(*) FROM external_series').fetchone()[0] == 0


def test_legacy_history_migrates_once_without_changing_original_file(tmp_path):
    legacy = tmp_path / 'research.sqlite3'
    with sqlite3.connect(legacy) as conn:
        conn.executescript('''
            CREATE TABLE research_runs(id TEXT, session_id TEXT, question TEXT, mode TEXT, status TEXT,
                                       created_at TEXT, finished_at TEXT, response_json TEXT);
            CREATE TABLE research_tool_results(id INTEGER, run_id TEXT, tool TEXT, arguments_json TEXT,
                                               output_json TEXT, status TEXT, created_at TEXT);
        ''')
        conn.execute('INSERT INTO research_runs VALUES (?,?,?,?,?,?,?,?)',
                     ('legacy', 's', 'q', 'auto', 'ok', '2026-01-01', '2026-01-01', '{"summary":"old"}'))
        conn.execute('INSERT INTO research_tool_results VALUES (?,?,?,?,?,?,?)',
                     (1, 'legacy', 'ingest_external', '{}', json.dumps(output()), 'ok', '2026-01-01'))
    original = legacy.read_bytes()
    store = EvidenceStore(legacy)
    assert store.path.suffix == '.duckdb'
    run = store.start('s', 'new question', 'auto')
    store.record(run, 'search_web', {}, {'status': 'ok'})
    reopened = EvidenceStore(legacy)
    assert reopened.get_run('s', 'legacy')['response']['summary'] == 'old'
    assert len(reopened.list_runs('s')) == 2
    with duckdb.connect(str(store.path), read_only=True) as conn:
        assert conn.execute('SELECT count(*) FROM research_tool_results').fetchone()[0] == 2
        assert conn.execute('SELECT count(*) FROM external_observations').fetchone()[0] == 2
    assert legacy.read_bytes() == original


def test_mixed_plan_sees_pdf_schema_before_ingestion_and_joins_bddk(tmp_path, monkeypatch):
    from backend.agent.pipeline import run_turn
    from backend.agent.planner import Plan, Step
    frame = parse_mock_pdf(monkeypatch, gold_text())
    document = {'kind': 'pdf', 'url': 'https://example.com/gold.pdf', 'text': gold_text(),
                'tabular': {'columns': list(frame.columns), 'preview': frame.to_dict(orient='records'),
                            **frame.attrs['source_metadata']}}
    url = document['url']
    loan = Step(op='fetch_series', source='bulletin', dataset='tuketici_kredileri',
                key='tuketici_kredileri_konut', currency='total', as_name='housing')
    initial = Plan(intent='url_analysis', start='2026-01-01', end='2026-02-01',
                   steps=[Step(op='read_url', url=url), loan])
    final = initial.model_copy(deep=True)
    final.steps += [Step(op='ingest_external', url=url, value_column='total_quantity_kg', as_name='gold'),
                    Step(op='transform', operation='index_to_base', column='housing', base_period='2026-01-01',
                         as_name='housing_index'),
                    Step(op='transform', operation='index_to_base', column='gold', base_period='2026-01-01',
                         as_name='gold_index')]
    decisions = []

    def make_plan(question, session, route, client):
        decisions.append(session.facts.get('external_catalog'))
        return initial if len(decisions) == 1 else final

    monkeypatch.setattr('backend.agent.pipeline.make_plan', make_plan)
    reader = Mock(return_value=document)
    store = EvidenceStore(tmp_path / 'research.duckdb')
    result = run_turn(f'Compare BDDK and {url}', client=Mock(), url_reader=reader,
                      evidence_store=store, compose_answer=False)
    assert decisions[0] is None
    assert decisions[1][0]['columns'] == ['period', *COLUMNS]
    assert reader.call_count == 1
    assert all(row['ok'] for row in result['audit'])
    assert [row['housing'] for row in result['table']['rows']] == [691343, 715799]
    assert [row['gold'] for row in result['table']['rows']] == [33584, 32719]
    assert result['table']['rows'][1]['housing_index'] == pytest.approx(103.53746259)
    assert result['table']['rows'][1]['gold_index'] == pytest.approx(97.42436875)
    saved = store.get_run('default', result['ingestion']['run_id'])
    assert [row['tool'] for row in saved['tool_results']] == ['read_url', 'ingest_external']
    from backend.agent.verifier import quotable_numbers, unsupported_numbers
    facts = quotable_numbers(result['session'])
    assert facts['series']['gold']['change_value'] == -865
    assert facts['sources'][-1]['document_year'] == 2026
    assert unsupported_numbers('Altın 865 kg azaldı; konut bakiyesi 24.456 milyon TL arttı.', facts) == []


def test_url_plan_keeps_routing_and_discovery_does_not_lose_keys_after_urls():
    from pydantic import ValidationError
    from backend.agent.planner import URLPlan
    from backend.tools.lakehouse import discover_concepts
    with pytest.raises(ValidationError):
        URLPlan(intent='unsupported', steps=[])
    question = ('Yerel DuckDB BDDK verisini https://www.example.com/a/b/report.pdf ile birleştir. '
                'Dönem 2026-01-01 ile 2026-02-28. source=bulletin, dataset=tuketici_kredileri, '
                'key=tuketici_kredileri_konut, currency=total. Konut kredisi bakiyesi milyon TL.')
    found = discover_concepts(question, limit=12)
    assert any(c['key'] == 'tuketici_kredileri_konut' and c['source'] == 'bulletin' for c in found['candidates'])
    assert all('https://' not in chunk for chunk in found['concepts'])
