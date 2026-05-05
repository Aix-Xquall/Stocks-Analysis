from __future__ import annotations

import concurrent.futures
import datetime as dt
import os
import subprocess
import sys
import textwrap
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
UTC = ZoneInfo("UTC")

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_QUOTA_CONSUMPTION_URL = "https://api.line.me/v2/bot/message/quota/consumption"
YAHOO_QUOTE_URLS = (
    "https://query1.finance.yahoo.com/v7/finance/quote",
    "https://query2.finance.yahoo.com/v7/finance/quote",
)
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

US_STOCKS = [
    {"symbol": "NVDA", "name": "Nvidia", "yahoo": "NVDA"},
    {"symbol": "TSM", "name": "TSMC ADR", "yahoo": "TSM"},
    {"symbol": "MU", "name": "Micron", "yahoo": "MU"},
    {"symbol": "GOOGL", "name": "Alphabet", "yahoo": "GOOGL"},
    {"symbol": "AVGO", "name": "Broadcom", "yahoo": "AVGO"},
    {"symbol": "ORCL", "name": "Oracle", "yahoo": "ORCL"},
    {"symbol": "TSLA", "name": "Tesla", "yahoo": "TSLA"},
    {"symbol": "META", "name": "Meta", "yahoo": "META"},
]

TW_STOCKS = [
    {"symbol": "2330.TW", "name": "台積電", "yahoo": "2330.TW"},
    {"symbol": "2454.TW", "name": "聯發科", "yahoo": "2454.TW"},
    {"symbol": "2308.TW", "name": "台達電", "yahoo": "2308.TW"},
    {"symbol": "8299.TWO", "name": "群聯", "yahoo": "8299.TWO"},
    {"symbol": "2408.TW", "name": "南亞科", "yahoo": "2408.TW"},
    {"symbol": "3260.TWO", "name": "威剛", "yahoo": "3260.TWO"},
    {"symbol": "2368.TW", "name": "金像電", "yahoo": "2368.TW"},
    {"symbol": "2327.TW", "name": "國巨", "yahoo": "2327.TW"},
]


class LineApiError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"LINE API failed with HTTP {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body


@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    published: str


def http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/json,text/xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    return session


SESSION = http_session()


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def now_taipei() -> dt.datetime:
    return dt.datetime.now(TAIPEI_TZ)


def to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt_num(value: Any, decimals: int = 2, sign: bool = False) -> str:
    number = to_float(value)
    if number is None:
        return "N/A"
    prefix = "+" if sign and number > 0 else ""
    return f"{prefix}{number:,.{decimals}f}"


def fmt_pct(value: Any) -> str:
    number = to_float(value)
    if number is None:
        return "N/A"
    prefix = "+" if number > 0 else ""
    return f"{prefix}{number:.2f}%"


def short_text(text: str, max_len: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def quote_change(quote: dict[str, Any]) -> float:
    return to_float(quote.get("regularMarketChange")) or 0.0


def quote_price(quote: dict[str, Any]) -> float | None:
    return to_float(quote.get("regularMarketPrice"))


def market_time_utc(quotes: list[dict[str, Any]]) -> dt.datetime | None:
    timestamps = [
        int(q["regularMarketTime"])
        for q in quotes
        if isinstance(q.get("regularMarketTime"), (int, float))
    ]
    if not timestamps:
        return None
    return dt.datetime.fromtimestamp(max(timestamps), UTC)


def fetch_yahoo_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    if bool_env("YAHOO_TRY_QUOTE_API", False):
        quotes = fetch_yahoo_quote_api(symbols)
        if quotes:
            return quotes
        print("Yahoo quote API unavailable; falling back to chart endpoint.", file=sys.stderr)

    chart_quotes: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {executor.submit(fetch_yahoo_chart_quote, symbol): symbol for symbol in symbols}
        for future in concurrent.futures.as_completed(future_map):
            symbol = future_map[future]
            try:
                quote = future.result()
                if quote:
                    chart_quotes[symbol] = quote
            except Exception as exc:  # noqa: BLE001 - keep other tickers available
                print(f"WARNING: Yahoo chart fetch failed for {symbol}: {exc}", file=sys.stderr)
    return chart_quotes


def fetch_yahoo_quote_api(symbols: list[str]) -> dict[str, dict[str, Any]]:
    params = {"symbols": ",".join(symbols), "lang": "en-US", "region": "US"}
    for url in YAHOO_QUOTE_URLS:
        try:
            response = SESSION.get(url, params=params, timeout=20)
            response.raise_for_status()
            result = response.json().get("quoteResponse", {}).get("result", [])
            quotes = {item["symbol"]: item for item in result if item.get("symbol")}
            if quotes:
                return quotes
        except Exception as exc:  # noqa: BLE001 - chart endpoint is the fallback
            print(f"WARNING: Yahoo quote API failed at {url}: {exc}", file=sys.stderr)
    return {}


def fetch_yahoo_chart_quote(symbol: str) -> dict[str, Any]:
    url = YAHOO_CHART_URL.format(symbol=urllib.parse.quote(symbol, safe=""))
    response = SESSION.get(
        url,
        params={"range": "10d", "interval": "1d", "includePrePost": "false"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        return {}

    meta = result.get("meta", {})
    timestamps = result.get("timestamp") or []
    quote_block = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote_block.get("close") or []
    valid_closes = [
        (int(ts), float(close))
        for ts, close in zip(timestamps, closes)
        if ts is not None and close is not None
    ]
    if not valid_closes:
        return {}

    last_ts, last_close = valid_closes[-1]
    previous_close = to_float(meta.get("previousClose")) or to_float(meta.get("chartPreviousClose"))
    if previous_close is None and len(valid_closes) >= 2:
        previous_close = valid_closes[-2][1]
    price = to_float(meta.get("regularMarketPrice")) or last_close
    market_time = int(meta.get("regularMarketTime") or last_ts)
    change = price - previous_close if previous_close else 0.0
    change_pct = (change / previous_close * 100.0) if previous_close else None

    return {
        "symbol": symbol,
        "currency": meta.get("currency", "USD"),
        "regularMarketPrice": price,
        "regularMarketChange": change,
        "regularMarketChangePercent": change_pct,
        "regularMarketTime": market_time,
    }


def google_news_url(query: str, locale: str) -> str:
    if locale == "tw":
        params = {"q": query, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}
    else:
        params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


def fetch_google_news(query: str, locale: str, limit: int = 1) -> list[NewsItem]:
    try:
        response = SESSION.get(google_news_url(query, locale), timeout=20)
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except Exception as exc:  # noqa: BLE001 - news should not block delivery
        print(f"WARNING: Google News fetch failed for {query!r}: {exc}", file=sys.stderr)
        return []

    items: list[NewsItem] = []
    for item in root.findall("./channel/item"):
        source = item.find("source")
        items.append(
            NewsItem(
                title=item.findtext("title", default="").strip(),
                link=item.findtext("link", default="").strip(),
                source=(source.text or "").strip() if source is not None else "",
                published=item.findtext("pubDate", default="").strip(),
            )
        )
        if len(items) >= limit:
            break
    return items


def collect_news(lookback_days: int = 2) -> dict[str, list[NewsItem]]:
    queries: dict[str, tuple[str, str]] = {}
    for stock in US_STOCKS:
        queries[stock["symbol"]] = (
            (
                f'"{stock["symbol"]}" "{stock["name"]}" '
                f"(earnings OR revenue OR outlook OR EPS OR guidance OR stock) "
                f"when:{lookback_days}d"
            ),
            "us",
        )
    for stock in TW_STOCKS:
        plain_symbol = stock["symbol"].split(".")[0]
        queries[stock["symbol"]] = (
            (
                f'"{stock["name"]}" {plain_symbol} '
                f"(營收 OR EPS OR 財報 OR 展望 OR 法說 OR 重大新聞) "
                f"when:{lookback_days}d"
            ),
            "tw",
        )

    news: dict[str, list[NewsItem]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {
            executor.submit(fetch_google_news, query, locale, 1): symbol
            for symbol, (query, locale) in queries.items()
        }
        for future in concurrent.futures.as_completed(future_map):
            symbol = future_map[future]
            try:
                news[symbol] = future.result()
            except Exception as exc:  # noqa: BLE001 - keep the digest moving
                print(f"WARNING: News task failed for {symbol}: {exc}", file=sys.stderr)
                news[symbol] = []
    return news


def get_line_usage(token: str) -> int:
    response = SESSION.get(
        LINE_QUOTA_CONSUMPTION_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    if not response.ok:
        raise LineApiError(response.status_code, response.text)
    return int(response.json().get("totalUsage", 0))


def send_line_messages(token: str, user_id: str, messages: list[dict[str, Any]]) -> None:
    payload = {"to": user_id, "messages": messages}
    response = SESSION.post(
        LINE_PUSH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    if not response.ok:
        raise LineApiError(response.status_code, response.text)


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msjhbd.ttc" if bold else "",
        "C:/Windows/Fonts/msjh.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def draw_centered(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font: ImageFont.ImageFont, fill: str) -> None:
    x, y = xy
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((x - (box[2] - box[0]) / 2, y), text, font=font, fill=fill)


def generate_us_overview_image(
    quotes: dict[str, dict[str, Any]],
    output_path: Path,
) -> Path:
    width = 1170
    row_h = 116
    gap = 14
    top = 255
    height = top + len(US_STOCKS) * (row_h + gap) + 90

    image = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(image)

    title_font = load_font(66, bold=True)
    subtitle_font = load_font(36)
    header_font = load_font(28, bold=True)
    code_font = load_font(40, bold=True)
    body_font = load_font(34, bold=True)
    trend_font = load_font(62, bold=True)
    footer_font = load_font(27)

    draw_centered(draw, (width // 2, 22), "美股漲跌概覽", title_font, "#050505")
    draw_centered(draw, (width // 2, 113), "依最新報價快照", subtitle_font, "#555b63")

    headers = [
        ("代碼", 70),
        ("公司名稱", 230),
        ("現價 (USD)", 430),
        ("漲跌 (USD)", 650),
        ("漲跌幅", 820),
        ("走勢", 1005),
    ]
    for label, x in headers:
        draw.text((x, 190), label, font=header_font, fill="#111111")

    for idx, stock in enumerate(US_STOCKS):
        y = top + idx * (row_h + gap)
        quote = quotes.get(stock["yahoo"], {})
        change = quote_change(quote)
        pct = quote.get("regularMarketChangePercent")
        price = quote_price(quote)

        if change > 0:
            color = "#07843b"
            fill = "#f4fff7"
            border = "#bfecc8"
            arrow = "↑"
        elif change < 0:
            color = "#c80b12"
            fill = "#fff7f7"
            border = "#ffc0c0"
            arrow = "↓"
        else:
            color = "#666666"
            fill = "#f8f8f8"
            border = "#d6d6d6"
            arrow = "→"

        draw.rounded_rectangle(
            (14, y, width - 14, y + row_h),
            radius=8,
            fill=fill,
            outline=border,
            width=1,
        )
        draw.text((48, y + 32), stock["symbol"], font=code_font, fill=color)
        draw.text((222, y + 35), stock["name"], font=body_font, fill="#15191f")
        draw.text((415, y + 35), f"USD {fmt_num(price)}", font=body_font, fill="#15191f")
        draw.text((650, y + 35), fmt_num(change, sign=True), font=body_font, fill=color)
        draw.text((820, y + 35), fmt_pct(pct), font=body_font, fill=color)
        draw.text((978, y + 20), arrow, font=trend_font, fill=color)
        draw.rounded_rectangle((1045, y + 51, 1115, y + 60), radius=5, fill=color)

    utc_time = market_time_utc([quotes.get(s["yahoo"], {}) for s in US_STOCKS])
    time_text = utc_time.strftime("%Y-%m-%d %H:%M UTC") if utc_time else "N/A"
    draw_centered(draw, (width // 2, height - 55), f"資料時間：{time_text}", footer_font, "#666b73")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, "PNG", optimize=True)
    return output_path


def git_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


def maybe_commit_generated_image(image_path: Path) -> bool:
    if not bool_env("COMMIT_GENERATED_IMAGE", False):
        return False
    if not Path(".git").exists():
        print("WARNING: .git not found; cannot publish generated image.", file=sys.stderr)
        return False

    git_run(["git", "config", "user.name", "github-actions[bot]"])
    git_run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    add_result = git_run(["git", "add", str(image_path)])
    if add_result.returncode != 0:
        print(f"WARNING: git add failed: {add_result.stderr}", file=sys.stderr)
        return False

    diff_result = git_run(["git", "diff", "--cached", "--quiet"])
    if diff_result.returncode == 0:
        return True

    commit_result = git_run(["git", "commit", "-m", "Update stock overview image"])
    if commit_result.returncode != 0:
        print(f"WARNING: git commit failed: {commit_result.stderr}", file=sys.stderr)
        return False

    push_result = git_run(["git", "push"])
    if push_result.returncode != 0:
        print(f"WARNING: git push failed: {push_result.stderr}", file=sys.stderr)
        return False
    return True


def raw_github_image_url(image_path: Path) -> str | None:
    explicit = os.environ.get("LINE_IMAGE_URL", "").strip()
    if explicit:
        return explicit
    if not bool_env("LINE_USE_RAW_GITHUB_IMAGE_URL", False):
        return None

    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    branch = os.environ.get("GITHUB_REF_NAME", "").strip() or "main"
    if not repo:
        return None
    path = urllib.parse.quote(str(image_path).replace("\\", "/"), safe="/")
    timestamp = now_taipei().strftime("%Y%m%d%H%M%S")
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}?ts={timestamp}"


def build_image_message(image_url: str) -> dict[str, Any]:
    return {
        "type": "image",
        "originalContentUrl": image_url,
        "previewImageUrl": image_url,
    }


def build_flex_message(quotes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for stock in US_STOCKS:
        quote = quotes.get(stock["yahoo"], {})
        change = quote_change(quote)
        color = "#07843B" if change > 0 else "#C80B12" if change < 0 else "#666666"
        arrow = "↑" if change > 0 else "↓" if change < 0 else "→"
        rows.append(
            {
                "type": "box",
                "layout": "horizontal",
                "paddingAll": "8px",
                "backgroundColor": "#F4FFF7" if change > 0 else "#FFF7F7" if change < 0 else "#F8F8F8",
                "contents": [
                    {"type": "text", "text": stock["symbol"], "weight": "bold", "size": "md", "color": color, "flex": 2},
                    {"type": "text", "text": stock["name"], "size": "sm", "color": "#111111", "flex": 3},
                    {"type": "text", "text": f"USD {fmt_num(quote_price(quote))}", "size": "sm", "align": "end", "flex": 3},
                    {"type": "text", "text": fmt_pct(quote.get("regularMarketChangePercent")), "size": "sm", "align": "end", "color": color, "flex": 2},
                    {"type": "text", "text": arrow, "size": "xl", "align": "end", "color": color, "flex": 1},
                ],
            }
        )

    utc_time = market_time_utc([quotes.get(s["yahoo"], {}) for s in US_STOCKS])
    time_text = utc_time.strftime("%Y-%m-%d %H:%M UTC") if utc_time else "N/A"
    return {
        "type": "flex",
        "altText": "美股漲跌概覽",
        "contents": {
            "type": "bubble",
            "size": "giga",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {"type": "text", "text": "美股漲跌概覽", "weight": "bold", "size": "xxl", "align": "center"},
                    {"type": "text", "text": "依最新報價快照", "size": "sm", "color": "#666666", "align": "center"},
                    {"type": "separator", "margin": "md"},
                    *rows,
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": f"資料時間：{time_text}", "size": "xs", "color": "#666666", "align": "center"},
                ],
            },
        },
    }


def eps_text(quote: dict[str, Any]) -> str:
    parts = []
    eps_ttm = to_float(quote.get("epsTrailingTwelveMonths"))
    eps_forward = to_float(quote.get("epsForward"))
    if eps_ttm is not None:
        parts.append(f"EPS TTM {eps_ttm:.2f}")
    if eps_forward is not None:
        parts.append(f"forward {eps_forward:.2f}")
    if not parts:
        parts.append("EPS/ESP 未見公開更新")
    return " / ".join(parts)


def stock_digest_lines(
    stocks: list[dict[str, str]],
    quotes: dict[str, dict[str, Any]],
    news: dict[str, list[NewsItem]],
) -> list[str]:
    lines: list[str] = []
    for stock in stocks:
        quote = quotes.get(stock["yahoo"], {})
        price = quote_price(quote)
        change = quote_change(quote)
        pct = quote.get("regularMarketChangePercent")
        currency = quote.get("currency", "USD")
        quote_line = (
            f"{stock['symbol']} {stock['name']}: {currency} {fmt_num(price)} "
            f"{fmt_num(change, sign=True)} ({fmt_pct(pct)}), {eps_text(quote)}"
        )
        lines.append(quote_line)

        item = (news.get(stock["symbol"]) or news.get(stock["yahoo"]) or [None])[0]
        if item:
            source = f" - {item.source}" if item.source else ""
            lines.append(f"新聞: {short_text(item.title, 74)}{source}\n{item.link}")
        else:
            lines.append("新聞: 未見重大更新")
    return lines


def stock_quote_line(stock: dict[str, str], quote: dict[str, Any]) -> str:
    price = quote_price(quote)
    change = quote_change(quote)
    pct = quote.get("regularMarketChangePercent")
    currency = quote.get("currency", "USD")
    return (
        f"{stock['symbol']} {stock['name']}: {currency} {fmt_num(price)} "
        f"{fmt_num(change, sign=True)} ({fmt_pct(pct)}), {eps_text(quote)}"
    )


def stock_news_line(stock: dict[str, str], news: dict[str, list[NewsItem]]) -> str:
    item = (news.get(stock["symbol"]) or news.get(stock["yahoo"]) or [None])[0]
    if not item:
        return "新聞: 未見重大更新"
    source = f" - {item.source}" if item.source else ""
    return f"新聞: {short_text(item.title, 62)}{source}\n{item.link}"


def build_text_digest(
    quotes: dict[str, dict[str, Any]],
    news: dict[str, list[NewsItem]],
    estimated_usage_after_send: int,
    display_limit: int,
) -> str:
    today = now_taipei().strftime("%Y-%m-%d")
    us_quotes = [quotes.get(stock["yahoo"], {}) for stock in US_STOCKS]
    ups = sum(1 for quote in us_quotes if quote_change(quote) > 0)
    downs = sum(1 for quote in us_quotes if quote_change(quote) < 0)
    flat = len(US_STOCKS) - ups - downs

    footer = f"本月LINE訊息: {estimated_usage_after_send} / {display_limit}"
    lines = [
        f"美台股晨間摘要 {today}",
        f"美股快照: 上漲 {ups} / 下跌 {downs} / 持平 {flat}",
        "重點: 股價漲跌、EPS/ESP、營收、展望與重大新聞連結。",
        "",
        "【美股】",
    ]

    def can_add(line: str) -> bool:
        candidate = "\n".join([*lines, line, "", footer])
        return len(candidate) <= 5000

    def add_line(line: str) -> None:
        if can_add(line):
            lines.append(line)

    def add_stock_group(stocks: list[dict[str, str]]) -> None:
        for stock in stocks:
            quote = quotes.get(stock["yahoo"], {})
            add_line(stock_quote_line(stock, quote))
            if not can_add(stock_news_line(stock, news)):
                add_line("新聞: 空間不足，略過連結")
            else:
                add_line(stock_news_line(stock, news))

    add_stock_group(US_STOCKS)
    lines.extend(["", "【台股】"])
    add_stock_group(TW_STOCKS)
    lines.extend(["", footer])
    return "\n".join(lines)[:5000]


def build_messages(
    quotes: dict[str, dict[str, Any]],
    news: dict[str, list[NewsItem]],
    usage_before_send: int,
    display_limit: int,
) -> list[dict[str, Any]]:
    image_path = generate_us_overview_image(quotes, Path("public/us_stock_overview.png"))
    image_url: str | None = None
    if os.environ.get("LINE_IMAGE_URL") or bool_env("LINE_USE_RAW_GITHUB_IMAGE_URL", False):
        if maybe_commit_generated_image(image_path):
            image_url = raw_github_image_url(image_path)

    first_message = build_image_message(image_url) if image_url else build_flex_message(quotes)
    text = build_text_digest(quotes, news, usage_before_send + 2, display_limit)
    return [first_message, {"type": "text", "text": text}]


def main() -> int:
    dry_run = bool_env("DRY_RUN", False)
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    user_id = os.environ.get("LINE_USER_ID", "").strip()
    stop_at = int(os.environ.get("LINE_STOP_AT", "198"))
    display_limit = int(os.environ.get("LINE_MONTHLY_DISPLAY_LIMIT", "200"))
    lookback_days = int(os.environ.get("NEWS_LOOKBACK_DAYS", "2"))

    symbols = [stock["yahoo"] for stock in [*US_STOCKS, *TW_STOCKS]]
    print("Fetching quotes...")
    quotes = fetch_yahoo_quotes(symbols)
    print(f"Fetched {len(quotes)} quote rows.")

    print("Fetching news...")
    news = collect_news(lookback_days=lookback_days)
    print(f"Fetched news buckets for {len(news)} tickers.")

    if dry_run:
        usage = int(os.environ.get("DRY_RUN_LINE_USAGE", "0"))
    else:
        if not token:
            raise SystemExit("Missing LINE_CHANNEL_ACCESS_TOKEN")
        if not user_id:
            raise SystemExit("Missing LINE_USER_ID")
        try:
            usage = get_line_usage(token)
        except LineApiError as exc:
            print(f"LINE quota check failed; skip sending to avoid exceeding quota. {exc}", file=sys.stderr)
            return 1

    print(f"LINE usage before send: {usage} / {display_limit}")
    if usage >= stop_at or usage + 2 > stop_at:
        print(
            textwrap.dedent(
                f"""
                Skip LINE send: current usage is {usage} / {display_limit}; stop threshold is {stop_at}.
                The workflow will try again on the next scheduled run, and LINE's monthly counter will reset automatically.
                """
            ).strip()
        )
        return 0

    messages = build_messages(quotes, news, usage, display_limit)
    first_type = messages[0].get("type")
    print(f"Prepared {len(messages)} LINE messages. First message type: {first_type}.")

    if dry_run:
        print("DRY_RUN=true; not sending LINE messages.")
        print(messages[1]["text"][:1200])
        return 0

    try:
        send_line_messages(token, user_id, messages)
    except LineApiError as exc:
        if first_type == "image":
            print(f"Image message failed; retrying with Flex fallback. {exc}", file=sys.stderr)
            messages[0] = build_flex_message(quotes)
            send_line_messages(token, user_id, messages)
        else:
            raise

    try:
        usage_after = get_line_usage(token)
        print(f"LINE send succeeded. Current usage: {usage_after} / {display_limit}")
    except LineApiError:
        print(f"LINE send succeeded. Estimated usage: {usage + 2} / {display_limit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
