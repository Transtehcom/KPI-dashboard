import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


FAKE_SERVER = r'''
import json, os, sys, time, tomllib
scenario, trace = sys.argv[1:3]
config = {}
for setting in json.loads(sys.argv[3]):
    parsed = tomllib.loads(setting)
    def merge(a, b):
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(a.get(k), dict): merge(a[k], v)
            else: a[k] = v
    merge(config, parsed)
def send(message):
    print(json.dumps(message), flush=True)
def emit(method, params):
    send({'method': method, 'params': params})
def finish(turn='turn-1'):
    emit('turn/completed', {'threadId': 'thread-1', 'turn': {'id': turn,
         'status': 'completed', 'items': [{'id': 'answer', 'type': 'agentMessage',
         'phase': 'final_answer', 'text': '{"answer":"готово"}'}]}})
for line in sys.stdin:
    m = json.loads(line)
    with open(trace, 'a', encoding='utf-8') as f: f.write(json.dumps(m) + '\n')
    method = m.get('method')
    if method == 'initialize':
        if scenario == 'startup_request':
            send({'id':901,'method':'account/chatgptAuthTokens/refresh','params':{}})
        else: send({'id': m['id'], 'result': {'userAgent': 'fake'}})
    elif method == 'config/read':
        if scenario == 'unsafe_config': config['mcp_servers'] = {'surprise': {'enabled': True}}
        send({'id': m['id'], 'result': {'config': config}})
    elif method in ('thread/start', 'thread/resume'):
        ident = 'wrong-thread' if scenario == 'wrong_resume' else 'thread-1'
        send({'id': m['id'], 'result': {'thread': {'id': ident}, 'model': 'test-model'}})
    elif method == 'turn/start':
        send({'id': m['id'], 'result': {'turn': {'id': 'turn-1', 'status': 'inProgress', 'items': []}}})
        if scenario in ('wait', 'stderr'):
            if scenario == 'stderr': sys.stderr.write('x' * 200000); sys.stderr.flush()
        elif scenario == 'malformed': print('not-json', flush=True)
        elif scenario == 'builtin':
            emit('item/started', {'threadId':'thread-1','turnId':'turn-1',
                                 'item':{'id':'unsafe','type':'commandExecution'}})
        elif scenario == 'delta':
            emit('item/agentMessage/delta', {'threadId':'thread-1','turnId':'turn-1',
                                            'itemId':'answer','delta':'PRIVATE RAW TEXT'})
            finish()
        elif scenario == 'failed':
            emit('turn/completed', {'threadId':'thread-1','turn':{'id':'turn-1',
                 'status':'failed','items':[],'error':{'message':'secret provider detail'}}})
        elif scenario == 'wrong_completed': finish('another-turn')
        elif scenario.startswith('tool'):
            params = {'threadId':'other' if scenario == 'tool_wrong_thread' else 'thread-1',
                      'turnId':'other' if scenario == 'tool_wrong_turn' else 'turn-1',
                      'callId':'call-1','tool':'unknown' if scenario == 'tool_unknown' else 'lookup',
                      'arguments':{'query':'hello'}}
            send({'id':900,'method':'item/tool/call','params':params})
            if scenario == 'tool_blocked_write':
                time.sleep(1.5)
                with open(trace, 'a', encoding='utf-8') as f: f.write('{"fixture":"unblocked"}\n')
        elif scenario == 'unknown_request':
            send({'id':900,'method':'item/commandExecution/requestApproval','params':{}})
        else: finish()
    elif method == 'turn/interrupt':
        send({'id':m['id'],'result':{}})
    elif m.get('id') == 900: finish()
    elif m.get('id') == 901: send({'id':1,'result':{'userAgent':'fake'}})
'''


class AppServerTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('app.gpt_appserver'), 'Native transport is missing')
        from app import gpt_appserver
        self.module = gpt_appserver
        base = Path('data/test-tmp')
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.cwd = str(Path(self.temp.name).resolve())
        patch.dict(os.environ, {'CODEX_HOME': self.cwd}).start()
        (Path(self.cwd) / 'models_cache.json').write_text(json.dumps({'identity':{'account':'never-copy'},
            'models':[{'slug':'test-model','apply_patch_tool_type':'freeform',
                       'experimental_supported_tools':['clock'], 'node_repl_disabled':False,
                       'supported_reasoning_levels':[{'reasoning_effort':'low'}]}]}), encoding='utf-8')
        self.trace = Path(self.cwd) / 'trace.jsonl'
        self.processes = []
        self.commands = []
        self.scenario = 'normal'
        real_popen = subprocess.Popen

        def spawn(command, **kwargs):
            if 'app-server' not in command:
                return real_popen(command, **kwargs)
            self.commands.append(command)
            overrides = [command[i + 1] for i, part in enumerate(command) if part == '-c']
            child = real_popen([sys.executable, '-u', '-c', FAKE_SERVER, self.scenario,
                                str(self.trace), json.dumps(overrides)], **kwargs)
            self.processes.append(child)
            return child

        self.addCleanup(patch.stopall)
        patch.object(gpt_appserver.subprocess, 'Popen', side_effect=spawn).start()
        patch.object(gpt_appserver.call_analysis, 'codex_path', return_value='codex').start()
        self.tools = [{'type':'function', 'name':'lookup', 'description':'Read scoped data',
                       'inputSchema':{'type':'object','properties':{'query':{'type':'string'}}}}]
        self.calls = []
        self.saved = []
        self.progress = []

    def messages(self):
        return [json.loads(line) for line in self.trace.read_text(encoding='utf-8').splitlines()]

    def run_turn(self, server, thread_id=None, **kwargs):
        def tool(name, args):
            self.calls.append((name, args))
            return [{'type':'inputText','text':'scoped result'}]
        values = dict(thread_id=thread_id, master='trusted instructions',
                      input=[{'type':'text','text':'hello'}], model='test-model', effort='low',
                      fast=False, output_schema={'type':'object'}, tools=self.tools,
                      tool_handler=tool, cancel_event=threading.Event(),
                      on_progress=self.progress.append, on_thread=self.saved.append)
        values.update(kwargs)
        return server.run(**values)

    def test_native_turn_persists_thread_before_model_request_and_reuses_loaded_thread(self):
        def persist(thread):
            self.assertFalse(any(m.get('method') == 'turn/start' for m in self.messages()))
            self.saved.append(thread)
        with self.module.AppServer({}, self.cwd) as server:
            self.assertEqual(self.run_turn(server, on_thread=persist), {'answer':'готово'})
            self.assertEqual(self.run_turn(server, thread_id='thread-1'), {'answer':'готово'})
        methods = [m.get('method') for m in self.messages()]
        self.assertEqual(methods.count('thread/start'), 1)
        self.assertEqual(methods.count('turn/start'), 2)
        self.assertNotIn('thread/resume', methods)
        self.assertEqual(self.saved, ['thread-1', 'thread-1'])
        self.assertIsNotNone(self.processes[0].poll())
        self.assertTrue(all(s.closed for s in (self.processes[0].stdin, self.processes[0].stdout, self.processes[0].stderr)))

    def test_resume_passes_exact_bound_thread_and_selection(self):
        with self.module.AppServer({}, self.cwd) as server:
            self.run_turn(server, 'thread-1', fast=True, effort='high')
        messages = self.messages()
        resume = next(m['params'] for m in messages if m.get('method') == 'thread/resume')
        turn = next(m['params'] for m in messages if m.get('method') == 'turn/start')
        self.assertEqual(resume['threadId'], 'thread-1')
        self.assertEqual(turn['serviceTier'], 'fast')
        self.assertEqual(turn['effort'], 'high')
        self.assertEqual(turn['model'], 'test-model')
        self.assertEqual(turn['environments'], [])
        self.assertNotIn('baseInstructions', resume)
        self.assertNotIn('developerInstructions', resume)

    def test_model_capabilities_remove_native_file_tools_without_copying_account_identity(self):
        with self.module.AppServer({}, self.cwd) as server:
            command = self.commands[0]
            setting = next(part for part in command if part.startswith('model_catalog_json='))
            path = Path(json.loads(setting.split('=', 1)[1]))
            catalog = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(set(catalog), {'models'})
            self.assertIsNone(catalog['models'][0]['apply_patch_tool_type'])
            self.assertEqual(catalog['models'][0]['experimental_supported_tools'], [])
            self.assertTrue(catalog['models'][0]['node_repl_disabled'])
            self.assertEqual(catalog['models'][0]['slug'], 'test-model')
        self.assertFalse(path.exists())

    def test_native_tool_result_preserves_content(self):
        self.scenario = 'tool'
        image = 'data:image/png;base64,' + 'A' * 200000
        with self.module.AppServer({}, self.cwd) as server:
            self.run_turn(server, tool_handler=lambda name, args: [{'type':'inputImage','imageUrl':image}])
        response = next(m['result'] for m in self.messages() if m.get('id') == 900)
        self.assertEqual(response, {'success':True,'contentItems':[{'type':'inputImage','imageUrl':image}]})

    def test_foreign_or_unknown_tool_requests_never_reach_handler(self):
        for scenario in ('tool_wrong_thread', 'tool_wrong_turn', 'tool_unknown'):
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                with self.module.AppServer({}, self.cwd) as server:
                    self.run_turn(server)
                self.assertEqual(self.calls, [])
                response = [m for m in self.messages() if m.get('id') == 900][-1]
                self.assertFalse(response['result']['success'])

    def test_tool_exception_becomes_safe_error_for_agent(self):
        self.scenario = 'tool'
        def broken(*args):
            raise RuntimeError('provider credentials password=private')
        with self.module.AppServer({}, self.cwd) as server:
            self.run_turn(server, tool_handler=broken)
        response = next(m['result'] for m in self.messages() if m.get('id') == 900)
        self.assertFalse(response['success'])
        self.assertNotIn('private', json.dumps(response))

    def test_unknown_server_request_is_denied(self):
        self.scenario = 'unknown_request'
        with self.module.AppServer({}, self.cwd) as server:
            self.run_turn(server)
        response = next(m for m in self.messages() if m.get('id') == 900)
        self.assertIn('error', response)
        self.assertEqual(self.calls, [])

    def test_unknown_request_during_initialization_is_denied_without_deadlock(self):
        self.scenario = 'startup_request'
        with patch.object(self.module, 'STARTUP_TIMEOUT', 1):
            with self.module.AppServer({}, self.cwd) as server:
                self.run_turn(server)
        response = next(m for m in self.messages() if m.get('id') == 901)
        self.assertIn('error', response)

    def test_inherited_connector_and_dotted_plugin_names_are_disabled(self):
        (Path(self.cwd) / 'config.toml').write_text(
            '[mcp_servers.connector]\ncommand="forbidden"\nenabled=true\n'
            '[plugins."plugin@market.place"]\nenabled=true\n', encoding='utf-8')
        with self.module.AppServer({}, self.cwd) as server:
            self.run_turn(server)

    def test_unrepresentable_mcp_name_fails_before_starting_process(self):
        (Path(self.cwd) / 'config.toml').write_text(
            '[mcp_servers."a.b"]\ncommand="forbidden"\nenabled=true\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'изолир'):
            with self.module.AppServer({}, self.cwd):
                pass
        self.assertEqual(self.processes, [])

    def test_cancellation_while_tool_runs_does_not_wait_for_handler(self):
        self.scenario = 'tool'
        event = threading.Event()
        done = threading.Event()
        def blocked(*_args):
            event.set()
            done.wait(2)
            return [{'type':'inputText','text':'late result'}]
        try:
            with self.assertRaisesRegex(RuntimeError, 'отмен'):
                with self.module.AppServer({}, self.cwd) as server:
                    self.run_turn(server, tool_handler=blocked, cancel_event=event)
        finally:
            done.set()
        self.assertFalse(any(m.get('id') == 900 for m in self.messages()))

    def test_unsafe_inherited_mcp_aborts_before_thread_start(self):
        self.scenario = 'unsafe_config'
        with self.assertRaisesRegex(RuntimeError, 'изоляц'):
            with self.module.AppServer({}, self.cwd) as server:
                self.run_turn(server)
        self.assertFalse(any(m.get('method') == 'thread/start' for m in self.messages()))

    def test_cancel_interrupts_and_cleans_up(self):
        self.scenario = 'wait'
        event = threading.Event()
        def progress(text):
            event.set()
        with self.assertRaisesRegex(RuntimeError, 'отмен'):
            with self.module.AppServer({}, self.cwd) as server:
                self.run_turn(server, cancel_event=event, on_progress=progress)
        self.assertIn('turn/interrupt', [m.get('method') for m in self.messages()])
        self.assertIsNotNone(self.processes[0].poll())

    def test_timeout_drains_stderr_and_has_no_retry(self):
        self.scenario = 'stderr'
        with patch.object(self.module, 'TURN_TIMEOUT', 0.2):
            with self.assertRaisesRegex(RuntimeError, 'время'):
                with self.module.AppServer({}, self.cwd) as server:
                    self.run_turn(server)
        self.assertEqual(len(self.processes), 1)
        self.assertIsNotNone(self.processes[0].poll())

    def test_blocked_large_tool_write_obeys_deadline(self):
        self.scenario = 'tool_blocked_write'
        with patch.object(self.module, 'TURN_TIMEOUT', 0.2):
            with self.assertRaisesRegex(RuntimeError, 'время'):
                with self.module.AppServer({}, self.cwd) as server:
                    self.run_turn(server, tool_handler=lambda *_: [{'type':'inputText','text':'a' * 1000000}])
        self.assertFalse(any(m.get('fixture') == 'unblocked' for m in self.messages()))
        self.assertIsNotNone(self.processes[0].poll())

    def test_bad_protocol_failed_turn_and_wrong_resume_fail_closed(self):
        for scenario in ('malformed','builtin','failed','wrong_resume','wrong_completed'):
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                with patch.object(self.module, 'TURN_TIMEOUT', 0.2):
                    with self.assertRaises(RuntimeError) as caught:
                        with self.module.AppServer({}, self.cwd) as server:
                            self.run_turn(server, thread_id='thread-1' if scenario == 'wrong_resume' else None)
                self.assertNotIn('secret provider detail', str(caught.exception))

    def test_progress_does_not_expose_raw_model_text(self):
        self.scenario = 'delta'
        with self.module.AppServer({}, self.cwd) as server:
            self.run_turn(server)
        self.assertTrue(self.progress)
        self.assertNotIn('PRIVATE RAW TEXT', '\n'.join(self.progress))


if __name__ == '__main__':
    unittest.main()
