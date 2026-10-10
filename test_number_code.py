"""Offline regression check: python test_number_code.py (no login or network)."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    "auto_checkin", Path(__file__).with_name("auto_checkin.py"))
app = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, STUDENT_ID="test-student", PASSWORD="test-password"), \
        patch.dict("sys.modules", requests=SimpleNamespace(),
                   xmulogin=SimpleNamespace(xmulogin=Mock())):
    spec.loader.exec_module(app)


def response(data, status=200):
    return Mock(status_code=status, json=Mock(return_value=data))


def item(rid=7, cid=9, code="0042"):
    return {"id": rid, "course": {"id": cid}, "number_code": code}


def lookup(reply, started="2030-06-04T07:48:00+08:00"):
    session = Mock(spec=["get"])
    session.get.return_value = reply
    code = app.get_number_code_from_timetable(7, 9, started, session)
    return code, session


def check_lookup():
    code, session = lookup(response({"rollcalls": [
        item(rid=8), item(cid=10), None, item(rid="7", cid="9")]}))
    assert code == "0042"
    session.get.assert_called_once_with(
        app.BASE_URL + "/api/timetable_rollcalls",
        params={"course_ids": "9", "rollcall_date": "2030-06-03"}, timeout=10)
    for data in ({"rollcalls": []}, {"rollcalls": [item(rid=8)]},
                 {"rollcalls": [item(cid=10)]}, {"rollcalls": [item(cid=None)]},
                 {"rollcalls": None}, [], {"rollcalls": [None]}):
        assert lookup(response(data))[0] is None
    for code in (None, "", 42, "42", "12345", "abcd", "００４２"):
        assert lookup(response({"rollcalls": [item(code=code)]}))[0] is None
    for status in (401, 403, 500):
        assert lookup(response({"rollcalls": [item()]}, status))[0] is None
    for started in (None, "bad-date"):
        code, session = lookup(response({"rollcalls": [item()]}), started)
        assert code is None
        session.get.assert_not_called()
    assert lookup(response({"rollcalls": [item()]}), "2030-06-04T00:00:00Z")[0] == "0042"
    session = Mock(spec=["get"])
    assert app.get_number_code_from_timetable(7, None, "2030-06-04T00:00:00Z", session) is None
    session.get.assert_not_called()
    session.get.side_effect = [response({"rollcalls": [item()]}), response({"rollcalls": []})]
    assert app.get_number_code_from_timetable(7, 9, "2030-06-04T00:00:00Z", session) == "0042"
    assert app.get_number_code_from_timetable(7, 9, "2030-06-04T00:00:00Z", session) is None
    reply = response({})
    reply.json.side_effect = ValueError("non-JSON response")
    assert lookup(reply)[0] is None
    session = Mock(spec=["get"])
    session.get.side_effect = OSError("network unavailable")
    assert app.get_number_code_from_timetable(7, 9, "2030-06-04T00:00:00Z", session) is None


def check_main_loop(old_code=None, rows=None, status="absent", wait=False, radar=False,
                    end_time="2099-01-01T00:00:00Z"):
    session = Mock(spec=["get", "put"])
    session.get.side_effect = [
        response({"number_code": old_code, "status": "in_progress",
                  "end_time": end_time}),
        response({"rollcalls": rows if rows is not None else [item()]}),
    ]
    session.put.return_value = response({})
    rc = {"rollcall_id": 7, "is_number": not radar, "is_radar": radar,
          "status": status, "rollcall_time": "2030-06-04T00:00:00Z"}
    with patch.object(app, "do_login", return_value=(session, 1)), \
            patch.object(app, "get_courses", return_value=[{"id": 9, "name": "Test"}]), \
            patch.object(app, "get_rollcalls", return_value=[rc]), \
            patch.object(app, "dashboard"), \
            patch.object(app, "checkin_gate", return_value=(wait, "")), \
            patch.object(app, "answer_radar_auto", return_value=(False, None, None)), \
            patch.object(app.time, "sleep", side_effect=KeyboardInterrupt):
        try:
            app.main_loop()
        except KeyboardInterrupt:
            pass
    return session


if __name__ == "__main__":
    check_lookup()
    for old_code, expected_gets, expected_code in (("0099", 1, "0099"), (None, 2, "0042")):
        session = check_main_loop(old_code=old_code)
        assert session.get.call_count == expected_gets
        assert session.put.call_args.args[0].endswith("/7/answer_number_rollcall")
        assert session.put.call_args.kwargs["json"]["numberCode"] == expected_code
    for kwargs in ({"rows": []}, {"status": "on_call_fine"}, {"wait": True},
                   {"radar": True}, {"status": None}, {"end_time": "2000-01-01T00:00:00Z"}):
        session = check_main_loop(**kwargs)
        session.put.assert_not_called()
        if kwargs != {"rows": []}:
            assert session.get.call_count == 1
    print("PASS: timetable lookup, UTC date, matching, error handling and main-loop integration")
