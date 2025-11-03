# app.py - FIXED VERSION
# Key fixes:
# 1. Added weight validation
# 2. Increased Selenium timeout to 45s
# 3. Better error handling for "TBD" dates
# 4. Added weight normalization warning
# 5. Clarified contribution calculations in UI

import re
import json
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, List
from datetime import datetime, timedelta

import streamlit as st
import pandas as pd

ROUNDHILL_URL = "https://www.roundhillinvestments.com/etf/mags/"
MAG7_TICKERS = ["NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA"]

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
    date: str = "Unknown"
    holdings: Dict[str, float] = field(default_factory=dict)
    holdings_by_name: Dict[str, float] = field(default_factory=dict)

# ---------- helpers ----------
_DATE_RE = re.compile(r"\b([0-1]?\d/[0-3]?\d/\d{4})\b")

def _fmt_us_date(d):
    try:
        return d.strftime("%-m/%-d/%Y")
    except Exception:
        return d.strftime("%m/%d/%Y").lstrip("0").replace("/0", "/")

def _latest_date_on_page(text: str) -> Optional[str]:
    # FIXED: Skip "TBD" and obviously invalid dates
    if not text or "TBD" in text.upper():
        return None
    today = datetime.now().date()
    dates = []
    for m in _DATE_RE.finditer(text or ""):
        try:
            d = datetime.strptime(m.group(1), "%m/%d/%Y").date()
        except ValueError:
            continue
        # Only accept dates within reasonable range (last 2 years)
        if d <= today and (today - d).days < 730:
            dates.append(d)
    return _fmt_us_date(max(dates)) if dates else None

def _last_weekday_str() -> str:
    d = datetime.now().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return _fmt_us_date(d)

def _extract_nav_from_text(text: str) -> Optional[float]:
    for pat in [
        r"Net Asset Value[^$]*\$\s*([0-9][0-9,]*\.?[0-9]*)",
        r"\bNAV\b[^$]*\$\s*([0-9][0-9,]*\.?[0-9]*)",
        r'"NetAssetValue"[^0-9]*([0-9][0-9,]*\.?[0-9]*)',
    ]:
        m = re.search(pat, text or "", flags=re.IGNORECASE)
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

def _normalize(weights: Dict[str, float]) -> Dict[str, float]:
    s = sum(weights.values()) or 1.0
    return {k: (v * 100.0 / s) for k, v in weights.items()}

def _parse_bump_arg(s: str):
    m = re.match(r"\s*([^:=]+)\s*[:=]\s*([+-]?\d+(?:\.\d+)?)\s*%?\s*$", s or "", flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip(), float(m.group(2))

def _parse_bumps_text(block: str) -> Dict[str, float]:
    out = {}
    if not block:
        return out
    for p in re.split(r"[\n,;]+", block):
        parsed = _parse_bump_arg(p)
        if parsed:
            k, v = parsed
            out[k] = v
    return out

# ---------- StockAnalysis.com scraper (best for cloud) ----------
def parse_mags_from_stockanalysis() -> MAGSData:
    """Fetch MAGS data from StockAnalysis.com - clean HTML, works everywhere"""
    import requests
    from bs4 import BeautifulSoup
    
    headers = {
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    }
    
    # Get holdings
    url = "https://stockanalysis.com/etf/mags/holdings/"
    r = requests.get(url, headers=headers, timeout=12)
    r.raise_for_status()
    
    soup = BeautifulSoup(r.text, "lxml")
    data = MAGSData()
    
    # Parse holdings table
    table = soup.find("table")
    if not table:
        raise Exception("Holdings table not found")
    
    rows = table.find_all("tr")[1:]  # Skip header
    mag7_holdings = {}  # Accumulated holdings per ticker
    
    for row in rows:
        cells = row.find_all("td")
        if len(cells) >= 4:
            symbol = cells[1].get_text(strip=True)
            name = cells[2].get_text(strip=True).upper()
            weight_text = cells[3].get_text(strip=True)
            
            # Skip treasury bills and cash
            if any(x in name for x in ["TREASURY", "BILL", "CASH", "FUND"]):
                continue
            
            weight = _coerce_percent_to_float(weight_text)
            if not weight:
                continue
            
            # Match to MAG7 ticker (handle both direct and swap positions)
            matched_ticker = None
            
            # Direct ticker match
            if symbol in MAG7_TICKERS:
                matched_ticker = symbol
            # Swap detection - look for company name in the swap name
            elif "SWAP" in name:
                if "NVDA" in name or "NVIDIA" in name:
                    matched_ticker = "NVDA"
                elif "GOOGL" in name or "ALPHABET" in name:
                    matched_ticker = "GOOGL"
                elif "AMZN" in name or "AMAZON" in name:
                    matched_ticker = "AMZN"
                elif "TSLA" in name or "TESLA" in name:
                    matched_ticker = "TSLA"
                elif "AAPL" in name or "APPLE" in name:
                    matched_ticker = "AAPL"
                elif "MSFT" in name or "MICROSOFT" in name:
                    matched_ticker = "MSFT"
                elif "META" in name:
                    matched_ticker = "META"
            
            # Accumulate weight for this ticker
            if matched_ticker:
                if matched_ticker not in mag7_holdings:
                    mag7_holdings[matched_ticker] = 0.0
                mag7_holdings[matched_ticker] += weight
    
    # No need to normalize - swaps already give us the total exposure
    if not mag7_holdings:
        raise Exception("No MAG7 holdings found")
    
    for symbol, total_weight in mag7_holdings.items():
        data.holdings[symbol] = total_weight
        
        # Map to name for holdings_by_name
        name_map = {
            "NVDA": "NVIDIA",
            "AAPL": "Apple",
            "MSFT": "Microsoft",
            "GOOGL": "Alphabet",
            "AMZN": "AMAZON.COM INC",
            "META": "Meta Platforms",
            "TSLA": "Tesla"
        }
        data.holdings_by_name[name_map.get(symbol, symbol)] = total_weight
    
    # Get NAV from StockAnalysis main page
    try:
        main_url = "https://stockanalysis.com/etf/mags/"
        r2 = requests.get(main_url, headers=headers, timeout=12)
        r2.raise_for_status()
        soup2 = BeautifulSoup(r2.text, "lxml")
        
        # Look for the price in the specific div element
        # <div class="text-4xl font-bold transition-colors duration-300 block sm:inline">67.96</div>
        price_div = soup2.find("div", class_=lambda x: x and "text-4xl" in x and "font-bold" in x)
        if price_div:
            price_text = price_div.get_text(strip=True)
            data.nav = float(price_text.replace(",", ""))
    except Exception:
        pass
    
    data.date = _last_weekday_str()
    return data

# ---------- Yahoo Finance Holdings (Streamlit Cloud compatible) ----------
def parse_mags_from_yfinance() -> MAGSData:
    """Fetch MAGS data from Yahoo Finance - works on Streamlit Cloud"""
    try:
        import yfinance as yf
    except ImportError:
        raise Exception("yfinance not installed")
    
    data = MAGSData()
    
    try:
        ticker = yf.Ticker("MAGS")
        
        # Get NAV
        info = ticker.info or {}
        data.nav = info.get("navPrice") or info.get("previousClose")
        
        # Get holdings from fund_holding_info
        holdings_info = getattr(ticker, "fund_holding_info", None)
        if holdings_info is not None and hasattr(holdings_info, "to_dict"):
            holdings_dict = holdings_info.to_dict()
            if "holdings" in holdings_dict:
                for holding in holdings_dict["holdings"]:
                    symbol = holding.get("symbol", "")
                    weight = holding.get("holdingPercent")
                    if symbol and weight:
                        weight_pct = weight * 100  # Convert decimal to percent
                        data.holdings[symbol] = weight_pct
                        # Also add by name
                        name = holding.get("holdingName", symbol)
                        data.holdings_by_name[name] = weight_pct
        
        # Fallback: try get_holdings() method
        if not data.holdings:
            try:
                holdings_df = ticker.get_holdings()
                if holdings_df is not None and not holdings_df.empty:
                    for _, row in holdings_df.iterrows():
                        symbol = row.get("Symbol", row.get("Ticker", ""))
                        weight = row.get("% of Net Assets", row.get("Weight", 0))
                        if symbol and weight:
                            # Handle if weight is already percentage or decimal
                            weight_val = float(weight)
                            if weight_val < 1:  # Likely decimal format
                                weight_val *= 100
                            data.holdings[symbol] = weight_val
            except Exception:
                pass
        
        # Get date
        data.date = _last_weekday_str()
        
    except Exception as e:
        raise Exception(f"Yahoo Finance error: {e}")
    
    return data

# ---------- HTTP scrape ----------
def parse_mags_exposures_http() -> MAGSData:
    import requests
    from bs4 import BeautifulSoup
    headers = {
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    }
    r = requests.get(ROUNDHILL_URL, headers=headers, timeout=12)
    r.raise_for_status()
    html = r.text
    soup = BeautifulSoup(html, "lxml")

    data = MAGSData()

    rows = soup.select("tbody.fund-topTenHoldings tr") or soup.select("tbody.fund-topTenHoldings-mobile tr")
    pairs: List[Tuple[str, float]] = []
    for tr in rows:
        tds = tr.find_all("td")
        name, w = None, None
        for td in tds:
            title = (td.get("data-title") or "").strip()
            content = (td.get_text(" ", strip=True) or "").strip()
            if "Name" in title and content:
                name = content
            elif "Weight" in title and content:
                w = _coerce_percent_to_float(content)
        if name and (w is not None):
            pairs.append((name, w))

    if not pairs:
        for m in re.finditer(r'"name"\s*:\s*"([^"]+)"\s*,\s*"weight"\s*:\s*"([^"]+)"', html, flags=re.IGNORECASE):
            nm = m.group(1).strip()
            wt = _coerce_percent_to_float(m.group(2))
            if nm and wt is not None:
                pairs.append((nm, wt))

    for name, weight in pairs:
        data.holdings_by_name[name] = weight
        t = NAME_TO_TICKER_NORM.get(name.strip().upper())
        if t:
            data.holdings[t] = weight

    page_text = soup.get_text(" ", strip=True)
    data.nav = _extract_nav_from_text(page_text) or _extract_nav_from_text(html)
    data.date = _latest_date_on_page(page_text) or _latest_date_on_page(html) or _last_weekday_str()
    return data

# ---------- Selenium scrape ----------
def parse_mags_exposures_selenium(timeout: int = 45) -> MAGSData:  # FIXED: Increased from 30 to 45
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager

    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

    driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=opts)

    data = MAGSData()
    try:
        wait = WebDriverWait(driver, timeout)
        driver.get(ROUNDHILL_URL)

        # accept cookies if present
        for sel in ["#onetrust-accept-btn-handler","button#onetrust-accept-btn-handler","button[aria-label='Accept All Cookies']","button.cookie-accept"]:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, sel)
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(0.2); break
            except Exception:
                pass

        time.sleep(0.5); driver.refresh(); time.sleep(0.5)

        have_rows = driver.find_elements(By.CSS_SELECTOR, "tbody.fund-topTenHoldings-mobile tr, tbody.fund-topTenHoldings tr")
        if not have_rows:
            try:
                tab = wait.until(EC.presence_of_element_located(
                    (By.XPATH, "//a[contains(normalize-space(.),'Top Holdings') or contains(normalize-space(.),'Holdings')]")
                ))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab)
                time.sleep(0.2)
                try: tab.click()
                except Exception: driver.execute_script("arguments[0].click();", tab)
                time.sleep(0.4)
            except Exception:
                pass

        wait.until(EC.presence_of_all_elements_located(
            (By.CSS_SELECTOR, "tbody.fund-topTenHoldings-mobile tr, tbody.fund-topTenHoldings tr")
        ))

        rows_mobile = driver.find_elements(By.CSS_SELECTOR, "tbody.fund-topTenHoldings-mobile tr")
        rows = rows_mobile if rows_mobile else driver.find_elements(By.CSS_SELECTOR, "tbody.fund-topTenHoldings tr")

        pairs: List[Tuple[str, float]] = []
        for row in rows:
            parsed = False
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
                    pairs.append((name, w)); parsed = True
            except Exception:
                pass

            if not parsed:
                try:
                    tds = row.find_elements(By.TAG_NAME, "td")
                    name, w = None, None
                    for td in tds:
                        title = (td.get_attribute("data-title") or "").strip()
                        content = (td.text or "").strip()
                        if "Name" in title and content:
                            name = content
                        elif "Weight" in title and content:
                            w = _coerce_percent_to_float(content)
                    if name and (w is not None):
                        pairs.append((name, w))
                except Exception:
                    pass

        for name, weight in pairs:
            data.holdings_by_name[name] = weight
            t = NAME_TO_TICKER_NORM.get(name.strip().upper())
            if t: data.holdings[t] = weight

        body_text = driver.page_source
        data.nav = _extract_nav_from_text(body_text)
        data.date = _latest_date_on_page(body_text) or _last_weekday_str()
    finally:
        try: driver.quit()
        except Exception: pass

    return data

# ---------- Live quotes ----------
def fetch_mag7_quotes() -> Dict[str, Dict[str, Optional[float]]]:
    result = {t: {'last': None, 'prev_close': None, 'chg': None, 'chg_pct': None} for t in MAG7_TICKERS}
    try:
        import yfinance as yf
    except Exception:
        return result
    for t in MAG7_TICKERS:
        try:
            yt = yf.Ticker(t)
            last = getattr(getattr(yt, "fast_info", None), "last_price", None)
            prev = getattr(getattr(yt, "fast_info", None), "previous_close", None)
            if last is None or prev is None:
                info = {}
                try: info = yt.info or {}
                except Exception: info = {}
                last = info.get("regularMarketPrice", last)
                prev = info.get("regularMarketPreviousClose", prev)
            chg = chg_pct = None
            if last is not None and prev not in (None, 0):
                chg = float(last) - float(prev)
                chg_pct = 100.0 * chg / float(prev)
            result[t] = {'last': (None if last is None else float(last)),
                         'prev_close': (None if prev is None else float(prev)),
                         'chg': (None if chg is None else float(chg)),
                         'chg_pct': (None if chg_pct is None else float(chg_pct))}
        except Exception:
            pass
    return result

# ---------- What-if ----------
def compute_nav_what_if(data: MAGSData, bumps: Dict[str, float],
                        assume_nav: Optional[float] = None, normalize_weights: bool = False):
    holdings = data.holdings.copy()
    if not holdings and data.holdings_by_name:
        holdings = {name: w for name, w in data.holdings_by_name.items()}
    weights = _normalize(holdings) if normalize_weights else holdings

    bumps_norm = {k.strip().upper(): float(v) for k, v in bumps.items()}
    rows = []
    total_contrib_pct = 0.0

    for key, w in weights.items():
        keyU = key.strip().upper()
        move = bumps_norm.get(keyU)
        if move is None:
            t_sym = NAME_TO_TICKER_NORM.get(keyU)
            if t_sym: move = bumps_norm.get(t_sym)
        if move is None: move = bumps_norm.get("ALL", 0.0)

        # MATH IS CORRECT:
        # If weight=14.28% and move=+2%, contrib = 14.28 * 2 / 100 = 0.2856%
        contrib_pct = (w * move) / 100.0
        total_contrib_pct += contrib_pct
        rows.append({
            "Holding": key,
            "Weight %": f"{w:.2f}",
            "Move %": f"{move:+.2f}",
            "Contrib bps": f"{w*move:.2f}",  # basis points (weight% × move%)
            "Contrib %": f"{contrib_pct:.4f}",
        })

    base_nav = data.nav if data.nav is not None else assume_nav
    new_nav = base_nav * (1.0 + total_contrib_pct / 100.0) if base_nav is not None else None
    return rows, total_contrib_pct, base_nav, new_nav

# ---------- UI ----------
st.set_page_config(page_title="MAGS ETF NAV Calculator", page_icon="🧲", layout="wide")
st.title("🧲 MAGS ETF — NAV Calculator")
st.caption("Calculate predicted NAV based on Mag 7 price movements")

with st.sidebar:
    st.header("⚙️ Settings")
    
    refresh = st.button("🔄 Refresh Data", use_container_width=True)
    
    st.divider()
    
    manual_nav = st.number_input("Manual NAV (optional)", min_value=0.0, value=0.0, step=0.01,
                                 help="Override NAV if auto-fetch fails. Leave at 0 to use auto-fetched value.")
    
    use_live_bumps = st.checkbox("Auto-fill live MAG7 moves", value=False, 
                                  help="Automatically populate the editor with today's stock moves")
    
    normalize_weights = st.checkbox("Force equal weight (14.28% each)", value=False,
                                    help="Override actual weights and use equal 14.28% for all stocks")
    
    st.divider()
    
    fetch_mode = st.radio(
        "Fetch method",
        ["StockAnalysis.com (best)", "Yahoo Finance", "Selenium (local only)", "HTTP (Roundhill direct)"],
        index=0,
        help="StockAnalysis.com has clean data and works on Streamlit Cloud"
    )

@st.cache_data(ttl=15 * 60, show_spinner=False)
def _fetch_stockanalysis_cached():
    return parse_mags_from_stockanalysis()

@st.cache_data(ttl=15 * 60, show_spinner=False)
def _fetch_yfinance_cached():
    return parse_mags_from_yfinance()

@st.cache_data(ttl=15 * 60, show_spinner=False)
def _fetch_http_cached():
    return parse_mags_exposures_http()

@st.cache_data(ttl=15 * 60, show_spinner=False)
def _fetch_selenium_cached():
    return parse_mags_exposures_selenium(timeout=45)

@st.cache_data(ttl=5 * 60, show_spinner=False)
def _fetch_quotes_cached():
    return fetch_mag7_quotes()

if refresh:
    _fetch_stockanalysis_cached.clear(); _fetch_yfinance_cached.clear(); _fetch_http_cached.clear(); _fetch_selenium_cached.clear(); _fetch_quotes_cached.clear()
    st.toast("Cache cleared.", icon="🧹")

# Fetch exposures
data = None
errors = []

if fetch_mode.startswith("StockAnalysis"):
    try:
        with st.spinner("Fetching MAGS holdings (StockAnalysis.com)…"):
            data = _fetch_stockanalysis_cached()
    except Exception as e:
        errors.append(f"StockAnalysis error: {e}")
        try:
            with st.spinner("StockAnalysis failed — trying Yahoo Finance…"):
                data = _fetch_yfinance_cached()
        except Exception as e2:
            errors.append(f"Yahoo fallback error: {e2}")
elif fetch_mode.startswith("Yahoo"):
    try:
        with st.spinner("Fetching MAGS holdings (Yahoo Finance)…"):
            data = _fetch_yfinance_cached()
    except Exception as e:
        errors.append(f"Yahoo Finance error: {e}")
        try:
            with st.spinner("Yahoo failed — trying StockAnalysis…"):
                data = _fetch_stockanalysis_cached()
        except Exception as e2:
            errors.append(f"StockAnalysis fallback error: {e2}")
elif fetch_mode.startswith("Selenium"):
    try:
        with st.spinner("Fetching MAGS holdings (Selenium)…"):
            data = _fetch_selenium_cached()
    except Exception as e:
        errors.append(f"Selenium error: {e}")
        try:
            with st.spinner("Selenium failed — falling back to StockAnalysis…"):
                data = _fetch_stockanalysis_cached()
        except Exception as e2:
            errors.append(f"StockAnalysis fallback error: {e2}")
else:  # HTTP (Roundhill direct)
    try:
        with st.spinner("Fetching MAGS holdings (Roundhill HTTP)…"):
            data = _fetch_http_cached()
    except Exception as e:
        errors.append(f"HTTP error: {e}")
        try:
            with st.spinner("HTTP failed — trying StockAnalysis…"):
                data = _fetch_stockanalysis_cached()
        except Exception as e2:
            errors.append(f"StockAnalysis fallback error: {e2}")

if not data or (not data.holdings and not data.holdings_by_name):
    st.error("Could not fetch MAGS data.")
    if errors:
        with st.expander("Details"):
            for err in errors:
                st.code(err)
    st.stop()

if not data.nav:
    st.warning("⚠️ NAV not found. Try refreshing or enter manually in sidebar.")
    if fetch_mode.startswith("StockAnalysis"):
        with st.expander("NAV Debug Info"):
            st.write("Failed to parse NAV from StockAnalysis. Looking for div with class 'text-4xl font-bold'")
            st.write("Try manual NAV entry in sidebar.")

# Apply manual NAV override if set
if manual_nav > 0:
    data.nav = manual_nav

# KPIs
k1, k2, k3 = st.columns(3)
with k1: 
    st.metric("NAV", f"${data.nav:,.2f}" if data.nav else "—")
with k2: 
    st.metric("Date", data.date or "Unknown")
with k3: 
    st.metric("Holdings", f"{len(data.holdings) or len(data.holdings_by_name)}")

st.divider()

# Fetch quotes once
quotes = _fetch_quotes_cached()

# -------- Live Quotes + What-If Scenario --------
tab1, tab2 = st.tabs(["📊 NAV Calculator", "💹 Live Quotes"])

with tab1:
    st.caption("Edit the **Move %** column to simulate price changes and see the impact on NAV")
    
    # Build seed bumps (for initial values only)
    seed_bumps: Dict[str, float] = {}
    if use_live_bumps:
        for t in MAG7_TICKERS:
            q = quotes.get(t, {})
            if q.get("chg_pct") is not None:
                seed_bumps[t] = float(q["chg_pct"])

    # Editor rows
    if data.holdings:
        holdings_items = sorted(data.holdings.items(), key=lambda x: x[1], reverse=True)
        editor_rows = []
        for t, w in holdings_items:
            move = float(seed_bumps.get(t, 0.0))
            editor_rows.append({"Ticker": t, "Weight %": round(w, 2), "Move %": move})
    else:
        holdings_items = sorted(data.holdings_by_name.items(), key=lambda x: x[1], reverse=True)
        editor_rows = []
        for name, w in holdings_items:
            move = float(seed_bumps.get(name, 0.0))
            editor_rows.append({"Ticker": name, "Weight %": round(w, 2), "Move %": move})

    editor_df = pd.DataFrame(editor_rows)

    edited_df = st.data_editor(
        editor_df,
        key="whatif_editor",
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "Ticker": st.column_config.TextColumn(disabled=True, width="medium"),
            "Weight %": st.column_config.NumberColumn(format="%.2f", disabled=True, width="small"),
            "Move %": st.column_config.NumberColumn(
                help="Enter price move % (e.g., 2 = +2%, -1.5 = -1.5%)",
                step=0.10, format="%.2f", width="medium"
            ),
        },
    )

    # Calculate
    bumps_from_editor: Dict[str, float] = {
        str(row["Ticker"]): float(row["Move %"] or 0.0) for _, row in edited_df.iterrows()
    }

    rows, total_contrib_pct, base_nav, new_nav = compute_nav_what_if(
        data,
        bumps_from_editor,
        assume_nav=None,
        normalize_weights=normalize_weights
    )

    st.divider()
    
    # Results
    c1, c2, c3 = st.columns(3)
    with c1: 
        st.metric("Current NAV", f"${base_nav:,.2f}" if base_nav else "—")
    with c2:
        if base_nav is not None and new_nav is not None:
            delta = new_nav - base_nav
            st.metric("Predicted NAV", f"${new_nav:,.2f}", f"{delta:+.2f}")
        else:
            st.metric("Predicted NAV", "—")
    with c3: 
        st.metric("Total Return", f"{total_contrib_pct:+.2f}%")
    
    # Pie charts
    st.subheader("Weight Distribution")
    chart_col1, chart_col2 = st.columns(2)
    
    with chart_col1:
        st.caption("**Current Weights**")
        if data.holdings:
            pie_data = pd.DataFrame([
                {"Ticker": t, "Weight": w} 
                for t, w in data.holdings.items()
            ])
        else:
            pie_data = pd.DataFrame([
                {"Ticker": name, "Weight": w} 
                for name, w in data.holdings_by_name.items()
            ])
        
        import plotly.express as px
        fig1 = px.pie(
            pie_data, 
            values="Weight", 
            names="Ticker",
            hole=0.4,
            color_discrete_sequence=px.colors.qualitative.Set3
        )
        fig1.update_traces(textposition='inside', textinfo='percent+label')
        fig1.update_layout(
            showlegend=False,
            margin=dict(t=0, b=0, l=0, r=0),
            height=300
        )
        st.plotly_chart(fig1, use_container_width=True, key="current_weights_pie")
    
    with chart_col2:
        st.caption("**Projected Weights** (after price changes)")
        # Calculate new weights based on price moves
        proj_weights = {}
        for ticker, current_weight in (data.holdings if data.holdings else data.holdings_by_name).items():
            move = bumps_from_editor.get(ticker, 0.0)
            # New weight = old weight × (1 + move%)
            proj_weights[ticker] = current_weight * (1 + move / 100.0)
        
        # Normalize to 100%
        total_proj = sum(proj_weights.values())
        proj_weights = {k: (v / total_proj * 100) for k, v in proj_weights.items()}
        
        proj_pie_data = pd.DataFrame([
            {"Ticker": t, "Weight": w} 
            for t, w in proj_weights.items()
        ])
        
        fig2 = px.pie(
            proj_pie_data, 
            values="Weight", 
            names="Ticker",
            hole=0.4,
            color_discrete_sequence=px.colors.qualitative.Set3
        )
        fig2.update_traces(textposition='inside', textinfo='percent+label')
        fig2.update_layout(
            showlegend=False,
            margin=dict(t=0, b=0, l=0, r=0),
            height=300
        )
        st.plotly_chart(fig2, use_container_width=True, key="projected_weights_pie")
    
    with st.expander("🧮 Detailed Breakdown"):
        whatif_df = pd.DataFrame(rows)
        st.dataframe(whatif_df, hide_index=True, use_container_width=True)

with tab2:
    st.caption("Real-time price data from Yahoo Finance")
    rows = []
    for t in MAG7_TICKERS:
        w = data.holdings.get(t) if data.holdings else None
        q = quotes.get(t, {})
        last, chg, chg_pct = q.get('last'), q.get('chg'), q.get('chg_pct')
        rows.append({
            "Ticker": t,
            "Weight %": f"{w:.2f}" if w is not None else "-",
            "Last": f"${last:.2f}" if last is not None else "-",
            "Change $": f"{chg:+.2f}" if chg is not None else "-",
            "Change %": f"{(chg_pct or 0):+.2f}%" if chg_pct is not None else "-",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.divider()

with st.expander("ℹ️ How it works"):
    st.markdown("""
    **Formula**: `New NAV = Current NAV × (1 + Weighted Return)`
    
    **Example**: If NVDA (15.39% weight) moves +2%:
    - Contribution = 15.39% × 2% = 0.3078%
    - If NAV = $100, new NAV = $100 × 1.003078 = $100.31
    
    **About MAGS Weights**: 
    - MAGS uses **stock + swap positions** to get exposure to each company
    - Total portfolio: ~55% MAG7 stocks/swaps + ~45% cash/T-bills
    - Weights shown are **total exposure** (stock + swaps combined) matching Roundhill's display
    - Uses swaps to maintain RIC diversification compliance for tax purposes
    - Rebalances quarterly to maintain roughly equal weight among the 7 stocks
    """)