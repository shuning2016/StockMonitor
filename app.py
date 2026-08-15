"""TopMonitor — 美股大市值监控

两个视图：
  1. 市值 > $100 亿的全部美股上市公司（市值 / 股价 / 行业）
  2. 当日跌幅 TOP 30，每家可手动拉取最新 10 条新闻

数据源：Nasdaq 公开 screener / news 接口（免费，无需 API key）。
后端做代理的原因：Nasdaq 接口不开放 CORS，且必须带 User-Agent。
"""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from flask import Flask, jsonify, render_template, request

# 显式指定模板目录：serverless 环境下工作目录不一定是本文件所在目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))

# ── 配置 ────────────────────────────────────────────────────────────
MIN_MARKET_CAP = 10_000_000_000        # $100 亿
TOP_LOSERS = 30                        # 跌幅榜条数
# 不纳入监控的板块（在数据源层剔除，市值榜/统计/跌幅榜口径一致）
EXCLUDED_SECTORS = {
    "Utilities",                # 公用事业
    "Real Estate",              # 房地产
    "Consumer Discretionary",   # 可选消费
    "Miscellaneous",            # 综合
}
CACHE_TTL = 600                        # 行情缓存 10 分钟
NEWS_LIMIT = 10                        # 每家公司新闻条数
# 短于 Vercel 函数执行上限，超时要能返回错误而不是被平台直接掐断
HTTP_TIMEOUT = 15
# 翻译是锦上添花，超时更短：宁可只显示英文原文，也不拖垮整个新闻请求
TRANSLATE_TIMEOUT = 6

SCREENER_URL = ("https://api.nasdaq.com/api/screener/stocks"
                "?tableonly=true&limit=10000&offset=0&download=true")
NEWS_URL = ("https://api.nasdaq.com/api/news/topic/articlebysymbol"
            "?q={sym}%7Cstocks&offset=0&limit={limit}&fallback=true")
# 新闻标题翻译（免费、无需 key，一次请求批量翻译整页标题）
TRANSLATE_URL = ("https://translate.googleapis.com/translate_a/t"
                 "?client=gtx&sl=en&tl=zh-CN&format=text&{qs}")
NASDAQ_WEB = "https://www.nasdaq.com"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

SECTOR_CN = {
    "Technology": "科技",
    "Finance": "金融",
    "Consumer Discretionary": "可选消费",
    "Consumer Staples": "必需消费",
    "Health Care": "医疗保健",
    "Industrials": "工业",
    "Energy": "能源",
    "Utilities": "公用事业",
    "Real Estate": "房地产",
    "Basic Materials": "基础材料",
    "Telecommunications": "电信",
    "Miscellaneous": "综合",
}

_cache = {"data": None, "ts": 0.0}
_lock = threading.Lock()


# ── 同一公司多代码合并 ──────────────────────────────────────────────
# Nasdaq 会把同一家公司的 A/B 股、ADR、优先股、存托凭证分别列成一行，
# 且给优先股行填的是「母公司市值」（例如 HBANL 显示 $509 亿，比正股
# HBAN 的 $359 亿还高）。所以合并时必须优先选普通股作为代表，
# 而不是简单取市值最大的那条。

# 优先股 / 存托凭证 / 票据等衍生条目的起始标志，从这里截断
_CUT = re.compile(
    r"\s+(Depositary Shares?|Depository Shares?|\d+(\.\d+)?%|Preferred"
    r"|Notes?\b|Warrants?\b|Rights?\b|Units?\b|Subordinated|Perpetual)", re.I)
# 股份类别描述
_SHARE = re.compile(
    r"\s+(Class\s+[A-Z]\b|Series\s+[A-Z]\b|Common Stock|Capital Stock"
    r"|Ordinary Shares?|American Depositary Shares?|New York Registry Shares?"
    r"|Registered Shares?|Sponsored ADR|ADS)\b.*$", re.I)
# 公司形式后缀
_SUFFIX = re.compile(
    r"\s*\b(Incorporated|Inc|Corporation|Corp|Company|Co|Limited|Ltd|PLC"
    r"|N\.V\.|NV|S\.A\.|SA|S\.p\.A\.|AG|SE|Holdings?|Group|Trust|LP|L\.P\.)\b\.?",
    re.I)
# 非普通股特征
_NON_COMMON = re.compile(
    r"(Preferred|Depositary Shares?|Depository Shares?|\d+(\.\d+)?%"
    r"|Notes?\b|Warrants?\b)", re.I)


def company_key(name):
    """把公司名归一化成合并用的 key。"""
    n = _CUT.split(name)[0]
    n = _SHARE.sub("", n)
    n = _SUFFIX.sub("", n)
    return re.sub(r"[^a-z0-9]", "", n.lower())


def is_common_share(name):
    """普通股/ADR 为 True，优先股、存托凭证、票据为 False。"""
    return not _NON_COMMON.search(name)


def mark_primaries(stocks):
    """给每行标记是否为所属公司的代表代码，并挂上被合并的兄弟代码。"""
    groups = {}
    for s in stocks:
        groups.setdefault(company_key(s["name"]), []).append(s)

    for members in groups.values():
        # 普通股优先，其次市值大的
        members.sort(key=lambda s: (0 if is_common_share(s["name"]) else 1,
                                    -s["marketCap"]))
        head, rest = members[0], members[1:]
        head["isPrimary"] = True
        head["aliases"] = [s["symbol"] for s in rest]
        for s in rest:
            s["isPrimary"] = False
            s["aliases"] = []
            s["mergedInto"] = head["symbol"]
    return stocks


# ── 数据抓取 ────────────────────────────────────────────────────────
def _get_json(url, timeout=None):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
        return json.load(resp)


def _to_float(raw):
    """把 '$123.45' / '1.23%' / '' 转成 float，失败返回 None。"""
    if raw is None:
        return None
    s = str(raw).strip().replace("$", "").replace("%", "").replace(",", "")
    if not s or s in {"--", "N/A", "NA"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_universe():
    """抓全部美股上市公司，过滤出市值 > MIN_MARKET_CAP 的。"""
    payload = _get_json(SCREENER_URL)
    rows = (payload.get("data") or {}).get("rows") or []

    out = []
    for row in rows:
        cap = _to_float(row.get("marketCap"))
        if cap is None or cap < MIN_MARKET_CAP:
            continue
        sector = (row.get("sector") or "").strip()
        if sector in EXCLUDED_SECTORS:
            continue
        out.append({
            "symbol": (row.get("symbol") or "").strip(),
            "name": (row.get("name") or "").strip(),
            "marketCap": cap,
            "marketCapB": round(cap / 1e9, 2),
            "price": _to_float(row.get("lastsale")),
            "changePct": _to_float(row.get("pctchange")),
            "sector": sector,
            "sectorCn": SECTOR_CN.get(sector, sector or "未分类"),
            "industry": (row.get("industry") or "").strip(),
            "country": (row.get("country") or "").strip(),
        })

    out.sort(key=lambda x: x["marketCap"], reverse=True)
    return mark_primaries(out)


def get_universe(force=False):
    """带 TTL 缓存的行情读取。"""
    with _lock:
        fresh = (not force
                 and _cache["data"] is not None
                 and time.time() - _cache["ts"] < CACHE_TTL)
        if fresh:
            return _cache["data"], _cache["ts"], True

    data = fetch_universe()
    with _lock:
        _cache["data"] = data
        _cache["ts"] = time.time()
    return data, _cache["ts"], False


def translate_titles(titles):
    """批量把英文标题译成中文。

    翻译失败不应连累新闻本身，所以任何异常都退化为「不翻译」，
    返回与入参等长的 None 列表，前端只显示原文。
    """
    if not titles:
        return []
    try:
        qs = "&".join("q=" + urllib.parse.quote(t) for t in titles)
        payload = _get_json(TRANSLATE_URL.format(qs=qs), timeout=TRANSLATE_TIMEOUT)
        # 单条时返回字符串，多条时返回列表；元素偶尔是 [译文, 源语言]
        if isinstance(payload, str):
            payload = [payload]
        out = []
        for item in payload:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, list) and item and isinstance(item[0], str):
                out.append(item[0])
            else:
                out.append(None)
        # 长度对不上说明格式不符合预期，宁可全部不翻译也不错位
        return out if len(out) == len(titles) else [None] * len(titles)
    except Exception:
        return [None] * len(titles)


def fetch_news(symbol):
    """取单只股票最新 NEWS_LIMIT 条新闻。"""
    sym = urllib.parse.quote(symbol.upper().replace("/", "."), safe="")
    payload = _get_json(NEWS_URL.format(sym=sym, limit=NEWS_LIMIT))
    rows = (payload.get("data") or {}).get("rows") or []

    items = []
    for row in rows[:NEWS_LIMIT]:
        url = (row.get("url") or "").strip()
        if url.startswith("/"):
            url = NASDAQ_WEB + url
        items.append({
            "title": (row.get("title") or "").strip(),
            "publisher": (row.get("publisher") or "").strip(),
            "created": (row.get("created") or "").strip(),
            "url": url,
        })

    for item, zh in zip(items, translate_titles([i["title"] for i in items])):
        # 译文与原文相同视为无效翻译，前端就不必重复显示两遍
        item["titleCn"] = zh if zh and zh != item["title"] else None

    return items


# ── 路由 ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    excluded_cn = "、".join(SECTOR_CN.get(s, s) for s in sorted(EXCLUDED_SECTORS))
    return render_template("index.html",
                           min_cap_cn=f"{MIN_MARKET_CAP / 1e8:.0f} 亿",
                           top_losers=TOP_LOSERS,
                           news_limit=NEWS_LIMIT,
                           excluded_cn=excluded_cn)


@app.route("/api/stocks")
def api_stocks():
    force = request.args.get("refresh") == "1"
    try:
        data, ts, cached = get_universe(force=force)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"行情抓取失败：{exc}"}), 502

    # 涨跌榜只取每家公司的代表代码，避免 GOOGL/GOOG 这类重复占位
    ranked = [s for s in data if s["isPrimary"] and s["changePct"] is not None]
    losers = sorted(ranked, key=lambda x: x["changePct"])[:TOP_LOSERS]
    gainers = sorted(ranked, key=lambda x: x["changePct"], reverse=True)[:TOP_LOSERS]

    return jsonify({
        "ok": True,
        "updatedAt": ts,
        "fromCache": cached,
        "count": len(data),
        "companyCount": sum(1 for s in data if s["isPrimary"]),
        "minMarketCap": MIN_MARKET_CAP,
        "stocks": data,
        "losers": losers,
        "gainers": gainers,
    })


@app.route("/api/news/<path:symbol>")
def api_news(symbol):
    try:
        items = fetch_news(symbol)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"新闻抓取失败：{exc}"}), 502
    return jsonify({
        "ok": True,
        "symbol": symbol.upper(),
        "fetchedAt": time.time(),
        "items": items,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="127.0.0.1", port=port, debug=True)
