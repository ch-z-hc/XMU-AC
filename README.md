# XMU Tronclass 全自动签到

厦门大学畅课（Tronclass）全自动签到脚本。支持数字签到和 GPS 签到（三角定位自动算坐标）。

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

## 声明

本项目仅供技术交流与学习，请勿用于非法用途。
