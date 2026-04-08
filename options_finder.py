#   BSD 3-Clause License
#
#   Copyright (c) 2023-Present, Prem Patel
#
#   Redistribution and use in source and binary forms, with or without
#   modification, are permitted provided that the following conditions are met:
#
#   1. Redistributions of source code must retain the above copyright notice, this
#      list of conditions and the following disclaimer.
#
#   2. Redistributions in binary form must reproduce the above copyright notice,
#      this list of conditions and the following disclaimer in the documentation
#      and/or other materials provided with the distribution.
#
#   3. Neither the name of the copyright holder nor the names of its
#      contributors may be used to endorse or promote products derived from
#      this software without specific prior written permission.
#
#   THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#   AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#   IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#   DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
#   FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
#   DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
#   SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
#   CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
#   OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#   OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""
Options Finder - Cash-Secured Puts Pricing Advantage Scanner

Identifies advantageously-priced cash-secured put opportunities by finding
NON penny-increment eligible options (minimum $0.05 tick) where the floor
price creates inflated premium relative to fair value.

Usage:
    python options_finder.py                    # Run with .env defaults
    python options_finder.py --max-price 5.00   # Override max underlying price
    python options_finder.py --max-dte 30       # Override max days to expiration
    python options_finder.py --dry-run          # Screen only, skip Slack alert
"""

import argparse
import csv
import io
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta

import requests
import robin_stocks.robinhood as rh
from dotenv import load_dotenv

load_dotenv()

CBOE_SYMBOL_REF_URL = "https://cdn.cboe.com/data/us/options/market_statistics/symbol_reference/cone-underlying.csv"

# Rate limit: pause between Robinhood API calls to avoid throttling
API_DELAY = 0.25


def fetch_nickel_tick_tickers():
    """Fetch the CBOE symbol reference CSV and return tickers with nickel ($0.05) tick type."""
    print("Fetching CBOE symbol reference data...")
    try:
        resp = requests.get(CBOE_SYMBOL_REF_URL, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Error fetching CBOE data: {e}")
        return []

    reader = csv.DictReader(io.StringIO(resp.text))

    # Normalize header names (strip whitespace)
    if reader.fieldnames:
        reader.fieldnames = [f.strip() for f in reader.fieldnames]

    nickel_tickers = []
    for row in reader:
        tick_type = row.get("Tick Type", "").strip().lower()
        symbol = row.get("Symbol", "").strip()
        if tick_type == "nickel" and symbol:
            nickel_tickers.append(symbol)

    print(f"Found {len(nickel_tickers)} nickel-tick tickers from CBOE data.")
    return nickel_tickers


def login_robinhood():
    """Authenticate with Robinhood via robin_stocks."""
    username = os.environ.get("ROBINHOOD_USERNAME", "")
    password = os.environ.get("ROBINHOOD_PASSWORD", "")
    mfa_code = os.environ.get("ROBINHOOD_MFA", "") or None

    if not username or not password:
        print("Error: ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD must be set in .env")
        sys.exit(1)

    print("Logging in to Robinhood...")
    try:
        rh.login(
            username=username,
            password=password,
            mfa_code=mfa_code,
            store_session=True,
        )
        print("Robinhood login successful.")
    except Exception as e:
        print(f"Robinhood login failed: {e}")
        sys.exit(1)


def filter_by_price(tickers, max_price):
    """Filter tickers to those with underlying price at or below max_price.

    Batches requests to Robinhood in groups to stay within rate limits.
    Returns list of (ticker, current_price) tuples.
    """
    print(f"Filtering {len(tickers)} tickers by price <= ${max_price:.2f}...")
    qualifying = []
    batch_size = 25

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i : i + batch_size]
        try:
            prices = rh.stocks.get_latest_price(batch)
        except Exception as e:
            print(f"  Warning: Failed to fetch prices for batch starting at {batch[0]}: {e}")
            continue

        for ticker, price_str in zip(batch, prices):
            if price_str is None:
                continue
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                continue
            if 0 < price <= max_price:
                qualifying.append((ticker, price))

        if i + batch_size < len(tickers):
            time.sleep(API_DELAY)

    print(f"Found {len(qualifying)} tickers priced at or below ${max_price:.2f}.")
    return qualifying


def get_expiration_dates_within(ticker, max_dte):
    """Get available options expiration dates for a ticker within max_dte days."""
    cutoff = (datetime.now() + timedelta(days=max_dte)).strftime("%Y-%m-%d")
    try:
        chains = rh.options.get_chains(ticker)
        if not chains or "expiration_dates" not in chains:
            return []
        dates = [d for d in chains["expiration_dates"] if d <= cutoff]
        return dates
    except Exception:
        return []


def scan_options_chains(qualifying_tickers, max_dte, min_oi):
    """Scan options chains for qualifying put opportunities.

    Looks for puts where ask_price is at the $0.05 minimum tick floor,
    indicating the true value may be lower but the tick size forces
    the price to the minimum tradeable increment.

    Returns list of opportunity dicts.
    """
    print(f"\nScanning options chains for {len(qualifying_tickers)} tickers...")
    opportunities = []
    total = len(qualifying_tickers)

    for idx, (ticker, current_price) in enumerate(qualifying_tickers, 1):
        print(f"  [{idx}/{total}] Scanning {ticker} (${current_price:.2f})...", end="")
        sys.stdout.flush()

        exp_dates = get_expiration_dates_within(ticker, max_dte)
        if not exp_dates:
            print(" no expirations found.")
            continue

        ticker_hits = 0
        for exp_date in exp_dates:
            time.sleep(API_DELAY)
            try:
                options = rh.options.find_tradable_options(
                    ticker,
                    expirationDate=exp_date,
                    optionType="put",
                )
            except Exception as e:
                print(f" error: {e}")
                continue

            if not options:
                continue

            for opt in options:
                try:
                    ask = float(opt.get("ask_price") or 0)
                    bid = float(opt.get("bid_price") or 0)
                    last = float(opt.get("last_trade_price") or 0)
                    strike = float(opt.get("strike_price") or 0)
                    oi = int(float(opt.get("open_interest") or 0))
                    iv = float(opt.get("implied_volatility") or 0)
                    mark = float(opt.get("mark_price") or opt.get("adjusted_mark_price") or 0)
                except (ValueError, TypeError):
                    continue

                # Only interested in puts where the ask is at the $0.05 floor
                if ask != 0.05:
                    continue

                # Skip if open interest is below minimum
                if oi < min_oi:
                    continue

                # Skip deep ITM puts (strike much higher than price)
                if strike > current_price * 1.5:
                    continue

                days_to_exp = (datetime.strptime(exp_date, "%Y-%m-%d") - datetime.now()).days
                if days_to_exp <= 0:
                    continue

                collateral = strike * 100
                premium = 5.00  # $0.05 * 100 shares
                return_pct = (premium / collateral * 100) if collateral > 0 else 0
                annualized_return = return_pct * (365 / days_to_exp)

                # Score: annualized return weighted by liquidity
                score = annualized_return * math.log1p(oi)

                opportunities.append({
                    "ticker": ticker,
                    "strike": strike,
                    "expiration": exp_date,
                    "bid": bid,
                    "ask": ask,
                    "last": last,
                    "mark": mark,
                    "iv": iv,
                    "open_interest": oi,
                    "underlying_price": current_price,
                    "collateral": collateral,
                    "premium": premium,
                    "return_pct": return_pct,
                    "annualized_return": annualized_return,
                    "days_to_exp": days_to_exp,
                    "score": score,
                })
                ticker_hits += 1

        print(f" {ticker_hits} opportunities.")

    opportunities.sort(key=lambda x: x["score"], reverse=True)
    return opportunities


def format_results(opportunities, max_results):
    """Format opportunities as a readable console table."""
    if not opportunities:
        return "No opportunities found matching criteria."

    display = opportunities[:max_results]
    header = (
        f"{'Ticker':<7} {'Strike':>7} {'Exp':>11} {'Bid':>6} {'Ask':>6} "
        f"{'Last':>6} {'Price':>7} {'Collat':>8} {'Ret%':>6} {'AnnRet%':>8} {'OI':>6} {'Score':>7}"
    )
    sep = "-" * len(header)
    lines = ["\nCSP Opportunities (Nickel Tick Advantage)\n", header, sep]

    for o in display:
        lines.append(
            f"{o['ticker']:<7} ${o['strike']:>5.2f} {o['expiration']:>11} "
            f"${o['bid']:>4.2f} ${o['ask']:>4.2f} ${o['last']:>4.2f} "
            f"${o['underlying_price']:>5.2f} ${o['collateral']:>7.0f} "
            f"{o['return_pct']:>5.2f}% {o['annualized_return']:>7.1f}% "
            f"{o['open_interest']:>6} {o['score']:>7.1f}"
        )

    lines.append(sep)
    lines.append(f"Showing {len(display)} of {len(opportunities)} total opportunities.")
    return "\n".join(lines)


def send_slack_alert(opportunities, webhook_url, max_results):
    """Send top opportunities to Slack via incoming webhook."""
    if not webhook_url:
        print("No Slack webhook URL configured. Skipping alert.")
        return

    display = opportunities[:max_results]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build markdown table rows
    rows = ["*Ticker* | *Strike* | *Exp* | *Ask* | *Price* | *Collat* | *Return* | *Ann.Ret* | *OI*"]
    for o in display:
        rows.append(
            f"`{o['ticker']}` | ${o['strike']:.2f} | {o['expiration']} | "
            f"${o['ask']:.2f} | ${o['underlying_price']:.2f} | "
            f"${o['collateral']:.0f} | {o['return_pct']:.2f}% | "
            f"{o['annualized_return']:.1f}% | {o['open_interest']}"
        )

    table_text = "\n".join(rows)

    payload = {
        "text": f"Options Finder: {len(opportunities)} CSP opportunities found",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"CSP Opportunities Found ({len(opportunities)} total)",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Scanned at {timestamp}\n"
                        f"Showing top {len(display)} by score (annualized return * liquidity)\n\n"
                        f"{table_text}"
                    ),
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "Strategy: Sell CSPs on nickel-tick stocks where $0.05 min tick "
                            "creates premium advantage. Collateral = strike * 100 shares."
                        ),
                    }
                ],
            },
        ],
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200:
            print("Slack alert sent successfully.")
        else:
            print(f"Slack alert failed (HTTP {resp.status_code}): {resp.text}")
    except requests.RequestException as e:
        print(f"Slack alert error: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Options Finder - Cash-Secured Puts Pricing Advantage Scanner"
    )
    parser.add_argument(
        "--max-price",
        type=float,
        default=float(os.environ.get("MAX_UNDERLYING_PRICE", 10.0)),
        help="Maximum underlying stock price to consider (default: 10.00)",
    )
    parser.add_argument(
        "--max-dte",
        type=int,
        default=int(os.environ.get("MAX_DTE", 45)),
        help="Maximum days to expiration (default: 45)",
    )
    parser.add_argument(
        "--min-oi",
        type=int,
        default=int(os.environ.get("MIN_OPEN_INTEREST", 1)),
        help="Minimum open interest (default: 1)",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=int(os.environ.get("MAX_RESULTS", 25)),
        help="Maximum results to display/alert (default: 25)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Screen only, skip Slack webhook alert",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  Options Finder - CSP Pricing Advantage Scanner")
    print("=" * 60)
    print(f"  Max underlying price: ${args.max_price:.2f}")
    print(f"  Max DTE:              {args.max_dte} days")
    print(f"  Min open interest:    {args.min_oi}")
    print(f"  Max results:          {args.max_results}")
    print(f"  Dry run:              {args.dry_run}")
    print("=" * 60)

    # Step 1: Fetch nickel-tick tickers from CBOE
    nickel_tickers = fetch_nickel_tick_tickers()
    if not nickel_tickers:
        print("No nickel-tick tickers found. Exiting.")
        sys.exit(1)

    # Step 2: Authenticate with Robinhood
    login_robinhood()

    # Step 3: Filter by underlying price
    qualifying = filter_by_price(nickel_tickers, args.max_price)
    if not qualifying:
        print("No tickers found within price range. Exiting.")
        rh.logout()
        sys.exit(0)

    # Step 4: Scan options chains for $0.05 floor puts
    opportunities = scan_options_chains(qualifying, args.max_dte, args.min_oi)

    # Step 5: Display results
    output = format_results(opportunities, args.max_results)
    print(output)

    # Step 6: Send Slack alert (unless dry run)
    if not args.dry_run and opportunities:
        webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
        send_slack_alert(opportunities, webhook_url, args.max_results)
    elif not opportunities:
        print("\nNo opportunities found. No alert sent.")

    rh.logout()
    print("\nDone.")


if __name__ == "__main__":
    main()
