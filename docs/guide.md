# 使用与签到规则

[返回 README](../README.md)

## 使用

```powershell
# 1. 安装依赖
pip install -r requirements.txt

# 2. 运行
python auto_checkin.py
```

运行时按提示输入学号和密码。程序会保存 Cookie，下次启动时尝试复用登录会话。账号也可通过环境变量传入：

```powershell
$env:STUDENT_ID = "学号"
$env:PASSWORD = "密码"
python auto_checkin.py
```

## 数字签到取码

优先读取旧明细接口 `GET /api/rollcall/{id}/student_rollcalls` 的 `number_code`；缺码时补查 `GET /api/timetable_rollcalls?course_ids={course_id}&rollcall_date=YYYY-MM-DD`。日期取课程点名记录 `rollcall_time` 的 UTC 日期，不直接使用北京时间；缺少有效时间或课程 ID 时不补查。

补查结果只接受点名 ID 和课程 ID 都匹配的四位数字字符串，保留前导零。不缓存签到码；补查失败或没有匹配记录时不提交，等下一轮重查。人数、本人状态、结束时间仍沿用旧接口，GPS 和节流逻辑不变。

离线回归检查：`python test_number_code.py` 和 `python test_checkin.py`，不需要账号或第三方依赖，不会访问网络或执行真实签到。

## 签到节流（等同学先签）

默认不当第一个签到的人：提交前先查本次签到的班级名单，等已签到比例**超过 30%** 再签。参数在 `auto_checkin.py` 顶部：

```python
CHECKIN_RATIO = 0.30        # 超过此比例才签，改成 0 关闭该优化
```

已签到比例必须严格超过 30%；恰好 30% 时仍等待。没有等待时长或剩余时间兜底，比例始终未达标时会等待到点名结束，不提交签到。名单接口异常、人数无法取得或判据不可信时，则跳过比例限制。

通过 `end_time` 判断点名是否结束，到点记录 `[END]` 并停止轮询该场次。而 `end_time` 只有点名详情接口给（课程列表里恒为 `None`，`is_expired` 即使 `finished` 也仍是 `False`），所以数字和 GPS 都会去读一次详情。

数字签到和 GPS 签到共用这套节流。等待是**非阻塞**的：未达标的签到只记在 `throttle_waiting` 里，下个轮询周期（15 秒）重新判断，期间的其他课程照常处理。控制台用 `[WAIT]` / `[GO]` 事件显示实时人数，例如：

```text
[WAIT] 高等数学 已签到 4/20 (20%) 未达 30%，继续等
[GO] 高等数学 已签到 8/20 (40%) 达标，开签
```

已签人数按名单里每人的 `rollcall_status == "on_call_fine"` 统计（名单接口 `GET /api/rollcall/{id}/student_rollcalls`），总人数即名单长度；任一记录缺少 `rollcall_status` 字段或结构不完整时，视为判据不可信，本轮不节流。字段存在但值为 `null` 表示尚未签到。注意名单里每人的 `updated_at` 从建记录起就有值（等于点名创建时间），`status` 恒为 `on_call`，**都不能**用来判已签。

拿不到名单（接口异常、返回空）或自校发现判据与接口口径不符（自己还没签却被算成已签）时立即签到，不因节流错过签到。

## GPS 签到与成功判定

GPS（雷达）点名先往两个探针坐标各提一次：命中就直接用，未命中则拿响应里的 `distance` 做两圆相交，反解出目标坐标后提交候选点。

签到结果以本人的签到状态为准。两个接口的 `status` 含义不同：

| 接口 | `status` 含义 | 见过的值 |
| --- | --- | --- |
| `GET /api/course/{id}/student/{sid}/rollcalls` | 本人在该条点名里的状态 | `absent`（未签）、`on_call_fine`（已签） |
| `GET /api/rollcall/{id}/student_rollcalls` 顶层 | 点名本身的状态 | `in_progress`、`finished` |

拿顶层 `status` 比 `on_call_fine` 永远不相等，会把**已经签上的** GPS 签到报成失败；所以提交后统一回查名单里 `user_no` 等于本人学号那条的 `rollcall_status`（`check_my_checkin_status`）。

若教师本次没有限制签到范围，探针坐标会被直接接受（名单里本人的 `distance` 为 `None`），日志里的坐标只是探针值，不代表真实位置。
