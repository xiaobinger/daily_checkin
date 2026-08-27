---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: eb68d53ec689089dc9e41227abe6bcc8_c4955dc7a06611f1a413525400287e28
    ReservedCode1: +JBfoaKxI4thEdL+29kn9nHo9WAvnqlh8/bgRkZebgamhXfmcpKPWbsQsLKckSuOakHb+h0vyWbIqKWB7vz6KmsnTMLI3N9otrz+9YgdicW1+Wf0stdQ+8YlWiV5gHy3FRWJlPVM/vjjZ/Vf4PfH6WxvEMVz+Ct6GdtKredwDzVLgAaxa215KDeIeMQ=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: eb68d53ec689089dc9e41227abe6bcc8_c4955dc7a06611f1a413525400287e28
    ReservedCode2: +JBfoaKxI4thEdL+29kn9nHo9WAvnqlh8/bgRkZebgamhXfmcpKPWbsQsLKckSuOakHb+h0vyWbIqKWB7vz6KmsnTMLI3N9otrz+9YgdicW1+Wf0stdQ+8YlWiV5gHy3FRWJlPVM/vjjZ/Vf4PfH6WxvEMVz+Ct6GdtKredwDzVLgAaxa215KDeIeMQ=
---

# TraeWorkCN + WorkBuddy 每日自动签到

一个脚本同时覆盖两个应用的每日签到，只依赖 Python 标准库，无第三方包。

## 文件说明

| 文件 | 说明 |
|------|------|
| `daily_checkin.py` | 签到主脚本 |
| `trae_login.py` | TraeWorkCN 凭证获取脚本（纯 Python，一次性使用） |
| `auths/` | TraeWorkCN 凭证目录（放 `trae-*.json`） |
| `checkin.log` | 每次运行自动追加日志 |

## 运行方式

```bat
python daily_checkin.py                :: 两个应用都签到
python daily_checkin.py --workbuddy-only
python daily_checkin.py --trae-only
python daily_checkin.py --check-only   :: 只查状态，不领取
```

## 1. WorkBuddy（开箱即用）

脚本自动读取本机 WorkBuddy 桌面端登录态：
`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`

只要桌面端登录过即可直接运行，无需额外配置。每日领取 100 积分；已签到会自动跳过（幂等）。

## 2. TraeWorkCN（需一次性配置凭证）

TRAE SOLO CN 客户端的登录 token 由系统安全存储加密，脚本无法直接读取，需要一次性导入凭证：

**方式一（推荐，纯 Python，无需 Go）**：运行同目录下的 `trae_login.py` 完成一次登录：

```bat
python trae_login.py
```

流程全自动：脚本会打开浏览器访问 trae.cn 授权页 → 手机号/验证码登录 → 本地回调服务自动捕获登录回调 → 自动换 token 并落盘 `auths/trae-{uid}.json` → 立即签到验证。
若 18080 端口被占用，自动降级为手动粘贴回调链接模式，按提示操作即可。

**方式二（备选）**：使用开源项目 [Sliverkiss/traework2api](https://github.com/Sliverkiss/traework2api) 的 `login.sh` 生成 `trae-*.json`，复制到 `auths/` 文件夹（需 Go 环境）。

脚本会自动发现 `auths/trae-*.json`（支持多账号），accessToken 临近过期时自动用 refreshToken 调用 ExchangeToken 轮换并写回。

**方式二（手动）**：抓包 TRAE SOLO CN 客户端的 `Cloud-IDE-JWT` token，按扁平格式写入脚本同目录 `trae_auth.json`：

```json
{
  "accessToken": "粘贴抓包到的 token",
  "refreshToken": "可留空",
  "expiresAt": 0,
  "deviceId": "客户端 X-Device-Id",
  "uid": "账号 uid"
}
```

## 配置文件（checkin_config.json）

计划任务的三项配置统一由同目录 `checkin_config.json` 管理，改完配置后**自动同步**到计划任务，无需手动改计划任务：

| 字段 | 说明 |
|------|------|
| `schedule_time` | 每日执行时间（24 小时制，如 `09:00`） |
| `python_path` | 启动程序路径（Python 解释器 `python.exe`） |
| `script_args` | 签到脚本参数；不写=三应用全量，可填 `--workbuddy-only` / `--trae-only` / `--minimax-only` |
| `task_name` | 计划任务名（默认 `Marvis_DailyCheckin`） |
| `script_path` | 签到主脚本路径 |

## 自动同步机制

- `sync_task.py`：读取配置，比对计划任务的执行时间与命令，**有差异才更新**（/F 重建），一致则零副作用；`python sync_task.py --check` 可只查不改。
- 计划任务 **Marvis_CheckinSync**：每 30 分钟自动运行一次 `sync_task.py`，保证改完配置后**最多 30 分钟内自动生效**，无需手动操作。
- 签到任务 `Marvis_DailyCheckin` 每日运行时先过 `run_checkin.py` 统一入口——先同步配置、再执行签到，双重保证配置始终对齐。

立即生效（不想等 30 分钟时）：
```bat
python sync_task.py                                  :: 立即同步一次
schtasks /Run /TN Marvis_DailyCheckin                :: 立即按新配置跑一次签到
```

任务管理：
```bat
schtasks /Query  /TN Marvis_DailyCheckin            :: 查看签到任务
schtasks /Query  /TN Marvis_CheckinSync             :: 查看同步任务
schtasks /Delete /TN Marvis_CheckinSync /F          :: 删除同步任务（此后不再自动同步）
```

## 注意事项

- 计划任务使用当前机器的 Python 解释器路径（`python.exe`）；若更换 Python 环境需同步更新任务命令
- 登录态失效时脚本会输出 `NO_SESSION`，重新登录对应桌面端即可恢复
- 日志见 `checkin.log`
*（内容由AI生成，仅供参考）*
