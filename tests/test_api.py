"""Exercise HTTP contracts, resource limits and actual workflow integration offline."""

import base64
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from api.main import ApiSettings, ModelNotConfigured, create_app
from agent.analytics_agent import build_agent
from agent.observability import ObservationSettings, invoke_analysis
from eval.phase0_baseline import create_fixture
from tools import sql_tool

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jV1sAAAAASUVORK5CYII=')


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.factory = Mock(return_value=object())
        self.result = {"status": "completed", "final_answer": "Verified result", "artifacts": [],
                       "metrics": {"model_calls": 2, "tool_calls": 1}}
        self.invoker = Mock(side_effect=lambda *a, **kw: dict(self.result))

    def app(self, **kwargs):
        return create_app(agent_factory=self.factory, invoker=self.invoker, output_dir=self.root, **kwargs)

    def test_health_does_not_construct_model_or_need_credentials(self):
        app = self.app()
        with patch.dict('os.environ', {'OPENAI_API_KEY': ''}), patch.object(sql_tool, 'DB_PATH', self.root/'missing.db'):
            with TestClient(app) as client:
                self.assertEqual(client.get('/health/live').status_code, 200)
                response = client.get('/health/ready')
                self.assertEqual(response.status_code, 503)
                self.assertFalse(response.json()['ready'])
        self.factory.assert_not_called()
        self.assertFalse((self.root/'missing.db').exists())

    def test_readiness_checks_readable_fixture_but_not_provider_authentication(self):
        path = self.root/'fixture.db'
        create_fixture(path)
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'configured-placeholder'}), patch.object(sql_tool, 'DB_PATH', path):
            with TestClient(self.app()) as client:
                self.assertTrue(client.get('/health/ready').json()['ready'])
        self.factory.assert_not_called()

    def test_independent_requests_have_server_generated_ids_and_no_message_history(self):
        with TestClient(self.app()) as client:
            first = client.post('/v1/analyses', json={'question': ' First question '})
            second = client.post('/v1/analyses', json={'question': 'Second question'})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first.json()['request_id'], second.json()['request_id'])
        self.assertNotIn('messages', first.json())
        self.factory.assert_called_once()
        calls = self.invoker.call_args_list
        self.assertEqual(calls[0].args[1]['messages'], [{'role': 'user', 'content': 'First question'}])
        self.assertEqual(len(calls[1].args[1]['messages']), 1)
        self.assertEqual(str(calls[0].kwargs['config']['run_id']), first.json()['request_id'])

    def test_invalid_questions_and_extra_fields_do_not_call_agent_or_echo_inputs(self):
        bodies = [{'question': ''}, {'question': '  '}, {'question': 'SECRET'*1001},
                  {'question': 123}, {'question': 'SECRET', 'thread_id': 'private-session'}]
        with TestClient(self.app()) as client:
            for body in bodies:
                response = client.post('/v1/analyses', json=body)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn('SECRET', response.text)
        self.factory.assert_not_called()

    def test_failed_workflow_is_preserved_as_failed(self):
        self.result.update(status='failed', final_answer='No verified result is available.')
        with TestClient(self.app()) as client:
            response = client.post('/v1/analyses', json={'question': 'Analyze'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'failed')
        self.assertIn('No verified', response.json()['answer'])

    def test_busy_request_returns_429_without_starting_more_work(self):
        app = self.app(settings=ApiSettings(max_active=1))
        app.state.runtime.slots.acquire()
        try:
            with TestClient(app) as client:
                response = client.post('/v1/analyses', json={'question': 'Analyze'})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers['retry-after'], '5')
            self.factory.assert_not_called()
        finally:
            app.state.runtime.slots.release()

    def test_errors_are_sanitized_and_capacity_is_released(self):
        self.invoker.side_effect = [RuntimeError('SECRET key and query'), self.result]
        with TestClient(self.app(settings=ApiSettings(max_active=1))) as client:
            failed = client.post('/v1/analyses', json={'question': 'Analyze'})
            recovered = client.post('/v1/analyses', json={'question': 'Retry'})
        self.assertEqual(failed.status_code, 502)
        self.assertNotIn('SECRET', failed.text)
        self.assertEqual(recovered.status_code, 200)

    def test_parallel_requests_stay_isolated_and_excess_work_is_rejected(self):
        ready, release = threading.Event(), threading.Event()
        lock, questions = threading.Lock(), []
        def invoke(agent, data, config):
            question = data['messages'][0]['content']
            with lock:
                questions.append(question)
                if len(questions) == 2: ready.set()
            if not release.wait(3): raise TimeoutError()
            return {**self.result, 'final_answer': question}
        self.invoker = invoke
        with TestClient(self.app(settings=ApiSettings(max_active=2))) as client, ThreadPoolExecutor(2) as pool:
            requests = [pool.submit(client.post, '/v1/analyses', json={'question': q}) for q in ['first', 'second']]
            try:
                self.assertTrue(ready.wait(3))
                self.assertEqual(client.post('/v1/analyses', json={'question': 'excess'}).status_code, 429)
            finally:
                release.set()
            self.assertEqual(sorted(f.result().json()['answer'] for f in requests), ['first', 'second'])
        self.assertEqual(sorted(questions), ['first', 'second'])

    def test_timeout_and_missing_credentials_have_distinct_status_codes(self):
        for error, status in [(TimeoutError('SECRET'), 504), (ModelNotConfigured(), 503)]:
            self.invoker.side_effect = error
            with TestClient(self.app()) as client:
                response = client.post('/v1/analyses', json={'question': 'Analyze'})
            self.assertEqual(response.status_code, status)
            self.assertNotIn('SECRET', response.text)

    def test_bearer_token_protects_analysis_and_artifacts_but_not_liveness(self):
        with TestClient(self.app(settings=ApiSettings(token='private-access-token'))) as client:
            self.assertEqual(client.get('/health/live').status_code, 200)
            self.assertEqual(client.post('/v1/analyses', json={'question': 'Analyze'}).status_code, 401)
            self.assertEqual(client.get(f'/v1/analyses/{uuid4()}/artifacts/{uuid4()}').status_code, 401)
            response = client.post('/v1/analyses', json={'question': 'Analyze'},
                                   headers={'Authorization': 'Bearer private-access-token'})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('private-access-token', response.text)

    def chart_result(self):
        path = self.root/'private-chart-name.png'
        path.write_bytes(PNG)
        self.result.update(artifacts=[str(path)], final_answer=f'Chart: {path}')
        return path

    def test_chart_link_downloads_verified_bytes_without_exposing_paths(self):
        self.chart_result()
        with TestClient(self.app()) as client:
            result = client.post('/v1/analyses', json={'question': 'Chart'}).json()
            self.assertNotIn(str(self.root), json.dumps(result))
            link = result['artifacts'][0]['url']
            response = client.get(link)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, PNG)
            self.assertEqual(response.headers['content-type'], 'image/png')
            self.assertEqual(client.get(link.replace(result['request_id'], str(uuid4()))).status_code, 404)
            self.assertEqual(client.get('/v1/analyses/x/artifacts/../../.env').status_code, 404)

    def test_chart_tampering_outside_paths_and_symlinks_are_rejected(self):
        path = self.chart_result()
        with TestClient(self.app()) as client:
            response = client.post('/v1/analyses', json={'question': 'Chart'})
            path.write_bytes(PNG+b'tampered')
            self.assertEqual(client.get(response.json()['artifacts'][0]['url']).status_code, 404)
        with tempfile.TemporaryDirectory() as temp:
            outside = Path(temp)/'outside.png'
            outside.write_bytes(PNG)
            self.result['artifacts'] = [str(outside)]
            with TestClient(self.app()) as client:
                self.assertEqual(client.post('/v1/analyses', json={'question': 'Chart'}).status_code, 502)
            alias = self.root/'alias.png'
            alias.symlink_to(outside)
            self.result['artifacts'] = [str(alias)]
            with TestClient(self.app()) as client:
                self.assertEqual(client.post('/v1/analyses', json={'question': 'Chart'}).status_code, 502)

    def test_artifact_index_expires_and_evicts_old_runs(self):
        self.chart_result()
        app = self.app(settings=ApiSettings(max_artifact_runs=1, artifact_ttl_seconds=10))
        clock = [0]
        app.state.runtime.artifacts.clock = lambda: clock[0]
        with TestClient(app) as client:
            first = client.post('/v1/analyses', json={'question': 'Chart'}).json()['artifacts'][0]['url']
            second = client.post('/v1/analyses', json={'question': 'Chart'}).json()['artifacts'][0]['url']
            self.assertEqual(client.get(first).status_code, 404)
            self.assertEqual(client.get(second).status_code, 200)
            clock[0] = 11
            self.assertEqual(client.get(second).status_code, 404)

    def test_real_sql_workflow_and_answer_validator_through_http_without_model_calls(self):
        class EvidenceModel:
            def bind_tools(self, tools): return self
            def invoke(self, messages, config=None):
                if messages[-1].type == 'human':
                    return AIMessage(content='', tool_calls=[{'name': 'query_retail_database',
                        'args': {'sql_query': 'SELECT SUM(Revenue) AS revenue FROM transactions'}, 'id': 'sql-1'}])
                evidence = messages[-1].artifact
                return AIMessage(content=json.dumps({'answer_template': 'Total: {{c0}}.',
                    'claims': [{'id': 'c0', 'source': evidence['id'], 'field': 'revenue', 'row': 0}]}))
        fixture = self.root/'fixture.db'
        create_fixture(fixture)
        app = create_app(agent_factory=lambda: build_agent(model=EvidenceModel()), output_dir=self.root,
                         invoker=lambda *a, **kw: invoke_analysis(*a, **kw, settings=ObservationSettings(), metrics_dir=None))
        with patch.object(sql_tool, 'DB_PATH', fixture), TestClient(app) as client:
            response = client.post('/v1/analyses', json={'question': 'Total revenue?'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['answer'], 'Total: 138.')
        self.assertEqual(response.json()['status'], 'completed')
        self.assertEqual(response.json()['metrics']['tool_calls'], 1)


if __name__ == '__main__':
    unittest.main()
