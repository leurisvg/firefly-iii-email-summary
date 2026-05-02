#!/usr/bin/env python3

"""
Firefly III Monthly Email Report Generator

This script generates a beautiful HTML email report from your Firefly III instance
containing category summaries, budget tracking, and financial overview.

Requirements:
    - Python 3.7+
    - Required packages: pyyaml, requests, beautifulsoup4
    - A running Firefly III instance with API access
    - SMTP server credentials for sending emails

Usage:
    1. Copy config-template.yaml to config.yaml
    2. Fill in your Firefly III URL, API token, and SMTP settings
    3. Run: python3 monthly-report.py
    4. Preview mode: python3 monthly-report.py --preview (generates preview.html)

Author: Community contribution
License: MIT
"""

import yaml
import sys
import traceback
import datetime
import requests
import re
import bs4
import ssl
import smtplib
import os
import argparse
import json
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from email.mime.image import MIMEImage

from email.message import EmailMessage
from email.headerregistry import Address
from email.utils import make_msgid


def fetch_exchange_rates(base_currency, foreign_currencies):
    """Fetch exchange rates (free, no API key required).
    Returns {foreign_currency: rate} where rate = units of base_currency per 1 foreign unit.
    Tries open.er-api.com first, falls back to frankfurter.app.
    """
    if not foreign_currencies:
        return {}

    def _parse_rates(data, foreign_currencies):
        raw_rates = data.get("rates", {})  # raw_rates[X] = units of X per 1 BASE
        inverted = {}
        for currency, rate in raw_rates.items():
            if currency in foreign_currencies and rate != 0:
                inverted[currency] = 1.0 / rate  # 1 FOREIGN = (1/rate) BASE units
        return inverted

    endpoints = [
        f"https://open.er-api.com/v6/latest/{base_currency}",
        f"https://api.frankfurter.app/latest?from={base_currency}",
    ]
    for url in endpoints:
        try:
            print(f"Trying {url} ...")
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if "rates" in data:
                rates = _parse_rates(data, foreign_currencies)
                print(f"Rates fetched from {url.split('/')[2]}:")
                for cur, rate in rates.items():
                    print(f"      1 {cur} = {rate:.6f} {base_currency}")
                return rates
            else:
                print(f"   ⚠️  Response missing 'rates' key: {data}")
        except Exception as e:
            print(f"   ❌ Failed: {e}")
            continue

    print("⚠️  Warning: Could not fetch exchange rates from any provider.")
    print("   Proceeding without currency conversion (using rate 1.0)...")
    return {}


def convert_amount(amount, from_currency, to_currency, rates):
    """Convert amount to to_currency using rates dict.
    rates[from_currency] = units of to_currency per 1 unit of from_currency.
    Returns (converted_amount, rate_used).
    """
    if from_currency == to_currency:
        return float(amount), 1.0
    rate = rates.get(from_currency)
    if rate is None:
        print(f"⚠️  Warning: No exchange rate for {from_currency}, using 1.0")
        return float(amount), 1.0
    return float(amount) * rate, rate


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Generate Firefly III monthly report")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Generate preview.html instead of sending email",
    )
    parser.add_argument("--month", type=int, help="Month number (1–12) for the report")
    parser.add_argument("--year", type=int, help="Four-digit year for the report")
    args = parser.parse_args()

    # Get the directory where this script is located
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, "config.yaml")

    # Load configuration safely
    try:
        with open(config_path, "r") as configFile:
            config = yaml.safe_load(configFile)
    except Exception:
        traceback.print_exc()
        print(f"ERROR: could not load config.yaml from {config_path}")
        sys.exit(1)
    except Exception as e:
        traceback.print_exc()
        print("ERROR: could not load config.yaml")
        sys.exit(1)

    # Validate required configuration
    required_fields = ["firefly-url", "accesstoken", "smtp", "email"]
    for field in required_fields:
        if field not in config:
            print(f"ERROR: Missing required field '{field}' in config.yaml")
            sys.exit(1)

    multi_currency_mode = "base_currency" in config

    #
    # Determine the applicable date range
    today = datetime.date.today()
    if args.month or args.year:
        if not (args.month and args.year):
            print("ERROR: --month and --year must be provided together")
            sys.exit(1)
        if not (1 <= args.month <= 12):
            print("ERROR: --month must be between 1 and 12")
            sys.exit(1)
        startDate = datetime.date(args.year, args.month, 1)
    else:
        startDate = (today.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)

    next_month_first = (startDate.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    endDate = next_month_first - datetime.timedelta(days=1)
    monthName = startDate.strftime("%B")

    print(f"Generating report for {monthName} {startDate.strftime('%Y')}...")

    #
    # Set us up for API requests
    HEADERS = {
        "Authorization": "Bearer {}".format(config["accesstoken"]),
        "Accept": "application/json",
    }
    with requests.Session() as s:
        s.headers.update(HEADERS)

        # Test API connection
        try:
            test_response = s.get(config["firefly-url"] + "/api/v1/about")
            if test_response.status_code != 200:
                print(
                    f"ERROR: Cannot connect to Firefly III API. Status code: {test_response.status_code}"
                )
                sys.exit(1)
        except Exception as e:
            print(f"ERROR: Cannot reach Firefly III instance: {e}")
            sys.exit(1)

        #
        # Previous month date range (for month-over-month comparison)
        prev_end = startDate - datetime.timedelta(days=1)
        prev_start = prev_end.replace(day=1)
        #
        # Get all the categories
        print("Fetching categories...")
        url = config["firefly-url"] + "/api/v1/categories"
        categories = s.get(url).json()
        #
        # Get the spent and earned totals for each category (current + previous month)
        totals = []
        for category in categories["data"]:
            cat_id = category["id"]

            def _fetch_cat(start, end):
                u = (
                    config["firefly-url"]
                    + "/api/v1/categories/"
                    + cat_id
                    + "?start=" + start.strftime("%Y-%m-%d")
                    + "&end=" + end.strftime("%Y-%m-%d")
                )
                return s.get(u).json()

            r = _fetch_cat(startDate, endDate)
            r_prev = _fetch_cat(prev_start, prev_end)
            categoryName = r["data"]["attributes"]["name"]

            def _parse_cat_entries(data):
                if multi_currency_mode:
                    spent = [
                        {"amount": float(e["sum"]), "currency": e.get("currency_code", "")}
                        for e in data["data"]["attributes"].get("spent", [])
                    ]
                    earned = [
                        {"amount": float(e["sum"]), "currency": e.get("currency_code", "")}
                        for e in data["data"]["attributes"].get("earned", [])
                    ]
                    return spent, earned, sum(e["amount"] for e in spent), sum(e["amount"] for e in earned)
                else:
                    try:
                        s_amt = float(data["data"]["attributes"]["spent"][0]["sum"])
                    except (KeyError, IndexError):
                        s_amt = 0
                    try:
                        e_amt = float(data["data"]["attributes"]["earned"][0]["sum"])
                    except (KeyError, IndexError):
                        e_amt = 0
                    return [], [], s_amt, e_amt

            spent_entries_raw, earned_entries_raw, categorySpent, categoryEarned = _parse_cat_entries(r)
            prev_spent_raw, prev_earned_raw, prevSpent, prevEarned = _parse_cat_entries(r_prev)

            categoryTotal = float(categoryEarned) + float(categorySpent)
            prevTotal = float(prevEarned) + float(prevSpent)
            totals.append(
                {
                    "name": categoryName,
                    "spent": categorySpent,
                    "earned": categoryEarned,
                    "total": categoryTotal,
                    "spent_entries_raw": spent_entries_raw,
                    "earned_entries_raw": earned_entries_raw,
                    "prev_total": prevTotal,
                    "prev_spent_raw": prev_spent_raw,
                    "prev_earned_raw": prev_earned_raw,
                }
            )
        #
        # Get all the budgets
        print("Fetching budgets...")
        url = config["firefly-url"] + "/api/v1/budgets"
        budgets = s.get(url).json()
        #
        # Get the spent totals for each budget
        budgetTotals = []
        for budget in budgets["data"]:
            url = (
                config["firefly-url"]
                + "/api/v1/budgets/"
                + budget["id"]
                + "?start="
                + startDate.strftime("%Y-%m-%d")
                + "&end="
                + endDate.strftime("%Y-%m-%d")
            )
            r = s.get(url).json()
            budgetName = r["data"]["attributes"]["name"]
            try:
                budgetLimit = r["data"]["attributes"]["auto_budget_amount"]
                if not budgetLimit:
                    # Try to get budget limit from budget limits
                    url_limits = (
                        config["firefly-url"]
                        + "/api/v1/budgets/"
                        + budget["id"]
                        + "/limits?start="
                        + startDate.strftime("%Y-%m-%d")
                        + "&end="
                        + endDate.strftime("%Y-%m-%d")
                    )
                    limits = s.get(url_limits).json()
                    if limits["data"]:
                        budgetLimit = limits["data"][0]["attributes"]["amount"]
                    else:
                        budgetLimit = 0
            except (KeyError, IndexError):
                budgetLimit = 0
            if multi_currency_mode:
                spent_entries_raw = [
                    {"amount": float(e["sum"]), "currency": e.get("currency_code", "")}
                    for e in r["data"]["attributes"].get("spent", [])
                ]
                budgetSpent = sum(e["amount"] for e in spent_entries_raw)
                # Try to get limit currency from limits API entry
                limit_currency = ""
                try:
                    url_limits = (
                        config["firefly-url"]
                        + "/api/v1/budgets/"
                        + budget["id"]
                        + "/limits?start="
                        + startDate.strftime("%Y-%m-%d")
                        + "&end="
                        + endDate.strftime("%Y-%m-%d")
                    )
                    limits_data = s.get(url_limits).json()
                    if limits_data["data"]:
                        limit_currency = limits_data["data"][0]["attributes"].get("currency_code", "")
                except Exception:
                    pass
                if not limit_currency and spent_entries_raw:
                    limit_currency = spent_entries_raw[0]["currency"]
            else:
                spent_entries_raw = []
                limit_currency = ""
                try:
                    budgetSpent = r["data"]["attributes"]["spent"][0]["sum"]
                except (KeyError, IndexError):
                    budgetSpent = 0

            if (
                budgetLimit or budgetSpent
            ):  # Only include budgets with limit or spending
                budgetRemaining = float(budgetLimit) + float(
                    budgetSpent
                )  # spent is negative
                budgetTotals.append(
                    {
                        "id": budget[
                            "id"
                        ],  # keep Firefly budget id for later transaction mapping
                        "name": budgetName,
                        "limit": budgetLimit,
                        "limit_currency": limit_currency,
                        "spent": budgetSpent,
                        "remaining": budgetRemaining,
                        "spent_entries_raw": spent_entries_raw,
                    }
                )
        #
        # Get general information
        print("Fetching financial summary...")
        monthSummary = s.get(
            config["firefly-url"]
            + "/api/v1/summary/basic"
            + "?start="
            + startDate.strftime("%Y-%m-%d")
            + "&end="
            + endDate.strftime("%Y-%m-%d")
        ).json()
        yearToDateSummary = s.get(
            config["firefly-url"]
            + "/api/v1/summary/basic"
            + "?start="
            + startDate.strftime("%Y")
            + "-01-01"
            + "&end="
            + endDate.strftime("%Y-%m-%d")
        ).json()
        currency = config.get("currency", None)
        currencySymbol = config.get("currency_symbol", "$")  # Default to $

        if multi_currency_mode:
            currencyName = config["base_currency"]
            currencySymbol = config.get("base_currency_symbol", currencySymbol)
        elif currency:
            currencyName = currency
        else:
            for key in monthSummary:
                if re.match(r"spent-in-.*", key):
                    currencyName = key.replace("spent-in-", "")

        if multi_currency_mode:
            # Collect all currencies encountered across categories, budgets, and summary
            all_currencies = set()
            for t in totals:
                for e in t["spent_entries_raw"] + t["earned_entries_raw"]:
                    if e["currency"]:
                        all_currencies.add(e["currency"])
            for b in budgetTotals:
                for e in b["spent_entries_raw"]:
                    if e["currency"]:
                        all_currencies.add(e["currency"])
                if b["limit_currency"]:
                    all_currencies.add(b["limit_currency"])
            for key in list(monthSummary) + list(yearToDateSummary):
                m = re.match(r"spent-in-(.*)", key)
                if m:
                    all_currencies.add(m.group(1))
            foreign_currencies = all_currencies - {currencyName}

            print(
                f"Fetching exchange rates (base: {currencyName},"
                f" foreign: {', '.join(sorted(foreign_currencies)) or 'none'})..."
            )
            exchange_rates = fetch_exchange_rates(currencyName, foreign_currencies)

            # Convert category amounts to base currency
            for t in totals:
                all_raw = t["spent_entries_raw"] + t["earned_entries_raw"]
                has_foreign = any(
                    e["currency"] != currencyName and e["amount"] != 0
                    for e in all_raw
                )
                spent_conv, earned_conv = 0.0, 0.0
                display = []
                for e in t["spent_entries_raw"]:
                    conv, rate = convert_amount(e["amount"], e["currency"], currencyName, exchange_rates)
                    spent_conv += conv
                    if e["amount"] != 0 and (e["currency"] != currencyName or has_foreign):
                        display.append({"original": e["amount"], "currency": e["currency"], "rate": rate})
                for e in t["earned_entries_raw"]:
                    conv, rate = convert_amount(e["amount"], e["currency"], currencyName, exchange_rates)
                    earned_conv += conv
                    if e["amount"] != 0 and (e["currency"] != currencyName or has_foreign):
                        display.append({"original": e["amount"], "currency": e["currency"], "rate": rate})
                t["spent"] = spent_conv
                t["earned"] = earned_conv
                t["total"] = spent_conv + earned_conv
                t["spent_display"] = [e for e in display if e["original"] < 0]
                t["earned_display"] = [e for e in display if e["original"] > 0]
                t["display"] = display
                # Convert previous month total for comparison
                prev_conv = sum(
                    convert_amount(e["amount"], e["currency"], currencyName, exchange_rates)[0]
                    for e in t["prev_spent_raw"] + t["prev_earned_raw"]
                )
                t["prev_total"] = prev_conv

            # Convert budget amounts to base currency
            for b in budgetTotals:
                spent_conv = 0.0
                b["spent_display"] = []
                for e in b["spent_entries_raw"]:
                    conv, rate = convert_amount(e["amount"], e["currency"], currencyName, exchange_rates)
                    spent_conv += conv
                    if e["currency"] != currencyName and e["amount"] != 0:
                        b["spent_display"].append({"original": e["amount"], "currency": e["currency"], "rate": rate})
                b["spent"] = spent_conv
                lim_cur = b["limit_currency"] or currencyName
                lim_conv, lim_rate = convert_amount(float(b["limit"]), lim_cur, currencyName, exchange_rates)
                b["limit_display"] = []
                if lim_cur != currencyName and float(b["limit"]) != 0:
                    b["limit_display"] = [{"original": float(b["limit"]), "currency": lim_cur, "rate": lim_rate}]
                b["limit"] = lim_conv
                b["remaining"] = b["limit"] + b["spent"]

            # Helper: aggregate a summary key across all currencies
            def _sum_summary(summary, prefix):
                total, display = 0.0, []
                for key, value in summary.items():
                    m = re.match(f"^{prefix}-(.+)", key)
                    if m:
                        cur = m.group(1)
                        amt = float(value["monetary_value"])
                        conv, rate = convert_amount(amt, cur, currencyName, exchange_rates)
                        total += conv
                        if cur != currencyName and amt != 0:
                            display.append({"original": amt, "currency": cur, "rate": rate})
                return total, display

            spentThisMonth, spentThisMonth_display = _sum_summary(monthSummary, "spent-in")
            earnedThisMonth, earnedThisMonth_display = _sum_summary(monthSummary, "earned-in")
            netChangeThisMonth, netChangeThisMonth_display = _sum_summary(monthSummary, "balance-in")
            spentThisYear, spentThisYear_display = _sum_summary(yearToDateSummary, "spent-in")
            earnedThisYear, earnedThisYear_display = _sum_summary(yearToDateSummary, "earned-in")
            netChangeThisYear, netChangeThisYear_display = _sum_summary(yearToDateSummary, "balance-in")
            netWorth, netWorth_display = _sum_summary(yearToDateSummary, "net-worth-in")
        else:
            exchange_rates = {}
            spentThisMonth = float(
                monthSummary["spent-in-" + currencyName]["monetary_value"]
            )
            earnedThisMonth = float(
                monthSummary["earned-in-" + currencyName]["monetary_value"]
            )
            netChangeThisMonth = float(
                monthSummary["balance-in-" + currencyName]["monetary_value"]
            )
            spentThisYear = float(
                yearToDateSummary["spent-in-" + currencyName]["monetary_value"]
            )
            earnedThisYear = float(
                yearToDateSummary["earned-in-" + currencyName]["monetary_value"]
            )
            netChangeThisYear = float(
                yearToDateSummary["balance-in-" + currencyName]["monetary_value"]
            )
            netWorth = float(
                yearToDateSummary["net-worth-in-" + currencyName]["monetary_value"]
            )
            spentThisMonth_display = earnedThisMonth_display = netChangeThisMonth_display = []
            spentThisYear_display = earnedThisYear_display = netChangeThisYear_display = netWorth_display = []
        def _fmtv(v):
            """Format a numeric value as a currency string with symbol, thousands sep, 2 decimals."""
            v = float(v)
            sign = "-" if v < 0 else ""
            return f"{currencySymbol}{sign}{abs(v):,.2f}"

        def _compact(v):
            av = abs(v)
            if av >= 1_000_000:
                s = f"{av / 1_000_000:.1f}M".rstrip("0").rstrip(".")
            elif av >= 1_000:
                s = f"{av / 1_000:.1f}k".rstrip("0").rstrip(".")
            else:
                s = f"{av:.0f}"
            return ("-" if v < 0 else "") + s

        def _amt_cell(value, display_entries, color_class, style="text-align: right;"):
            """Return a <td> HTML string for an amount, with foreign currency sub-lines."""
            inner = _fmtv(value)
            if multi_currency_mode:
                for e in (display_entries or []):
                    orig = e["original"]
                    rate = e["rate"]
                    sign = "+" if orig >= 0 else "-"
                    orig_str = f"{sign}{abs(orig):,.2f} {e['currency']}"
                    if rate == 1.0:
                        inner += f'<br><span class="original-amount">{orig_str}</span>'
                    else:
                        conv = orig * rate
                        conv_sign = "+" if conv >= 0 else "-"
                        conv_str = f"{currencySymbol}{conv_sign}{abs(conv):,.2f}"
                        inner += (
                            f'<br><span class="original-amount">'
                            f'{orig_str} → {conv_str}'
                            f' <span class="exchange-rate">(×{rate:.4f})</span>'
                            f'</span>'
                        )
            return f'<td style="{style}" class="amount {color_class}">{inner}</td>'

        #
        # Sort categories: by total (descending), with zeros at the end
        totals.sort(key=lambda x: (float(x["total"]) == 0, -abs(float(x["total"]))))
        #
        prev_month_name = prev_start.strftime("%B")

        def _mom_cell(current, previous):
            """Return a <td> with the previous month amount, delta and percentage."""
            if previous == 0:
                if current == 0:
                    return '<td style="text-align: right;" class="mom-delta zero">—</td>'
                return (
                    f'<td style="text-align: right;" class="mom-delta positive">'
                    f'<span class="mom-prev">—</span><br>New</td>'
                )
            delta = current - previous
            pct = (delta / abs(previous)) * 100
            arrow = "↑" if delta > 0 else "↓"
            css = "positive" if delta > 0 else "negative"
            sign = "+" if delta > 0 else ""
            prev_color = "positive" if previous > 0 else "negative"
            return (
                f'<td style="text-align: right;" class="mom-delta {css}">'
                f'<span class="mom-prev {prev_color}">{_fmtv(previous)}</span><br>'
                f'{sign}{_fmtv(delta)} {arrow}{abs(pct):.1f}%'
                f'</td>'
            )

        # Set up the categories table
        print("Building category table...")
        categoriesTableBody = (
            '<table>'
            '<tr>'
            '<th>Category</th>'
            '<th style="text-align: right;">Total</th>'
            f'<th style="text-align: right;">vs Last Month ({prev_month_name})</th>'
            '</tr>'
        )
        # Separate non-zero and zero categories
        nonZeroCategories = [c for c in totals if float(c["total"]) != 0]
        zeroCategories = [c for c in totals if float(c["total"]) == 0]
        maxCategoryAbs = max((abs(float(c["total"])) for c in nonZeroCategories), default=1)

        # Add non-zero categories
        for category in nonZeroCategories:
            total = float(category["total"])
            color_class = "positive" if total > 0 else "negative"
            rel_pct = (abs(total) / maxCategoryAbs) * 100
            cat_bar = (
                f'<div style="margin-top:4px;width:{rel_pct:.1f}%;height:3px;'
                f'background:rgba(102,126,234,0.35);border-radius:2px;line-height:3px;font-size:0;"></div>'
            )
            categoriesTableBody += (
                f"<tr><td>{category['name']}{cat_bar}</td>"
                + _amt_cell(total, category.get("display"), color_class)
                + _mom_cell(total, float(category.get("prev_total", 0)))
                + "</tr>"
            )

        # Add zero categories grouped together
        if zeroCategories:
            zeroNames = ", ".join([c["name"] for c in zeroCategories])
            categoriesTableBody += (
                    '<tr class="zero"><td colspan="2">'
                    + zeroNames
                    + '</td><td style="text-align: right;" class="amount">'
                    + _fmtv(0)
                    + "</td></tr>"
            )

        categoriesTableBody += "</table>"
        #
        # Sort budgets: by spent amount (descending), with zeros at the end
        budgetTotals.sort(key=lambda x: (float(x["spent"]) == 0, float(x["spent"])))
        #
        # Set up the budgets table
        print("Building budget table...")
        budgetsTableBody = ""
        if budgetTotals:
            budgetsTableBody = '<table><tr><th>Budget</th><th style="text-align: right;">Limit</th><th style="text-align: right;">Spent</th><th style="text-align: right;">Remaining</th></tr>'

            # Separate non-zero and zero budgets
            nonZeroBudgets = [b for b in budgetTotals if float(b["spent"]) != 0]
            zeroBudgets = [b for b in budgetTotals if float(b["spent"]) == 0]

            # Add non-zero budgets
            for budget in nonZeroBudgets:
                remaining = float(budget["remaining"])
                limit_f   = float(budget["limit"])
                spent_f   = abs(float(budget["spent"]))
                remaining_class = "negative" if remaining < 0 else "positive"
                if limit_f > 0:
                    pct_raw  = (spent_f / limit_f) * 100
                    pct_fill = min(pct_raw, 100)
                    bar_color = "#ef4444" if pct_raw >= 100 else "#f59e0b" if pct_raw >= 80 else "#10b981"
                    bar_html = (
                        f'<div style="margin-top:6px;background:#f0f0f0;border-radius:3px;height:4px;width:100%;line-height:4px;font-size:0;">'
                        f'<div style="width:{pct_fill:.1f}%;height:4px;border-radius:3px;background:{bar_color};line-height:4px;font-size:0;"></div></div>'
                    )
                else:
                    bar_html = ""
                budgetsTableBody += (
                    f"<tr><td>{budget['name']}{bar_html}</td>"
                    + _amt_cell(limit_f, budget.get("limit_display"), "")
                    + _amt_cell(spent_f, budget.get("spent_display"), "negative")
                    + _amt_cell(remaining, None, remaining_class)
                    + "</tr>"
                )

            # Add zero budgets grouped together
            if zeroBudgets:
                zeroNames = ", ".join([b["name"] for b in zeroBudgets])
                # Calculate total limit for zero budgets
                totalZeroLimit = sum([float(b["limit"]) for b in zeroBudgets])
                budgetsTableBody += (
                    '<tr class="zero"><td>'
                    + zeroNames
                    + '</td><td style="text-align: right;" class="amount">'
                    + _fmtv(totalZeroLimit)
                    + '</td><td style="text-align: right;" class="amount">'
                    + _fmtv(0)
                    + '</td><td style="text-align: right;" class="amount">'
                    + _fmtv(totalZeroLimit)
                    + "</td></tr>"
                )

            budgetsTableBody += "</table>"
        #
        # Set up the general information table
        print("Building financial overview...")

        def _stat_card(label, value, display_entries, color):
            inner = _fmtv(value)
            if multi_currency_mode:
                for e in (display_entries or []):
                    orig = e["original"]
                    rate = e["rate"]
                    sign = "+" if orig >= 0 else "-"
                    orig_str = f"{sign}{abs(orig):,.2f} {e['currency']}"
                    if rate == 1.0:
                        inner += f'<br><span style="font-size:11px;font-weight:400;color:#888;">{orig_str}</span>'
                    else:
                        conv = orig * rate
                        conv_sign = "+" if conv >= 0 else "-"
                        conv_str = f"{currencySymbol}{conv_sign}{abs(conv):,.2f}"
                        inner += (
                            f'<br><span style="font-size:11px;font-weight:400;color:#888;">'
                            f'{orig_str} → {conv_str}'
                            f' <span style="font-size:10px;color:#aaa;font-style:italic;">(×{rate:.4f})</span>'
                            f'</span>'
                        )
            return (
                f'<td style="width:33.33%;padding:8px;vertical-align:top;">'
                f'<div style="background:#f8f9fa;border-radius:8px;padding:16px;border-left:4px solid {color};">'
                f'<div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;color:#6b7280;margin-bottom:8px;">{label}</div>'
                f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:18px;font-weight:700;color:#1a1a1a;">{inner}</div>'
                f'</div></td>'
            )

        net_m_color = "#10b981" if netChangeThisMonth >= 0 else "#ef4444"
        net_y_color = "#10b981" if netChangeThisYear  >= 0 else "#ef4444"
        nw_color    = "#10b981" if netWorth           >= 0 else "#ef4444"

        # Net worth multi-currency display
        nw_inner = _fmtv(netWorth)
        if multi_currency_mode:
            for e in (netWorth_display or []):
                orig = e["original"]
                rate = e["rate"]
                sign = "+" if orig >= 0 else "-"
                orig_str = f"{sign}{abs(orig):,.2f} {e['currency']}"
                if rate == 1.0:
                    nw_inner += f'<br><span style="font-size:11px;font-weight:400;color:#888;">{orig_str}</span>'
                else:
                    conv = orig * rate
                    conv_sign = "+" if conv >= 0 else "-"
                    conv_str = f"{currencySymbol}{conv_sign}{abs(conv):,.2f}"
                    nw_inner += (
                        f'<br><span style="font-size:11px;font-weight:400;color:#888;">'
                        f'{orig_str} → {conv_str}'
                        f' <span style="font-size:10px;color:#aaa;font-style:italic;">(×{rate:.4f})</span>'
                        f'</span>'
                    )

        generalTableBody = (
            '<table style="width:100%;border-collapse:collapse;margin-top:0;">'
            '<tr><td colspan="3" style="padding:4px 8px 2px;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#9ca3af;border-bottom:none;">This Month</td></tr>'
            '<tr>'
            + _stat_card("Earned", earnedThisMonth,      earnedThisMonth_display,      "#10b981")
            + _stat_card("Spent",  abs(spentThisMonth),  spentThisMonth_display,       "#ef4444")
            + _stat_card("Net",    netChangeThisMonth,   netChangeThisMonth_display,   net_m_color)
            + '</tr>'
            '<tr><td colspan="3" style="padding:12px 8px 2px;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#9ca3af;border-bottom:none;">Year to Date</td></tr>'
            '<tr>'
            + _stat_card("Earned", earnedThisYear,       earnedThisYear_display,       "#10b981")
            + _stat_card("Spent",  abs(spentThisYear),   spentThisYear_display,        "#ef4444")
            + _stat_card("Net",    netChangeThisYear,    netChangeThisYear_display,    net_y_color)
            + '</tr>'
            '<tr><td colspan="3" style="padding:12px 8px 2px;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#9ca3af;border-bottom:none;">Net Worth</td></tr>'
            f'<tr><td colspan="3" style="padding:8px;border-bottom:none;">'
            f'<div style="background:#f8f9fa;border-radius:8px;padding:16px;border-left:4px solid {nw_color};">'
            f'<div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.8px;color:#6b7280;margin-bottom:8px;">Current Net Worth</div>'
            f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:24px;font-weight:700;color:{nw_color};">'
            + nw_inner
            + '</div></div></td></tr></table>'
        )
        #
        # Compute savings rate and build highlights section
        if earnedThisMonth > 0:
            savingsRate = (netChangeThisMonth / earnedThisMonth) * 100
        else:
            savingsRate = 0.0

        kpi_earned  = _fmtv(earnedThisMonth)
        kpi_spent   = _fmtv(abs(spentThisMonth))
        kpi_net     = _fmtv(netChangeThisMonth)
        kpi_net_cls = "kpi-positive" if netChangeThisMonth >= 0 else "kpi-negative"
        kpi_sr_cls  = "kpi-blue" if savingsRate >= 0 else "kpi-negative"
        kpi_sr_text = f"{savingsRate:.1f}%"

        highlightsSection = (
            '<div class="kpi-row">'
            '<div class="kpi-card kpi-green">'
            '<div class="kpi-label">Earned</div>'
            f'<div class="kpi-value">{kpi_earned}</div>'
            '</div>'
            '<div class="kpi-card kpi-red">'
            '<div class="kpi-label">Spent</div>'
            f'<div class="kpi-value">{kpi_spent}</div>'
            '</div>'
            f'<div class="kpi-card {kpi_net_cls}">'
            '<div class="kpi-label">Net Change</div>'
            f'<div class="kpi-value">{kpi_net}</div>'
            '</div>'
            f'<div class="kpi-card {kpi_sr_cls}">'
            '<div class="kpi-label">Savings Rate</div>'
            f'<div class="kpi-value">{kpi_sr_text}</div>'
            '</div>'
            '</div>'
        )
        #
        # Build Sankey chart data - Income → Budgets → Categories
        print("Building Sankey chart data...")

        # Fetch revenue accounts to categorize income
        print("Fetching revenue accounts...")
        revenue_accounts_url = config["firefly-url"] + "/api/v1/accounts?type=revenue"
        revenue_accounts = s.get(revenue_accounts_url).json()

        # Fetch income transactions to map accounts to categories
        print("Fetching income transactions...")
        income_trans_url = (
            config["firefly-url"]
            + "/api/v1/transactions?start="
            + startDate.strftime("%Y-%m-%d")
            + "&end="
            + endDate.strftime("%Y-%m-%d")
            + "&type=deposit"
        )
        income_transactions = s.get(income_trans_url).json()

        # Build revenue account to category mapping with amounts
        revenue_to_category = {}  # revenue_account -> {category: amount}

        for trans in income_transactions.get("data", []):
            for t in trans["attributes"]["transactions"]:
                raw_amount = abs(float(t["amount"]))
                tx_currency = t.get("currency_code", currencyName)
                if multi_currency_mode:
                    amount, _ = convert_amount(raw_amount, tx_currency, currencyName, exchange_rates)
                else:
                    amount = raw_amount
                source_name = t.get("source_name", "Other Income")
                category = t.get("category_name") or ""

                if source_name not in revenue_to_category:
                    revenue_to_category[source_name] = {}
                revenue_to_category[source_name][category] = (
                    revenue_to_category[source_name].get(category, 0) + amount
                )

        sankeyNodes = []
        sankeyLinks = []
        nodeIndex = 0

        # Separate expense categories
        expenseCategories = [c for c in totals if float(c["total"]) < 0]

        # Level 1: Revenue accounts (income sources)
        revenue_indices = {}
        for revenue_account in revenue_to_category.keys():
            revenue_indices[revenue_account] = nodeIndex
            sankeyNodes.append(
                {"id": f"revenue_{revenue_account}", "label": revenue_account}
            )
            nodeIndex += 1

        # Level 2: Income categories
        income_category_indices = {}
        all_income_categories = set()
        for revenue_cats in revenue_to_category.values():
            all_income_categories.update(revenue_cats.keys())

        for income_cat in all_income_categories:
            income_category_indices[income_cat] = nodeIndex
            sankeyNodes.append({"id": f"income_cat_{income_cat}", "label": income_cat})
            nodeIndex += 1

        # Level 3: Income hub
        income_hub_index = nodeIndex
        sankeyNodes.append({"id": "income_hub", "label": "Total Income"})
        nodeIndex += 1

        # Level 4: Budgets (from budgetTotals)
        budget_indices = {}
        for budget in budgetTotals:
            if float(budget["spent"]) != 0:
                budget_indices[budget["name"]] = nodeIndex
                sankeyNodes.append(
                    {"id": f"budget_{budget['name']}", "label": budget["name"]}
                )
                nodeIndex += 1

        # Level 5: Expense categories
        category_indices = {}
        for cat in expenseCategories:
            category_indices[cat["name"]] = nodeIndex
            sankeyNodes.append({"id": f"category_{cat['name']}", "label": cat["name"]})
            nodeIndex += 1

        # Add surplus savings node if positive; avoid label collision with existing 'Savings' budget
        savings_index = None
        if netChangeThisMonth > 0:
            existing_budget_names_lower = {b["name"].lower() for b in budgetTotals}
            surplus_label = (
                "Savings"
                if "savings" not in existing_budget_names_lower
                else "Net Savings"
            )
            savings_index = nodeIndex
            sankeyNodes.append({"id": "net_savings", "label": surplus_label})
            nodeIndex += 1

        # Create links: Revenue accounts → Income categories
        for revenue_account, categories in revenue_to_category.items():
            for category, amount in categories.items():
                sankeyLinks.append(
                    {
                        "source": revenue_indices[revenue_account],
                        "target": income_category_indices[category],
                        "value": amount,
                    }
                )

        # Create links: Income categories → Income hub
        for income_cat in all_income_categories:
            total_for_cat = sum(
                cats.get(income_cat, 0) for cats in revenue_to_category.values()
            )
            if total_for_cat > 0:
                sankeyLinks.append(
                    {
                        "source": income_category_indices[income_cat],
                        "target": income_hub_index,
                        "value": total_for_cat,
                    }
                )

        # Create links: Income hub → Budgets
        for budget in budgetTotals:
            if float(budget["spent"]) != 0:
                sankeyLinks.append(
                    {
                        "source": income_hub_index,
                        "target": budget_indices[budget["name"]],
                        "value": abs(float(budget["spent"])),
                    }
                )

        # Build actual budget -> category expense mapping using budget transactions
        print("Fetching budget transactions for category mapping...")
        budget_category_map = {}  # budget_name -> {category_name: amount}
        for b in budgetTotals:
            b_id = b["id"]
            # Firefly API: budgets/{id}/transactions with date range
            b_tx_url = (
                f"{config['firefly-url']}/api/v1/budgets/{b_id}/transactions?start="
                + startDate.strftime("%Y-%m-%d")
                + "&end="
                + endDate.strftime("%Y-%m-%d")
            )
            try:
                b_tx_resp = s.get(b_tx_url).json()
            except Exception:
                continue  # skip on error
            for entry in b_tx_resp.get("data", []):
                for t in entry.get("attributes", {}).get("transactions", []):
                    try:
                        raw_amt = abs(float(t.get("amount", 0)))
                    except (ValueError, TypeError):
                        raw_amt = 0
                    if raw_amt == 0:
                        continue
                    tx_currency = t.get("currency_code", currencyName)
                    if multi_currency_mode:
                        conv_amt, _ = convert_amount(raw_amt, tx_currency, currencyName, exchange_rates)
                    else:
                        conv_amt = raw_amt
                    cat_name = t.get("category_name") or "Uncategorized"
                    budget_name = b["name"]
                    if budget_name not in budget_category_map:
                        budget_category_map[budget_name] = {}
                    budget_category_map[budget_name][cat_name] = budget_category_map[
                        budget_name
                    ].get(cat_name, 0) + conv_amt

        # Create links: Budgets → Categories using real mapped amounts
        categories_reached_via_budget = set()
        for budget_name, cat_map in budget_category_map.items():
            if budget_name not in budget_indices:
                continue
            for cat_name, amt in cat_map.items():
                if cat_name in category_indices and amt > 0:
                    sankeyLinks.append(
                        {
                            "source": budget_indices[budget_name],
                            "target": category_indices[cat_name],
                            "value": amt,
                        }
                    )
                    categories_reached_via_budget.add(cat_name)

        # Fallback: expense categories not covered by any budget link directly from Income hub
        for cat in expenseCategories:
            if cat["name"] not in categories_reached_via_budget:
                sankeyLinks.append(
                    {
                        "source": income_hub_index,
                        "target": category_indices[cat["name"]],
                        "value": abs(float(cat["total"])),
                    }
                )

        # Add savings flow from income hub
        if savings_index is not None:
            sankeyLinks.append(
                {
                    "source": income_hub_index,
                    "target": savings_index,
                    "value": float(netChangeThisMonth),
                }
            )

        # Generate Sankey chart as static image using Plotly
        print("Generating Sankey chart image...")
        sankey_image_path = os.path.join(base_dir, "sankey_chart.png")

        # Compute total flow for each node (incoming for most; outgoing for source nodes)
        node_values = [0.0] * len(sankeyNodes)
        for link in sankeyLinks:
            node_values[link["target"]] += link["value"]
        # Revenue accounts have no incoming links — use their outgoing total
        for idx in revenue_indices.values():
            node_values[idx] = sum(
                lk["value"] for lk in sankeyLinks if lk["source"] == idx
            )

        def _sfmt(v):
            return f"{currencySymbol}{abs(v):,.2f}"

        # Prepare data for Plotly Sankey
        node_labels = [
            f"{node['label']}: {_sfmt(node_values[i])}"
            for i, node in enumerate(sankeyNodes)
        ]
        link_sources = [link["source"] for link in sankeyLinks]
        link_targets = [link["target"] for link in sankeyLinks]
        link_values = [link["value"] for link in sankeyLinks]

        # Define colors for nodes (aligned to report palette)
        node_colors = []
        for node in sankeyNodes:
            node_id = node["id"]
            if node_id.startswith("revenue_"):
                node_colors.append("rgba(147, 51, 234, 0.85)")   # Purple — revenue sources
            elif node_id.startswith("income_cat_"):
                node_colors.append("rgba(52, 211, 153, 0.85)")   # Light emerald — income categories
            elif node_id == "income_hub":
                node_colors.append("rgba(102, 126, 234, 0.9)")   # Indigo — income hub
            elif node_id.startswith("budget_"):
                node_colors.append("rgba(245, 158, 11, 0.85)")   # Amber — budgets
            elif node_id == "net_savings":
                node_colors.append("rgba(16, 185, 129, 0.9)")    # Emerald — net savings
            else:
                node_colors.append("rgba(239, 68, 68, 0.85)")    # Red — expense categories

        # Define colors for links
        link_colors = []
        for i, link in enumerate(sankeyLinks):
            source_node = sankeyNodes[link["source"]]
            target_node = sankeyNodes[link["target"]]

            # Color based on flow type (aligned to report palette)
            if target_node["id"] == "income_hub":
                link_colors.append("rgba(52, 211, 153, 0.35)")   # Income → hub
            elif target_node["id"] == "net_savings":
                link_colors.append("rgba(16, 185, 129, 0.45)")   # Hub → savings
            elif source_node["id"] == "income_hub" and target_node["id"].startswith(
                "budget_"
            ):
                link_colors.append("rgba(245, 158, 11, 0.35)")   # Hub → budgets
            elif source_node["id"].startswith("budget_"):
                link_colors.append("rgba(239, 68, 68, 0.35)")    # Budget → expenses
            else:
                link_colors.append("rgba(147, 51, 234, 0.35)")   # Revenue source flows

        # Create Plotly Sankey diagram
        fig = go.Figure(
            data=[
                go.Sankey(
                    arrangement="snap",
                    node=dict(
                        pad=22,
                        thickness=24,
                        line=dict(color="rgba(15, 23, 42, 0.08)", width=0.5),
                        label=node_labels,
                        color=node_colors,
                        hovertemplate="%{label}<extra></extra>",
                    ),
                    link=dict(
                        source=link_sources,
                        target=link_targets,
                        value=link_values,
                        color=link_colors,
                        hovertemplate="%{source.label} → %{target.label}<br>"
                                      f"{currencySymbol}" + "%{value:,.2f}<extra></extra>",
                    ),
                )
            ]
        )

        fig.update_layout(
            font=dict(size=12, family="Inter, Arial", color="#1a1a1a"),
            plot_bgcolor="white",
            paper_bgcolor="white",
            height=600,
            margin=dict(l=10, r=10, t=10, b=10),
        )

        # Save as PNG
        try:
            fig.write_image(
                sankey_image_path, format="png", width=800, height=600, scale=3
            )
            print(f"✅ Sankey chart saved: {sankey_image_path}")
        except Exception as e:
            print(f"⚠️  Warning: Could not generate Sankey chart image: {e}")
            print("   Continuing without Sankey chart...")
            sankey_image_path = None

        # For preview mode, keep the JSON data for interactive chart
        sankeyData = json.dumps({"nodes": sankeyNodes, "links": sankeyLinks})

        #
        # Fetch top 5 largest expenses
        print("Fetching top transactions...")
        top_tx_url = (
            config["firefly-url"]
            + "/api/v1/transactions?start="
            + startDate.strftime("%Y-%m-%d")
            + "&end="
            + endDate.strftime("%Y-%m-%d")
            + "&type=withdrawal&limit=100"
        )
        top_tx_resp = s.get(top_tx_url).json()
        all_expenses = []
        for entry in top_tx_resp.get("data", []):
            for t in entry.get("attributes", {}).get("transactions", []):
                try:
                    raw_amt = abs(float(t.get("amount", 0)))
                except (ValueError, TypeError):
                    continue
                if raw_amt == 0:
                    continue
                tx_currency = t.get("currency_code", currencyName)
                if multi_currency_mode:
                    conv_amt, _ = convert_amount(raw_amt, tx_currency, currencyName, exchange_rates)
                else:
                    conv_amt = raw_amt
                all_expenses.append({
                    "amount": conv_amt,
                    "original_amount": raw_amt,
                    "currency": tx_currency,
                    "description": t.get("description", "—"),
                    "category": t.get("category_name") or "—",
                    "date": t.get("date", "")[:10],
                })
        all_expenses.sort(key=lambda x: x["amount"], reverse=True)
        top5 = all_expenses[:5]

        topTransactionsSection = ""
        if top5:
            rows = ""
            for tx in top5:
                try:
                    date_fmt = datetime.date.fromisoformat(tx["date"]).strftime("%b %d")
                except ValueError:
                    date_fmt = tx["date"]
                if multi_currency_mode and tx["currency"] != currencyName:
                    amt_html = (
                        f'<span class="amount negative">{_fmtv(-tx["amount"])}</span>'
                        f'<br><span class="original-amount">{tx["currency"]} {tx["original_amount"]:,.2f}</span>'
                    )
                else:
                    amt_html = f'<span class="amount negative">{_fmtv(-tx["amount"])}</span>'
                rows += (
                    f'<tr>'
                    f'<td>{tx["description"]}</td>'
                    f'<td>{tx["category"]}</td>'
                    f'<td style="text-align: center;">{date_fmt}</td>'
                    f'<td style="text-align: right;">{amt_html}</td>'
                    f'</tr>'
                )
            topTransactionsSection = (
                '<div class="section">'
                '<h3>🧾 Top 5 Largest Expenses</h3>'
                '<table>'
                '<tr>'
                '<th>Description</th>'
                '<th>Category</th>'
                '<th style="text-align: center;">Date</th>'
                '<th style="text-align: right;">Amount</th>'
                '</tr>'
                + rows +
                '</table>'
                '</div>'
            )
        #
        # Fetch savings accounts and build 6-month balance chart
        print("Fetching savings accounts...")
        savings_cid = make_msgid(domain="firefly-report")
        savings_image_path = os.path.join(base_dir, "savings_chart.png")

        # Build list of last 6 month-end dates (ending with the report month)
        month_ends = []
        for i in range(5, -1, -1):
            # Go back i months from startDate
            d = startDate
            for _ in range(i):
                d = (d.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
            last_day = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
            month_ends.append(last_day)

        month_labels = [d.strftime("%b %Y") for d in month_ends]

        asset_url = config["firefly-url"] + "/api/v1/accounts?type=asset&limit=50"
        asset_resp = s.get(asset_url).json()
        excluded_accounts = {
            name.lower() for name in config.get("exclude_accounts", [])
        }
        savings_accounts = [
            a for a in asset_resp.get("data", [])
            if "credit" not in a["attributes"].get("account_type", "").lower()
            and a["attributes"]["name"].lower() not in excluded_accounts
        ]
        print(f"Found {len(savings_accounts)} savings accounts: "
              + ", ".join(a["attributes"]["name"] for a in savings_accounts))

        # Fetch monthly balances for each savings account
        account_series = []
        for acct in savings_accounts:
            acct_id = acct["id"]
            acct_name = acct["attributes"]["name"]
            acct_currency = acct["attributes"].get("currency_code", currencyName)
            balances = []
            for d in month_ends:
                url_bal = f"{config['firefly-url']}/api/v1/accounts/{acct_id}?date={d.strftime('%Y-%m-%d')}"
                try:
                    bal_resp = s.get(url_bal).json()
                    raw_bal = float(bal_resp["data"]["attributes"].get("current_balance", 0))
                    if multi_currency_mode and acct_currency != currencyName:
                        raw_bal, _ = convert_amount(raw_bal, acct_currency, currencyName, exchange_rates)
                except Exception:
                    raw_bal = 0
                balances.append(raw_bal)
            if any(b != 0 for b in balances):
                account_series.append({"name": acct_name, "balances": balances})

        savings_section_html = ""
        savings_image_path_valid = None
        if account_series:
            n = len(account_series)
            cols_per_row = 3
            n_cols = min(n, cols_per_row)
            n_rows = (n + cols_per_row - 1) // cols_per_row
            fig_savings = make_subplots(
                rows=n_rows, cols=n_cols,
                subplot_titles=[a["name"] for a in account_series],
                shared_yaxes=False,
                horizontal_spacing=0.10,
                vertical_spacing=0.15,
            )
            palette = [
                ("#667eea", "rgba(102, 126, 234, 0.12)"),  # Indigo
                ("#10b981", "rgba(16, 185, 129, 0.12)"),   # Emerald
                ("#f59e0b", "rgba(245, 158, 11, 0.12)"),   # Amber
                ("#ef4444", "rgba(239, 68, 68, 0.12)"),    # Red
                ("#9333ea", "rgba(147, 51, 234, 0.12)"),   # Purple
                ("#0ea5e9", "rgba(14, 165, 233, 0.12)"),   # Sky
                ("#f43f5e", "rgba(244, 63, 94, 0.12)"),    # Rose
                ("#14b8a6", "rgba(20, 184, 166, 0.12)"),   # Teal
            ]
            for idx, acct in enumerate(account_series):
                color, fill_color = palette[idx % len(palette)]
                r = idx // cols_per_row + 1
                c = idx % cols_per_row + 1
                fig_savings.add_trace(
                    go.Scatter(
                        x=month_labels,
                        y=acct["balances"],
                        mode="lines+markers",
                        name=acct["name"],
                        line=dict(color=color, width=2.5, shape="spline", smoothing=0.6),
                        marker=dict(
                            size=7,
                            color=color,
                            line=dict(color="white", width=1.5),
                        ),
                        fill="tozeroy",
                        fillcolor=fill_color,
                        connectgaps=True,
                        showlegend=False,
                        hovertemplate="%{x}: " + currencySymbol + "%{y:,.2f}<extra></extra>",
                    ),
                    row=r, col=c,
                )
                fig_savings.update_xaxes(
                    tickangle=0,
                    tickfont=dict(size=9, color="#6b7280"),
                    showgrid=False,
                    showline=True,
                    linecolor="#e5e7eb",
                    linewidth=1,
                    zeroline=False,
                    row=r, col=c,
                )
                fig_savings.update_yaxes(
                    tickprefix=currencySymbol,
                    tickformat=".2s",
                    tickfont=dict(size=9, color="#6b7280"),
                    showgrid=True,
                    gridcolor="#f0f0f0",
                    gridwidth=1,
                    zeroline=True,
                    zerolinecolor="#e5e7eb",
                    zerolinewidth=1,
                    nticks=4,
                    row=r, col=c,
                )

            for annotation in fig_savings.layout.annotations:
                annotation.font = dict(size=12, color="#374151", family="Inter, Arial")

            chart_width = max(800, 320 * n_cols)
            chart_height = 340 * n_rows
            fig_savings.update_layout(
                paper_bgcolor="white",
                plot_bgcolor="white",
                font=dict(family="Inter, Arial", size=12, color="#1a1a1a"),
                margin=dict(l=20, r=20, t=50, b=50),
                height=chart_height,
            )
            try:
                fig_savings.write_image(
                    savings_image_path, format="png",
                    width=chart_width, height=chart_height, scale=3,
                )
                savings_image_path_valid = savings_image_path
                print(f"✅ Savings chart saved: {savings_image_path}")
            except Exception as e:
                print(f"⚠️  Could not generate savings chart: {e}")

            savings_section_html = (
                '<div class="section">'
                '<h3>🏦 Savings Accounts – Balance (Last 6 Months)</h3>'
                '__SAVINGS_CHART__'
                '</div>'
            )
        else:
            savings_section_html = ""
        #
        # Assemble the email
        print("Composing email...")
        msg = EmailMessage()
        base_subject = config.get("email_subject", "Firefly III: Monthly report")
        msg["Subject"] = f"{base_subject} – {monthName} {startDate.strftime('%Y')}"
        msg["From"] = config["email"]["from"]
        msg["To"] = tuple(config["email"]["to"])

        # Create a unique content ID for the Sankey image
        sankey_cid = make_msgid(domain="firefly-report")

        # Build the HTML body with budgets section
        budgetSection = ""
        if budgetsTableBody:
            budgetSection = (
                '<div class="section"><h3>💰 Budget Summary</h3>'
                + budgetsTableBody
                + "</div>"
            )

        htmlBody = """
		<html>
			<head>
				<meta charset="UTF-8">
				<meta name="viewport" content="width=device-width, initial-scale=1.0">
				<style>
					@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@500&display=swap');
					
					body {{
						font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
						line-height: 1.6;
						color: #1a1a1a;
						max-width: 800px;
						margin: 0 auto;
						padding: 20px;
						background-color: #f5f5f5;
						-webkit-font-smoothing: antialiased;
						-moz-osx-font-smoothing: grayscale;
					}}
					.header {{
						background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
						color: white;
						padding: 30px;
						border-radius: 10px;
						margin-bottom: 30px;
						box-shadow: 0 4px 6px rgba(0,0,0,0.1);
					}}
					.header h1 {{
						margin: 0;
						font-size: 32px;
						font-weight: 700;
						letter-spacing: -0.5px;
					}}
					.header p {{
						margin: 10px 0 0 0;
						opacity: 0.95;
						font-size: 17px;
						font-weight: 400;
						letter-spacing: 0.2px;
					}}
					.section {{
						background: white;
						padding: 25px;
						margin-bottom: 20px;
						border-radius: 8px;
						box-shadow: 0 2px 4px rgba(0,0,0,0.08);
					}}
					h3 {{
						margin: 0 0 20px 0;
						color: #667eea;
						font-size: 22px;
						font-weight: 700;
						border-left: 4px solid #667eea;
						padding-left: 12px;
						padding-bottom: 0;
						letter-spacing: -0.3px;
						line-height: 1.3;
					}}
					table {{
						width: 100%;
						border-collapse: collapse;
						margin-top: 10px;
					}}
					th {{
						background-color: #f8f9fa;
						padding: 12px;
						text-align: left;
						font-weight: 600;
						color: #495057;
						border-bottom: 2px solid #dee2e6;
						font-size: 13px;
						text-transform: uppercase;
						letter-spacing: 0.8px;
					}}
					td {{
						padding: 14px 12px;
						border-bottom: 1px solid #f0f0f0;
						font-size: 15px;
					}}
					tr:last-child td {{
						border-bottom: none;
					}}
					tr:nth-child(even) td {{
						background-color: #fafafa;
					}}
					tr:hover td {{
						background-color: #f0f4ff;
					}}
					.total-row:hover td {{
						background-color: #5a6fd6;
					}}
					.amount {{
						font-weight: 600;
						font-family: 'JetBrains Mono', 'SF Mono', 'Monaco', 'Inconsolata', 'Fira Code', 'Droid Sans Mono', 'Courier New', monospace;
						font-size: 15px;
						letter-spacing: -0.3px;
						white-space: nowrap;
					}}
					.positive {{
						color: #10b981;
					}}
					.negative {{
						color: #ef4444;
					}}
					.zero {{
						color: #999;
						font-style: italic;
					}}
					.original-amount {{
						font-size: 0.8em;
						color: #888;
						font-weight: 400;
					}}
					.exchange-rate {{
						font-size: 0.75em;
						color: #aaa;
						font-style: italic;
						font-weight: 400;
					}}
					.total-row .original-amount,
					.total-row .exchange-rate {{
						color: rgba(255, 255, 255, 0.85);
					}}
					.kpi-row {{
						display: table;
						width: 100%;
						border-collapse: separate;
						border-spacing: 12px 0;
						margin-bottom: 20px;
					}}
					.kpi-card {{
						display: table-cell;
						width: 25%;
						background: white;
						border-radius: 10px;
						padding: 18px 16px;
						box-shadow: 0 2px 6px rgba(0,0,0,0.08);
						vertical-align: top;
						border-top: 4px solid #ccc;
					}}
					.kpi-label {{
						font-size: 11px;
						font-weight: 600;
						text-transform: uppercase;
						letter-spacing: 0.8px;
						color: #6b7280;
						margin-bottom: 6px;
					}}
					.kpi-value {{
						font-family: 'JetBrains Mono', 'SF Mono', 'Monaco', monospace;
						font-size: 20px;
						font-weight: 700;
						color: #1a1a1a;
						white-space: nowrap;
					}}
					.kpi-green {{ border-top-color: #10b981; }}
					.kpi-red {{ border-top-color: #ef4444; }}
					.kpi-positive {{ border-top-color: #10b981; }}
					.kpi-negative {{ border-top-color: #ef4444; }}
					.kpi-blue {{ border-top-color: #667eea; }}
					.mom-delta {{
						font-size: 0.85em;
						font-weight: 500;
						white-space: nowrap;
					}}
					.summary-row {{
						background-color: #f8f9fa;
						font-weight: 600;
					}}
					.total-row {{
						color: white;
						font-weight: 700;
						font-size: 17px;
					}}
					.total-row.positive {{
						background-color: #10b981;
					}}
					.total-row.negative {{
						background-color: #ef4444;
					}}
					.total-row:hover {{
						opacity: 0.95;
					}}
					.total-row td {{
						padding: 16px 12px;
						border-bottom: none;
					}}
					.total-row .amount {{
						color: white !important;
					}}
					canvas {{
						max-width: 100%;
						height: auto !important;
					}}
					.footer {{
						text-align: center;
						margin-top: 30px;
						padding: 20px;
						color: #999;
						font-size: 13px;
						font-weight: 400;
					}}
					
					/* Mobile responsive styles */
					@media only screen and (max-width: 600px) {{
						body {{
							padding: 10px;
						}}
						.header {{
							padding: 20px;
						}}
						.header h1 {{
							font-size: 24px;
						}}
						.section {{
							padding: 15px;
						}}
						h3 {{
							font-size: 18px;
						}}
						th, td {{
							padding: 10px 8px;
							font-size: 14px;
						}}
						.amount {{
							font-size: 14px;
						}}
						th {{
							font-size: 11px;
						}}
						.kpi-card {{
							display: block;
							width: 100%;
							margin-bottom: 12px;
						}}
					}}
				</style>
			</head>
			<body>
				<div class="header">
					<h1>📊 Firefly III Monthly Report</h1>
					<p>{monthName} {year}</p>
				</div>
				{highlightsSection}
				<div class="section">
					<h3>🏷️ Category Summary</h3>
					{categoriesTableBody}
				</div>
				<div class="section">
					<h3>💸 Money Flow</h3>
					{sankeySection}
				</div>
				{budgetSection}
				{topTransactionsSection}
				{savingsSection}
				<div class="section">
					<h3>📈 Financial Overview</h3>
					{generalTableBody}
				</div>
				<div class="footer">
					Generated by <a href="https://github.com/yemzikk/firefly-iii-email-summary" style="color: #999;">Firefly III Email Summary</a> • <a href="https://yemzikk.in" style="color: #999; text-decoration: none;">Yemzikk</a>
				</div>
		"""

        # Format the HTML body first (without sankey section)
        htmlBody = htmlBody.format(
            monthName=monthName,
            year=startDate.strftime("%Y"),
            categoriesTableBody=categoriesTableBody,
            budgetSection=budgetSection,
            topTransactionsSection=topTransactionsSection,
            savingsSection=savings_section_html,
            generalTableBody=generalTableBody,
            highlightsSection=highlightsSection,
            sankeySection="{sankeySection}",  # Placeholder
        )

        # Determine Sankey section content based on preview mode
        if args.preview:
            # For preview, use interactive JavaScript chart
            sankeySection = """<div style="position: relative; height: 500px;">
                        <canvas id="sankeyChart"></canvas>
                    </div>"""
            # Add the JavaScript for preview
            javascript_code = f"""
                <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.js"></script>
                <script src="https://cdn.jsdelivr.net/npm/chartjs-chart-sankey@0.12.0/dist/chartjs-chart-sankey.min.js"></script>
                <script>
                    const sankeyData = {sankeyData};
                    const ctx = document.getElementById('sankeyChart');
					
                    // Transform data for Chart.js Sankey
                    const data = {{
                        datasets: [{{
                            data: sankeyData.links.map(link => ({{
                                from: sankeyData.nodes[link.source].label,
                                to: sankeyData.nodes[link.target].label,
                                flow: link.value
                            }})),
                            colorFrom: (c) => {{
                                const fromLabel = c.dataset.data[c.dataIndex].from;
                                const toLabel = c.dataset.data[c.dataIndex].to;
                                // Revenue accounts (Level 1 - dark green)
                                if (toLabel !== 'Total Income' && fromLabel !== 'Total Income') return 'rgba(27, 94, 32, 0.7)';
                                // Income categories (Level 2 - light green)
                                if (toLabel === 'Total Income') return 'rgba(76, 175, 80, 0.7)';
                                // Income hub (Level 3 - blue)
                                if (fromLabel === 'Total Income') return 'rgba(102, 126, 234, 0.7)';
                                // Budgets (Level 4 - purple)
                                return 'rgba(156, 39, 176, 0.7)';
                            }},
                            colorTo: (c) => {{
                                const toLabel = c.dataset.data[c.dataIndex].to;
                                const fromLabel = c.dataset.data[c.dataIndex].from;
                                // Surplus Savings (green)
                                if (toLabel === 'Savings' || toLabel === 'Net Savings') return 'rgba(16, 185, 129, 0.7)';
                                // Income categories (light green)
                                if (toLabel !== 'Total Income' && fromLabel !== 'Total Income') return 'rgba(76, 175, 80, 0.7)';
                                // Income hub (blue)
                                if (toLabel === 'Total Income') return 'rgba(102, 126, 234, 0.7)';
                                // Budgets (purple)
                                if (fromLabel === 'Total Income' && toLabel !== 'Savings' && toLabel !== 'Net Savings') return 'rgba(156, 39, 176, 0.7)';
                                // Categories (red)
                                return 'rgba(239, 68, 68, 0.7)';
                            }},
                            borderWidth: 0,
                            nodeWidth: 10,
                            size: 'max'
                        }}]
                    }};
					
                    new Chart(ctx, {{
                        type: 'sankey',
                        data: data,
                        options: {{
                            responsive: true,
                            maintainAspectRatio: false,
                            plugins: {{
                                legend: {{
                                    display: false
                                }},
                                tooltip: {{
                                    callbacks: {{
                                        label: function(context) {{
                                            const item = context.dataset.data[context.dataIndex];
                                            const totalIncome = {earnedThisMonth};
                                            const percentage = ((item.flow / totalIncome) * 100).toFixed(1);
                                            return item.from + ' → ' + item.to + ': {currencySymbol}' + Math.round(item.flow).toLocaleString() + ' (' + percentage + '%)';
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }});
                </script>
            </body>
        </html>
        """
            # Savings chart for preview: embed as file:// img if generated
            if savings_image_path_valid:
                savings_img_tag = f'<img src="file://{savings_image_path_valid}" alt="Savings Chart" style="width: 100%; height: auto; border-radius: 8px;" />'
            else:
                savings_img_tag = '<p style="color: #999; padding: 20px 0;">No savings accounts found.</p>'
            htmlBody = (
                htmlBody.replace("{sankeySection}", sankeySection)
                        .replace("__SAVINGS_CHART__", savings_img_tag)
                + javascript_code
            )
        else:
            # For email, use embedded static image
            if sankey_image_path and os.path.exists(sankey_image_path):
                sankeySection = f'<img src="cid:{sankey_cid[1:-1]}" alt="Money Flow Chart" style="width: 100%; max-width: 800px; height: auto; border-radius: 8px;" />'
            else:
                sankeySection = '<p style="text-align: center; color: #999; padding: 40px;">Sankey chart could not be generated</p>'
            if savings_image_path_valid:
                savings_img_tag = f'<img src="cid:{savings_cid[1:-1]}" alt="Savings Chart" style="width: 100%; height: auto; border-radius: 8px;" />'
            else:
                savings_img_tag = '<p style="color: #999; padding: 20px 0;">No savings accounts found.</p>'
            htmlBody = (
                htmlBody.replace("{sankeySection}", sankeySection)
                        .replace("__SAVINGS_CHART__", savings_img_tag)
                + """
            </body>
        </html>
        """
            )
        msg.set_content(
            bs4.BeautifulSoup(htmlBody, "html.parser").get_text()
        )  # just html to text
        msg.add_alternative(htmlBody, subtype="html")

        # Attach Sankey chart image for email mode
        if not args.preview and sankey_image_path and os.path.exists(sankey_image_path):
            with open(sankey_image_path, "rb") as img_file:
                img_data = img_file.read()
                msg.get_payload()[1].add_related(
                    img_data, maintype="image", subtype="png", cid=sankey_cid
                )
            print("✅ Sankey chart image attached to email")
        # Attach savings chart image for email mode
        if not args.preview and savings_image_path_valid and os.path.exists(savings_image_path_valid):
            with open(savings_image_path_valid, "rb") as img_file:
                img_data = img_file.read()
                msg.get_payload()[1].add_related(
                    img_data, maintype="image", subtype="png", cid=savings_cid
                )
            print("✅ Savings chart image attached to email")
        #
        # Check if we're in preview mode
        if args.preview:
            # Generate preview.html file
            preview_path = os.path.join(base_dir, "preview.html")
            # Create a standalone HTML document
            preview_html = """<!DOCTYPE html>
<html>
	<head>
		<meta charset="UTF-8">
		<meta name="viewport" content="width=device-width, initial-scale=1.0">
		<title>Firefly III Monthly Report - Preview</title>
	</head>
	{body}
</html>""".format(
                body=htmlBody
            )

            with open(preview_path, "w", encoding="utf-8") as f:
                f.write(preview_html)

            print(f"✅ Preview generated: {preview_path}")
            print(f"   Open in browser: file://{preview_path}")
            return
        #
        # Set up the SSL context for SMTP if necessary
        context = ssl.create_default_context()
        #
        # Send off the message
        print("Sending email...")
        try:
            with smtplib.SMTP(
                host=config["smtp"]["server"], port=config["smtp"]["port"]
            ) as s:
                s.set_debuglevel(0)  # Set to 1 for debugging
                if config["smtp"]["starttls"]:
                    s.ehlo()
                    try:
                        s.starttls(context=context)
                        s.ehlo()  # Re-identify after STARTTLS
                    except Exception as e:
                        traceback.print_exc()
                        print(
                            f"ERROR: could not connect to SMTP server with STARTTLS: {e}"
                        )
                        sys.exit(2)
                if config["smtp"]["authentication"]:
                    try:
                        s.login(
                            user=config["smtp"]["user"],
                            password=config["smtp"]["password"],
                        )
                    except Exception as e:
                        traceback.print_exc()
                        print(f"ERROR: could not authenticate with SMTP server: {e}")
                        sys.exit(3)
                s.send_message(msg)
                print("✅ Email sent successfully!")

            # Optional: Ping healthcheck URL if configured
            if "healthcheck_url" in config and config["healthcheck_url"]:
                print("Pinging healthcheck...")
                try:
                    ping_response = requests.get(config["healthcheck_url"], timeout=10)
                    if ping_response.status_code == 200:
                        print("✅ Healthcheck ping sent successfully!")
                    else:
                        print(
                            f"⚠️  Healthcheck ping returned status code: {ping_response.status_code}"
                        )
                except Exception as e:
                    print(f"⚠️  Warning: Could not send healthcheck ping: {e}")
                    # Don't exit on healthcheck failure - email was sent successfully

        except Exception as e:
            traceback.print_exc()
            print(f"ERROR: Failed to send email: {e}")
            sys.exit(4)


if __name__ == "__main__":
    main()
