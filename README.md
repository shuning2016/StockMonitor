# TopMonitor · 美股大市值监控

一个本地 Flask 小应用，两个视图：

1. **市值榜** — 全部市值 > $100 亿的美股上市公司：市值 / 股价 / 涨跌幅 / 所属行业 / 细分行业，支持搜索、行业筛选、按市值/股价/涨跌幅排序。
2. **今日跌幅 TOP 30** — 按最近一个交易日收盘跌幅排序，每家公司一个「刷新新闻」按钮，点击即时抓取该公司最新 10 条新闻。

## 运行

```bash
pip install -r requirements.txt
python app.py
```

打开 http://127.0.0.1:5001

## 数据源

Nasdaq 公开接口，**无需 API key**：

| 用途 | 接口 |
|------|------|
| 全市场行情 | `api.nasdaq.com/api/screener/stocks` |
| 个股新闻 | `api.nasdaq.com/api/news/topic/articlebysymbol` |

覆盖 NYSE / NASDAQ / NYSE American 全部约 7000 只上市股票（含 ADR）。

> 为什么要后端：Nasdaq 接口不开放 CORS，且必须带 User-Agent，浏览器无法直连，所以用 Flask 做一层代理。

## 配置

改 `app.py` 顶部常量即可：

| 常量 | 默认 | 说明 |
|------|------|------|
| `MIN_MARKET_CAP` | `10_000_000_000` | 市值门槛（$100 亿） |
| `TOP_LOSERS` | `30` | 跌幅榜条数 |
| `NEWS_LIMIT` | `10` | 每家公司新闻条数 |
| `CACHE_TTL` | `600` | 行情缓存秒数，右上角「刷新行情」可强制绕过 |

## API

| 路由 | 说明 |
|------|------|
| `GET /api/stocks` | 全量列表 + 跌幅榜 + 涨幅榜；`?refresh=1` 强制重抓 |
| `GET /api/news/<symbol>` | 指定代码的最新 10 条新闻 |

## 说明

- 行情为**最近一个完整交易日**的收盘价与涨跌幅，非盘中实时。
- 同一公司的多个股票代码（如 GOOGL/GOOG、BRK/A + BRK/B）会分别列出。
- 仅供研究参考，不构成投资建议。

## 配色

遵循 Shopee 品牌规范：主色橙 `#EE4D2D`、Navy `#172B4D`、Bright Blue `#0080C6`、Yellow `#FCCD34`、Red `#F05025`。
唯一例外是涨幅绿 `#12805C` —— 品牌色板没有涨跌语义色，跌幅用的是品牌红 `#F05025`。
