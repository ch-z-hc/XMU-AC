# XMU Tronclass 全自动签到

厦门大学畅课（Tronclass）全自动签到脚本。支持数字签到和 GPS 签到（三角定位自动算坐标），并带签到节流（等班级超过 30% 的人签过之后再签）。

## 使用

```powershell
# 1. 安装依赖
pip install -r requirements.txt

# 2. 运行
python auto_checkin.py
```

首次运行会提示输入学号和密码，后续通过 Cookie 持久化自动登录。也可通过环境变量传入：

```powershell
$env:STUDENT_ID = "学号"
$env:PASSWORD = "密码"
python auto_checkin.py
```

## 签到节流（等同学先签）

默认不当第一个签到的人：提交前先查本次签到的班级名单，等已签到比例**超过 30%** 再签。参数在 `auto_checkin.py` 顶部：

```python
CHECKIN_RATIO = 0.30        # 超过此比例才签，改成 0 关闭该优化
```

**判据只有比例这一条，没有任何时间兜底**：没超过 30% 就一直等，等完整场也不签（宁可缺勤，不当那一小批人）。以前有「等超 120 秒直接签」「只剩 20 秒直接签」两条，都已删掉。唯一还绕过比例的情况是名单接口异常、拿不到人数 —— 那是「不知道」不是「没到」，不该把签到卡死。

不签不等于卡住：点名有没有结束靠 `end_time` 判，到点打一行 `[END]` 收尾并从轮询里摘掉。而 `end_time` 只有点名详情接口给（课程列表里恒为 `None`，`is_expired` 即使 `finished` 也仍是 `False`），所以数字和 GPS 都会去读一次详情。

数字签到和 GPS 签到共用这套节流。等待是**非阻塞**的：未达标的签到只记在 `throttle_waiting` 里，下个轮询周期（15 秒）重新判断，期间的其他课程照常处理。控制台用 `[WAIT]` / `[GO]` 事件显示实时人数，例如：

```text
[WAIT] 高等数学 已签到 4/20 (20%) 未达 30%，继续等
[GO] 高等数学 已签到 8/20 (40%) 达标，开签
```

已签人数按名单里每人的 `rollcall_status == "on_call_fine"` 统计（名单接口 `GET /api/rollcall/{id}/student_rollcalls`），总人数即名单长度；若接口没有 `rollcall_status` 字段则退回按 `updated_at` 是否写入判。注意名单里每人的 `updated_at` 从建记录起就有值（等于点名创建时间），`status` 恒为 `on_call`，**都不能**用来判已签。

拿不到名单（接口异常、返回空）或自校发现判据与接口口径不符（自己还没签却被算成已签）时立即签到，不因节流错过签到。

## GPS 签到与成功判定

GPS（雷达）点名先往两个探针坐标各提一次：命中就直接用，未命中则拿响应里的 `distance` 做两圆相交，反解出目标坐标后提交候选点。

⚠️ **签到成没成功只能看「本人」的状态**，别看点名的状态。两个接口的 `status` 不是一回事：

| 接口 | `status` 含义 | 见过的值 |
| --- | --- | --- |
| `GET /api/course/{id}/student/{sid}/rollcalls` | 本人在该条点名里的状态 | `absent`（未签）、`on_call_fine`（已签） |
| `GET /api/rollcall/{id}/student_rollcalls` 顶层 | 点名本身的状态 | `in_progress`、`finished` |

拿顶层 `status` 比 `on_call_fine` 永远不相等，会把**已经签上的** GPS 签到报成失败（曾经踩过）；所以提交后统一回查名单里 `user_no` 等于本人学号那条的 `rollcall_status`（`check_my_checkin_status`）。

若教师本次没有限制签到范围，探针坐标会被直接接受（名单里本人的 `distance` 为 `None`），日志里的坐标只是探针值，不代表真实位置。

## 声明

本项目仅供技术交流与学习，请勿用于非法用途。
