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
CHECKIN_MAX_WAIT = 120      # 最多等 120 秒，超时直接签（小班 / 冷课堂保底）
CHECKIN_MIN_REMAINING = 20  # 距签到结束不足 20 秒不再等，立即签
```

数字签到和 GPS 签到共用这套节流。等待是**非阻塞**的：未达标的签到只记录在 `ratio_wait_since`，下个轮询周期（15 秒）重新判断，期间的其他课程照常处理。控制台用 `[WAIT]` / `[GO]` 事件显示实时人数，例如：

```text
[WAIT] 高等数学 已签到 4/20 (20%) 未达 30%，继续等
[GO] 高等数学 已签到 8/20 (40%) 达标，开签
```

拿不到名单（接口异常、返回空）时立即签到，不因节流错过签到。

## 声明

本项目仅供技术交流与学习，请勿用于非法用途。
