#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
泡沫监控看板 · 取数脚本（标准库零依赖）

从 FRED / Yahoo / HKMA / Tushare 拉数 → 算出每个量化指标的读数与建议状态
→ 写入 data.json（公开安全，不含任何密钥）。

  python3 fetch.py                 # 读 .env 里的 TUSHARE_TOKEN
  TUSHARE_TOKEN=xxx python3 fetch.py

任何源失败都会被捕获记进 data.json 的 errors，脚本始终产出 data.json。
阈值集中在各 build_* 里，注释标了，可随时调。状态只是“建议”，网页里手动可覆盖。
"""
import json, os, re, ssl, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone, date, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")
CTX = ssl.create_default_context()

def load_env():
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
load_env()
TOKEN = os.environ.get("TUSHARE_TOKEN", "").strip()

IND, CONTEXT, ERRORS = {}, {}, []
def err(src, e):
    ERRORS.append(f"{src}: {e}")
    print(f"  ! {src}: {e}", file=sys.stderr)

def http(url, data=None, headers=None, timeout=25, retries=2):
    h = {"User-Agent": UA}
    if headers: h.update(headers)
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))  # 退避重试，缓解瞬时失败/限流
    raise last

# ---------- 数据源 ----------
def fred(series_id):
    """FRED fredgraph.csv（无需 key），返回 [(date,float)] 升序。"""
    txt = http(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}")
    out = []
    for ln in txt.splitlines()[1:]:
        if "," not in ln: continue
        d, v = ln.split(",", 1); v = v.strip()
        if v in (".", ""): continue
        try: out.append((d, float(v)))
        except ValueError: pass
    return out

def yahoo(symbol, rng="6mo"):
    s = urllib.parse.quote(symbol)
    last = None
    for host in ("query1", "query2"):  # query1 限流时退到 query2
        try:
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{s}?range={rng}&interval=1d"
            j = json.loads(http(url, retries=1))
            res = j["chart"]["result"][0]
            ts, cl = res["timestamp"], res["indicators"]["quote"][0]["close"]
            out = []
            for t, c in zip(ts, cl):
                if c is None: continue
                out.append((datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"), float(c)))
            return out
        except Exception as e:
            last = e
    raise last

def hkma(path, pagesize=50):
    url = (f"https://api.hkma.gov.hk/public/market-data-and-statistics/"
           f"daily-monetary-statistics/{path}?sortby=end_of_date&sortorder=desc&pagesize={pagesize}")
    return json.loads(http(url))["result"]["records"]

def tushare(api_name, params=None, fields=""):
    if not TOKEN: raise RuntimeError("无 TUSHARE_TOKEN")
    body = json.dumps({"api_name": api_name, "token": TOKEN,
                       "params": params or {}, "fields": fields}).encode()
    j = json.loads(http("http://api.tushare.pro", data=body,
                        headers={"Content-Type": "application/json"}))
    if j.get("code") != 0:
        raise RuntimeError(j.get("msg") or "tushare error")
    d = j["data"]
    return [dict(zip(d["fields"], r)) for r in d["items"]]

# ---------- SEC EDGAR（官方 XBRL API，免 key；用于“增速二阶导”） ----------
SEC_UA = "bubble-monitor/1.0 (open-source personal dashboard)"
EDGAR_CIK = {"NVDA": "0001045810", "MSFT": "0000789019", "GOOGL": "0001652044",
             "AMZN": "0001018724", "META": "0001326801"}
REV_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"]
CAPEX_TAGS = ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"]

def _quarterize(ents):
    """XBRL 条目 → [(end_date, val)] 升序季度序列。
    10-K 只报全年的，用 全年 − 三个已知季度 推导缺失的 Q4。"""
    def days(a, b):
        return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days
    q, ann = {}, {}
    for e in ents:
        s, en, v = e.get("start"), e.get("end"), e.get("val")
        if not s or not en or v is None: continue
        d = days(s, en); f = e.get("filed", "")
        if 80 <= d <= 100:
            if en not in q or f > q[en][1]: q[en] = (float(v), f)
        elif 350 <= d <= 380:
            if en not in ann or f > ann[en][2]: ann[en] = (float(v), s, f)
    for en, (av, s, _) in list(ann.items()):
        if en in q: continue
        ins = [v for k, (v, _) in q.items() if s < k < en]
        if len(ins) == 3: q[en] = (av - sum(ins), "derived")
    return sorted((k, v[0]) for k, v in q.items())

def edgar_quarters(symbol, tags):
    """按候选标签依次尝试，返回该公司的季度序列。"""
    cik = EDGAR_CIK[symbol]; last = None
    for tag in tags:
        try:
            j = json.loads(http(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json",
                                headers={"User-Agent": SEC_UA, "Accept-Encoding": "identity"}, retries=1))
            ser = _quarterize(j.get("units", {}).get("USD", []))
            if len(ser) >= 6: return ser
            last = last or RuntimeError(f"{symbol}/{tag} 季度点不足")
        except Exception as e: last = e
    raise last or RuntimeError(f"{symbol} 无可用标签")

# ---------- 辅助 ----------
def put(id, value, label, status, detail, series=None, unit="", asof=""):
    IND[id] = {"value": value, "label": label, "status": status, "detail": detail,
               "series": series or [], "unit": unit, "asof": asof, "auto": True}
def tail(series, n=48): return [round(v, 4) for _, v in series[-n:]]
def pct(a, b): return (a / b - 1.0) * 100.0 if b else None

# ================= 各维度 =================
def build_fred_us():
    # 火柴 M3 · SOFR − IORB 利差（回购市场即时报警器）—— >0 即吃紧，≥5bp 警报
    try:
        s, io = fred("SOFR"), fred("IORB")
        ds, vs = s[-1]; iv = io[-1][1]; sp = (vs - iv) * 100
        st = "alert" if sp >= 5 else ("watch" if sp >= 0 else "normal")
        put("sofr", round(sp, 1), f"SOFR−IORB {sp:+.1f}bp", st,
            f"SOFR {vs:.2f}% vs IORB {iv:.2f}%；持续 >0 = 回购市场紧张", tail(s), "bp", ds)
    except Exception as e: err("FRED SOFR/IORB", e)

    # 火柴 M3 · ON RRP（缓冲垫）—— <2500亿→观察，<500亿→警报（趋近枯竭）
    try:
        r = fred("RRPONTSYD"); d, v = r[-1]
        st = "alert" if v < 50 else ("watch" if v < 250 else "normal")
        put("rrp", round(v, 1), f"ON RRP ${v:,.0f}B", st,
            f"隔夜逆回购 ${v:,.0f}B；趋近枯竭=再抽就抽到银行准备金本身", tail(r), "B$", d)
    except Exception as e: err("FRED RRP", e)

    # 火柴 M3 · 银行准备金 —— <3000B观察，<2700B警报（接近“仅够用”红线，启发式）
    try:
        r = fred("WRESBAL"); d, v = r[-1]
        st = "alert" if v < 2700 else ("watch" if v < 3000 else "normal")
        put("reserves", round(v), f"准备金 ${v:,.0f}B", st,
            f"银行准备金 ${v:,.0f}B；美联储称已到“仅够用”附近", tail(r), "B$", d)
    except Exception as e: err("FRED 准备金", e)

    # 火柴 M1 · 资产负债表 WALCL —— 近13周缩表且加速→观察
    try:
        w = fred("WALCL"); d, v = w[-1]
        at = lambda k: w[-1 - k][1] if len(w) > k else None
        c13 = (v - at(13)) if at(13) is not None else None
        p13 = (at(13) - at(26)) if at(26) is not None else None
        accel = c13 is not None and c13 < 0 and p13 is not None and c13 < p13
        fast = c13 is not None and c13 < 0 and at(4) is not None and (v - at(4)) < -50000
        st = "watch" if (accel or fast) else "normal"
        tn = v / 1e6
        det = "资产负债表 ${:.2f}T".format(tn)
        if c13 is not None: det += "；近13周{:+.2f}T".format(c13 / 1e6) + ("（缩表加速）" if accel else "")
        put("qt_balance", round(tn, 3), f"表 ${tn:.2f}T", st, det,
            [round(x / 1e6, 3) for _, x in w[-48:]], "T$", d)
    except Exception as e: err("FRED WALCL", e)

    # 火柴 M2 · 核心 PCE 同比 —— ≥3%观察，≥3.5%且回升→警报（降息无望→纯收紧）
    try:
        p = fred("PCEPILFE"); d, v = p[-1]
        yoy = pct(v, p[-13][1]) if len(p) >= 13 else None
        yoy3 = pct(p[-4][1], p[-16][1]) if len(p) >= 16 else None
        rising = yoy is not None and yoy3 is not None and yoy > yoy3
        st = "normal"
        if yoy is not None:
            if yoy >= 3.5 and rising: st = "alert"
            elif yoy >= 3.0: st = "watch"
        ser = [round(pct(p[i][1], p[i - 12][1]), 2) for i in range(max(12, len(p) - 36), len(p))]
        put("cpi", round(yoy, 2) if yoy is not None else None,
            (f"核心PCE {yoy:.1f}%" if yoy is not None else "核心PCE —"), st,
            (f"核心PCE 同比 {yoy:.2f}%{'（回升中）' if rising else ''}；>3%且回升=卡住降息那条腿" if yoy is not None else "无数据"),
            ser, "%", d)
    except Exception as e: err("FRED 核心PCE", e)

    # 火柴 M2 · 油价（Yahoo CL=F 优先，FRED 兜底）
    o = None
    try: o = yahoo("CL=F")
    except Exception as e: err("Yahoo CL=F", e)
    if not o:
        try: o = fred("DCOILWTICO")
        except Exception as e: err("FRED WTI", e)
    if o:
        try:
            d, v = o[-1]; v3 = o[-63][1] if len(o) >= 63 else o[0][1]
            ch = pct(v, v3); rising = ch is not None and ch > 0
            st = "alert" if (v >= 90 and rising) else ("watch" if (v >= 80 and rising) else "normal")
            put("oil", round(v, 1), f"WTI ${v:.0f}", st,
                f"WTI 原油 ${v:.1f}，近3月{ch:+.0f}%；油价是卡住降息的最直接变量", tail(o), "$", d)
        except Exception as e: err("油价计算", e)

    # 火柴 M2 · 失业率（萨姆规则）—— 近3月均值较12月低点 +0.3观察 / +0.5警报
    try:
        u = fred("UNRATE"); d, v = u[-1]
        a3 = sum(x for _, x in u[-3:]) / 3
        gap = a3 - min(x for _, x in u[-12:])
        st = "alert" if gap >= 0.5 else ("watch" if gap >= 0.3 else "normal")
        put("jobs", round(v, 1), f"失业率 {v:.1f}%", st,
            f"失业率 {v:.1f}%；近3月均值较12月低点 +{gap:.1f}（≥0.5触发萨姆规则）。就业弱+通胀高=两难", tail(u), "%", d)
    except Exception as e: err("FRED 失业率", e)

    # 美股传导 · 货币基金总资产（流出=资金真被抽离）
    try:
        m = fred("MMMFFAQ027S"); d, v = m[-1]; ch = v - m[-2][1]
        st = "watch" if ch < 0 else "normal"
        put("mmf", round(v / 1e6, 2), f"MMF ${v / 1e6:.2f}T", st,
            f"货币基金总资产 ${v / 1e6:.2f}T，环比{'-' if ch < 0 else '+'}${abs(ch) / 1e6:.2f}T；流出才说明资金被抽离（季度数据，滞后）",
            [round(x / 1e6, 2) for _, x in m[-24:]], "T$", d)
    except Exception as e: err("FRED MMF", e)

    # 美股传导 · 高收益债利差（走阔=借贷成本升，压 AI 债务基建）
    try:
        h = fred("BAMLH0A0HYM2"); d, v = h[-1]
        v1 = h[-22][1] if len(h) >= 22 else h[0][1]; rising = v > v1
        st = "alert" if v >= 6 else ("watch" if (v >= 4.5 or (rising and v >= 4)) else "normal")
        put("aicost", round(v, 2), f"HY利差 {v:.2f}%", st,
            f"高收益债利差 {v:.2f}%{'（走阔）' if rising else ''}；走阔=压依赖发债的 AI 基建", tail(h), "%", d)
    except Exception as e: err("FRED HY利差", e)

def build_cape():
    # 炸药 D1 · CAPE（海拔背景，best-effort 抓 multpl）
    try:
        html = http("https://www.multpl.com/shiller-pe")
        m = re.search(r'Current[^0-9]{0,40}?([0-9]{2}\.[0-9]+)', html, re.I) or re.search(r'([0-9]{2}\.[0-9]+)', html)
        v = float(m.group(1))
        st = "watch" if v >= 35 else "normal"   # 仅背景，权重低；高也只提示
        put("cape", round(v, 1), f"CAPE {v:.1f}", st,
            f"Shiller PE {v:.1f}（海拔表·背景，不择时；>30偏贵 >35极端）", [], "x", "")
    except Exception as e: err("multpl CAPE", e)

def build_buffett():
    # 炸药 D1 · 巴菲特指标（市值/GDP 代理：企业股权负债项/GDP）——背景海拔表，不择时
    try:
        eq = fred("NCBEILQ027S"); gdp = fred("GDP")  # eq 百万美元、GDP 十亿美元，均为季度
        v = eq[-1][1] / 1000.0 / gdp[-1][1] * 100.0
        st = "watch" if v >= 150 else "normal"
        gd = dict(gdp); ser = [round(x / 1000.0 / gd[d] * 100.0, 1) for d, x in eq[-24:] if gd.get(d)]
        put("buffett", round(v), f"市值/GDP {v:.0f}%", st,
            f"巴菲特指标 {v:.0f}%（历史均值~85%，>150% 极端海拔；背景指标，不择时）", ser, "%", eq[-1][0])
    except Exception as e: err("FRED 巴菲特指标", e)

def build_ai_fundamentals():
    # 炸药 D2 · 增速的二阶导（SEC EDGAR 财报）——QoQ 增速及其变化，最有价值的早期信号
    def grade(g):
        (d0, g0), (_, g1), (_, g2) = g[-1], g[-2], g[-3]
        falling1 = g0 < g1; falling2 = falling1 and g1 < g2
        st = "alert" if falling2 else ("watch" if falling1 else "normal")
        note = "，连续两季回落" if falling2 else ("，单季回落" if falling1 else "")
        return d0, g0, g1, st, note
    try:  # NVDA 营收：AI 链最干净的收入代理
        ser = edgar_quarters("NVDA", REV_TAGS)
        g = [(ser[i][0], (ser[i][1] / ser[i - 1][1] - 1) * 100) for i in range(1, len(ser)) if ser[i - 1][1]]
        if len(g) < 3: raise RuntimeError("增速点不足")
        d0, g0, g1, st, note = grade(g)
        put("rev2d", round(g0, 1), f"NVDA营收QoQ {g0:+.1f}%", st,
            f"NVDA 季度营收环比 {g1:+.1f}%→{g0:+.1f}%（加速度 {g0 - g1:+.1f}pp{note}）；市场按加速度定价，加速度转负早于价格见顶",
            [round(x, 1) for _, x in g[-16:]], "%", d0)
    except Exception as e: err("EDGAR NVDA营收", e)
    try:  # 四巨头 capex 合计（上游裂缝早于股价）
        per = {}
        for s in ("MSFT", "GOOGL", "AMZN", "META"):
            try: per[s] = dict(edgar_quarters(s, CAPEX_TAGS))
            except Exception as e: err(f"EDGAR {s} capex", e)
        if len(per) < 3: raise RuntimeError(f"可用公司不足({len(per)}/4)")
        common = sorted(set.intersection(*[set(d) for d in per.values()]))
        tot = [(k, sum(d[k] for d in per.values())) for k in common]
        g = [(tot[i][0], (tot[i][1] / tot[i - 1][1] - 1) * 100) for i in range(1, len(tot)) if tot[i - 1][1]]
        if len(g) < 3: raise RuntimeError("增速点不足")
        d0, g0, g1, st, note = grade(g)
        put("capex2d", round(g0, 1), f"巨头capex QoQ {g0:+.1f}%", st,
            f"{'+'.join(sorted(per))} 合计资本开支环比 {g1:+.1f}%→{g0:+.1f}%（加速度 {g0 - g1:+.1f}pp{note}；未季调，留意季节性）",
            [round(x, 1) for _, x in g[-16:]], "%", d0)
    except Exception as e: err("EDGAR capex合计", e)

def build_hk():
    # 中港 · 香港银行体系总结余 + 隔夜 HIBOR（联汇下的流动性总开关）
    try:
        recs = hkma("daily-figures-interbank-liquidity")
        rec = recs[0]
        bk = (next((k for k in rec if "aggr" in k.lower() and "bal" in k.lower()), None)
              or next((k for k in rec if "balance" in k.lower()), None))
        if not bk: raise RuntimeError(f"找不到总结余字段，可用键: {list(rec)[:8]}")
        to_b = lambda x: float(x) / 1000.0  # 港币百万 → 亿
        ser = [to_b(r[bk]) for r in recs if r.get(bk) not in (None, "")][::-1]
        bal = to_b(rec[bk]); d = rec.get("end_of_date", "")
        hib = None
        try:
            hr = hkma("hk-interbank-interest-rates")[0]
            ok = next((k for k in hr if "overnight" in k.lower()), None)
            hib = float(hr[ok]) if ok and hr.get(ok) not in (None, "") else None
        except Exception as e: err("HKMA HIBOR", e)
        st = "watch" if bal < 500 else "normal"   # 总结余<500亿港元偏紧（启发式）
        put("hk_liq", round(bal, 1), f"总结余 {bal:.0f}亿", st,
            f"香港银行体系总结余 {bal:.0f}亿港元" + (f"；隔夜HIBOR {hib:.2f}%" if hib is not None else ""),
            [round(x, 1) for x in ser[-48:]], "亿HKD", d)
    except Exception as e: err("HKMA 总结余", e)

def build_cn():
    if not TOKEN:
        err("Tushare", "未提供 token，跳过中港线"); return
    # 中港 · 南向资金（港股最直接的资金面温度计）
    # 注意：moneyflow_hsgt 需要日期参数；south_money 是“累计净买入(亿元)”，
    # 日净流入 = 逐日差分；近N日净流入 = 累计值的窗口差。
    try:
        end = date.today(); start = end - timedelta(days=100)
        rows = [r for r in tushare("moneyflow_hsgt",
                    {"start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")},
                    "trade_date,south_money") if r.get("south_money") not in (None, "")]
        rows.sort(key=lambda r: r["trade_date"])
        cum = [float(r["south_money"]) for r in rows]            # 累计净买入, 亿元
        daily = [round(cum[i] - cum[i - 1], 1) for i in range(1, len(cum))]  # 日净流入, 亿元
        net5 = round(cum[-1] - cum[-6], 1) if len(cum) >= 6 else round(cum[-1] - cum[0], 1)
        net20 = round(cum[-1] - cum[-21], 1) if len(cum) >= 21 else round(cum[-1] - cum[0], 1)
        d = rows[-1]["trade_date"]
        st = "alert" if (net20 < 0 and net5 < 0) else ("watch" if net5 < 0 else "normal")
        put("southbound", net5, f"南向5日 {net5:+.0f}亿", st,
            f"南向资金近5日净{'流出' if net5 < 0 else '流入'} {abs(net5):.0f}亿元，20日累计 {net20:+.0f}亿（{d}）；持续净流出=港股资金面警报",
            daily[-48:], "亿元", f"{d[:4]}-{d[4:6]}-{d[6:]}")
    except Exception as e: err("Tushare 南向(moneyflow_hsgt)", e)
    # 中国宏观背景（不设状态卡，作为 CN 区上下文）
    try:
        c = [r for r in tushare("cn_cpi", {}, "month,nt_yoy") if r.get("nt_yoy") is not None]
        c.sort(key=lambda r: r["month"])
        CONTEXT["cn_cpi"] = f"中国CPI {c[-1]['nt_yoy']}% YoY ({c[-1]['month']})"
    except Exception as e: err("Tushare cn_cpi", e)
    try:
        m = [r for r in tushare("cn_m", {}, "month,m2_yoy") if r.get("m2_yoy") is not None]
        m.sort(key=lambda r: r["month"])
        CONTEXT["cn_m2"] = f"中国M2 {m[-1]['m2_yoy']}% YoY ({m[-1]['month']})"
    except Exception as e: err("Tushare cn_m", e)

    # 中港 · A股融资融券余额（借来的钱接盘 → 杠杆/烈度，本地可取）
    try:
        end = date.today(); start = end - timedelta(days=90)
        rows = tushare("margin", {"start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")}, "trade_date,exchange_id,rzrqye")
        byd = {}
        for r in rows:
            v = r.get("rzrqye")
            if v in (None, ""): continue
            byd[r["trade_date"]] = byd.get(r["trade_date"], 0.0) + float(v)  # 各交易所汇总
        days = sorted(byd); tot = [byd[d] / 1e12 for d in days]  # 万亿元
        latest = tot[-1]; d = days[-1]
        chg20 = pct(latest, tot[-21]) if len(tot) >= 21 else pct(latest, tot[0])
        near_high = latest >= 0.995 * max(tot)
        st = "watch" if (near_high and (chg20 or 0) > 0) else "normal"
        put("cn_margin", round(latest, 2), f"两融 {latest:.2f}万亿", st,
            f"A股融资融券余额 {latest:.2f}万亿，20日{chg20:+.1f}%{'，创区间新高' if near_high else ''}；借来的钱接盘=破灭时连锁清算",
            [round(x, 3) for x in tot[-48:]], "万亿", f"{d[:4]}-{d[4:6]}-{d[6:]}")
    except Exception as e: err("Tushare 两融(margin)", e)
    # 中国宏观/IPO 背景（context）
    try:
        p = [r for r in tushare("cn_pmi", {}, "month,pmi010000") if r.get("pmi010000") is not None]
        p.sort(key=lambda r: r["month"]); v = p[-1]["pmi010000"]
        CONTEXT["cn_pmi"] = f"制造业PMI {v}（{'扩张' if v >= 50 else '收缩'}，{p[-1]['month']}）"
    except Exception as e: err("Tushare cn_pmi", e)
    try:
        s = [r for r in tushare("sf_month", {}, "month,inc_month") if r.get("inc_month") is not None]
        s.sort(key=lambda r: r["month"])
        CONTEXT["cn_sf"] = f"新增社融 {float(s[-1]['inc_month']) / 10000:.2f}万亿（{s[-1]['month']}）"
    except Exception as e: err("Tushare sf_month", e)
    try:
        end = date.today(); start = end - timedelta(days=30)
        ipo = [r for r in tushare("new_share", {"start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")}, "ts_code,ipo_date,amount,price") if r.get("ipo_date")]
        raised = sum(float(r.get("amount") or 0) * float(r.get("price") or 0) / 10000.0 for r in ipo)  # 万股×元/1e4=亿元
        CONTEXT["cn_ipo"] = f"近30日A股IPO {len(ipo)}家、募资约{raised:.0f}亿（A股抽水参考）"
    except Exception as e: err("Tushare new_share", e)

def build_ai_context():
    # 炸药 D2 上下文 · AI 龙头价格动能（仅参考，营收/capex 二阶导仍需手动判断）
    chs = []
    for s in ("NVDA", "MSFT", "GOOGL", "AMZN", "META"):
        try:
            y = yahoo(s, "3mo"); chs.append((s, pct(y[-1][1], y[0][1])))
        except Exception as e: err(f"Yahoo {s}", e)
    if chs:
        CONTEXT["ai_momentum"] = "AI龙头近3月: " + " · ".join(f"{s} {c:+.0f}%" for s, c in chs) + "（仅价格动能）"
    try:
        ix = yahoo("^IXIC", "3mo"); CONTEXT["ndx"] = f"纳指 {ix[-1][1]:,.0f}（3月 {pct(ix[-1][1], ix[0][1]):+.0f}%）"
    except Exception as e: err("Yahoo 纳指", e)
    try:
        vx = yahoo("^VIX", "1mo"); CONTEXT["vix"] = f"VIX {vx[-1][1]:.1f}"
    except Exception as e: err("Yahoo VIX", e)

def write_history():
    """把本次自动状态合并进 history.json（前端用它画“增还是减”趋势与象限轨迹）。
    同日多次运行做并集（本地 Tushare 跑 + 云端全量跑互不覆盖），保留最近 400 天。"""
    p = os.path.join(ROOT, "history.json")
    try:
        hist = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []
        if not isinstance(hist, list): hist = []
    except Exception: hist = []
    today = date.today().isoformat()
    snap = {k: v.get("status") for k, v in IND.items() if v.get("status")}
    cur = next((h for h in hist if isinstance(h, dict) and h.get("d") == today), None)
    if cur: cur.setdefault("s", {}).update(snap)
    else: hist.append({"d": today, "s": snap})
    hist = [h for h in hist if isinstance(h, dict) and h.get("d")][-400:]
    json.dump(hist, open(p, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    return len(hist)

def main():
    print("拉数中…")
    build_fred_us(); build_cape(); build_buffett(); build_hk(); build_cn(); build_ai_context()
    build_ai_fundamentals()
    print(f"history.json: {write_history()} 天")
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "indicators": IND, "context": CONTEXT, "errors": ERRORS}
    with open(os.path.join(ROOT, "data.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"完成：{len(IND)} 指标 / {len(ERRORS)} 错误 → data.json")
    for k, v in IND.items():
        print(f"  {k:12s} {v['status']:6s} {v['label']}")
    for e in ERRORS: print("  错误:", e)

if __name__ == "__main__":
    main()
