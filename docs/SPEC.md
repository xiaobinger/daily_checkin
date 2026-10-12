# 桌面悬浮球 · 五家额度与签到看板

> 规格说明书 v1 · 2026-09-01
> 基于 `每日积分签到` 现有项目扩展，覆盖 WorkBuddy / Qoder CN / TraeWork CN / Zcode / MiniMax Code

---

## 一、现状体检（来自代码与日志的实测结论）

### 1.1 现有能力的真实边界

`daily_checkin.py` 只实现**签到/领取**语义，**没有一处查询余额或额度**。要做"额度看板"，这一层必须从零补——它是本项目最大的工作量，不是 UI。

### 1.2 四条硬发现

| # | 发现 | 证据 | 对设计的影响 |
|---|---|---|---|
| 1 | TraeWork CN 连续 3 天签到失败 | `checkin.log` 8/30、8/31、9/1 均为 `code=9074 当前参与用户太多，请稍后再试` | 采集层必须内置重试；UI 需"失败"态 |
| 2 | Qoder CN 每天返回"无可领取活动" | 日志逐日 `NOT_YET: 当前无可领取的活动` | 这不是失败，需独立的"无活动"状态色 |
| 3 | 五家计量性质根本不同 | 见 1.3 | 数据模型必须同时容纳 points 与 quota |
| 4 | 积分型三家没有"总量" | 实测无 balance 接口 | 进度条分母问题，见 §4.3 |

### 1.3 五家计量性质

| 应用 | 性质 | 有总量上限吗 |
|---|---|---|
| Zcode | **窗口配额**（5 小时 / 周双窗口） | 有 |
| TraeWork CN | 累积积分（`credits`） | 无 |
| WorkBuddy | 累积积分（每日领取） | 无 |
| MiniMax Code | 累积积分（7 日签到日历） | 无 |
| Qoder CN | Credits 资源包（活动领取） | 待测 |

---

## 二、架构

```
┌─ UI 层 · 桌面悬浮球 ───────────────────────────────┐
│   球体（56px 环形进度）  +  点击展开玻璃面板         │
└──────────────────┬───────────────────────────────┘
                   │ 读快照
┌─ 状态层 · quota_state.json ────────────────────────┐
│   额度 · 签到 · 时间戳 · 近 30 天历史（sparkline）  │
└──────────────────┬───────────────────────────────┘
                   │ 定时写入
┌─ 采集层 · 五个 provider 适配器 ────────────────────┐
│  WorkBuddy │ TraeWork CN │ Qoder CN │ MiniMax │ Zcode │
│  统一返回结构 + 缓存节流 + 失败指数退避重试          │
└──────────────────┬───────────────────────────────┘
                   │ 调用
┌─ 数据源 · 本机登录态与官方接口 ────────────────────┐
│   登录态文件 · 凭证（DPAPI / AES-GCM 解密）· HTTPS  │
└──────────────────────────────────────────────────┘
```

**核心原则：采集与展示彻底解耦。** UI 只读 JSON，永不直接碰网络和凭证。

- 悬浮球启动为毫秒级：先渲染上次快照，再异步刷新
- UI 崩溃不影响每日签到计划任务
- 将来换 UI 技术栈不需要动采集层

**复用承诺：** `daily_checkin.py` 中已实现的登录态读取逻辑（WorkBuddy auth 文件解析、Trae refreshToken 轮换、MiniMax `md5(unix+salt+body)` 签名、Qoder AES-GCM + DPAPI 解密）**原样搬入 provider，不重写**。

---

## 三、技术选型（12 项决策，已与用户确认）

| # | 决策项 | 选定 |
|---|---|---|
| 1 | 技术栈 | **Python + pywebview**（后端全复用，前端 HTML/CSS 做玻璃拟态） |
| 2 | 交互形态 | **极简圆球 + 点击展开玻璃面板** |
| 3 | 信息层级 | **额度为主 + 签到为角标** |
| 4 | 视觉风格 | **深色玻璃拟态**（半透明毛玻璃 + 细描边 + 微内发光） |
| 5 | 球体聚合口径 | **木桶原理：显示最紧张的那家** |
| 6 | 刷新与告警 | **15 分钟刷新 + 阈值变色**（<20% 橙，<5% 或失败红） |
| 7 | 操作权限 | **只读 + 一键刷新**（先做最小闭环） |
| 8 | 账号呈现 | **每账号独立一行 + 昵称脱敏**（`xiao***er`） |
| 9 | 百分比分母 | **混合显示**：有总量画进度环，无总量显示绝对值 + 变动箭头 |
| 10 | Zcode 额度来源 | **挖掘 BigModel 配额接口**（已完成，见 §4.1） |
| 11 | 工程化交付 | **独立 venv + 一键启动 .bat + 开机自启**（不打包 exe） |
| 12 | 历史数据 | **存近 30 天 + 面板 7 日 sparkline** |

---

## 四、S0 接口侦察结果（实测，非推测）

### 4.1 Zcode ✅ 完全打通（最难的一项，已攻克）

**凭证解密**（`~/.zcode/v2/credentials.json`，`enc:v1:` 格式）

```
密文格式 = enc:v1:<base64url(nonce 12B)>.<base64url(tag 16B)>.<base64url(ciphertext)>
算法     = aes-256-gcm
密钥     = sha256(process.env.ZCODE_CREDENTIAL_SECRET
                 || `zcode-credential-fallback:${platform}:${homedir}:${username}`)
```

> 注意：**不是** Electron safeStorage。这是 Zcode 自定义的加密方案。

**额度接口**

```
GET https://open.bigmodel.cn/api/monitor/usage/quota/limit
Authorization: Bearer <oauth:bigmodel:access_token>
```

**实测返回**

```json
{"code":200,"msg":"操作成功","success":true,
 "data":{"level":"lite",
   "limits":[
     {"type":"CREDIT_LIMIT","unit":3,"number":5,"usage":2000,
      "currentValue":0,   "remaining":2000,"percentage":0},
     {"type":"CREDIT_LIMIT","unit":6,"number":1,"usage":2000,
      "currentValue":2009,"remaining":0,  "percentage":100,
      "nextResetTime":1788403840995}]}}
```

**周期单位枚举**（从 app.asar 解出常量）

| 窗口 | 时长常量 |
|---|---|
| `FIVE_HOUR` | `300 * 60 * 1000` ms |
| `WEEK` | `10080 * 60 * 1000` ms |

→ `unit=3, number=5` = **5 小时窗口**；`unit=6, number=1` = **周窗口**（`nextResetTime` 实测为 47 小时后，与周窗口吻合）。

> **重要认知修正：** Zcode 没有"每日额度"概念，是**双窗口配额**。当前账号为 `lite` 套餐，**周窗口已 100% 用尽**（2009/2000），5 小时窗口尚余 2000。

**附带发现**（可用于后续功能扩展）

- `POST /api/v1/coding-plan/reset` —— 主动重置配额窗口
- `GET /api/biz/subscription/list` —— 订阅列表
- 响应字段：`available_five_hour_resets` / `available_week_resets` / `has_unread_history`

### 4.2 TraeWork CN ✅ 可用

```
POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/status
Authorization: Cloud-IDE-JWT <accessToken>
```
实测：`{"checked_in": false, "credits": 200, "enable": true}` —— 额度字段 `credits` 可用。
（`checked_in: false` 印证了当日 9074 失败）

### 4.3 WorkBuddy ⚠️ 只有签到，无余额

```
POST https://copilot.tencent.com/v2/billing/meter/checkin-activity-status
```
实测：`{"today_checked_in": true, "streak_days": 1, "daily_credit": 100, "today_credit": 100, ...}`

**余额接口不存在**：`/v2/billing/meter/balance`、`/v2/billing/balance`、`/v2/billing/credit`、`/v2/billing/meter/credit/balance` 全部 `404 Route Not Found`。

> 陷阱提醒：WorkBuddy 的查询接口是 **POST**，用 GET 会返回 404。

### 4.4 MiniMax Code ⚠️ 只有签到日历，无余额

```
GET https://agent.minimaxi.com/minimax-cloud/api/v1/signin/status
```
实测：`data` 只有 `scene` 与 `days`（7 日签到日历），今日为 `day_no:7, points:1000, is_today:true`。
`/v1/points`、`/v1/user/points`、`/v1/user/info` 全部 404。

### 4.5 Qoder CN ❌ 无活动、无余额

`/api/v2/activity/claim/eligibility` 返回 `{"code":0,"data":[]}`（空数组 = 无活动）。
`/api/v2/account/balance`、`/api/v2/user/credits`、`/api/v2/credits/balance`、`/api/v2/user/info` 全部 **503**（ALB 层拦截，即路由不存在）。

### 4.6 数据源可行性总表

| 应用 | 签到状态 | 额度/余额 | 进度环能否画 | 结论 |
|---|---|---|---|---|
| **Zcode** | 不适用 | ✅ 完整（总量/已用/剩余/百分比/重置时间） | ✅ 能 | **理想** |
| **TraeWork CN** | ✅ `checked_in` | ✅ `credits` | ❌ 无总量 | 显示绝对值 |
| **WorkBuddy** | ✅ `today_checked_in` + 连续天数 | ❌ 无余额接口 | ❌ | 显示今日领取 + 连续天数 |
| **MiniMax Code** | ✅ 7 日日历 | ❌ 无余额接口 | ❌ | 显示今日领取 |
| **Qoder CN** | ⚠️ 无活动 | ❌ 无余额接口 | ❌ | 仅状态：无活动 / 失效 |

> 这张表直接印证了决策 #9「混合显示」的正确性：**只有 Zcode 一家能画真正的进度环**。
> 球体采用木桶原理时，当前会显示 Zcode 周窗口的 **0%** 并转红——恰好演示了这个设计的价值。

---

## 五、分阶段路线

| 阶段 | 内容 | 产出 | 状态 |
|---|---|---|---|
| **S0** | 接口侦察，摸清五家数据源 | 本文档 §4 | ✅ **已完成** |
| **S1** | 采集内核：provider 抽象 + 统一结构 + 快照存储 + 缓存节流 | `quotaball/` | ✅ **已完成（五家实测全通）** |
| **S2** | 悬浮球 UI：球体渲染 + 玻璃面板 + 交互 | `quotaball/ui/` | ✅ **已完成（全链路验收通过）** |
| **S3** | 打磨：动效、阈值配色、开机自启、一键启动脚本 | 成品 | ✅ **已完成** |

> **S1 已实现并验证**：`python -m quotaball.cli [--force] [--json]`。
> 五家并发采集 0.5s 完成，10 分钟节流缓存 0.0s 命中，历史按日落入 `quota_state.json`。
> 相对草案的调整：`latest` 为账号记录列表（Trae 多账号各一条）；`history` 按 `app:脱敏账号` 键控；
> 状态枚举补 `no_activity`（Qoder 无活动）与 `quota_exhausted`（额度用尽）。
> 技术收获：Zcode 解密用**纯 Python AES-GCM**（`quotaball/aesgcm.py`），不依赖 Node；
> 真实 Windows 用户名是 `xiongbing`（≠ 配置文件夹名 `admin`），密钥派生必须用 `getpass.getuser()`。

> **S2/S3 已实现并验收**（2026-09-02）：
> - 架构：`quotaball/ui/app.py`（pywebview 宿主 + js_api 桥）+ `quotaball/ui/index.html`（深色玻璃 UI）。
> - 形态：真透明 76x76 圆球 ↔ 展开 446px 玻璃面板（flip 自动翻转）；默认真透明，
>   `--rgn` 降级（GPU 合成损坏时 SetWindowRgn 裁形，区域按请求几何计算）。
> - 拖拽：JS 报增量 + Python `window.move`；单实例守卫；`get_payload` 秒回缓存，采集后台化 + 主动推送。
> - 关键坑（详见技能 `pywebview-ball-debug`）：Api 公开属性会被桥扫描递归展开 .NET 对象图致 OOM
>   （内部引用一律 `_` 前缀）；WinForms resize+move 后 WebView2 子控件可能失同步
>   （BeginInvoke 强制 `Bounds = ClientRectangle`）；时钟跳变会毁新起 WebView2 的 GPU（自愈）；
>   小视口里 `vh` 与 flex-shrink 会破坏面板测量。
> - 开机自启：HKCU Run 键 `QuotaBall` → `.venv-ball\Scripts\pythonw.exe quotaball/ui/app.py`。
> - 验收：`probe/verify_ui4.py` 全绿（尺寸稳定/四角透明/球体渲染/拖拽/展开/收起）。

### S1 统一返回结构（草案）

```python
{
  "app": "Zcode",
  "account": "bigmodel-***",        # 脱敏
  "fetched_at": 1788233049,
  "checkin": {                       # 无签到概念的应用为 null
    "state": "ok|already|pending|no_activity|no_session|error",
    "today_gain": 100,               # 今日领取，无则为 null
    "streak_days": 1                 # 无则为 null
  },
  "quota": {                         # 无总量则为 null
    "windows": [
      {"label":"5 小时窗口","used":0,"total":2000,"remaining":2000,
       "percent_used":0.0,"reset_at":1788403840995}
    ]
  },
  "absolute": {"value": 200, "unit": "credits", "delta_1d": 100},  # 无总量时用
  "error": None
}
```

### S1 重试策略（专治 Trae 9074）

- 仅对**可重试错误**重试：HTTP 5xx、网络超时、业务码 9074（限流）
- 指数退避：2s → 4s → 8s，最多 3 次
- 不可重试错误直接判定：401/403（凭证失效）、404（接口变更）

---

## 六、风险与降级

| 风险 | 影响 | 降级方案 |
|---|---|---|
| WorkBuddy / MiniMax / Qoder 始终查不到余额 | 三家只能显示签到状态 | 已接受：按"混合显示"方案，显示绝对值与状态；不伪造百分比 |
| Zcode 自定义加密方案随版本变更 | 凭证解密失败 | 捕获异常 → 状态置 `no_session`，提示"请重新登录 Zcode" |
| pywebview 依赖 WebView2 运行时 | 启动失败 | S1 前先检测；缺失则引导安装 Evergreen Runtime |
| Qoder 长期无活动 | 面板长期显示"无活动" | 归入中性灰，不误报为异常 |
| 各家接口变更（404/503） | provider 失效 | 每个 provider 独立 try/except，单家失败不影响其他四家 |

---

## 七、工程化

- 独立 venv：`E:\Desktop\每日积分签到\.venv-ball\`，**不污染计划任务使用的 Python 3.14**
- 一键启动：`启动悬浮球.bat`
- 开机自启：注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`

---

## 八、待确认项的处理结果

1. ~~WorkBuddy 余额是否不存在~~ → **已确认不存在**（balance 类接口全 404），按"混合显示"只显示今日领取 + 连续天数，已在 S1 落地。
2. ~~Qoder CN 的 Credits 余额是否值得深挖~~ → **已实测**：余额类路径全部 503（路由不存在），Qoder 只呈现"暂无活动 / 可领取 / 失效"状态。
3. ~~Trae 9074 是否自动重试~~ → **按吊毛要求：默认不自动重试**，失败如实上报由 UI 呈现，用户手动触发刷新；采集层保留 `RetryableError` 类型与 `RETRY_ENABLED` 开关，将来要开只需改一个常量。
