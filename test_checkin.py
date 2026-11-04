"""离线回归：python test_checkin.py。使用模拟账号和接口，无网络或第三方依赖。"""
import io
import math
import os
import sys
import contextlib
import importlib.util

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SRC = os.environ.get("AC_SRC", str(Path(__file__).with_name("auto_checkin.py")))
spec = importlib.util.spec_from_file_location("auto_checkin", SRC)
ac = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, STUDENT_ID="test-student", PASSWORD="test-password"), \
        patch.dict("sys.modules", requests=SimpleNamespace(),
                   xmulogin=SimpleNamespace(xmulogin=Mock())):
    spec.loader.exec_module(ac)

SID = ac.STUDENT_ID
RID = 1234
fails = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


class Resp:
    def __init__(self, code=200, body=None, text=None):
        self.status_code = code
        self._body = body if body is not None else {}
        self.text = text if text is not None else str(self._body)

    def json(self):
        return self._body


class FakeSession:
    """按调用顺序回放响应；PUT 记录提交过的坐标，roster 状态可按脚本演进。"""

    def __init__(self, put_code=200, put_body=None, my_status="on_call_fine",
                 roster_seq=None, top_status="in_progress", put_error=None):
        self.puts = []
        self.gets = 0
        self.put_code = put_code
        self.put_body = put_body if put_body is not None else {"message": "提交成功"}
        self.my_status = my_status
        self.roster_seq = roster_seq or []
        self.top_status = top_status
        self.put_error = put_error

    def put(self, url, json=None, timeout=None):
        if self.put_error:
            raise self.put_error
        self.puts.append((float(json["latitude"]), float(json["longitude"])))
        return Resp(self.put_code, self.put_body)

    def get(self, url, timeout=None):
        i = self.gets
        self.gets += 1
        st = self.my_status
        if self.roster_seq and i < len(self.roster_seq):
            st = self.roster_seq[i]
        return Resp(200, {
            "status": self.top_status,          # 点名本身的状态：故意设成 in_progress
            "student_rollcalls": [
                {"user_no": "other", "rollcall_status": "on_call_fine"},
                {"user_no": str(SID), "rollcall_status": st},
            ],
        })


def run(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn()
    return out, buf.getvalue()


# --- 1. 探针 200 + 名单已签上（就是 9-23 那次的真实情形）---
s = FakeSession(put_code=200, put_body={"message": "ok"}, my_status="on_call_fine")
out, log = run(lambda: ac.answer_radar_auto(RID, s))
ok, lat, lon = out
check(ok is True, "1 探针命中且名单为 on_call_fine → 判定成功")
check((lat, lon) == (ac.PROBE_LAT2, ac.PROBE_LON2), "1 报的是最后一次生效的坐标")
check("本人状态已确认" in log, "1 日志说明是本人状态")

# --- 2. 名单先 absent 后 on_call_fine（落库延迟）---
s = FakeSession(put_code=200, my_status="x", roster_seq=["absent", "absent", "on_call_fine"])
out, log = run(lambda: ac.answer_radar_auto(RID, s))
check(out[0] is True, "2 回查带重读，落库延迟不会误报失败")

# --- 3. 真的没签上（探针 200 但名单一直 absent，且无 distance）---
s = FakeSession(put_code=200, put_body={"message": "已记录"}, my_status="absent")
out, log = run(lambda: ac.answer_radar_auto(RID, s))
check(out[0] is False, "3 名单未签上 → 判失败（不会假成功）")
check("未获取到距离" in log and "探针1 HTTP 200 响应" in log,
      "3 缺距离时把原始响应打出来，便于下次核对接口形状")

# --- 4. 认不出自己 ---
s = FakeSession()
s.my_status = "on_call_fine"
orig_get = s.get


def get_no_me(url, timeout=None):
    r = orig_get(url, timeout=timeout)
    r._body["student_rollcalls"] = [{"user_no": "someone-else", "rollcall_status": "on_call_fine"}]
    return r


s.get = get_no_me
out, log = run(lambda: ac.answer_radar_auto(RID, s))
check(out[0] is False, "4 名单里找不到本人 → 不猜成功")
check("没找到本人记录" in log, "4 且明确告警，不静默")

# --- 5. 距离反解这条链路仍然通（有 distance 时算出候选并签上）---
target = (24.45, 118.1)
lat0 = (ac.PROBE_LAT1 + ac.PROBE_LAT2) / 2
lon0 = (ac.PROBE_LON1 + ac.PROBE_LON2) / 2
xt, yt = ac._latlon_to_xy(target[0], target[1], lat0, lon0)
p1 = ac._latlon_to_xy(ac.PROBE_LAT1, ac.PROBE_LON1, lat0, lon0)
p2 = ac._latlon_to_xy(ac.PROBE_LAT2, ac.PROBE_LON2, lat0, lon0)
d = [math.hypot(xt - p1[0], yt - p1[1]), math.hypot(xt - p2[0], yt - p2[1])]


class DistSession(FakeSession):
    def put(self, url, json=None, timeout=None):
        x, y = ac._latlon_to_xy(float(json["latitude"]), float(json["longitude"]), lat0, lon0)
        near = math.hypot(x - xt, y - yt) < 50
        self.puts.append((float(json["latitude"]), float(json["longitude"])))
        if near:
            self.my_status = "on_call_fine"
            return Resp(200, {"message": "签到成功"})
        body = {"message": "不在范围内"}
        i = len(self.puts) - 1
        if i < 2:
            body["distance"] = round(d[i], 1)
        return Resp(400, body)


s = DistSession(my_status="absent")
out, log = run(lambda: ac.answer_radar_auto(RID, s))
check(out[0] is True and abs(out[1] - target[0]) < 0.01 and abs(out[2] - target[1]) < 0.01,
      "5 反解出的候选坐标能签上（三角定位未回归）")

# --- 6. absent 是活跃状态，不是终止状态 ---
import datetime as dt
future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat()
past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).isoformat()
check(ac._is_terminal("absent", future) is False, "6 absent + 未到期 → 仍可尝试签到")
check(ac._is_terminal("absent", past) is True, "6 absent + 已到期 → 终止")
check("absent" in ac.ACTIVE_STATUSES, "6 absent 已列入已知状态（不再刷 [?]）")

# --- 7. 节流统计没被 _find_me 重构弄坏 ---
s = FakeSession(my_status="absent")
(signed, total), log = run(lambda: ac.get_signed_stats(RID, s))
check((signed, total) == (1, 2), "7 已签人数统计口径不变")
s2 = FakeSession(my_status="on_call_fine")
check(run(lambda: ac.get_signed_stats(RID, s2))[0] == (None, None),
      "7 判据自校仍在（自己未签却被计为已签 → 不节流）")

# --- 8. 节流闸门：只看比例，没有任何时间兜底 ---
import datetime as _dt
import inspect


def gate(sess_roster, end_time):
    """按签名适配地调 checkin_gate，让改前/改后两份代码都能跑同一套断言。"""
    # 按被测试代码的签名喂参数：改前是 (rid, session, end_time, waited_sec)，改后只有前两个
    ps = list(inspect.signature(ac.checkin_gate).parameters)
    args = [RID, sess_roster]
    if "end_time" in ps:
        args.append(end_time)
    if "waited_sec" in ps:
        args.append(9999)               # 旧版：当作已经等很久了
    try:
        return ac.checkin_gate(*args)
    except Exception as e:
        return None, f"调用失败: {e}"


class RosterSession(FakeSession):
    """给 get_signed_stats 用：共 total 人，其中 signed 人已签，本人未签（排在最后一条）"""

    def __init__(self, signed, total, top_end=None):
        # signed = 全班已签人数（本人未签，所以本人排在那 signed 个之外）
        super().__init__()
        rows = [{"user_no": f"u{i}", "rollcall_status": "on_call_fine" if i < signed else None}
                for i in range(total - 1)]
        rows.append({"user_no": str(SID), "rollcall_status": None})
        self._roster = {"status": "in_progress", "end_time": top_end, "student_rollcalls": rows}

    def get(self, url, timeout=None):
        self.gets += 1
        return Resp(200, self._roster)


far = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=30)).isoformat()
near = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=5)).isoformat()

check(not hasattr(ac, "CHECKIN_MAX_WAIT"), "8 无「等超 N 秒直接签」常量")
check(not hasattr(ac, "CHECKIN_MIN_REMAINING"), "8 无「只剩 N 秒直接签」常量")
ps = list(inspect.signature(ac.checkin_gate).parameters)
check(ps == ["rollcall_id", "session"], f"8 闸门只看比例，连参数里都不再有时间（{ps}）")

need, m = gate(RosterSession(20, 145, far), far)
check(need is True and "继续等" in m, f"8 20/145（14%）→ 继续等（{m}）")
need, m = gate(RosterSession(20, 145, near), near)
check(need is True, f"8 只剩 5 秒也不签（{m}）")
need, m = gate(RosterSession(60, 145, far), far)
check(need is False and "达标" in m, f"8 60/145（41%）→ 开签（{m}）")
need, m = gate(RosterSession(30, 100, far), far)
check(need is True, f"8 恰好 30%（不算「超过」）→ 仍等（{m}）")
need, m = gate(RosterSession(31, 100, far), far)
check(need is False, f"8 31% → 达标（{m}）")

# 「不签」不等于「卡住」：点名一结束就该变终端状态、从轮询里摘掉
past = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1)).isoformat()
check(ac._is_terminal("absent", past) is True, "8 结束后 absent + end_time 已过 → 终端（会打 [END] 收尾）")
check(ac._is_terminal("absent", None) is False, "8 拿不到 end_time 时不算结束（所以详情那次 GET 不能省）")

d = RosterSession(1, 3, far)
et_fun = getattr(ac, "get_rollcall_detail", None) or getattr(ac, "get_number_code")
code, top_status, et = run(lambda: et_fun(RID, d))[0]
check(et == far and top_status == "in_progress", "8 详情接口给得出 end_time（收尾的依据）")

# 缺失/部分缺失字段、坏记录均不能被解释为已知的低比例。
for label, rows in [
    ("全部缺字段且时间为空", [{"user_no": str(SID), "updated_at": None}]),
    ("全部缺字段且有时间", [{"user_no": str(SID), "updated_at": "2030-01-01"}]),
    ("部分缺字段", [{"user_no": str(SID), "rollcall_status": None}, {"user_no": "other", "updated_at": None}]),
    ("混入无效记录", [{"user_no": str(SID), "rollcall_status": None}, None]),
]:
    session = Mock()
    session.get.return_value = Resp(200, {"student_rollcalls": rows})
    check(run(lambda: ac.get_signed_stats(RID, session))[0] == (None, None), label + " → 判据不可用")
    check(run(lambda: ac.checkin_gate(RID, session))[0][0] is False, label + " → 放行")

session = RosterSession(0, 2)
for row in session._roster["student_rollcalls"]:
    row["updated_at"] = "2030-01-01"
    row["status"] = "on_call_fine"
check(run(lambda: ac.get_signed_stats(RID, session))[0] == (0, 2), "状态字段为 null → 已知零人，忽略其它字段")
check(run(lambda: ac.checkin_gate(RID, session))[0][0] is True, "已知零人 → 继续等待")

print()
print(f"{len(fails)} 项失败" if fails else "全部通过")
sys.exit(1 if fails else 0)
