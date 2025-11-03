#!/usr/bin/env python3
"""
MAGS ETF Data Fetcher — TUI + What-If NAV nudger + MAG7 live prices

Features:
- Scrape Roundhill MAGS Top Holdings (robust mobile/desktop parsing)
- Extract NAV and choose most-recent (<= today) "As of" date from the page
- Show MAG7 live prices (NVDA, AAPL, MSFT, GOOGL, AMZN, META, TSLA):
    Ticker | Weight% | Last | Δ$ | Δ%
- What-if engine: bumps per holding (tickers or names) + ALL
    New NAV ≈ NAV * (1 + Σ(weight% × move%) / 10,000)
- Optionally apply live MAG7 % changes as your scenario bumps
"""

import re
import json
import time
import argparse
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, List
from datetime import datetime, timedelta

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.remote.webelement import WebElement
from webdriver_manager.chrome import ChromeDriverManager

# ---------------------- Config ----------------------

ROUNDHILL_URL = "https://www.roundhillinvestments.com/etf/mags/"

MAG7_TICKERS = ["NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA"]

# Map company names (as shown on site) to tickers
NAME_TO_TICKER = {
    "NVIDIA": "NVDA",
    "Alphabet": "GOOGL",
    "AMAZON.COM INC": "AMZN",
    "Amazon": "AMZN",
    "Tesla": "TSLA",
    "Apple": "AAPL",
    "Microsoft": "MSFT",
    "Meta Platforms": "META",
    "Meta": "META",
}
NAME_TO_TICKER_NORM = {k.strip().upper(): v for k, v in NAME_TO_TICKER.items()}

@dataclass
class MAGSData:
    nav: Optional[float] = None
    date: str = "Unknown"  # last trading day shown on the page (most recent date ≤ today)
    holdings: Dict[str, float] = field(default_factory=dict)         # ticker -> weight
    holdings_by_name: Dict[str, float] = field(default_factory=dict) # company name -> weight

# ---------------------- Browser helpers ----------------------

def make_driver(headless: bool, window_size: Tuple[int, int]) -> webdriver.Chrome:
    width, height = window_size
    opts = webdriver.ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument(f"--window-size={width},{height}")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    service = ChromeService(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)

def _accept_cookies_if_present(driver: webdriver.Chrome):
    for sel in [
        "#onetrust-accept-btn-handler",
        "button#onetrust-accept-btn-handler",
        "button[aria-label='Accept All Cookies']",
        "button.cookie-accept",
    ]:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(0.2)
            break
        except Exception:
            pass

# ---------------------- Parsing helpers ----------------------

def _extract_nav_from_text(text: str) -> Optional[float]:
    for pat in [
        r"Net Asset Value[^$]*\$\s*([0-9][0-9,]*\.?[0-9]*)",
        r"\bNAV\b[^$]*\$\s*([0-9][0-9,]*\.?[0-9]*)",
        r'"NetAssetValue"[^0-9]*([0-9][0-9,]*\.?[0-9]*)',
    ]:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except Exception:
                pass
    return None

def _coerce_percent_to_float(s: str) -> Optional[float]:
    if s is None:
        return None
    s = str(s).strip()
    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%?$", s)
    try:
        return float(m.group(1)) if m else None
    except Exception:
        return None

def _dedupe_pairs(pairs: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    seen = set()
    out: List[Tuple[str, float]] = []
    for name, w in pairs:
        key = (name.strip(), round(w, 6))
        if key not in seen:
            seen.add(key)
            out.append((name.strip(), w))
    return out

def _collect_holdings_rows(driver: webdriver.Chrome) -> List[WebElement]:
    rows_mobile = driver.find_elements(By.CSS_SELECTOR, "tbody.fund-topTenHoldings-mobile tr")
    if rows_mobile:
        return rows_mobile
    return driver.find_elements(By.CSS_SELECTOR, "tbody.fund-topTenHoldings tr")

def _parse_holdings_table_rows(rows: List[WebElement], driver: webdriver.Chrome) -> List[Tuple[str, float]]:
    pairs: List[Tuple[str, float]] = []
    time.sleep(0.3)  # small settle

    for row in rows:
        parsed = False
        row_html = ""
        try:
            row_html = row.get_attribute("outerHTML") or ""
        except Exception:
            pass

        # 1) JS textContent (handles visually hidden text)
        try:
            js = """
                const row = arguments[0];
                const nameEl = row.querySelector('td[data-title="Name"]');
                const wgtEl  = row.querySelector('td[data-title="Weight"]');
                return {
                  name: nameEl ? (nameEl.textContent || nameEl.innerText || '').trim() : '',
                  weight: wgtEl ? (wgtEl.textContent || wgtEl.innerText || '').trim() : ''
                };
            """
            result = driver.execute_script(js, row)
            name = (result.get("name") or "").strip()
            w = _coerce_percent_to_float((result.get("weight") or "").strip())
            if name and w is not None:
                pairs.append((name, w))
                parsed = True
        except Exception:
            pass

        # 2) Selenium-visible text
        if not parsed:
            try:
                tds = row.find_elements(By.TAG_NAME, "td")
                name, w = None, None
                for td in tds:
                    title = (td.get_attribute("data-title") or "").strip()
                    content = (td.text or "").strip()
                    if not title:
                        continue
                    if "Name" in title and content:
                        name = content
                    elif "Weight" in title and content:
                        w = _coerce_percent_to_float(content)
                if name and w is not None:
                    pairs.append((name, w))
                    parsed = True
            except Exception:
                pass

        # 3) Regex over HTML
        if not parsed and row_html:
            try:
                name_match = re.search(r'<td[^>]*data-title="Name"[^>]*>(.*?)</td>',
                                       row_html, re.DOTALL | re.IGNORECASE)
                wgt_match = re.search(r'<td[^>]*data-title="Weight"[^>]*>(.*?)</td>',
                                      row_html, re.DOTALL | re.IGNORECASE)
                if name_match and wgt_match:
                    name = re.sub(r'<[^>]+>', '', name_match.group(1)).strip()
                    w = _coerce_percent_to_float(re.sub(r'<[^>]+>', '', wgt_match.group(1)).strip())
                    if name and w is not None:
                        pairs.append((name, w))
            except Exception:
                pass

    return _dedupe_pairs(pairs)

# ---------------------- Date logic (last trading day on page) ----------------------

_DATE_RE = re.compile(r"\b([0-1]?\d/[0-3]?\d/\d{4})\b")  # mm/dd/yyyy

def _latest_date_on_page(text: str) -> Optional[str]:
    today = datetime.now().date()
    dates = []
    for m in _DATE_RE.finditer(text):
        s = m.group(1)
        try:
            d = datetime.strptime(s, "%m/%d/%Y").date()
        except ValueError:
            continue
        if d <= today:
            dates.append(d)
    if not dates:
        return None
    latest = max(dates)
    try:
        return latest.strftime("%-m/%-d/%Y")
    except Exception:
        return latest.strftime("%m/%d/%Y")

def _last_weekday_str() -> str:
    d = datetime.now().date()
    while d.weekday() >= 5:  # Sat/Sun
        d -= timedelta(days=1)
    try:
        return d.strftime("%-m/%-d/%Y")
    except Exception:
        return d.strftime("%m/%d/%Y")

# ---------------------- Orchestration (scrape) ----------------------

def parse_mags_exposures(
    driver: webdriver.Chrome,
    timeout: int,
) -> MAGSData:
    data = MAGSData()
    wait = WebDriverWait(driver, timeout)

    driver.get(ROUNDHILL_URL)
    _accept_cookies_if_present(driver)

    time.sleep(0.8)
    driver.refresh()
    time.sleep(0.8)

    # Ensure rows exist (click "Top Holdings" if needed)
    have_rows = driver.find_elements(By.CSS_SELECTOR, "tbody.fund-topTenHoldings-mobile tr, tbody.fund-topTenHoldings tr")
    if not have_rows:
        try:
            tab = wait.until(
                EC.presence_of_element_located(
                    (By.XPATH, "//a[contains(normalize-space(.),'Top Holdings') or contains(normalize-space(.),'Holdings')]")
                )
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab)
            time.sleep(0.2)
            try:
                tab.click()
            except Exception:
                driver.execute_script("arguments[0].click();", tab)
            time.sleep(0.4)
        except Exception:
            pass

    wait.until(
        EC.presence_of_all_elements_located(
            (By.CSS_SELECTOR, "tbody.fund-topTenHoldings-mobile tr, tbody.fund-topTenHoldings tr")
        )
    )

    row_nodes = _collect_holdings_rows(driver)
    pairs = _parse_holdings_table_rows(row_nodes, driver)

    # Fallback: try exposed window variables
    if not pairs:
        try:
            js_pairs = driver.execute_script("""
                const candidates = [
                  window.mobile_tables, window.mobileTables,
                  window['mobile_tables'], window['mobileTables']
                ];
                function coerce(val){ return typeof val === 'string' ? val : (val==null ? '' : String(val)); }
                for (const c of candidates) {
                  if (!c) continue;
                  const arr = Array.isArray(c) ? c : (c.topTen || c.holdings || c.data || []);
                  const out = [];
                  (arr || []).forEach(obj => {
                    if (!obj) return;
                    const keys = Object.keys(obj);
                    const kName = keys.find(k => /name/i.test(k));
                    const kWgt  = keys.find(k => /weight/i.test(k));
                    if (kName && kWgt) out.push([coerce(obj[kName]), coerce(obj[kWgt])]);
                  });
                  if (out.length) return out;
                }
                return null;
            """)
            if js_pairs:
                for n, w in js_pairs:
                    m = re.search(r"([0-9]+(?:\\.[0-9]+)?)\\s*%", w)
                    if n and m:
                        pairs.append((n.strip(), float(m.group(1))))
        except Exception:
            pass

    # Save by name
    for name, weight in pairs:
        data.holdings_by_name[name] = weight
    # Map to tickers (case-insensitive names)
    for name, weight in pairs:
        ticker = NAME_TO_TICKER_NORM.get(name.strip().upper())
        if ticker:
            data.holdings[ticker] = weight

    # Page text for NAV + date
    body_text = driver.page_source
    data.nav = _extract_nav_from_text(body_text)
    latest_date = _latest_date_on_page(body_text)
    data.date = latest_date or _last_weekday_str()

    return data

# ---------------------- Live quotes (MAG7) ----------------------

def fetch_mag7_quotes() -> Dict[str, Dict[str, Optional[float]]]:
    """
    Returns {ticker: {'last': float, 'prev_close': float, 'chg': float, 'chg_pct': float}}
    chg = last - prev_close
    chg_pct = 100 * chg / prev_close
    If yfinance isn't installed or a ticker fails, values may be None.
    """
    result: Dict[str, Dict[str, Optional[float]]] = {t: {'last': None, 'prev_close': None, 'chg': None, 'chg_pct': None}
                                                     for t in MAG7_TICKERS}
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        print("Note: live quotes require 'yfinance'. Install with: pip install yfinance")
        return result

    for t in MAG7_TICKERS:
        try:
            yt = yf.Ticker(t)
            last = None
            prev = None

            # Prefer fast_info when available
            fast = getattr(yt, "fast_info", None)
            if fast:
                last = getattr(fast, "last_price", None)
                prev = getattr(fast, "previous_close", None)

            # Fallback to .info keys if needed
            if last is None or prev is None:
                info = {}
                try:
                    info = yt.info or {}
                except Exception:
                    info = {}
                if last is None:
                    last = info.get("regularMarketPrice")
                if prev is None:
                    prev = info.get("regularMarketPreviousClose")

            chg = None
            chg_pct = None
            if last is not None and prev is not None and prev != 0:
                chg = float(last) - float(prev)
                chg_pct = 100.0 * chg / float(prev)

            result[t] = {
                'last': float(last) if last is not None else None,
                'prev_close': float(prev) if prev is not None else None,
                'chg': float(chg) if chg is not None else None,
                'chg_pct': float(chg_pct) if chg_pct is not None else None,
            }
        except Exception:
            # Leave defaults (None)
            pass
    return result

# ---------------------- What-if engine ----------------------

def _normalize(weights: Dict[str, float]) -> Dict[str, float]:
    s = sum(weights.values()) or 1.0
    return {k: (v * 100.0 / s) for k, v in weights.items()}

def _print_2col(rows: List[Tuple[str, str]], headers: Tuple[str, str]):
    col1_w = max(len(headers[0]), *(len(r[0]) for r in rows)) if rows else len(headers[0])
    col2_w = max(len(headers[1]), *(len(r[1]) for r in rows)) if rows else len(headers[1])
    line = f"+-{'-'*col1_w}-+-{'-'*col2_w}-+"
    print(line)
    print(f"| {headers[0]:<{col1_w}} | {headers[1]:<{col2_w}} |")
    print(line)
    for a, b in rows:
        print(f"| {a:<{col1_w}} | {b:<{col2_w}} |")
    print(line)

def _print_quotes_table(rows: List[Tuple[str, str, str, str, str]]):
    headers = ("Ticker", "Weight %", "Last", "Δ $", "Δ %")
    col_w = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            col_w[i] = max(col_w[i], len(cell))
    line = "+-" + "-+-".join("-"*w for w in col_w) + "-+"
    print(line)
    print("| " + " | ".join(h.ljust(col_w[i]) for i, h in enumerate(headers)) + " |")
    print(line)
    for r in rows:
        print("| " + " | ".join(r[i].ljust(col_w[i]) for i in range(len(headers))) + " |")
    print(line)

def _print_whatif_table(rows: List[Tuple[str, str, str, str, str]]):
    headers = ("Holding", "Weight %", "Move %", "Contrib bps", "Contrib %")
    col_w = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            col_w[i] = max(col_w[i], len(cell))
    line = "+-" + "-+-".join("-"*w for w in col_w) + "-+"
    print(line)
    print("| " + " | ".join(h.ljust(col_w[i]) for i, h in enumerate(headers)) + " |")
    print(line)
    for r in rows:
        print("| " + " | ".join(r[i].ljust(col_w[i]) for i in range(len(headers))) + " |")
    print(line)

def _parse_bump_arg(s: str) -> Optional[Tuple[str, float]]:
    # Accept "NVDA:+2", "AAPL=-1.5", "Meta:+0.5%", "ALL:+0.25"
    m = re.match(r"\s*([^:=]+)\s*[:=]\s*([+-]?\d+(?:\.\d+)?)\s*%?\s*$", s or "", flags=re.IGNORECASE)
    if not m:
        return None
    key = m.group(1).strip()
    val = float(m.group(2))
    return key, val

def _load_bumps_from_file(path: str) -> Dict[str, float]:
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    out: Dict[str, float] = {}
    for k, v in obj.items():
        pct = _coerce_percent_to_float(v)
        if pct is not None:
            out[str(k)] = pct
    return out

def compute_nav_what_if(
    data: MAGSData,
    bumps: Dict[str, float],
    assume_nav: Optional[float] = None,
    normalize_weights: bool = False,
):
    """
    bumps keys can be tickers, company names, or 'ALL'.
    Move units are percent (e.g., +2 = +2%).
    """
    # Build working holdings: prefer ticker mapping; if empty, fall back to names
    holdings = data.holdings.copy()
    hold_names = data.holdings_by_name.copy()

    using_names = False
    if not holdings and hold_names:
        using_names = True
        holdings = {name: w for name, w in hold_names.items()}

    # Optional normalization
    weights = holdings.copy()
    if normalize_weights:
        weights = _normalize(weights)

    # Normalize bump keys
    bumps_norm: Dict[str, float] = {}
    for k, v in bumps.items():
        bumps_norm[k.strip().upper()] = float(v)

    rows = []
    total_contrib_pct = 0.0  # percentage points

    for key, w in weights.items():
        ident_upper = key.strip().upper()

        # Resolve bump: ticker and name are both accepted + ALL
        move = bumps_norm.get(ident_upper)
        if move is None:
            # try resolve name->ticker
            t_sym = NAME_TO_TICKER_NORM.get(ident_upper)
            if t_sym:
                move = bumps_norm.get(t_sym)
        if move is None:
            move = bumps_norm.get("ALL", 0.0)

        contrib_pct = (w * move) / 100.0  # e.g., 15% * 2% = 0.30%
        total_contrib_pct += contrib_pct

        rows.append((
            key,
            f"{w:.2f}",
            f"{move:+.2f}",
            f"{w*move:.2f}",
            f"{contrib_pct:.4f}",
        ))

    base_nav = data.nav if data.nav is not None else assume_nav
    new_nav = base_nav * (1.0 + total_contrib_pct / 100.0) if base_nav is not None else None

    return rows, total_contrib_pct, base_nav, new_nav

# ---------------------- CLI ----------------------

def main():
    ap = argparse.ArgumentParser(description="MAGS Exposures (Selenium) — NAV nudger + MAG7 prices")
    ap.add_argument("--headed", "-H", action="store_true", help="Run with visible browser window")
    ap.add_argument("--timeout", type=int, default=30, help="Wait timeout for elements (seconds)")
    ap.add_argument("--width", type=int, default=1400, help="Browser window width")
    ap.add_argument("--height", type=int, default=900, help="Browser window height")

    # NAV nudger controls
    ap.add_argument("--assume-nav", type=float, default=None, help="If NAV not found, assume this NAV")
    ap.add_argument("--normalize-weights", action="store_true",
                    help="Rescale parsed weights to 100% before applying moves")
    ap.add_argument("--bump", action="append", default=[],
                    help='Repeatable. Format: "NVDA:+2" or "AAPL=-1.5" or "Meta:+0.5" or "ALL:+0.25"')
    ap.add_argument("--bump-file", type=str, default=None,
                    help='JSON file like {"NVDA": 2, "AAPL": -1.5, "ALL": 0.25}')

    # Live quotes controls
    ap.add_argument("--no-quotes", action="store_true", help="Skip live MAG7 price fetch")
    ap.add_argument("--use-live-bumps", action="store_true",
                    help="Use fetched MAG7 % changes as scenario bumps (can be overridden by --bump)")

    args = ap.parse_args()

    print("=" * 60)
    print("ROUNDHILL MAGS ETF — MAG7 Snapshot + NAV What-If")
    print("=" * 60 + "\n")
    print(f"Headed: {args.headed} | Timeout: {args.timeout}s | Window: {args.width}x{args.height}\n")

    # Parse bumps from CLI + file
    bumps: Dict[str, float] = {}
    for b in args.bump:
        parsed = _parse_bump_arg(b)
        if parsed:
            k, v = parsed
            bumps[k] = v
        else:
            print(f"Warning: could not parse --bump '{b}' (expected like NVDA:+2)")
    if args.bump_file:
        try:
            bumps_from_file = _load_bumps_from_file(args.bump_file)
            bumps.update(bumps_from_file)
        except Exception as e:
            print(f"Warning: failed to load bump-file '{args.bump_file}': {e}")

    driver = make_driver(
        headless=not args.headed,
        window_size=(args.width, args.height)
    )

    try:
        data = parse_mags_exposures(
            driver=driver,
            timeout=args.timeout,
        )

        # Console summary
        if data.nav is not None:
            print(f"✓ NAV: ${data.nav:.2f}")
        else:
            if args.assume_nav is not None:
                print(f"✗ NAV not found — will assume NAV=${args.assume_nav:.2f} for scenario math")
            else:
                print("✗ NAV: Not found (you can pass --assume-nav 100)")

        print(f"As of: {data.date}")

        # Holdings table (by ticker if mapped, else by name)
        if data.holdings:
            hold_rows = [(t, f"{w:.2f}%") for t, w in sorted(data.holdings.items(), key=lambda x: x[1], reverse=True)]
            print("\nTop Holdings (by ticker):")
            _print_2col(hold_rows, ("Ticker", "Weight"))
        else:
            hold_rows = [(n, f"{w:.2f}%") for n, w in sorted(data.holdings_by_name.items(), key=lambda x: x[1], reverse=True)]
            print("\nTop Holdings (by name):")
            _print_2col(hold_rows, ("Name", "Weight"))

        # Live MAG7 quotes
        live_bumps: Dict[str, float] = {}
        if not args.no_quotes:
            quotes = fetch_mag7_quotes()

            # Compose MAG7 table aligned with parsed weights (if missing, weight shows as '-')
            print("\nMAG7 Price Snapshot:")
            quote_rows: List[Tuple[str, str, str, str, str]] = []
            for t in MAG7_TICKERS:
                w = data.holdings.get(t)
                w_s = f"{w:.2f}" if w is not None else "-"
                q = quotes.get(t, {})
                last = q.get('last')
                chg = q.get('chg')
                chg_pct = q.get('chg_pct')
                quote_rows.append((
                    t,
                    w_s,
                    f"{last:.2f}" if last is not None else "-",
                    f"{chg:+.2f}" if chg is not None else "-",
                    f"{chg_pct:+.2f}%" if chg_pct is not None else "-",
                ))
                if args.use_live_bumps and chg_pct is not None:
                    live_bumps[t] = float(chg_pct)  # percent moves

            _print_quotes_table(quote_rows)

        # What-if section
        combined_bumps = {}
        combined_bumps.update(live_bumps)  # live first
        combined_bumps.update(bumps)       # CLI overrides

        if combined_bumps:
            print("\nWhat-if moves (applied):")
            rows, total_contrib_pct, base_nav, new_nav = compute_nav_what_if(
                data, combined_bumps, assume_nav=args.assume_nav, normalize_weights=args.normalize_weights
            )
            _print_whatif_table(rows)
            print(f"Weighted return: {total_contrib_pct:+.4f}%")
            if base_nav is not None and new_nav is not None:
                delta = new_nav - base_nav
                print(f"Predicted NAV: ${new_nav:.4f}  (Δ ${delta:+.4f}, {total_contrib_pct:+.4f}%)")
            else:
                print("Predicted NAV: (base NAV unknown) — report is % change only")
        else:
            print("\nTip: add moves with --bump (e.g., --bump NVDA:+2) or apply live moves with --use-live-bumps")

    finally:
        try:
            driver.quit()
        except Exception:
            pass

if __name__ == "__main__":
    main()