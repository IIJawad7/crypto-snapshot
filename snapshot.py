"""Crypto snapshot: pulls market data + news and writes a report Claude can read.

Stdlib only (no pip install needed). Read-only: uses Binance *public* market data,
never needs API keys, never places orders.

Outputs:
  reports/latest.md    human/Claude-readable report
  reports/latest.json  same data, structured
  reports/history/     one timestamped .md per run
"""

import html
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(ROOT, "reports")
UA = {"User-Agent": "Mozilla/5.0 (crypto-snapshot)"}

# data-api.binance.vision is Binance's public market-data mirror; it works from
# GitHub's US runners, where api.binance.com is geo-blocked (HTTP 451).
BINANCE_HOSTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
]

NEWS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
}


# ---------------------------------------------------------------- fetching

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def binance(path, params):
    qs = urllib.parse.urlencode(params)
    last = None
    for host in BINANCE_HOSTS:
        try:
            return json.loads(http_get(f"{host}{path}?{qs}"))
        except Exception as e:  # try next host
            last = e
    raise RuntimeError(f"Binance unreachable ({last})")


def klines(pair, interval, limit):
    rows = binance("/api/v3/klines", {"symbol": pair, "interval": interval, "limit": limit})
    return [
        {
            "t": datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc),
            "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
            "c": float(r[4]), "v": float(r[7]),  # quote volume (USDT)
        }
        for r in rows
    ]


# ---------------------------------------------------------------- indicators

def sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):  # Wilder smoothing
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def atr_pct(bars, n=14):
    if len(bars) <= n:
        return None
    trs = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        trs.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    return sum(trs[-n:]) / n / bars[-1]["c"] * 100


def realized_vol(closes, n=30):
    if len(closes) <= n:
        return None
    rets = [math.log(b / a) for a, b in zip(closes[-n - 1:-1], closes[-n:])]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * 100  # daily %, not annualised


def pct(a, b):
    return (a / b - 1) * 100 if b else None


def analyse(symbol, quote, levels):
    pair = f"{symbol}{quote}"
    t24 = binance("/api/v3/ticker/24hr", {"symbol": pair})
    d1 = klines(pair, "1d", 200)
    h4 = klines(pair, "4h", 60)
    closes = [b["c"] for b in d1]
    price = float(t24["lastPrice"])

    last90 = d1[-90:]
    last30 = d1[-30:]
    hi90 = max(last90, key=lambda b: b["h"])
    lo90 = min(last90, key=lambda b: b["l"])
    vol_today = float(t24["quoteVolume"])
    vol_avg20 = sum(b["v"] for b in d1[-21:-1]) / 20

    out = {
        "pair": pair,
        "price": price,
        "change_24h_pct": float(t24["priceChangePercent"]),
        "high_24h": float(t24["highPrice"]),
        "low_24h": float(t24["lowPrice"]),
        "range_24h_pct": pct(float(t24["highPrice"]), float(t24["lowPrice"])),
        "change_7d_pct": pct(price, closes[-8]),
        "change_30d_pct": pct(price, closes[-31]),
        "ma7": sma(closes, 7),
        "ma25": sma(closes, 25),
        "ma99": sma(closes, 99),
        "rsi14_daily": rsi(closes),
        "rsi14_4h": rsi([b["c"] for b in h4]),
        "atr14_pct": atr_pct(d1),
        "realized_vol_30d_daily_pct": realized_vol(closes),
        "volume_24h_usdt": vol_today,
        "volume_vs_20d_avg": vol_today / vol_avg20 if vol_avg20 else None,
        "high_90d": hi90["h"], "high_90d_date": hi90["t"].date().isoformat(),
        "low_90d": lo90["l"], "low_90d_date": lo90["t"].date().isoformat(),
        "high_30d": max(b["h"] for b in last30),
        "low_30d": min(b["l"] for b in last30),
        "last_7_daily_closes": [
            {"date": b["t"].date().isoformat(), "close": b["c"]} for b in d1[-7:]
        ],
        "levels": {},
    }
    out["from_90d_high_pct"] = pct(price, out["high_90d"])
    out["from_90d_low_pct"] = pct(price, out["low_90d"])
    for name, lvl in (levels or {}).items():
        if lvl:
            out["levels"][name] = {"price": lvl, "distance_pct": pct(lvl, price)}
    return out


# ---------------------------------------------------------------- news

def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def read_feed(source, url):
    root = ET.fromstring(http_get(url))
    items = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        def get(*names):
            # NB: childless Elements are falsy, so compare with None explicitly.
            for name in names:
                for c in it:
                    if c.tag.split("}")[-1] == name:
                        return c
            return None

        title = (get("title").text or "").strip() if get("title") is not None else ""
        link_el = get("link")
        link = ""
        if link_el is not None:
            link = (link_el.text or link_el.get("href") or "").strip()
        date_el = get("pubDate", "published", "updated")
        desc_el = get("description", "summary")
        desc = re.sub(r"<[^>]+>", "", (desc_el.text or "") if desc_el is not None else "")
        src_el = get("source")
        items.append({
            "source": (src_el.text.strip() if src_el is not None and src_el.text else source),
            "title": title,
            "link": link,
            "published": parse_date(date_el.text if date_el is not None else None),
            "summary": re.sub(r"\s+", " ", desc).strip()[:280],
        })
    return items


CRYPTO_CONTEXT = re.compile(
    r"\b(crypto\w*|token|coin|blockchain|ledger|etf|sec|stablecoin|rlusd|price|bitcoin|btc|altcoin)\b", re.I
)


def news_for(keywords, weak_keywords, hours, limit, errors):
    """Headlines mentioning a strong keyword, or a weak (ambiguous) keyword plus
    crypto context -- so "Ripple effect: local watershed" is dropped."""
    feeds = dict(NEWS_FEEDS)
    q = " OR ".join(f'"{k}"' for k in keywords)
    feeds["Google News"] = (
        "https://news.google.com/rss/search?"
        + urllib.parse.urlencode({"q": f"({q}) when:2d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    alt = lambda ks: r"\b(" + "|".join(map(re.escape, ks)) + r")\b"
    strong = re.compile(alt(keywords), re.I)
    weak = re.compile(alt(weak_keywords), re.I) if weak_keywords else None
    google_cap = max(1, limit // 2)  # keep room for the crypto outlets
    seen, found = set(), []
    for source, url in feeds.items():
        try:
            items = read_feed(source, url)
        except Exception as e:
            errors.append(f"news feed {source}: {e}")
            continue
        for it in items:
            it["title"] = html.unescape(it["title"]).strip()
            it["summary"] = html.unescape(it["summary"]).replace("\xa0", " ").strip()
            text = it["title"] + " " + it["summary"]
            relevant = strong.search(text) or (weak and weak.search(text) and CRYPTO_CONTEXT.search(text))
            if not relevant:
                continue
            if it["published"] and it["published"] < cutoff:
                continue
            key = re.sub(r"\W+", "", it["title"].lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            # Google News "summaries" just repeat the headline
            if it["summary"][:40].lower() in it["title"].lower() or it["title"][:40].lower() in it["summary"].lower():
                it["summary"] = ""
            it["via"] = source
            found.append(it)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    found.sort(key=lambda x: x["published"] or epoch, reverse=True)
    out, g = [], 0
    for it in found:
        if it["via"] == "Google News":
            if g >= google_cap:
                continue
            g += 1
        it["published"] = it["published"].isoformat() if it["published"] else None
        out.append(it)
        if len(out) >= limit:
            break
    return out


def fear_greed(errors):
    try:
        d = json.loads(http_get("https://api.alternative.me/fng/?limit=2"))["data"]
        return {"today": int(d[0]["value"]), "label": d[0]["value_classification"],
                "yesterday": int(d[1]["value"])}
    except Exception as e:
        errors.append(f"fear & greed: {e}")
        return None


# ---------------------------------------------------------------- report

def f(x, d=4):
    return "n/a" if x is None else f"{x:,.{d}f}"


def s(x, d=2):
    return "n/a" if x is None else f"{x:+.{d}f}%"


def render(report, tz_hours):
    local = datetime.fromisoformat(report["generated_utc"]) + timedelta(hours=tz_hours)
    L = [f"# Crypto snapshot — {local:%Y-%m-%d %H:%M} (UTC{tz_hours:+d})", ""]

    ctx = report.get("context", {})
    fg = report.get("fear_greed")
    L.append("## Market context")
    for sym, c in ctx.items():
        L.append(f"- {sym}: {f(c['price'], 2)} ({s(c['change_24h_pct'])} 24h, {s(c['change_7d_pct'])} 7d)")
    if fg:
        L.append(f"- Fear & Greed index: {fg['today']} ({fg['label']}), yesterday {fg['yesterday']}")
    L.append("")

    for coin in report["coins"]:
        a = coin.get("analysis")
        L.append(f"## {coin['symbol']}")
        if not a:
            L.append("_market data unavailable — see errors_\n")
        else:
            L += [
                f"**Price:** {f(a['price'])}  |  24h {s(a['change_24h_pct'])}  |  7d {s(a['change_7d_pct'])}  |  30d {s(a['change_30d_pct'])}",
                "",
                "| Metric | Value |", "|---|---|",
                f"| 24h high / low | {f(a['high_24h'])} / {f(a['low_24h'])} (range {f(a['range_24h_pct'], 2)}%) |",
                f"| MA7 / MA25 / MA99 (daily) | {f(a['ma7'])} / {f(a['ma25'])} / {f(a['ma99'])} |",
                f"| Price vs MA7 / MA25 / MA99 | {s(pct(a['price'], a['ma7']))} / {s(pct(a['price'], a['ma25']))} / {s(pct(a['price'], a['ma99']))} |",
                f"| RSI14 daily / 4h | {f(a['rsi14_daily'], 1)} / {f(a['rsi14_4h'], 1)} |",
                f"| ATR14 (avg daily move) | {f(a['atr14_pct'], 2)}% |",
                f"| 30d realized vol (daily σ) | {f(a['realized_vol_30d_daily_pct'], 2)}% |",
                f"| 24h volume vs 20d avg | {f(a['volume_vs_20d_avg'], 2)}x ({f(a['volume_24h_usdt'] / 1e6, 1)}M USDT) |",
                f"| 30d high / low | {f(a['high_30d'])} / {f(a['low_30d'])} |",
                f"| 90d high | {f(a['high_90d'])} on {a['high_90d_date']} ({s(a['from_90d_high_pct'])} from it) |",
                f"| 90d low | {f(a['low_90d'])} on {a['low_90d_date']} ({s(a['from_90d_low_pct'])} from it) |",
                "",
                "Last 7 daily closes: " + ", ".join(f"{x['date'][5:]} {f(x['close'])}" for x in a["last_7_daily_closes"]),
                "",
            ]
            if a["levels"]:
                L += ["**My levels**", "", "| Level | Price | Distance from now |", "|---|---|---|"]
                for name, lv in a["levels"].items():
                    L.append(f"| {name} | {f(lv['price'])} | {s(lv['distance_pct'])} |")
                L.append("")
        news = coin.get("news", [])
        L.append(f"**News (last {report['news_hours']}h, {len(news)} items)**")
        L.append("")
        if not news:
            L.append("_no matching headlines found_")
        for n in news:
            when = n["published"][:16].replace("T", " ") + " UTC" if n["published"] else "?"
            L.append(f"- [{n['title']}]({n['link']}) — {n['source']}, {when}")
            if n["summary"]:
                L.append(f"  - {n['summary']}")
        L.append("")

    if report["errors"]:
        L += ["## Errors", ""] + [f"- {e}" for e in report["errors"]] + [""]
    return "\n".join(L)


def main():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    quote = cfg.get("quote", "USDT")
    errors = []
    report = {
        "generated_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "news_hours": cfg.get("news_hours", 48),
        "context": {},
        "coins": [],
        "errors": errors,
    }

    for sym in cfg.get("context_symbols", []):
        try:
            a = analyse(sym, quote, None)
            report["context"][sym] = {k: a[k] for k in ("price", "change_24h_pct", "change_7d_pct")}
        except Exception as e:
            errors.append(f"{sym} context: {e}")
    report["fear_greed"] = fear_greed(errors)

    for coin in cfg["coins"]:
        entry = {"symbol": coin["symbol"], "analysis": None, "news": []}
        try:
            entry["analysis"] = analyse(coin["symbol"], quote, coin.get("levels"))
        except Exception as e:
            errors.append(f"{coin['symbol']} market data: {e}")
        kws = coin.get("news_keywords") or [coin["symbol"].lower()]
        entry["news"] = news_for(kws, coin.get("news_weak_keywords", []), report["news_hours"],
                                 cfg.get("max_news_items", 12), errors)
        report["coins"].append(entry)

    md = render(report, cfg.get("timezone_offset_hours", 0))

    def js(o):
        if isinstance(o, datetime):
            return o.isoformat()
        raise TypeError(type(o))

    os.makedirs(os.path.join(REPORT_DIR, "history"), exist_ok=True)
    stamp = report["generated_utc"][:16].replace(":", "").replace("T", "-")
    for path, body in [
        (os.path.join(REPORT_DIR, "latest.md"), md),
        (os.path.join(REPORT_DIR, "history", f"{stamp}.md"), md),
        (os.path.join(REPORT_DIR, "latest.json"), json.dumps(report, default=js, indent=2)),
    ]:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
    print(md)
    # Fail the run only if we got no market data at all.
    if all(c["analysis"] is None for c in report["coins"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
