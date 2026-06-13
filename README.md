# 泡沫破灭监控看板 · 火柴 × 炸药

把"外部冲击(火柴) vs 内部结构(炸药量)"框架做成一个看板：量化指标云端自动取数、按阈值给出建议状态，定性指标手动判断，手动可覆盖自动。真正的红灯是**两者同时高**（光点滑进右上象限）。

## 文件

| 文件 | 作用 |
|---|---|
| `index.html` | 看板本体（纯静态，读 `data.json`，无任何密钥） |
| `fetch.py` | 取数脚本（标准库零依赖）：FRED+Yahoo+HKMA+Tushare → 算状态 → 写 `data.json` |
| `data.json` | 取数结果（公开安全，由 Actions 生成/覆盖） |
| `history.json` | 每日自动状态快照（Actions 维护，前端画"增还是减"趋势与象限轨迹） |
| `data.sample.json` / `history.sample.json` | 示例数据，本地预览：`cp data.sample.json data.json; cp history.sample.json history.json`（看完记得删 history.json，别让假历史被合并） |
| `serve.py` | 可选的本地服务器（含 `/refresh` 实时重拉），`python3 serve.py` |
| `.github/workflows/refresh.yml` | 定时任务：云端跑 `fetch.py` 并提交 `data.json` |
| `.env` | 本地 token（已被 `.gitignore` 排除，**绝不提交**） |

## ⚠️ 安全模型（关键）

**静态网页无法隐藏浏览器要用的密钥**——所以 token 永远不进网页、不进 `data.json`、不进仓库：

- 本地：token 放在 `.env`（`.gitignore` 已排除）。
- 线上：token 作为 GitHub **仓库 Secret**（加密），只有 Actions 在云端取数时用，产出的 `data.json` 只含派生的公开数据。

> 你的 token 若曾在聊天/邮件等处明文出现过，建议到 https://tushare.pro/user/token **重新生成一次**作废旧的。

## 网络说明（为什么必须云端取数）

FRED / Yahoo / HKMA 等境外源在中国大陆网络下会被重置（连接 reset）。**GitHub Actions 跑在境外云端，能正常访问这些源**——所以"云端定时取数 + 网页只读 data.json"不只是方便，对墙内用户是必须的：你本机直接 `python3 fetch.py` 通常拉不到境外数据（除非有代理）。Tushare 是境内源，本机可达。

> 注意：GitHub Pages（`*.github.io`）在大陆有时不稳定/被墙，发布后若打不开属此原因，可考虑自定义域名 / Cloudflare / 国内静态托管。

## 发布到 GitHub（Pages + Actions）

```bash
cd /Users/huangyi/stock/bubbles
git init && git add . && git commit -m "init bubble monitor"
git branch -M main
git remote add origin https://github.com/<你>/<仓库>.git
git push -u origin main
```

然后在 GitHub 仓库里：

1. **Settings → Secrets and variables → Actions → New repository secret**
   名称 `TUSHARE_TOKEN`，值填你的（重新生成后的）Tushare token。
2. **Settings → Pages**：Source 选 `main` 分支根目录，保存。几分钟后得到 `https://<你>.github.io/<仓库>/`。
3. **Actions** 标签页 → 选 *Refresh dashboard data* → **Run workflow** 手动跑一次（首次生成真实 `data.json`）。之后按 `refresh.yml` 里的 cron 每个工作日自动刷新（美股收盘后、港A股收盘后各一次）。

网页打开后每 30 分钟自动重载 `data.json`，并在切回标签页时刷新。

## 本地预览

```bash
cp data.sample.json data.json      # 用示例数据先看效果
python3 -m http.server 8753        # 浏览器开 http://localhost:8753
# 或：python3 serve.py             # 多一个 ↻ 实时重拉按钮（需能访问境外源）
```

## 调阈值

自动状态的阈值都在 `fetch.py` 各 `build_*` 函数里、有中文注释（如 `SOFR−IORB ≥5bp 警报`、`ON RRP <250亿 观察`）。改完重新取数即可。状态只是"建议"，网页里任何指标手动点一下就能覆盖，再点一次取消、回落到自动。

## 指标覆盖

- **自动**（FRED/Yahoo/HKMA/Tushare）：SOFR−IORB、ON RRP、银行准备金、WALCL、核心PCE、油价、失业率、MMF、HY利差、CAPE、巴菲特指标(市值/GDP)、香港总结余+HIBOR、南向资金、A股两融余额。
- **自动·增速二阶导**（SEC EDGAR 官方 XBRL，免 key）：NVDA 营收 QoQ 增速及加速度、四巨头(MSFT/GOOGL/AMZN/META)合计 capex QoQ 增速及加速度——单季增速回落=观察、连续两季回落=警报（10-K 缺失的 Q4 用 全年−三季 推导；capex 未季调，留意季节性）。
- **自动·背景上下文**（Tushare，墙内本地可取）：中国 CPI / M2 / 制造业PMI / 新增社融 / 近30日A股IPO。
- **手动**（定性，机器替不了判断）：叙事相变、FOMC 措辞、Warsh 表态、AI 营收/capex 二阶导、美股 Margin Debt、散户期权、AI 债务融资占比、IPO 首日表现。

> 墙内/墙外差异：本地直接 `python3 fetch.py` 只能取到 Tushare 部分（南向、两融、中国宏观）；境外源（FRED/Yahoo/HKMA）必须由 GitHub Actions 在云端取。两者都会写进同一个 `data.json`。

## 看板用法

- **趋势 · 增还是减**：当前态势卡里的双线趋势图回放 `history.json` 每日快照算出的炸药/火柴分数（自动口径，今日一点含手动覆盖），并给出近 7 日 Δ——直接回答框架的核心问题"炸药量在增还是减"。象限图上的小白点是光点最近 12 天的轨迹。历史由云端 Actions 每日积累，需要跑几天后才出现。
- 数据超过 4 天未更新时，页头会出现 ⚠ 警告（Actions 挂了能立刻看出来）。
- **当前态势**卡底部的「当前亮灯」列出所有处于观察/警报的指标（自动+手动），点击直接跳到对应卡片。
- 每个量化卡显示实时读数 + 迷你走势图 + 蓝色「自动」标；手动点状态即覆盖（显示「手动覆盖」），再点一次取消、回落自动。
- 顶部「集中看触发器」只盯 5 个引爆点（FOMC 措辞 / 资产负债表收缩 / Warsh / SOFR / 叙事相变），任一动了才需全面复盘。
