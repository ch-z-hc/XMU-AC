"""
XMU Tronclass 全自动签到脚本
- xmulogin 纯 API CAS 登录（无浏览器，稳定）
- Cookie 持久化（重启免登录）
- 数字签到：API 直接抓码 → 提交
- GPS 签到：两条线三角定位自动算坐标 → 提交
  提交后按名单里本人那条 rollcall_status 确认是否真签上（不是看点名状态）
- 签到节流：等班级超过 CHECKIN_RATIO 的人签过之后再签（避免第一个签）
  已签人数按名单每人的 rollcall_status 统计；没到比例就一直等，宁可缺勤也不提前签
- 内置看门狗：崩溃自动重启

用法:
  $env:STUDENT_ID="你的学号"
  $env:PASSWORD="你的密码"
  python auto_checkin.py
"""
import getpass
import json
import math
import os
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from xmulogin import xmulogin

# ============================================================
# 配置 —— 优先环境变量，否则交互输入
# ============================================================
STUDENT_ID = os.environ.get("STUDENT_ID")
PASSWORD = os.environ.get("PASSWORD")
INTERVAL = 15           # 轮询间隔秒数
# GPS 坐标通过三角定位自动获取，无需手动填写

# ---- 签到节流（等同学先签）----
CHECKIN_RATIO = 0.30        # 已签到比例超过此值才轮到自己签；0 = 关闭
# 判据只有比例这一条，**没有任何时间兜底**：原来的
# 「等超 CHECKIN_MAX_WAIT 直接签」和「只剩 CHECKIN_MIN_REMAINING 直接签」
# 两条都已删掉 —— 没到比例就干脆不签。
# 不签也不等于卡住：点名有没有结束靠 end_time 判（见 _is_terminal），到点会打一行
# [END] 收尾并从轮询里摘掉；而 end_time 只有详情接口给（课程列表里恒为 None）。
# 唯一还绕过比例的情况是名单接口异常、拿不到人数 —— 那时是「不知道」而不是「没到」。
# ============================================================

if not STUDENT_ID:
    STUDENT_ID = input("学号: ").strip()
if not PASSWORD:
    PASSWORD = getpass.getpass("密码: ")

if not STUDENT_ID or not PASSWORD:
    print("错误: 学号和密码不能为空")
    exit(1)

BASE_URL = "https://lnt.xmu.edu.cn"
SESSION_FILE = Path(__file__).resolve().parent / "auto_checkin_session.json"

HEADERS_BASE = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
}

# GPS 三角定位误差容忍（米）：两圆交点距离在此范围内视为相交
GPS_DISTANCE_TOLERANCE = 5.0
# 未知状态最大重试次数（超出后视为终端，不再重试）
MAX_UNKNOWN_RETRIES = 10


# ============================================================
# 登录模块：xmulogin + cookie 持久化
# ============================================================

def _save_cookies(session):
    """保存完整 cookie jar 到文件（含 domain/path/expires/secure 等属性）"""
    try:
        cookies = []
        for cookie in session.cookies:
            cookies.append({
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "expires": cookie.expires,
                "secure": cookie.secure,
                "rest": cookie._rest,
            })
        with open(SESSION_FILE, "w") as f:
            json.dump(cookies, f)
        print(f"[登录] Cookie 已保存到 {SESSION_FILE}")
    except Exception as e:
        print(f"[登录] 保存 Cookie 失败: {e}")


def _load_cookies(session):
    """从文件加载完整 cookie jar"""
    if not SESSION_FILE.exists():
        return False
    try:
        with open(SESSION_FILE, "r") as f:
            cookies = json.load(f)
        for c in cookies:
            session.cookies.set(
                name=c["name"],
                value=c["value"],
                domain=c.get("domain", ""),
                path=c.get("path", "/"),
                expires=c.get("expires"),
                secure=c.get("secure", False),
                rest=c.get("rest", {}),
            )
        return True
    except Exception as e:
        print(f"[登录] 加载 Cookie 失败: {e}")
        return False


def do_login():
    """xmulogin 纯 API CAS 登录，返回 (requests.Session, student_id)"""
    session = requests.Session()
    session.headers.update(HEADERS_BASE)

    # 尝试加载已保存的 session
    if _load_cookies(session):
        try:
            r = session.get(f"{BASE_URL}/api/profile", timeout=10)
            if r.status_code == 200 and r.json().get("id"):
                sid = r.json()["id"]
                print(f"[登录] Cookie 有效, student_id={sid}")
                return session, sid
            print("[登录] Cookie 已过期，重新登录...")
        except Exception as e:
            print(f"[登录] Cookie 校验请求失败: {e}，重新登录...")

    # xmulogin 登录（type=3 为 Tronclass）
    print("[登录] xmulogin CAS 登录中...")
    session = xmulogin(type=3, username=STUDENT_ID, password=PASSWORD)
    if not session:
        print("[登录] 登录失败")
        return None, None

    # 获取 student_id
    r = session.get(f"{BASE_URL}/api/profile", timeout=10)
    if r.status_code != 200:
        print("[登录] 获取用户信息失败")
        return None, None
    student_id = r.json()["id"]
    print(f"[登录] 登录成功, student_id={student_id}")

    _save_cookies(session)
    return session, student_id


# ============================================================
# API 工具函数（全部接收 requests.Session）
# ============================================================

def _get_semester(session):
    try:
        r = session.get(f"{BASE_URL}/api/current-semester-info", timeout=5)
        if r.status_code == 200:
            data = r.json()
            return str(data["semester"]["id"]), str(data["academic_year"]["id"])
    except Exception as e:
        print(f"[!] 获取学期信息失败: {e}，使用默认值")
    return "29", "12"


def get_courses(session):
    s_id, y_id = _get_semester(session)
    payload = {
        "conditions": {
            "semester_id": [s_id], "academic_year_id": [y_id],
            "keyword": "", "classify_type": "recently_started",
            "display_studio_list": False,
        },
        "fields": "id,name,display_name",
        "page": 1, "page_size": 30,
        "showScorePassedStatus": False,
    }
    r = session.post(f"{BASE_URL}/api/my-courses", json=payload, timeout=10)
    try:
        data = r.json()
    except Exception:
        return []
    if isinstance(data, list):
        courses = data
    elif "courses" in data:
        courses = data["courses"]
    elif "data" in data:
        courses = data["data"]
    else:
        return []
    seen_cids, unique = set(), []
    for c in courses:
        cid = c.get("id")
        if cid not in seen_cids:
            seen_cids.add(cid)
            unique.append(c)
    return unique


class RollcallSessionExpired(RuntimeError):
    pass


def get_rollcalls(course_id, session, student_id):
    url = f"{BASE_URL}/api/course/{course_id}/student/{student_id}/rollcalls?page=1&page_size=20"
    r = session.get(url, timeout=10)
    # 不输出响应正文 / 跳转 URL，避免把登录票据写进日志。
    info = (f"HTTP {r.status_code}, Content-Type={r.headers.get('Content-Type', '未知')}, "
            f"重定向={'是' if r.history else '否'}")
    if r.status_code == 401:
        raise RollcallSessionExpired(f"签到列表登录已失效 ({info})")
    if r.status_code != 200:
        raise RuntimeError(f"签到列表接口异常 ({info})")
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"签到列表响应不是 JSON ({info})") from None
    if isinstance(data, dict):
        data = data.get("rollcalls", data.get("data"))
    if not isinstance(data, list) or not all(isinstance(rc, dict) for rc in data):
        raise RuntimeError(f"签到列表响应结构异常 ({info})")
    return data


def get_rollcall_detail(rollcall_id, session):
    """读点名详情接口，返回 (number_code, 点名级 status, end_time) 或 (None, None, None)。

    ⚠️ 这里的 status 是**点名本身**的状态（in_progress / finished…），不是本人签没签上；
    数字签到取码、GPS 签到取 end_time 都用这个接口，本人的状态看课程列表或名单。"""
    url = f"{BASE_URL}/api/rollcall/{rollcall_id}/student_rollcalls"
    try:
        r = session.get(url, timeout=10)
        data = r.json()
        return data.get("number_code"), data.get("status"), data.get("end_time")
    except Exception as e:
        print(f"  [X] 读取点名详情失败: {e}")
        return None, None, None


def get_number_code_from_timetable(rollcall_id, course_id, rollcall_time, session):
    """旧明细缺码时，按课程和点名开始时间的 UTC 日期补查数字码。"""
    started = _parse_end_time(rollcall_time)
    if course_id is None or started is None:
        return None
    try:
        r = session.get(
            f"{BASE_URL}/api/timetable_rollcalls",
            params={"course_ids": str(course_id),
                    "rollcall_date": started.astimezone(timezone.utc).date().isoformat()},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or not isinstance(data.get("rollcalls"), list):
            return None
        for item in data["rollcalls"]:
            if not isinstance(item, dict) or str(item.get("id")) != str(rollcall_id):
                continue
            course = item.get("course")
            if not isinstance(course, dict) or str(course.get("id")) != str(course_id):
                continue
            code = item.get("number_code")
            if isinstance(code, str) and len(code) == 4 and code.isascii() and code.isdigit():
                return code
    except Exception as e:
        print(f"  [X] 日期接口取码失败: {e}")
    return None


def answer_number(rollcall_id, code, session):
    url = f"{BASE_URL}/api/rollcall/{rollcall_id}/answer_number_rollcall"
    payload = {"deviceId": str(uuid.uuid4()), "numberCode": str(code)}
    try:
        r = session.put(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"  [X] 提交签到失败: {e}")
        return False


# 名单里判断「某人已签到」的依据：优先用每人的 rollcall_status（签到后变
# on_call_fine）；若接口无此字段，退回旧口径（签到时才写 updated_at）。
SIGNED_STUDENT_STATUS = "on_call_fine"


def _student_signed(rec, use_status):
    """按名单里的单条记录判断该学生是否已签到。

    use_status 在名单层面统一定口径，且看字段是否存在而不是看值：无人签到时
    rollcall_status 会全为 null，按值判断会误退回 updated_at；也不逐条判断，
    已签者有值、未签者为 null 的接口逐条退回会把未签算成已签。
    """
    if use_status:
        return rec.get("rollcall_status") == SIGNED_STUDENT_STATUS
    return bool(rec.get("updated_at"))


def _find_me(students):
    """在名单里找本人那条记录，找不到返回 None（调用方要能区分「没签上」和「认不出自己」）"""
    return next((s for s in students if isinstance(s, dict)
                 and s.get("user_no") is not None
                 and str(s["user_no"]) == str(STUDENT_ID)), None)


def check_my_checkin_status(rollcall_id, session, retries=3, delay=0.4):
    """回查「本人」在本次签到里的状态，返回名单里的 rollcall_status（签上后为 on_call_fine）。

    ⚠️ 只能读名单里自己那条 rollcall_status。详情接口顶层的 status 是**点名本身**的状态
    （in_progress / finished…），跟本人签没签上无关；拿它判成功会把已经签上的 GPS 签到
    报成失败（2026-09-23 实测踩过）。
    提交后服务端落库有延迟，所以带几次重读。
    """
    url = f"{BASE_URL}/api/rollcall/{rollcall_id}/student_rollcalls"
    status = None
    for i in range(retries):
        try:
            r = session.get(url, timeout=10)
            if r.status_code != 200:
                return None
            students = r.json().get("student_rollcalls")
            if not isinstance(students, list):
                return None
            me = _find_me(students)
            if me is None:
                print("  [!] 名单里没找到本人记录，无法确认签到结果")
                return None
            status = me.get("rollcall_status")
            if status == SIGNED_STUDENT_STATUS:
                return status
        except Exception as e:
            print(f"  [X] 回查本人状态失败: {e}")
            return None
        if i < retries - 1:
            time.sleep(delay)
    return status


def get_signed_stats(rollcall_id, session):
    """统计本次签到的已签人数，返回 (signed, total) 或 (None, None)。

    total 取名单长度，即本班应到人数。

    走到调用点说明本机还没签到，所以若判据把自己也算成已签，说明它与
    接口口径不符（例如拿建记录就有值的 updated_at 当已签）：此时按「拿不到
    人数」处理并告警，宁可立即签到也不因错判而卡住。
    """
    url = f"{BASE_URL}/api/rollcall/{rollcall_id}/student_rollcalls"
    try:
        r = session.get(url, timeout=10)
        if r.status_code != 200:
            return None, None
        data = r.json()
        students = data.get("student_rollcalls") if isinstance(data, dict) else None
        if not isinstance(students, list) or not students:
            return None, None
        students = [s for s in students if isinstance(s, dict)]
        if not students:
            return None, None        # 全被过滤时不能让下游除零
        use_status = any("rollcall_status" in s for s in students)
        me = _find_me(students)
        if me is not None and _student_signed(me, use_status):
            print("  [!] 签到人数判据与接口不符（自己未签却被计为已签），本次不节流")
            return None, None
        return sum(1 for s in students if _student_signed(s, use_status)), len(students)
    except Exception as e:
        print(f"  [X] 查询签到人数失败: {e}")
        return None, None


def checkin_gate(rollcall_id, session):
    """判断这一轮要不要因为「同学签得还不够」而不签。

    返回 (need_wait, msg)：need_wait 为 True 时本轮跳过，下个轮询周期再试。
    判据只有比例这一条，没有时间兜底：没超过 CHECKIN_RATIO 就一直等，等到点名结束
    也不签（宁可缺勤也不当那一小批人）。名单接口异常、拿不到人数时照样放行 ——
    那是「不知道」不是「没到」，别让它把签到卡死。
    """
    if CHECKIN_RATIO <= 0:
        return False, ""

    signed, total = get_signed_stats(rollcall_id, session)
    if signed is None:
        return False, ""

    ratio = signed / total
    msg = f"已签到 {signed}/{total} ({ratio:.0%})"
    if ratio > CHECKIN_RATIO:
        return False, msg + " 达标，开签"

    return True, msg + f" 未达 {CHECKIN_RATIO:.0%}，继续等"


# ============================================================
# GPS 三角定位（两条线交点反推目标坐标）
# ============================================================

PROBE_LAT1, PROBE_LON1 = 24.3, 118.0
PROBE_LAT2, PROBE_LON2 = 24.6, 118.2


def _latlon_to_xy(lat, lon, lat0, lon0):
    R = 6371000.0
    x = math.radians(lon - lon0) * R * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * R
    return x, y


def _xy_to_latlon(x, y, lat0, lon0):
    R = 6371000.0
    lat = lat0 + math.degrees(y / R)
    lon = lon0 + math.degrees(x / (R * math.cos(math.radians(lat0))))
    return lat, lon


def _circle_intersections(x1, y1, d1, x2, y2, d2):
    """两圆交点。返回 ((x1,y1), (x2,y2)) 或 None。

    容忍 GPS 距离舍入误差：两圆若在 tolerance 米内不相交，
    尝试微调 d1/d2 使其相切。
    """
    x1, y1, d1 = float(x1), float(y1), float(d1)
    x2, y2, d2 = float(x2), float(y2), float(d2)
    D = math.hypot(x2 - x1, y2 - y1)

    if D < 1e-9:
        # 同心圆：无法确定方向
        return None

    if D > d1 + d2:
        # 两圆分离，尝试 GPS 误差容忍
        gap = D - (d1 + d2)
        if gap <= GPS_DISTANCE_TOLERANCE:
            # 将两圆半径各扩大 gap/2，使其相切
            adjust = gap / 2.0 + 0.1
            d1 += adjust
            d2 += adjust
        else:
            return None

    if D < abs(d1 - d2):
        # 一圆包含另一圆，尝试容忍
        gap = abs(d1 - d2) - D
        if gap <= GPS_DISTANCE_TOLERANCE:
            # 缩小大圆或扩大小圆使其相切
            if d1 > d2:
                d1 = D + d2 - 0.1
            else:
                d2 = D + d1 - 0.1
        else:
            return None

    a = (d1 ** 2 - d2 ** 2 + D ** 2) / (2 * D)
    h_sq = max(0, d1 ** 2 - a ** 2)
    h = math.sqrt(h_sq)
    xm = x1 + a * (x2 - x1) / D
    ym = y1 + a * (y2 - y1) / D
    rx = -(y2 - y1) * (h / D)
    ry = (x2 - x1) * (h / D)
    return (xm + rx, ym + ry), (xm - rx, ym - ry)


def _solve_target(lat1, lon1, lat2, lon2, d1, d2):
    lat0 = (lat1 + lat2) / 2
    lon0 = (lon1 + lon2) / 2
    x1, y1 = _latlon_to_xy(lat1, lon1, lat0, lon0)
    x2, y2 = _latlon_to_xy(lat2, lon2, lat0, lon0)
    sols = _circle_intersections(x1, y1, d1, x2, y2, d2)
    if sols is None:
        return None
    p1 = _xy_to_latlon(sols[0][0], sols[0][1], lat0, lon0)
    p2 = _xy_to_latlon(sols[1][0], sols[1][1], lat0, lon0)
    return p1, p2


def answer_radar_auto(rollcall_id, session):
    """GPS 签到：两条线三角定位，自动算目标坐标。

    返回 (success, lat, lon)。提交后回查名单里本人的 rollcall_status 确认签上（
    详情接口顶层的 status 是点名本身的状态，不能拿来判成功，见
    check_my_checkin_status 的注释）。
    """
    url = f"{BASE_URL}/api/rollcall/{rollcall_id}/answer"

    def _payload(lat, lon):
        return {
            "accuracy": 35, "altitude": 0, "altitudeAccuracy": None,
            "deviceId": str(uuid.uuid4()), "heading": None,
            "latitude": str(lat), "longitude": str(lon), "speed": None,
        }

    try:
        r1 = session.put(url, json=_payload(PROBE_LAT1, PROBE_LON1), timeout=10)
        r2 = session.put(url, json=_payload(PROBE_LAT2, PROBE_LON2), timeout=10)
    except Exception as e:
        print(f"  探针请求失败: {e}")
        return False, None, None

    # 探针直接命中（本次点名不限制范围 / 探针坐标已在范围内，不限制时名单里
    # 本人的 distance 为 None）。
    # 两次都 200 时按最后一次报坐标；服务端最终留哪一次无从核对，别当精确事实。
    hit = None
    for r, lat, lon in [(r1, PROBE_LAT1, PROBE_LON1), (r2, PROBE_LAT2, PROBE_LON2)]:
        if r.status_code == 200:
            hit = (lat, lon)
    if hit is not None and check_my_checkin_status(rollcall_id, session) == SIGNED_STUDENT_STATUS:
        print(f"  探针命中，本人状态已确认: {SIGNED_STUDENT_STATUS}")
        return True, hit[0], hit[1]

    try:
        d1 = r1.json().get("distance")
        d2 = r2.json().get("distance")
    except Exception:
        print("  服务器未返回距离信息")
        return False, None, None

    if d1 is None or d2 is None:
        # 既没签上又拿不到距离 = 三角定位这条路当场废掉。把原始响应记下来，
        # 下次出这问题能直接看出是接口换了形状还是返回了别的错。
        print(f"  未获取到距离: d1={d1}, d2={d2}")
        for tag, r in (("探针1", r1), ("探针2", r2)):
            try:
                print(f"    {tag} HTTP {r.status_code} 响应: {str(r.text)[:200]}")
            except Exception:
                pass
        return False, None, None

    print(f"  探针距离: d1={d1}m, d2={d2}m")

    sols = _solve_target(PROBE_LAT1, PROBE_LON1, PROBE_LAT2, PROBE_LON2, d1, d2)
    if sols is None:
        print("  两圆无交点（已尝试误差容忍）")
        return False, None, None

    (lat_a, lon_a), (lat_b, lon_b) = sols

    for lat, lon in [(lat_a, lon_a), (lat_b, lon_b)]:
        try:
            r = session.put(url, json=_payload(lat, lon), timeout=10)
            if r.status_code == 200:
                status = check_my_checkin_status(rollcall_id, session)
                if status == SIGNED_STUDENT_STATUS:
                    print(f"  精确坐标命中，本人状态已确认: {status}")
                    return True, lat, lon
                print(f"  坐标提交 200 但本人状态为 {status}，尝试下一个...")
        except Exception as e:
            print(f"  候选坐标提交异常: {e}")

    print("  两个候选坐标均未确认成功")
    return False, None, None


# ============================================================
# 辅助
# ============================================================

# 终端状态：签到成功、已结束、已过期、已关闭、已请假
# （on_call_fine 是「本人已签上」，名单接口本人那条的 rollcall_status / 课程列表的 status）
TERMINAL_STATUSES = {"finished", "expired", "closed", "on_call_fine", "on_personal_leave"}

# 明确活跃的状态（可尝试签到）。
# ⚠️ 两个接口的 status 不同口径：课程列表（GET /api/course/{cid}/student/{sid}/rollcalls）
# 给的是「本人在本次点名里的状态」（未签时为 absent，签上后 on_call_fine）；
# 点名详情（GET /api/rollcall/{rid}/student_rollcalls）顶层的 status 是「点名本身」
# 的状态（in_progress / finished…）。两套名字都归到下面两个集合里，所以哪个接口
# 的状态都能拿来判「还要不要接着处理」；但**判签有没有成功只能用本人的状态**，
# 见 check_my_checkin_status。
# absent 不放进这里的话，GPS 签到的每个轮询周期会多一行 [?] 未知状态值 刷屏。
ACTIVE_STATUSES = {
    "active", "on_going", "on_coming", "started", "in_progress",
    "waiting", "pending", "not_started", "absent",
}


def _parse_end_time(end_time):
    """解析 end_time 为带时区的 datetime，失败返回 None"""
    if not end_time:
        return None
    try:
        dt = datetime.fromisoformat(str(end_time).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _end_time_passed(end_time):
    """检查 end_time 是否已过"""
    dt = _parse_end_time(end_time)
    return dt is not None and dt < datetime.now(timezone.utc)


def _is_terminal(status, end_time):
    """判断签到是否已终止（成功或过期），不应再重试。"""
    if status in TERMINAL_STATUSES:
        return True
    if status in ACTIVE_STATUSES:
        return _end_time_passed(end_time)
    # 未知 status + end_time 已过 → 视为终端
    if end_time and _end_time_passed(end_time):
        return True
    return False


def rollcall_label(rollcall):
    if rollcall.get("is_radar"):
        return "GPS"
    if rollcall.get("is_number"):
        return "数字"
    return "其他"


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def _term_width():
    try:
        return os.get_terminal_size().columns
    except Exception:
        return 80


def _display_width(text):
    """计算字符串在终端中的显示宽度（CJK 字符占 2 列）"""
    w = 0
    for ch in text:
        ea = unicodedata.east_asian_width(ch)
        w += 2 if ea in ("W", "F") else 1
    return w


def _cline(text):
    """居中打印（按显示宽度）"""
    w = _term_width()
    dw = _display_width(text)
    print(" " * max(0, (w - dw) // 2) + text)


def dashboard(courses, seen_count, query_count, start_time, events):
    clear()
    elapsed = int(time.time() - start_time)
    m, s = divmod(elapsed, 60)
    h, m = divmod(m, 60)
    t = time.strftime("%H:%M:%S")
    rt = f"{h}h{m}m{s}s" if h else f"{m}m{s}s"

    sep = "=" * 46

    for line in [
        "",
        "XMU Tronclass 全自动签到",
        sep,
        f"时间: {t}    运行: {rt}",
        f"课程: {len(courses)} 门    查询: {query_count} 次",
        "GPS : 自动三角定位",
        f"节流: 等 {CHECKIN_RATIO:.0%} 同学先签" if CHECKIN_RATIO > 0 else "节流: 关闭",
        sep,
    ]:
        _cline(line)

    if events:
        for evt in events[-8:]:
            _cline(evt)
    else:
        _cline("(监控中...)")

    _cline(sep)
    _cline("Ctrl+C 退出")


# ============================================================
# 主循环
# ============================================================

def main_loop():
    # 1. 登录
    session, student_id = do_login()
    if not session or not student_id:
        print("[错误] 登录失败")
        return False

    # 2. 获取课程
    courses = get_courses(session)
    if not courses:
        print("[错误] 获取课程列表失败")
        return False
    course_names = {
        c["id"]: c.get("display_name") or c.get("name") or "?"
        for c in courses
    }
    print(f"[课程] 共 {len(courses)} 门")

    # 3. 监控循环
    seen = set()                # 已处理终端 / 已成功签到的 rid
    initialized_courses = set() # 每门课第一次成功查询时，历史不支持类型静默跳过
    poll_errors = {}            # cid → (错误描述, 连续失败次数)
    unknown_retries = {}        # rid → 未知状态重试次数
    throttle_waiting = set()    # 已报过 [NEW]、还没收尾的 rid（[NEW] 只报一次靠它）
    events = []
    query_count = 0
    start_time = time.time()
    last_poll = 0

    dashboard(courses, len(seen), query_count, start_time, events)

    while True:
        now = time.time()

        # 每隔 INTERVAL 秒轮询一次课程
        if now - last_poll >= INTERVAL:
            for c in courses:
                cid = c["id"]
                name = course_names.get(cid, "?")
                try:
                    rollcalls = get_rollcalls(cid, session, student_id)
                except RollcallSessionExpired as e:
                    print(f"[登录] {e}，重新登录后重试")
                    session, student_id = do_login()
                    if not session or not student_id:
                        return False
                    continue
                except Exception as e:
                    error = str(e)
                    previous, failures = poll_errors.get(cid, (None, 0))
                    poll_errors[cid] = (error, failures + 1)
                    if error != previous:
                        print(f"[!] 获取 {name} 签到列表失败: {error}，将在下一轮重试")
                    continue

                if cid in poll_errors:
                    _, failures = poll_errors.pop(cid)
                    print(f"[恢复] {name} 签到列表查询恢复（此前连续失败 {failures} 次）")
                initial_poll = cid not in initialized_courses
                initialized_courses.add(cid)
                query_count += 1

                for rc in rollcalls:
                    rid = rc.get("id") or rc.get("rollcall_id")
                    if not rid or rid in seen:
                        continue

                    # 详情：本人状态一律看课程列表里那条的 status（未签 = absent、
                    # 签上 = on_call_fine）；详情接口只用来取数字签到码和结束时间。
                    # ⚠️ 详情顶层的 status 是**点名级**的（in_progress / finished），拿它判
                    # “我该不该接着处理”会出两类错，都是 2026-09-23 实测到的：GPS 那边
                    # 永远不终态（把已经签成功的报成 [FAIL]），数字那边则会把自己已签上、
                    # 但点名还没结束的那场重复签一遍（重启后最容易撞上）。
                    # 列表里也不给 end_time（恒 None），而「点名结束了没有」全靠它判 ——
                    # 没到比例的那些就是靠它收尾的，不然会永远在轮询里空转。
                    is_num = rc.get("is_number") and not rc.get("is_radar")
                    is_radar = bool(rc.get("is_radar"))
                    code = end_time = None
                    if is_num:
                        code, _, end_time = get_rollcall_detail(rid, session)
                    elif is_radar:
                        _, _, end_time = get_rollcall_detail(rid, session)
                    status = rc.get("status")

                    # 终端状态 → 标记已处理，不再重试
                    if _is_terminal(status, end_time):
                        # 之前对它报过 [NEW]（进过 throttle_waiting）才补一行收尾，
                        # 否则日志会停在 [FAIL] 将重试 上看不出最终结果；
                        # 启动时批量吐掉的历史点名不在里面，不会刷屏。
                        if rid in throttle_waiting:
                            events.append(f"[END] {name} 不再处理，当前状态 {status}")
                        seen.add(rid)
                        continue

                    # 非数字 / GPS 点名无法自动提交，不应当成状态获取失败重试。
                    if not (is_num or is_radar):
                        if not initial_poll:
                            events.append(f"[SKIP] {name} 不支持的签到类型，跳过")
                        seen.add(rid)
                        continue

                    # 课程列表未返回本人 status → 限次重试
                    if status is None:
                        retries = unknown_retries.get(rid, 0)
                        if retries >= MAX_UNKNOWN_RETRIES:
                            seen.add(rid)
                            events.append(
                                f"[GIVEUP] {name} 状态获取连续失败 {retries} 次，放弃"
                            )
                            unknown_retries.pop(rid, None)
                        else:
                            unknown_retries[rid] = retries + 1
                        continue

                    # 非标准 status（不在已知终端/活跃列表）→ 打印并继续尝试签到
                    if status not in TERMINAL_STATUSES and status not in ACTIVE_STATUSES:
                        events.append(
                            f"[?] {name} 未知状态值: '{status}'，仍尝试签到"
                        )

                    # 可以尝试签到
                    rtype = rollcall_label(rc)

                    # 签到节流：等班级超过 CHECKIN_RATIO 的人签过之后再签，没到就一直等。
                    # 等待期间 [NEW] 只报一次，否则每个轮询周期都会刷屏。
                    # CHECKIN_RATIO=0 时整块跳过，保持无节流时的原始行为。
                    if CHECKIN_RATIO > 0 and (rc.get("is_number") or rc.get("is_radar")):
                        if rid not in throttle_waiting:
                            events.append(f"[NEW] {name} ({rtype})")
                            throttle_waiting.add(rid)
                        need_wait, gate_msg = checkin_gate(rid, session)
                        if need_wait:
                            events.append(f"[WAIT] {name} {gate_msg}")
                            continue
                        if gate_msg:
                            events.append(f"[GO] {name} {gate_msg}")
                    else:
                        events.append(f"[NEW] {name} ({rtype})")

                    if rc.get("is_number") and not rc.get("is_radar"):
                        if not code:
                            code = get_number_code_from_timetable(
                                rid, cid, rc.get("rollcall_time"), session)
                        if code:
                            ok = answer_number(rid, code, session)
                            if ok:
                                seen.add(rid)
                                events.append(f"[OK] 数字 {name} 码={code}")
                            else:
                                events.append(f"[FAIL] 数字 {name} 提交失败，将重试")
                        else:
                            events.append(f"[!] {name} 签到码暂未获取，将重试")

                    elif rc.get("is_radar"):
                        ok, lat, lon = answer_radar_auto(rid, session)
                        if ok:
                            seen.add(rid)
                            events.append(f"[OK] GPS {name} ({lat:.4f}, {lon:.4f})")
                        else:
                            events.append(f"[FAIL] GPS {name} 将重试")

            # 已签到或已结束的 rid 不必再占着等待记录
            throttle_waiting -= seen

            last_poll = now

        # 限制 events 长度，防止内存泄漏
        if len(events) > 200:
            events = events[-100:]

        dashboard(courses, len(seen), query_count, start_time, events)
        time.sleep(1)


# ============================================================
# 看门狗入口
# ============================================================

def main():
    print()
    print(f"  XMU Tronclass 全自动签到 v1.0")
    print(f"  {'=' * 46}")
    print(f"  学号: {STUDENT_ID}")
    print(f"  登录: xmulogin 纯 API (无浏览器)")
    print(f"  {'=' * 46}")

    restart_count = 0
    restart_times = []

    while True:
        restart_count += 1
        try:
            ok = main_loop()
            if ok is False:
                restart_times.append(time.time())
                restart_times = [t for t in restart_times if time.time() - t < 600]
                if len(restart_times) > 10:
                    print("[看门狗] 10 分钟内失败超过 10 次，放弃。")
                    break
                print(f"[看门狗] 60s 后重试 (第 {restart_count} 次)...")
                time.sleep(60)
                # 仅登录失败时清除 session（网络故障不删）
                if SESSION_FILE.exists():
                    SESSION_FILE.unlink()
                    print("[看门狗] 已清除过期 Session")
            else:
                # main_loop 正常永不返回；到这里说明异常退出
                break
        except KeyboardInterrupt:
            print("\n[看门狗] 用户中断，退出。")
            break
        except Exception as e:
            print(f"[看门狗] 崩溃: {e}")
            restart_times.append(time.time())
            restart_times = [t for t in restart_times if time.time() - t < 600]
            if len(restart_times) > 10:
                print("[看门狗] 10 分钟内崩溃超过 10 次，放弃。")
                break
            print(f"[看门狗] 5s 后重启 (第 {restart_count} 次)...")
            time.sleep(5)


if __name__ == "__main__":
    main()
