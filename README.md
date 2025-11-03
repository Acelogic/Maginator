# MAGS ETF NAV Calculator

Interactive Streamlit app to calculate predicted NAV for the Roundhill Magnificent Seven ETF (MAGS) based on real-time price movements of the Mag 7 stocks.

## Features

- **Real-time Data Scraping**: Fetches current holdings and NAV from Roundhill's website
- **Live Stock Quotes**: Gets real-time prices from Yahoo Finance
- **Interactive NAV Calculator**: Edit stock moves to see predicted NAV impact
- **Visual Weight Distribution**: Side-by-side pie charts showing current vs projected weights
- **Flexible Scraping**: Selenium-first with HTTP fallback for reliability

## The Magnificent Seven

- **NVDA** - NVIDIA
- **AAPL** - Apple
- **MSFT** - Microsoft
- **GOOGL** - Alphabet (Google)
- **AMZN** - Amazon
- **META** - Meta Platforms
- **TSLA** - Tesla

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
streamlit run app.py
```

The app will open in your browser at `http://localhost:8501`

## How It Works

### NAV Calculation Formula

```
New NAV = Current NAV × (1 + Weighted Return)

Weighted Return = Σ(weight% × move%) / 100
```

**Example**: If NVDA has 14.28% weight and moves +2%:
- Contribution = 14.28% × 2% = 0.2856%
- If NAV = $100, new NAV = $100 × 1.002856 = $100.29

### Weight Projection

When stocks move by different percentages, their weights shift:
```
New Weight = Old Weight × (1 + move%) / Normalization Factor
```

The projected pie chart shows how the equal-weight structure drifts based on price movements.

## Features Breakdown

### 📊 NAV Calculator Tab
- Edit stock moves in the data editor
- See instant NAV predictions
- View current vs projected weight distribution
- Detailed breakdown of each holding's contribution

### 💹 Live Quotes Tab
- Real-time prices from Yahoo Finance
- Today's change in $ and %
- Current portfolio weights

### ⚙️ Settings Sidebar
- **Auto-fill live moves**: Populate calculator with today's actual moves
- **Normalize weights**: Force weights to sum to 100%
- **Fetch method**: Choose Selenium (reliable) or HTTP (faster)

## About MAGS ETF

The Roundhill Magnificent Seven ETF (Cboe BZX: MAGS):
- Equal-weight exposure to all 7 stocks (~14.28% each)
- Rebalanced quarterly
- 0.29% expense ratio
- Launched April 11, 2023

## Technical Details

### Scraping Methods

1. **Selenium (Default)**: 
   - More reliable for JavaScript-heavy pages
   - Handles cookie banners and dynamic content
   - 45-second timeout with smart retries

2. **HTTP Fallback**:
   - Faster, simpler requests
   - BeautifulSoup HTML parsing
   - Used if Selenium fails

### Data Caching

- Holdings data: 15 minutes
- Stock quotes: 5 minutes
- Prevents rate limiting and improves performance

## Troubleshooting

**"Could not fetch MAGS data"**
- Check your internet connection
- Try switching fetch method in sidebar
- Clear cache with the refresh button

**Stock quotes not loading**
- Yahoo Finance may be rate-limiting
- Wait a few minutes and refresh

**Selenium timeout**
- Website may be slow
- Check if Chrome/Chromium is installed
- Try HTTP-only mode

## Requirements

- Python 3.8+
- Chrome/Chromium (for Selenium)
- Internet connection

## License

MIT

## Disclaimer

This tool is for educational and informational purposes only. It is not financial advice. Always do your own research before making investment decisions. Past performance does not guarantee future results.

MAGS ETF data is sourced from Roundhill Investments. Stock prices from Yahoo Finance. All trademarks are property of their respective owners.
