"""生成 data/earnings.json —— 每只股票的上一次 / 下一次财报日期。

Nasdaq 没有「按代码查财报日」的接口，只有「按日期查当天财报公司」的日历接口，
所以只能逐日扫描一个时间窗口再倒排成 代码 → 日期。约 136 个请求、并发跑 30 秒，
超过 Vercel 函数执行上限，因此离线生成、随仓库提交，应用启动时直接加载。

用法：
    python3 build_earnings.py            # 默认前后各 100 天
    python3 build_earnings.py 120        # 自定义窗口天数

财报日期变动很慢，每周重新生成一次即可。
"""
import datetime
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CAL_URL = "https://api.nasdaq.com/api/calendar/earnings?date={date}"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0 Safari/537.36"),
    "Accept": "application/json",
}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "earnings.json")
WORKERS = 16
TIMEOUT = 25


def weekdays(start, days_back, days_fwd):
    """窗口内的工作日（财报只在工作日公布，跳过周末省掉四成请求）。"""
    out = []
    for offset in range(-days_back, days_fwd + 1):
        d = start + datetime.timedelta(days=offset)
        if d.weekday() < 5:
            out.append(d)
    return out


def fetch_day(day):
    url = CAL_URL.format(date=day.isoformat())
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.load(resp)
        rows = (payload.get("data") or {}).get("rows") or []
        return day, [(r.get("symbol") or "").strip().upper() for r in rows if r.get("symbol")]
    except Exception as exc:
        print(f"  ! {day} 失败: {exc}", file=sys.stderr)
        return day, None


def main():
    days_window = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    today = datetime.date.today()
    days = weekdays(today, days_window, days_window)
    print(f"扫描 {days[0]} ~ {days[-1]}，共 {len(days)} 个工作日…")

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(fetch_day, days))

    failed = [d for d, syms in results if syms is None]
    last, nxt = {}, {}
    for day, syms in results:
        if syms is None:
            continue
        iso = day.isoformat()
        for sym in syms:
            if day < today:
                # 保留最靠近今天的过去日期
                if sym not in last or iso > last[sym]:
                    last[sym] = iso
            elif day >= today:
                # 保留最靠近今天的未来日期
                if sym not in nxt or iso < nxt[sym]:
                    nxt[sym] = iso

    symbols = sorted(set(last) | set(nxt))
    data = {
        "generatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
        "windowFrom": days[0].isoformat(),
        "windowTo": days[-1].isoformat(),
        "failedDays": [d.isoformat() for d in failed],
        "earnings": {s: {"last": last.get(s), "next": nxt.get(s)} for s in symbols},
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    print(f"覆盖代码 {len(symbols)} 个（有上次 {len(last)}，有下次 {len(nxt)}）")
    if failed:
        print(f"失败日期 {len(failed)} 个：{[d.isoformat() for d in failed][:5]}")
    print(f"已写入 {OUT}（{os.path.getsize(OUT) / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
