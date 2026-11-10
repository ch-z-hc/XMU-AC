"""二维码离线回归：模拟账号与接口，无网络、无第三方依赖。"""
import contextlib
import importlib.util
import io
import json
import os
import runpy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import quote

spec = importlib.util.spec_from_file_location(
    'auto_checkin', os.environ.get('AC_SRC', str(Path(__file__).with_name('auto_checkin.py'))))
app = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, STUDENT_ID='test-student', PASSWORD='test-password'), \
        patch.dict('sys.modules', requests=SimpleNamespace(),
                   xmulogin=SimpleNamespace(xmulogin=Mock())):
    spec.loader.exec_module(app)

TOKEN = 'test-token+~!%2B'
PAYLOAD = {'courseId': 9, 'rollcallId': 7, 'data': TOKEN}
COURSES = [{'id': 9, 'name': 'Test'}]
FUTURE = '2099-01-01T00:00:00Z'
PAST = '2000-01-01T00:00:00Z'


def response(body=None, status=200):
    return Mock(status_code=status, json=Mock(return_value={} if body is None else body))


def rollcall(**extra):
    return {'id': 7, 'type': 'qr_rollcall', 'status': 'absent', **extra}


def roster(signed=4, mine='absent'):
    return [{'user_no': 'test-student', 'rollcall_status': mine}] + [
        {'user_no': f'other-{i}', 'rollcall_status': 'on_call_fine' if i < signed else None}
        for i in range(9)]


class QrTests(unittest.TestCase):
    def test_payload_formats(self):
        compact = '!0~\x109!3~test-token+\x1f\x1e%2B!4~\x107'
        expected = {'courseId': '9', 'rollcallId': '7', 'data': TOKEN}
        for raw in (json.dumps(PAYLOAD), compact):
            for wrapped in (raw, '/j?p=' + quote(raw, safe=''),
                            'tronclass://scanner?_p=' + quote(raw, safe=''),
                            'https://example.invalid/_wg_qr?_wg_p=' + quote(raw, safe='') + '&other=1',
                            'https://example.invalid/#/j?p=' + quote(raw, safe='')):
                with self.subTest(payload=wrapped):
                    self.assertEqual(app.parse_qr_payload(wrapped), expected)
        self.assertEqual(app.parse_qr_payload('!4~\x10z!3~abc')['rollcallId'], '35')
        self.assertEqual(app.parse_qr_payload('!4~7!3~abc\x1f')['data'], 'abc~')
        self.assertEqual(app.parse_qr_payload(json.dumps({'rollcallId': '007', 'data': 'abc'})),
                         {'rollcallId': '7', 'data': 'abc'})

    def test_invalid_payloads(self):
        cases = ['', None, {}, 'x' * 16385, '[]', 'null', '{', '!3~abc',
                 '!4~7!4~8!3~abc', '!4~\x10z.z!3~abc', '!4~\x10?!3~abc',
                 'https://evil.invalid/!4~7!3~abc', '/j?p=%7Bbad', '!4~7!3~\x101']
        for rid in (None, True, -1, 0, 7.0, {}, '7/answer', '７'):
            cases.append(json.dumps({'rollcallId': rid, 'data': 'abc'}))
        for token in ('', ' ', None, 7, {}):
            cases.append(json.dumps({'rollcallId': 7, 'data': token}))
        cases.append(json.dumps({**PAYLOAD, 'courseId': None}))
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertIsNone(app.parse_qr_payload(raw))

    def test_fetch(self):
        session = Mock()
        for field in ('data', 'qr_code'):
            session.get.return_value = response({field: TOKEN})
            self.assertEqual(app.get_qr_token(7, 9, session), (TOKEN, ''))
        session.get.assert_called_with(app.BASE_URL + '/api/course/9/rollcall/7/qr_code', timeout=10)
        for body in ([], {}, {'data': 3}, {'data': {'token': TOKEN}}, {'data': ' '}):
            session.get.return_value = response(body)
            self.assertIsNone(app.get_qr_token(7, 9, session)[0])
        for code in (401, 403, 404, 500):
            session.get.return_value = response({'data': TOKEN}, code)
            self.assertIsNone(app.get_qr_token(7, 9, session)[0])
        session.get.return_value = response()
        session.get.return_value.json.side_effect = ValueError(TOKEN)
        self.assertIsNone(app.get_qr_token(7, 9, session)[0])
        session.get.side_effect = OSError(TOKEN)
        self.assertNotIn(TOKEN, app.get_qr_token(7, 9, session)[1])

    def test_submit_and_confirmation(self):
        session = Mock()
        session.put.return_value = response()
        session.get.side_effect = [response({'student_rollcalls': roster(mine=state)})
                                   for state in ('absent', 'on_call_fine')]
        with patch.object(app.time, 'sleep'):
            self.assertTrue(app.answer_qr(7, TOKEN, session)[0])
        session.put.assert_called_once()
        self.assertEqual(session.put.call_args.args[0], app.BASE_URL + '/api/rollcall/7/answer_qr_rollcall')
        self.assertEqual(session.put.call_args.kwargs['json']['data'], TOKEN)
        self.assertTrue(session.put.call_args.kwargs['json']['deviceId'])
        for state in ('absent', None):
            session.get.side_effect = None
            session.get.return_value = response({'status': 'in_progress',
                                                'student_rollcalls': roster(mine=state)})
            with patch.object(app.time, 'sleep'):
                self.assertFalse(app.answer_qr(7, TOKEN, session)[0])
        session.get.return_value = response({'student_rollcalls': []})
        self.assertFalse(app.answer_qr(7, TOKEN, session)[0])

    def test_submit_errors_are_redacted(self):
        session = Mock()
        for code in (400, 401, 403, 500):
            session.put.return_value = response({'message': TOKEN, 'error': 'qr_code_expired'}, code)
            ok, reason = app.answer_qr(7, TOKEN, session)
            self.assertFalse(ok)
            self.assertIn('qr_code_expired', reason)
            self.assertNotIn(TOKEN, reason)
        session.get.assert_not_called()
        session.put.side_effect = OSError(TOKEN)
        self.assertNotIn(TOKEN, app.answer_qr(7, TOKEN, session)[1])
        session.reset_mock()
        self.assertFalse(app.answer_qr(7, '', session)[0])
        session.put.assert_not_called()

    def manual(self, rc=None, signed=4, end=FUTURE, detail_status='in_progress', payload=None):
        session = Mock()
        session.get.return_value = response({'student_rollcalls': roster(signed)})
        with patch.object(app, 'get_rollcalls', return_value=[rc or rollcall()]), \
                patch.object(app, 'get_rollcall_detail', return_value=(None, detail_status, end)), \
                patch.object(app, 'answer_qr', return_value=(True, 'confirmed')) as answer:
            result = app.submit_qr_payload(json.dumps(PAYLOAD if payload is None else payload),
                                           session, 1, COURSES)
        return result, answer

    def test_manual_skips_gate_but_checks_target(self):
        for signed in (0, 3):
            with patch.object(app, "checkin_gate", side_effect=AssertionError("manual must not throttle")):
                result, answer = self.manual(signed=signed)
            self.assertTrue(result[0])
            answer.assert_called_once()
        result, answer = self.manual(signed=4)
        self.assertTrue(result[0])
        answer.assert_called_once()
        for kwargs in ({'end': PAST}, {'detail_status': 'finished'},
                       {'rc': rollcall(status='finished')}, {'rc': rollcall(status=None)},
                       {'rc': rollcall(type='other')}, {'payload': {**PAYLOAD, 'courseId': 10}},
                       {'payload': {**PAYLOAD, 'rollcallId': 8}}):
            result, answer = self.manual(**kwargs)
            self.assertFalse(result[0], kwargs)
            answer.assert_not_called()
        result, answer = self.manual(rc=rollcall(status='on_call_fine'))
        self.assertTrue(result[0])
        answer.assert_not_called()
        result, answer = self.manual(payload={'rollcallId': 7, 'data': TOKEN})
        self.assertTrue(result[0])
        answer.assert_called_once()

    def test_manual_roster_unavailable_is_not_throttled(self):
        session = Mock()
        session.get.return_value = response({}, 500)
        with patch.object(app, 'get_rollcalls', return_value=[rollcall()]), \
                patch.object(app, 'get_rollcall_detail', return_value=(None, 'in_progress', FUTURE)), \
                patch.object(app, 'answer_qr', return_value=(True, 'confirmed')) as answer:
            self.assertTrue(app.submit_qr_payload(json.dumps(PAYLOAD), session, 1, COURSES)[0])
            answer.assert_called_once()

    def loop(self, rows=None, signed=4, end=FUTURE, detail_status='in_progress',
             token=TOKEN, success=True, rounds=2):
        session = Mock()
        session.get.return_value = response({'student_rollcalls': roster(signed)})
        clock = iter(range(100, 10000, 20))
        with patch.object(app, 'do_login', return_value=(session, 1)), \
                patch.object(app, 'get_courses', return_value=COURSES), \
                patch.object(app, 'get_rollcalls', return_value=rows or [rollcall()]), \
                patch.object(app, 'get_rollcall_detail', return_value=(None, detail_status, end)), \
                patch.object(app, 'get_qr_token', return_value=(token, 'fetch failed')) as fetch, \
                patch.object(app, 'answer_qr', return_value=(success, 'confirmed')) as answer, \
                patch.object(app, 'dashboard'), patch.object(app.time, 'time', side_effect=lambda: next(clock)), \
                patch.object(app.time, 'sleep', side_effect=[None] * (rounds - 1) + [KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                app.main_loop()
        return fetch, answer

    def test_loop_gate_and_terminal(self):
        for kwargs in ({'signed': 0}, {'signed': 3}, {'end': PAST}, {'detail_status': 'finished'},
                       {'rows': [rollcall(status='on_call_fine')]}, {'rows': [rollcall(status=None)]}):
            fetch, answer = self.loop(**kwargs)
            fetch.assert_not_called()
            answer.assert_not_called()
        for row in (rollcall(), rollcall(type='other', is_qr=True)):
            self.assertEqual(app.rollcall_label(row), '二维码')
            fetch, answer = self.loop(rows=[row])
            fetch.assert_called_once()
            answer.assert_called_once()

    def test_loop_retry_and_no_token_cache(self):
        fetch, answer = self.loop(token=None)
        self.assertEqual(fetch.call_count, 2)
        answer.assert_not_called()
        fetch, answer = self.loop(success=False)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(answer.call_count, 2)

    def test_auto_protocol_roundtrip(self):
        session = Mock()
        trace = []
        signed_up = False

        def get(url, **kwargs):
            trace.append(url.removeprefix(app.BASE_URL))
            if url.endswith('/qr_code'):
                return response({'data': TOKEN})
            if url.endswith('/student_rollcalls'):
                return response({'status': 'in_progress', 'end_time': FUTURE,
                                 'student_rollcalls': roster(mine='on_call_fine' if signed_up else 'absent')})
            if '/student/' in url:
                return response({'rollcalls': [rollcall()]})
            raise AssertionError(url)

        def put(url, **kwargs):
            nonlocal signed_up
            trace.append(url.removeprefix(app.BASE_URL))
            self.assertEqual(kwargs['json']['data'], TOKEN)
            signed_up = True
            return response()

        session.get.side_effect = get
        session.put.side_effect = put
        with patch.object(app, 'do_login', return_value=(session, 1)), \
                patch.object(app, 'get_courses', return_value=COURSES), \
                patch.object(app, 'dashboard'), \
                patch.object(app.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                app.main_loop()
        self.assertEqual(trace, [
            '/api/course/9/student/1/rollcalls?page=1&page_size=20',
            '/api/rollcall/7/student_rollcalls', '/api/rollcall/7/student_rollcalls',
            '/api/course/9/rollcall/7/qr_code', '/api/rollcall/7/answer_qr_rollcall',
            '/api/rollcall/7/student_rollcalls'])

    def test_cli_entrypoint(self):
        with patch.dict(os.environ, STUDENT_ID='test-student', PASSWORD='test-password'), \
                patch.dict('sys.modules', requests=SimpleNamespace(),
                           xmulogin=SimpleNamespace(xmulogin=Mock())), \
                patch('sys.argv', ['auto_checkin.py', '--invalid']), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(spec.origin, run_name='__main__')
            self.assertEqual(result.exception.code, 2)

    def test_cli_logs_in_before_prompt(self):
        order = []
        with patch.object(app, 'do_login', side_effect=lambda: (order.append('login') or (Mock(), 1))), \
                patch.object(app, 'get_courses', return_value=COURSES), \
                patch('builtins.input', side_effect=lambda _: (order.append('input') or '')):
            self.assertEqual(app.qr_cli(), 0)
        self.assertEqual(order, ['login', 'input'])


if __name__ == '__main__':
    unittest.main()
