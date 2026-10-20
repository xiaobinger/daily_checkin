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

# TraeWorkCN + WorkBuddy + MiniMax Code + Qoder CN + LobsterAI 每日自动签到

一个脚本同时覆盖五个应用的每日签到/积分领取，只依赖 Python 标准库，无第三方包。

## 文件说明

| 文件 | 说明 |
|------|------|
| `daily_checkin.py` | 签到主脚本 |
| `trae_login.py` | TraeWorkCN 凭证获取脚本（纯 Python，一次性使用） |
| `auths/` | TraeWorkCN 凭证目录（放 `trae-*.json`） |
| `checkin.log` | 每次运行自动追加日志 |
| `quotaball/` | 采集层（悬浮球用，只读快照） |
| `tools/` | 辅助工具：`wb_extract.js`（WorkBuddy 密钥提取）、`wb_cache_key.py`（DPAPI 缓存密钥）、`mm_sign_check.py`（MiniMax 签名回归自检） |

## 运行方式

```bat
python daily_checkin.py                :: 五个应用都签到
python daily_checkin.py --workbuddy-only
python daily_checkin.py --trae-only
python daily_checkin.py --minimax-only
python daily_checkin.py --qoder-only
python daily_checkin.py --lobster-only
python daily_checkin.py --check-only   :: 只查状态，不领取
```

## 1. WorkBuddy（开箱即用）

脚本自动读取本机 WorkBuddy 桌面端登录态：
`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`

只要桌面端登录过即可直接运行，无需额外配置。每日领取 100 积分；已签到会自动跳过（幂等）。

> **WorkBuddy 5.7+ 的加密登录态（已适配）**
> 5.7 起 `auth.accessToken`、`account.nickname` 等敏感字段改成了
> `{"$wbEncrypted":1,"envelope":{...}}` 的 AES-256-GCM 信封，解密实现见
> `quotaball/wbatrest.py`（AAD 绑定 keyId/套件/framing）。
> 解密密钥（`atRestSecretKey`）由桌面端打过补丁的 Electron 原生模块
> `electron_browser_workbuddy_storage.loggerGet()` 提供，**磁盘上没有副本**，
> 需要一次性提取：
>
> ```bat
> :: 1) 退出 WorkBuddy 桌面端
> node tools\wb_extract.js
> :: 2) 把输出的 atRestSecretKey 用 DPAPI 缓存到 %LOCALAPPDATA%\quotaball
> python tools\wb_cache_key.py "<atRestSecretKey>"
> ```
>
> 缓存后每天自动解密，无需重复提取。也可设环境变量 `WORKBUDDY_TOKEN`
> 直接注入明文 accessToken 作为兜底。

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

## 3. MiniMax Code（开箱即用）

依赖本机 MiniMax Code 桌面端登录态。

> **3.1 版本起换成了 OAuth 设备码流**，凭证明文在
> `%USERPROFILE%\.minimax\auth\<buildEnv>\<region>\mcode-public\auth.json`，
> 里面是 `records[<service>\0<account>]`：
> `accessToken`（`mmoat_` 前缀，**只有 35~60 分钟有效期**）、`refreshToken`（`mmort_` 前缀）、`expiresAtMs`、`generation`。
>
> 旧的 `local-runtime.auth.json`（JWT）自 2026-09-04 起就没再更新，2026-10-14 过期，
> 所以 10-15 起每天 401。

脚本会自己续期，不用开客户端：临近过期就用 `refreshToken` 调
`POST https://account.minimax.cn/oauth2/token`（`grant_type=refresh_token`，`client_id=mcode-public`）
换新的 `accessToken`，并原子写回 `auth.json`（服务端同时下发新的 refreshToken，旧的不会失效）。
逻辑见 `quotaball/mmauth.py`。

请求签名也复刻了 3.1 桌面端（逆向自 `app.asar`）：

```
Authorization: Bearer <accessToken>
yy          = md5(encodeURIComponent(path + "?" + URLSearchParams(params)) + "_" + yyBody
                  + md5(str(time_ms)) + "ooui")
x-timestamp = floor(time_ms / 1000)
x-signature = md5(str(second) + "I*7Cf%WZ#S&%1RlZJ&C2" + bodyText)
```

两个容易踩的坑：

- 签名串里的 `params` 是 `URLSearchParams` 序列化结果，**包含** `op_ticket=undefined` 与
  `token=null`；真正发到服务器的 URL 由 axios 序列化，会跳过这两个 null/undefined 键。两串本来就不一样。
- `time_ms = Date.parse(new Date().toString())` 只到秒精度，所以 `unix` 参数和签名时间戳都是整秒 × 1000。

设备指纹（`uuid` / `device_id` / `user_id` / 屏幕尺寸等）会随安装变化，脚本每次从桌面端最新
API 日志 `%APPDATA%\MiniMax\logs\api-*.log` 里抄一份真实 query 回来，按客户端的固定顺序重建。

接口：`GET /minimax-cloud/api/v1/signin/status` 查状态、`POST /minimax-cloud/api/v1/signin/claim` 领取
（body `{}`），每日 400+ 积分。签到接口幂等，重复领取不会重复计分；脚本以服务端返回的
`status==3`（当日已领）为准，辅以本地 `claim_id` 记录做二次去重。

只要桌面端登录过即可直接运行，无需额外配置。

## 4. Qoder CN（开箱即用）

依赖本机 Qoder CN / QoderWork CN 桌面端登录态，脚本自动读取并解密：

- AES-256-GCM 密钥：`%APPDATA%\QoderCN\Local State` 的 `os_crypt.encrypted_key`（DPAPI 保护，纯 Python 解密）
- 登录态：`%APPDATA%\QoderCN\User\globalStorage\state.vscdb` 的 `secret://aicoding.auth.userInfo`（Chromium v10 密文）

调用 `https://openapi.qoder.com.cn` 活动接口：先 `GET /api/v2/activity/claim/eligibility` 查询可领取活动，再对每个可领取活动 `POST /api/v2/activity/claim?activityId=...` 完成领取。

token（`dt-xxx` 格式）由客户端自动续签，过期后需打开一次 Qoder CN 让客户端续期。支持多活动批量领取，结果逐条报告。

## 5. LobsterAI（开箱即用）

登录态来自桌面端 sqlite 库 `%APPDATA%\LobsterAI\lobsterai.sqlite` 的 `kv` 表：
`auth_tokens`（accessToken + refreshToken，HS512 JWT，约 30 天）、`auth_user`（昵称）。

调用官方活动接口 `https://lobsterai-server.youdao.com`：

| 步骤 | 请求 |
|------|------|
| 活动槽位 | `GET /api/client-activities/slot?placement=desktop_sidebar&clientVersion=&containerApiVersion=2&platform=win32` |
| 签到状态 | `GET /api/client-activities/{activityCode}/context?configRevision=N` |
| 领取 | `POST /api/client-activities/{activityCode}/actions/check_in`，body `{configRevision, idempotencyKey, payload:{}}` |

活动码与配置版本会随灰度变化，脚本每次都重新拉取，不写死。每日 100 credits，
业务码 `51104` 表示今日已领、`51106` 表示活动配置已更新（重跑一次即可）。

## 配置文件（checkin_config.json）

计划任务的三项配置统一由同目录 `checkin_config.json` 管理，改完配置后**自动同步**到计划任务，无需手动改计划任务：

| 字段 | 说明 |
|------|------|
| `schedule_time` | 每日执行时间（24 小时制，如 `09:00`） |
| `python_path` | 启动程序路径（Python 解释器 `python.exe`） |
| `script_args` | 签到脚本参数；不写=四应用全量，可填 `--workbuddy-only` / `--trae-only` / `--minimax-only` / `--qoder-only` |
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
