#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
康波周期月度研判报告 — 云端自动生成与邮件发送
==============================================
设计目标：可在 GitHub Actions / 任意云端环境运行，不依赖本地环境。

数据源（全部公开、免鉴权、云端可访问）：
  1. 腾讯行情    : https://qt.gtimg.cn/q=...            (GBK)
  2. 腾讯K线     : https://web.ifzq.gtimg.cn/appstock/app/fqkline/get (UTF-8)
  3. 新浪伦敦金  : https://hq.sinajs.cn/list=hf_XAU     (GBK, 需 Referer)
  4. 东方财富    : https://push2.eastmoney.com/api/qt/stock/get?secid=100.UDI (DXY美元指数)

环境变量：
  SMTP_USER       发件邮箱 (默认 378261712@qq.com)
  SMTP_AUTH_CODE  QQ邮箱 SMTP 授权码 (必填)
  MAIL_TO         收件人 (默认 378261712@qq.com)

用法：
  python kondratiev_cloud_report.py            # 生成报告并发送邮件
  python kondratiev_cloud_report.py --dry-run  # 仅生成 HTML，不发送
  python kondratiev_cloud_report.py --json     # 仅输出结构化数据 JSON
"""

import calendar
import datetime as dt
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.request
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

# ---------------------------------------------------------------------------
# 常量配置
# ---------------------------------------------------------------------------
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
TENCENT_KLINE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                     "?param={code},day,,,{days},qfq")
SINA_XAU_URL = "https://hq.sinajs.cn/list=hf_XAU"
EM_DXY_URLS = [
    ("https://push2.eastmoney.com/api/qt/stock/get"
     "?secid=100.UDI&fields=f43,f44,f45,f46,f57,f58,f60,f86,f169,f170"),
    ("https://push2delay.eastmoney.com/api/qt/stock/get"
     "?secid=100.UDI&fields=f43,f44,f45,f46,f57,f58,f60,f86,f169,f170"),
]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# 需要抓取的行情代码（腾讯格式）
QUOTE_CODES = [
    # 黄金
    "fuGC",            # COMEX黄金期货
    "spAU9999",        # 上海金AU9999
    "sh518880",        # 华安黄金ETF
    "sz159934",        # 易方达黄金ETF
    # A股
    "sh600519",        # 贵州茅台
    "sz000001",        # 平安银行
    "sz300750",        # 宁德时代
    "sh688981",        # 中芯国际
    # 港股
    "hk00700",         # 腾讯控股
    "hk09988",         # 阿里巴巴
    # 美股
    "usAAPL", "usNVDA", "usMSFT", "usGOOGL", "usAMZN",
    # 指数
    "sh000001",        # 上证指数
    "sh000300",        # 沪深300
    "sh000016",        # 上证50
    "hkHSI",           # 恒生指数
    "us.INX",          # 标普500
    "us.IXIC",         # 纳斯达克
    # 汇率
    "fxUSDCNY", "fxEURUSD", "fxUSDJPY",
]

KLINE_CODES = {
    "sh_index": ("sh000001", 40),   # 上证指数（腾讯）
}

# 新浪全球期货日K线（COMEX黄金，2016至今）
SINA_GC_KLINE_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php"
                     "/var%20_GC=/GlobalFuturesService."
                     "getGlobalFuturesDailyKLine?symbol=GC")

MAX_RETRY = 3


# ---------------------------------------------------------------------------
# 网络请求（带重试）
# ---------------------------------------------------------------------------
def http_get(url, retries=MAX_RETRY, referer=None, timeout=15):
    headers = dict(UA)
    if referer:
        headers["Referer"] = referer
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"请求失败 {url}: {last_err}")


# ---------------------------------------------------------------------------
# 腾讯行情解析
# ---------------------------------------------------------------------------
def fetch_tencent_quotes(codes):
    """批量获取腾讯实时行情。返回 {code: {...}}"""
    url = TENCENT_QUOTE_URL.format(codes=",".join(codes))
    raw = http_get(url).decode("gbk", errors="replace")
    out = {}
    for m in re.finditer(r'v_([^=]+)="([^"]*)"', raw):
        code, payload = m.group(1), m.group(2)
        fields = payload.split("~")
        if len(fields) < 5:
            continue
        out[code] = parse_tencent(code, fields)
    return out


def parse_tencent(code, f):
    """按市场类型解析腾讯行情字段。"""
    mtype = f[0]
    name = f[1] if len(f) > 1 else code
    price = float(f[3]) if len(f) > 3 and _num(f[3]) else None
    prev = float(f[4]) if len(f) > 4 and _num(f[4]) else None
    chg_pct = None

    if mtype in ("1", "51", "100"):          # A股 / ETF / 港股 / 指数
        t = f[30] if len(f) > 30 else ""
        chg_pct = float(f[32]) if len(f) > 32 and _num(f[32]) else None
    elif mtype == "200":                     # 美股（字段与A股一致）
        t = f[30] if len(f) > 30 else ""
        chg_pct = float(f[32]) if len(f) > 32 and _num(f[32]) else None
    elif mtype == "delay":                   # 外盘期货（字段与A股一致）
        t = f[30] if len(f) > 30 else ""
        chg_pct = float(f[32]) if len(f) > 32 and _num(f[32]) else None
    elif mtype == "320":                     # 上海金/国内期货（字段与A股一致）
        t = f[30] if len(f) > 30 else ""
        chg_pct = float(f[32]) if len(f) > 32 and _num(f[32]) else None
    elif mtype == "310":                     # 外汇
        t = f[5] if len(f) > 5 else ""
        chg_pct = float(f[13]) if len(f) > 13 and _num(f[13]) else None
        prev = float(f[6]) if len(f) > 6 and _num(f[6]) else None
    else:
        t = ""

    # 兜底：用 现价/昨收 计算涨跌幅
    if chg_pct is None and price and prev:
        chg_pct = round((price - prev) / prev * 100, 2)

    return {
        "code": code, "name": name, "price": price, "prev": prev,
        "chg_pct": chg_pct, "time": t,
    }


def _num(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def fetch_tencent_kline(code, days):
    """获取日K线，返回 [[date, open, close, high, low, volume], ...]"""
    url = TENCENT_KLINE_URL.format(code=code, days=days)
    raw = http_get(url).decode("utf-8", errors="replace")
    data = json.loads(raw).get("data", {})
    node = data.get(code, {})
    klines = node.get("day") or node.get("qfqday") or []
    rows = []
    for k in klines:
        if len(k) >= 6:
            rows.append([k[0], float(k[1]), float(k[2]),
                         float(k[3]), float(k[4]), float(k[5])])
    return rows


def fetch_sina_gc_kline():
    """新浪全球期货：COMEX黄金全量日K线（2016至今）
    返回 [[date, open, close, high, low, volume], ...]"""
    raw = http_get(SINA_GC_KLINE_URL, referer="https://finance.sina.com.cn",
                   timeout=25)
    text = raw.decode("utf-8", errors="replace")
    m = re.search(r"\((\[.*\])\)", text, re.S)
    if not m:
        raise RuntimeError("新浪GC K线解析失败")
    rows = json.loads(m.group(1))
    out = []
    for k in rows:
        try:
            out.append([k["date"], float(k["open"]), float(k["close"]),
                        float(k["high"]), float(k["low"]),
                        float(k.get("volume") or 0)])
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# 新浪伦敦金现货（XAU）
# ---------------------------------------------------------------------------
def fetch_sina_xau():
    raw = http_get(SINA_XAU_URL, referer="https://finance.sina.com.cn")
    text = raw.decode("gbk", errors="replace")
    m = re.search(r'hf_XAU="([^"]*)"', text)
    if not m:
        raise RuntimeError("新浪 XAU 返回为空")
    f = m.group(1).split(",")
    if len(f) < 8:
        raise RuntimeError(f"新浪 XAU 字段不足: {m.group(1)}")
    price = float(f[0])
    prev = float(f[7]) if f[7] else None
    high = float(f[4]) if f[4] else None
    low = float(f[5]) if f[5] else None
    chg = round((price - prev) / prev * 100, 2) if prev else None
    return {
        "name": "伦敦金现货", "price": price, "prev": prev,
        "high": high, "low": low, "chg_pct": chg,
        "time": f"{f[12]} {f[6]}", "source": "sina",
    }


# ---------------------------------------------------------------------------
# 东方财富美元指数（DXY）
# ---------------------------------------------------------------------------
def fetch_eastmoney_dxy():
    last_err = None
    for url in EM_DXY_URLS:
        try:
            raw = http_get(url, retries=2)
            j = json.loads(raw.decode("utf-8", errors="replace"))
            d = j.get("data")
            if not d:
                raise RuntimeError("东财 DXY 返回为空")
            # f43 最新价(×100)  f60 昨收(×100)  f170 涨跌幅(×100)  f58 名称
            price = d.get("f43") / 100 if d.get("f43") is not None else None
            prev = d.get("f60") / 100 if d.get("f60") is not None else None
            chg = d.get("f170") / 100 if d.get("f170") is not None else None
            ts = d.get("f86")
            t = (dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                 if ts else "")
            return {
                "name": "美元指数(DXY)", "price": price, "prev": prev,
                "chg_pct": chg, "time": t, "source": "eastmoney",
            }
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"东财 DXY 全部接口失败: {last_err}")


# ---------------------------------------------------------------------------
# 康波规则分析引擎
# ---------------------------------------------------------------------------
def analyze(data):
    """基于实时数据 + 康波框架规则，生成报告核心结论。"""
    now = dt.datetime.now()
    month_cn = f"{now.year}年{now.month}月"

    gold_fut = data["quotes"].get("fuGC", {})
    gold_sh = data["quotes"].get("spAU9999", {})
    xau = data.get("xau", {})
    dxy = data.get("dxy", {})
    gold_k = data["klines"].get("gold", [])
    gold_1y = data["klines"].get("gold_1y", [])
    sh_k = data["klines"].get("sh_index", [])

    # ---- 黄金趋势 ----
    gold_now = xau.get("price") or gold_fut.get("price")
    gold_chg_d = xau.get("chg_pct") or gold_fut.get("chg_pct")
    gold_30d = _trend_pct(gold_k, 30)
    gold_20d = _trend_pct(gold_k, 20)
    gold_1y_pct = _trend_pct(gold_1y, 250)

    # 30日高低点（关键价位）
    highs = [r[3] for r in gold_k if len(r) > 3]
    lows = [r[4] for r in gold_k if len(r) > 4]
    high_30 = max(highs) if highs else None
    low_30 = min(lows) if lows else None

    # ---- 美元趋势 ----
    dxy_chg = dxy.get("chg_pct")
    eurusd = data["quotes"].get("fxEURUSD", {})
    eurusd_chg = eurusd.get("chg_pct")
    usdcny = data["quotes"].get("fxUSDCNY", {})
    usdcny_chg = usdcny.get("chg_pct")

    # ---- 黄金三属性打分 (1-5) ----
    # 商品属性：黄金30日趋势
    if gold_30d is None:
        comm = 3
    elif gold_30d > 10:
        comm = 5
    elif gold_30d > 5:
        comm = 4
    elif gold_30d > 0:
        comm = 3
    elif gold_30d > -5:
        comm = 2
    else:
        comm = 1

    # 金融属性：美元强弱（当日）+ 黄金20日趋势
    dollar_bear = 0
    if dxy_chg is not None and dxy_chg < 0:
        dollar_bear += 1
    if eurusd_chg is not None and eurusd_chg > 0:
        dollar_bear += 1
    if usdcny_chg is not None and usdcny_chg < 0:
        dollar_bear += 1
    fin = 3 + dollar_bear - (1 if gold_20d is not None and gold_20d < 0 else 0)
    fin = max(1, min(5, fin))

    # 货币属性：黄金一年涨幅（去美元化定价强度）
    if gold_1y_pct is None:
        monet = 4
    elif gold_1y_pct > 50:
        monet = 5
    elif gold_1y_pct > 20:
        monet = 4
    elif gold_1y_pct > 0:
        monet = 3
    else:
        monet = 2

    attrs = {"commodity": comm, "financial": fin, "monetary": monet}
    # 主驱动判断：并列时优先货币属性（结构性主线）> 金融属性 > 商品属性
    if monet >= comm and monet >= fin:
        main_attr = "monetary"
    elif fin >= comm:
        main_attr = "financial"
    else:
        main_attr = "commodity"

    # ---- 综合评级 ----
    overall = round((comm * 0.3 + fin * 0.3 + monet * 0.4), 1)

    # ---- 情景判断 ----
    if overall >= 4:
        bias = "强势"
    elif overall >= 3:
        bias = "中性偏强"
    else:
        bias = "震荡"

    # ---- 四周期定位 ----
    cycles = [
        ("康德拉季耶夫(50-60年)", "第五波萧条末→第六波回升初", "底部转折期"),
        ("库兹涅茨(15-25年)", "全球地产周期调整尾声", "出清接近完成"),
        ("朱格拉(8-10年)", "设备投资周期新起点", "制造业资本开支回升"),
        ("基钦(3-4年)", "被动去库存→主动补库存", "企业盈利拐点临近"),
    ]

    # ---- 配置建议 ----
    gold_alloc = {"稳健": "12%", "均衡": "15%", "进取": "18%"}
    if overall >= 4.5:
        gold_alloc = {"稳健": "15%", "均衡": "18%", "进取": "20%"}
    elif overall <= 2.5:
        gold_alloc = {"稳健": "8%", "均衡": "10%", "进取": "12%"}

    alloc = [
        ("黄金/贵金属", gold_alloc, "三属性共振，康波转折期压舱石"),
        ("AI科技(美股+A股)", {"稳健": "15%", "均衡": "25%", "进取": "35%"},
         "第六波康波主导技术，应用爆发前夜"),
        ("新能源+生物科技", {"稳健": "5%", "均衡": "10%", "进取": "15%"},
         "第六波康波两翼，长期结构性需求"),
        ("新兴市场(印度/东南亚)", {"稳健": "5%", "均衡": "8%", "进取": "10%"},
         "康波共生模式受益者，制造业转移"),
        ("农产品/加密(卫星仓)", {"稳健": "0%", "均衡": "3%", "进取": "5%"},
         "高波动，严格止损，仅卫星仓位"),
        ("现金/短债", {"稳健": "45%", "均衡": "27%", "进取": "15%"},
         "保留弹药，等待右侧确认"),
        ("债券/固收", {"稳健": "10%", "均衡": "5%", "进取": "5%"},
         "回升期吸引力下降，仅短久期"),
        ("房地产/REITs", {"稳健": "5%", "均衡": "10%", "进取": "5%"},
         "库兹涅茨下行段，仅数据中心REITs"),
    ]

    # ---- 本月关键事件 ----
    events = build_events(now)

    # ---- 核心结论摘要 ----
    summary_lines = [
        f"{month_cn}，黄金处于回调后的修复上行阶段：现货伦敦金 ${gold_now:,.0f}"
        if gold_now else f"{month_cn}，黄金行情数据暂缺。",
    ]
    if dxy.get("price"):
        summary_lines.append(
            f"美元指数 {dxy['price']:.2f}"
            + (f"（当日{'走强' if (dxy_chg or 0) > 0 else '走弱'} {dxy_chg:+.2f}%）"
               if dxy_chg is not None else ""))
    if gold_1y_pct is not None:
        summary_lines.append(
            f"黄金过去一年上涨 {gold_1y_pct:+.1f}%，货币属性(去美元化)仍是定价主线。")
    if gold_30d is not None:
        summary_lines.append(
            f"近30日黄金{'上涨' if gold_30d >= 0 else '回调'} {gold_30d:+.1f}%，"
            f"三属性综合评级 {overall}/5，{bias}。")
    summary_lines.append(
        "康波框架定位：第五波萧条末→第六波回升初，四周期罕见共振，"
        "为战略建仓窗口期。策略核心：黄金保底 + AI进攻，逢回调分批布局。")

    report = {
        "month": month_cn,
        "summary": summary_lines,
        "quotes": data["quotes"],
        "xau": xau,
        "dxy": dxy,
        "gold": {
            "now": gold_now, "chg_day": gold_chg_d, "chg_30d": gold_30d,
            "chg_1y": gold_1y_pct, "high_30": high_30, "low_30": low_30,
            "sh_price": gold_sh.get("price"),
            "sh_chg": gold_sh.get("chg_pct"),
            "etf_ha": data["quotes"].get("sh518880"),
            "etf_yf": data["quotes"].get("sz159934"),
        },
        "dollar": {
            "dxy": dxy, "eurusd": eurusd, "usdcny": usdcny,
        },
        "attributes": attrs,
        "main_attribute": main_attr,
        "overall_score": overall,
        "bias": bias,
        "cycles": cycles,
        "allocation": alloc,
        "events": events,
        "index_trend": _trend_pct(sh_k, 20),
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
    }
    return report


def _trend_pct(klines, days):
    """最近 days 日涨跌幅（取倒数第 days+1 根收盘 vs 最新收盘）"""
    if not klines:
        return None
    recent = klines[-days:] if len(klines) >= days else klines
    if len(recent) < 2:
        return None
    start = recent[0][2]
    end = recent[-1][2]
    if not start:
        return None
    return round((end - start) / start * 100, 1)


def build_events(now):
    """生成当月关注事件（基于日历规则）"""
    y, m = now.year, now.month
    _, last_day = calendar.monthrange(y, m)
    ev = []

    # 美联储议息会议（每年约8次：1/3/5/6/7/9/10/12月）
    fed_months = {1, 3, 5, 6, 7, 9, 10, 12}
    if m in fed_months:
        ev.append(("美联储议息会议", "月中前后", "利率路径直接影响美元与黄金金融属性"))
    else:
        ev.append(("美联储议息会议(休会月)", "—", "关注官员讲话与点阵图预期变化"))

    # 常规经济数据
    ev.append(("美国非农就业", "第一个周五", "就业强弱决定加息/降息预期"))
    ev.append(("美国CPI", "月中", "通胀粘性是金融属性关键变量"))
    ev.append(("央行购金数据", "月初", "中国/印度/俄罗斯央行是否继续增持黄金"))
    ev.append(("中国PMI", "月末", "制造业景气度，验证库存周期回升"))

    # 月度季节性
    if m == 8:
        ev.append(("Jackson Hole全球央行年会", "8月下旬", "全球央行政策风向标，黄金重要节点"))
    if m == 9:
        ev.append(("美联储9月议息+点阵图", "9月", "年内降息路径定调"))
    if m == 10:
        ev.append(("印度排灯节+婚礼季", "10-11月", "实物黄金需求季节性高峰"))
    if m == 12:
        ev.append(("年底流动性窗口", "12月", "机构调仓与避险需求，黄金季节性偏强"))
    if m == 1:
        ev.append(("中国春节需求高峰", "1-2月", "全球最大黄金消费国需求旺季"))
    if m in (3, 6, 9, 12):
        ev.append(("季末调仓窗口", "季末", "机构再平衡，注意短期波动"))

    return ev


# ---------------------------------------------------------------------------
# HTML 报告生成
# ---------------------------------------------------------------------------
def build_html(r):
    q = r["quotes"]
    xau = r["xau"]
    dxy = r["dollar"]["dxy"]
    g = r["gold"]

    def pct(v, sign=False):
        if v is None:
            return "--"
        s = f"{v:+.2f}%" if sign else f"{v:.2f}%"
        return s

    def updown(v):
        if v is None:
            return "#888"
        return "#e05252" if v >= 0 else "#2ecc71"  # 涨红跌绿(中国习惯)

    # ---- 数据速览表 ----
    rows = []
    def add_row(name, price, chg, unit=""):
        if price is None:
            rows.append(f"<tr><td>{name}</td><td>--</td><td style='color:#888'>--</td></tr>")
        else:
            fmt = f"{price:,.2f}" if isinstance(price, float) else str(price)
            rows.append(
                f"<tr><td>{name}</td><td>{fmt}{unit}</td>"
                f"<td style='color:{updown(chg)}'>{pct(chg, True)}</td></tr>")

    add_row("伦敦金现货 XAU", xau.get("price"), xau.get("chg_pct"), " $/oz")
    add_row("COMEX黄金期货", q.get("fuGC", {}).get("price"),
            q.get("fuGC", {}).get("chg_pct"), " $/oz")
    add_row("上海金 AU9999", g.get("sh_price"), g.get("sh_chg"), " ¥/g")
    add_row("黄金ETF华安 518880", g.get("etf_ha", {}).get("price"),
            g.get("etf_ha", {}).get("chg_pct"), " ¥")
    add_row("美元指数 DXY", dxy.get("price"), dxy.get("chg_pct"))
    add_row("美元/人民币", r["dollar"]["usdcny"].get("price"),
            r["dollar"]["usdcny"].get("chg_pct"))
    add_row("欧元/美元", r["dollar"]["eurusd"].get("price"),
            r["dollar"]["eurusd"].get("chg_pct"))
    add_row("上证指数", q.get("sh000001", {}).get("price"),
            q.get("sh000001", {}).get("chg_pct"))
    add_row("沪深300", q.get("sh000300", {}).get("price"),
            q.get("sh000300", {}).get("chg_pct"))
    add_row("恒生指数", q.get("hkHSI", {}).get("price"),
            q.get("hkHSI", {}).get("chg_pct"))
    add_row("标普500", q.get("us.INX", {}).get("price"),
            q.get("us.INX", {}).get("chg_pct"))
    add_row("纳斯达克", q.get("us.IXIC", {}).get("price"),
            q.get("us.IXIC", {}).get("chg_pct"))

    # ---- 三属性 ----
    a = r["attributes"]
    attr_rows = ""
    attr_desc = {
        "commodity": ("商品属性", "矿产供给见顶+需求支撑，商品周期底部"),
        "financial": ("金融属性", "实际利率与美元强弱，Fed政策路径"),
        "monetary": ("货币属性", "央行购金与去美元化，结构性主驱动"),
    }
    for key, (label, desc) in attr_desc.items():
        star = "★" * a[key] + "☆" * (5 - a[key])
        attr_rows += (
            f"<tr><td>{label}</td><td>{desc}</td>"
            f"<td style='color:#daa520'>{star} {a[key]}/5</td></tr>")

    # ---- 关键价位 ----
    levels = ""
    if g["now"]:
        levels += f"<tr><td>当前价</td><td>${g['now']:,.0f}</td></tr>"
    if g["high_30"]:
        levels += f"<tr><td>近30日高点(阻力参考)</td><td>${g['high_30']:,.0f}</td></tr>"
    if g["low_30"]:
        levels += f"<tr><td>近30日低点(支撑参考)</td><td>${g['low_30']:,.0f}</td></tr>"

    # ---- 四周期 ----
    cycle_rows = "".join(
        f"<tr><td>{name}</td><td>{pos}</td><td style='color:#14a085'>{sig}</td></tr>"
        for name, pos, sig in r["cycles"])

    # ---- 配置建议 ----
    alloc_rows = "".join(
        f"<tr><td>{name}</td>"
        f"<td>{w['稳健']}</td><td>{w['均衡']}</td><td>{w['进取']}</td>"
        f"<td style='color:#888;font-size:12px'>{why}</td></tr>"
        for name, w, why in r["allocation"])

    # ---- 事件 ----
    ev_rows = "".join(
        f"<tr><td>{name}</td><td>{t}</td><td style='color:#888;font-size:12px'>{d}</td></tr>"
        for name, t, d in r["events"])

    # ---- 摘要 ----
    summary_html = "".join(f"<p style='margin:8px 0;line-height:1.7'>{s}</p>"
                           for s in r["summary"])

    # ---- 美元 ----
    dx = r["dollar"]
    dx_line = (
        f"美元指数 {dxy.get('price') or '--'}"
        f"（当日 {pct(dxy.get('chg_pct'), True)}）"
        f"，欧元/美元 {dx['eurusd'].get('price') or '--'}"
        f"（{pct(dx['eurusd'].get('chg_pct'), True)}），"
        f"美元/人民币 {dx['usdcny'].get('price') or '--'}"
        f"（{pct(dx['usdcny'].get('chg_pct'), True)}）。"
    )

    # ---- 摘要核心结论 ----
    headline = (
        f"黄金{r['bias']}，评级 {r['overall_score']}/5"
        f" | 主驱动：{attr_desc[r['main_attribute']][0]}"
    )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:'Microsoft YaHei',Arial,sans-serif;background-color:#f0f2f5;color:#1a1a1a;margin:0;padding:20px;">
<table width="100%" cellpadding="0" cellspacing="0" bgcolor="#f0f2f5" style="background-color:#f0f2f5;"><tr><td align="center">
<table width="700" cellpadding="0" cellspacing="0" bgcolor="#ffffff" style="width:700px;max-width:100%;background-color:#ffffff;border:1px solid #e0e0e0;border-radius:12px;overflow:hidden;">

  <tr><td style="background-color:#a67c00;padding:22px 30px;">
    <h1 style="color:#ffffff;font-size:22px;margin:0;font-weight:bold;">【康波月报】{r['month']}康波周期研判</h1>
    <p style="color:#f7eccb;font-size:13px;margin:6px 0 0 0;">生成时间 {r['generated_at']} · 数据源：腾讯/新浪/东方财富 · 云端自动运行</p>
  </td></tr>

  <tr><td style="padding:30px;background-color:#ffffff;color:#1a1a1a;">

  <div style="background-color:#e8f3f0;border-radius:8px;padding:16px 20px;margin-bottom:22px;border-left:4px solid #14a085;">
    <p style="color:#0e7c6f;font-size:15px;font-weight:bold;margin:0 0 6px 0;">🎯 核心结论：{headline}</p>
    {summary_html}
  </div>

  <h2 style="color:#a67c00;font-size:17px;border-bottom:1px solid #e0e0e0;padding-bottom:8px;">一、关键市场数据速览</h2>
  <table style="width:100%;border-collapse:collapse;margin:12px 0 24px 0;font-size:13px;color:#333;">
    <tr style="background-color:#f5f5f5;">
      <th style="text-align:left;padding:8px;color:#333;">指标</th>
      <th style="text-align:right;padding:8px;color:#333;">最新值</th>
      <th style="text-align:right;padding:8px;color:#333;">涨跌幅</th>
    </tr>
    {''.join(rows)}
  </table>

  <h2 style="color:#a67c00;font-size:17px;border-bottom:1px solid #e0e0e0;padding-bottom:8px;">二、康波周期定位</h2>
  <p style="line-height:1.8;font-size:14px;color:#333;">
    当前处于<strong style="color:#0e7c6f">第五波康波萧条末 → 第六波回升初</strong>的历史性转折窗口。
    上一轮类似四周期共振底部出现在 1982-1983 年（随后开启第五波繁荣大牛市）。
    本次共振叠加 AI 主导技术革命，第六波康波（AI + 新能源 + 生物科技三驾马车）正进入回升早期。
    策略含义：<strong style="color:#a67c00">从防御逐步转向进攻，逢回调战略建仓。</strong>
  </p>
  <table style="width:100%;border-collapse:collapse;margin:12px 0 24px 0;font-size:13px;color:#333;">
    <tr style="background-color:#f5f5f5;">
      <th style="text-align:left;padding:8px;color:#333;">周期</th>
      <th style="text-align:left;padding:8px;color:#333;">当前位置</th>
      <th style="text-align:left;padding:8px;color:#333;">信号</th>
    </tr>
    {cycle_rows}
  </table>

  <h2 style="color:#a67c00;font-size:17px;border-bottom:1px solid #e0e0e0;padding-bottom:8px;">三、黄金专题：三属性定价模型</h2>
  <table style="width:100%;border-collapse:collapse;margin:12px 0;font-size:13px;color:#333;">
    <tr style="background-color:#f5f5f5;">
      <th style="text-align:left;padding:8px;color:#333;">属性</th>
      <th style="text-align:left;padding:8px;color:#333;">逻辑</th>
      <th style="text-align:right;padding:8px;color:#333;">强度</th>
    </tr>
    {attr_rows}
  </table>
  <p style="line-height:1.8;font-size:14px;color:#333;">
    当前黄金主驱动力为<strong style="color:#a67c00">{attr_desc[r['main_attribute']][0]}</strong>。
    过去一年涨幅 {pct(g['chg_1y'], True)}，近30日 {pct(g['chg_30d'], True)}，
    当日 {pct(g['chg_day'], True)}。
    美元端：{dx_line}
  </p>
  <table style="width:100%;border-collapse:collapse;margin:12px 0 24px 0;font-size:13px;color:#333;">
    <tr style="background-color:#f5f5f5;">
      <th style="text-align:left;padding:8px;color:#333;">关键价位</th>
      <th style="text-align:right;padding:8px;color:#333;">参考值</th>
    </tr>
    {levels}
  </table>

  <h2 style="color:#a67c00;font-size:17px;border-bottom:1px solid #e0e0e0;padding-bottom:8px;">四、四周期共振分析</h2>
  <p style="line-height:1.8;font-size:14px;color:#333;">
    四周期（康波/库兹涅茨/朱格拉/基钦）当前在 2025-2027 窗口形成<strong style="color:#0e7c6f">罕见共振底部</strong>。
    库存周期正从被动去库存转向主动补库存，企业盈利拐点临近；
    设备投资周期开启新起点，制造业资本开支有望回升；
    地产周期出清接近完成。共振底部 + 货币宽松预期，构成大类资产的战略配置窗口。
  </p>

  <h2 style="color:#a67c00;font-size:17px;border-bottom:1px solid #e0e0e0;padding-bottom:8px;">五、资产配置建议</h2>
  <table style="width:100%;border-collapse:collapse;margin:12px 0 24px 0;font-size:13px;color:#333;">
    <tr style="background-color:#f5f5f5;">
      <th style="text-align:left;padding:8px;color:#333;">资产</th>
      <th style="text-align:center;padding:8px;color:#333;">稳健型</th>
      <th style="text-align:center;padding:8px;color:#333;">均衡型</th>
      <th style="text-align:center;padding:8px;color:#333;">进取型</th>
      <th style="text-align:left;padding:8px;color:#333;">逻辑</th>
    </tr>
    {alloc_rows}
  </table>

  <h2 style="color:#a67c00;font-size:17px;border-bottom:1px solid #e0e0e0;padding-bottom:8px;">六、本月关注事件</h2>
  <table style="width:100%;border-collapse:collapse;margin:12px 0 24px 0;font-size:13px;color:#333;">
    <tr style="background-color:#f5f5f5;">
      <th style="text-align:left;padding:8px;color:#333;">事件</th>
      <th style="text-align:left;padding:8px;color:#333;">时间</th>
      <th style="text-align:left;padding:8px;color:#333;">影响</th>
    </tr>
    {ev_rows}
  </table>

  <h2 style="color:#a67c00;font-size:17px;border-bottom:1px solid #e0e0e0;padding-bottom:8px;">七、风险提示</h2>
  <ol style="font-size:13px;line-height:1.9;color:#444;padding-left:20px;">
    <li>AI 泡沫化风险：若应用商业化不及预期，科技股可能回调 20-30%，需分批建仓。</li>
    <li>通胀反复：若通胀反弹迫使央行转鹰，黄金与成长股同承压，保留 15% 以上现金。</li>
    <li>地缘冲突升级：中美科技脱钩与地区冲突可能冲击供应链，配置需多元化。</li>
    <li>流动性尾部风险：萧条末尾可能出现类似 2020 年 3 月的流动性危机，黄金+现金提供缓冲。</li>
    <li>数据延迟说明：COMEX 期货为延时行情，外盘数据以交易所官方为准。</li>
  </ol>

  <p style="color:#999;font-size:12px;margin-top:25px;border-top:1px solid #e0e0e0;padding-top:15px;">
    本报告由 WorkBuddy 康波周期云端自动化任务生成，仅供参考，不构成投资建议。投资有风险，决策需谨慎。
  </p>

  </td></tr>
</table>
</td></tr></table>
</body></html>"""
    return html


# ---------------------------------------------------------------------------
# 邮件发送
# ---------------------------------------------------------------------------
def send_email(subject, html_body, smtp_user, auth_code, mail_to):
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((str(Header("康波周期研究", "utf-8")), smtp_user))
    msg["To"] = mail_to
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.qq.com", 465, context=ctx, timeout=30) as srv:
        srv.login(smtp_user, auth_code)
        srv.sendmail(smtp_user, [mail_to], msg.as_string())
    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def collect_data():
    data = {"quotes": {}, "klines": {}, "xau": None, "dxy": None}

    # 行情
    try:
        data["quotes"] = fetch_tencent_quotes(QUOTE_CODES)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 腾讯行情获取失败: {e}", file=sys.stderr)

    # 黄金K线（新浪全量，取近30日/近1年）
    try:
        gc_all = fetch_sina_gc_kline()
        data["klines"]["gold"] = gc_all[-65:]
        data["klines"]["gold_1y"] = gc_all[-250:]
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 黄金K线获取失败: {e}", file=sys.stderr)
        data["klines"]["gold"] = []
        data["klines"]["gold_1y"] = []

    # 其他K线（腾讯）
    for key, (code, days) in KLINE_CODES.items():
        try:
            data["klines"][key] = fetch_tencent_kline(code, days)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] K线 {key} 获取失败: {e}", file=sys.stderr)
            data["klines"][key] = []

    # XAU
    try:
        data["xau"] = fetch_sina_xau()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] XAU 获取失败: {e}", file=sys.stderr)

    # DXY
    try:
        data["dxy"] = fetch_eastmoney_dxy()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] DXY 获取失败: {e}", file=sys.stderr)

    return data


def main():
    dry_run = "--dry-run" in sys.argv
    json_mode = "--json" in sys.argv

    print("== 康波月报云端生成 ==")
    print(f"[1/4] 抓取市场数据 ... {dt.datetime.now().strftime('%H:%M:%S')}")
    data = collect_data()

    if json_mode:
        print(json.dumps({"quotes": data["quotes"], "xau": data["xau"],
                          "dxy": data["dxy"],
                          "gold_klines_tail": data["klines"].get("gold", [])[-5:]},
                         ensure_ascii=False, indent=2))
        return

    print("[2/4] 运行康波规则分析引擎 ...")
    report = analyze(data)

    print("[3/4] 生成 HTML 报告 ...")
    html_body = build_html(report)
    os.makedirs("output", exist_ok=True)
    fname = f"output/kondratiev_{dt.date.today().isoformat()}.html"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(html_body)
    print(f"      报告已保存: {fname}")

    if dry_run:
        print("[4/4] dry-run 模式，跳过邮件发送。")
        print("核心结论:", report["bias"], "评级", report["overall_score"])
        return

    smtp_user = os.environ.get("SMTP_USER", "378261712@qq.com")
    auth_code = os.environ.get("SMTP_AUTH_CODE", "")
    mail_to = os.environ.get("MAIL_TO", "378261712@qq.com")
    if not auth_code:
        print("[ERROR] 缺少环境变量 SMTP_AUTH_CODE", file=sys.stderr)
        sys.exit(1)

    subject = f"【康波月报】{report['month']}康波周期研判 — 黄金{report['bias']}（评级{report['overall_score']}/5）"

    print(f"[4/4] 发送邮件至 {mail_to} ...")
    send_email(subject, html_body, smtp_user, auth_code, mail_to)
    print(f"      邮件发送成功: {subject}")
    print("== 完成 ==")


if __name__ == "__main__":
    main()
