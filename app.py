
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Ledger Agent")

ROOT_URL = os.environ.get("LEDGER_ROOT_URL", "")
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
SESSION = requests.Session()

orders = []
rates = {}
products = set()
regions = set()
customers = set()


class Question(BaseModel):
    question: str


def parse_date(value):
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def fetch_ledger():
    if not ROOT_URL:
        raise RuntimeError("Set LEDGER_ROOT_URL before starting the service")

    root = SESSION.get(ROOT_URL, timeout=8)
    root.raise_for_status()
    links = root.json()["links"]

    rate_response = SESSION.get(links["rates"], timeout=8)
    rate_response.raise_for_status()
    exchange_rates = rate_response.json()["usd_per_unit"]

    export_response = SESSION.get(links["export"], timeout=10)
    export_response.raise_for_status()

    # Export is newline-delimited JSON, not a JSON array.
    import json
    rows = [
        json.loads(line)
        for line in export_response.text.splitlines()
        if line.strip()
    ]

    # Keep the latest version of every order.
    latest = {}
    for row in rows:
        order_id = row["id"]
        previous = latest.get(order_id)
        if previous is None or parse_date(row["updated_at"]) > parse_date(
            previous["updated_at"]
        ):
            latest[order_id] = row

    return list(latest.values()), exchange_rates


@app.on_event("startup")
def startup():
    global orders, rates, products, regions, customers
    orders, rates = fetch_ledger()
    products = {o["product"] for o in orders}
    regions = {o["region"] for o in orders}
    customers = {o["customer"] for o in orders}


@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "Acme Ledger Agent",
        "orders_loaded": len(orders),
    }


def amount_usd(order):
    return float(order["amount"]) * float(rates[order["currency"]])


def find_month(text):
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11,
        "december": 12,
    }
    match = re.search(
        r"\b(" + "|".join(months) + r")\s+(20\d{2})\b",
        text,
        re.I,
    )
    if not match:
        return None
    return int(match.group(2)), months[match.group(1).lower()]


def filter_orders(question):
    q = question.lower()
    result = list(orders)

    # Product and region are matched against the actual API spelling.
    for product in products:
        if product.lower() in q:
            result = [o for o in result if o["product"] == product]
            break

    for region in regions:
        if re.search(r"\b" + re.escape(region.lower()) + r"\b", q):
            result = [o for o in result if o["region"] == region]
            break

    # Customer IDs, e.g. C1080.
    customer_match = re.search(r"\bc\d{4}\b", q, re.I)
    if customer_match:
        cid = customer_match.group().upper()
        result = [o for o in result if o["customer"].upper() == cid]

    month = find_month(q)
    if month:
        year, month_number = month
        result = [
            o for o in result
            if (dt := parse_date(o["created_at"])) is not None
            and dt.year == year and dt.month == month_number
        ]
    else:
        # Optional year-only filter.
        year_match = re.search(r"\b(20\d{2})\b", q)
        if year_match:
            year = int(year_match.group(1))
            result = [
                o for o in result
                if parse_date(o["created_at"]).year == year
            ]

    return result


def money(value):
    return round(value + 1e-9, 2)


def answer_question(question):
    q = question.lower()
    selected = filter_orders(q)

    if not selected:
        return 0

    # Explicit order status rules from the API.
    if "refund" in q:
        relevant = [o for o in selected if o["status"] == "refunded"]
        if any(word in q for word in ("how many", "count", "number of")):
            return len(relevant)
        if any(word in q for word in ("quantity", "units")):
            return sum(int(o["qty"]) for o in relevant)
        return money(sum(amount_usd(o) for o in relevant))

    if any(word in q for word in ("void", "cancelled", "canceled")):
        relevant = [o for o in selected if o["status"] == "void"]
        if "how many" in q or "count" in q or "number of" in q:
            return len(relevant)
        return money(sum(amount_usd(o) for o in relevant))

    # Revenue means paid orders only.
    relevant = [o for o in selected if o["status"] == "paid"]

    if any(word in q for word in ("how many", "count", "number of orders")):
        return len(relevant)

    if any(word in q for word in ("quantity", "units sold", "items sold")):
        return sum(int(o["qty"]) for o in relevant)

    # Grouped questions: identify the requested dimension.
    if any(word in q for word in ("by product", "each product", "per product")):
        return {
            product: money(sum(amount_usd(o) for o in relevant
                               if o["product"] == product))
            for product in sorted(products)
        }

    if any(word in q for word in ("by region", "each region", "per region")):
        return {
            region: money(sum(amount_usd(o) for o in relevant
                              if o["region"] == region))
            for region in sorted(regions)
        }

    if any(word in q for word in ("by customer", "each customer", "per customer")):
        return {
            customer: money(sum(amount_usd(o) for o in relevant
                                if o["customer"] == customer))
            for customer in sorted(customers)
        }

    # Ranking questions.
    if any(word in q for word in ("top product", "best-selling product",
                                  "most popular product", "highest revenue product")):
        totals = {
            p: sum(amount_usd(o) for o in relevant if o["product"] == p)
            for p in products
        }
        return max(totals, key=totals.get) if totals else ""

    if any(word in q for word in ("top region", "best region",
                                  "highest revenue region")):
        totals = {
            r: sum(amount_usd(o) for o in relevant if o["region"] == r)
            for r in regions
        }
        return max(totals, key=totals.get) if totals else ""

    # If the question asks for a particular currency, convert to that currency.
    currency_match = re.search(r"\b(USD|EUR|INR)\b", question, re.I)
    if currency_match:
        currency = currency_match.group().upper()
        total = sum(float(o["amount"]) * float(rates[o["currency"]])
                    for o in relevant)
        return money(total / float(rates[currency]))

    # Revenue, sales, spending, or order value in USD.
    return money(sum(amount_usd(o) for o in relevant))


@app.post("/")
def ask(payload: Question):
    if not orders:
        raise HTTPException(status_code=503, detail="Ledger not loaded")
    try:
        return {"answer": answer_question(payload.question)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
