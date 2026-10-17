
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Appliances Ledger Agent")

ROOT_URL = os.environ.get("LEDGER_ROOT_URL", "").strip()
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
        raise RuntimeError(
            "Missing LEDGER_ROOT_URL environment variable"
        )

    root_response = SESSION.get(ROOT_URL, timeout=10)
    root_response.raise_for_status()
    links = root_response.json()["links"]

    rates_response = SESSION.get(links["rates"], timeout=10)
    rates_response.raise_for_status()
    exchange_rates = rates_response.json()["usd_per_unit"]

    export_response = SESSION.get(links["export"], timeout=15)
    export_response.raise_for_status()

    rows = [
        json.loads(line)
        for line in export_response.text.splitlines()
        if line.strip()
    ]

    # Deduplicate: keep the latest updated_at for each order ID.
    latest = {}

    for row in rows:
        order_id = row["id"]
        previous = latest.get(order_id)

        if (
            previous is None
            or parse_date(row["updated_at"])
            > parse_date(previous["updated_at"])
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

    print(f"Loaded {len(orders)} current orders")


@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "Acme Ledger Agent",
        "orders_loaded": len(orders),
    }


def amount_usd(order):
    currency = order["currency"]

    if currency not in rates:
        raise ValueError(f"Unknown currency: {currency}")

    return float(order["amount"]) * float(rates[currency])


def converted_amount(order, currency="USD"):
    return amount_usd(order) / float(rates[currency])


def money(value):
    return round(float(value) + 1e-9, 2)


def sum_amount(selected, currency="USD"):
    return money(
        sum(converted_amount(o, currency) for o in selected)
    )


def find_month(text):
    months = {
        "january": 1, "february": 2, "march": 3,
        "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9,
        "october": 10, "november": 11, "december": 12,
    }

    pattern = r"\b(" + "|".join(months) + r")\s+(20\d{2})\b"
    match = re.search(pattern, text, re.IGNORECASE)

    if not match:
        return None

    return int(match.group(2)), months[match.group(1).lower()]


def filter_orders(question):
    q = question.lower()
    selected = list(orders)

    # Match product names exactly as provided by the API.
    for product in sorted(products, key=len, reverse=True):
        if product.lower() in q:
            selected = [
                o for o in selected if o["product"] == product
            ]
            break

    # Match region.
    for region in sorted(regions, key=len, reverse=True):
        if re.search(r"\b" + re.escape(region.lower()) + r"\b", q):
            selected = [
                o for o in selected if o["region"] == region
            ]
            break

    # Match customer ID, e.g. C1080.
    customer_match = re.search(r"\bc\d{4}\b", q, re.IGNORECASE)

    if customer_match:
        cid = customer_match.group().upper()
        selected = [
            o for o in selected if o["customer"].upper() == cid
        ]

    # Business dates use Asia/Kolkata.
    month = find_month(q)

    if month:
        year, month_number = month
        selected = [
            o for o in selected
            if (
                (dt := parse_date(o["created_at"])) is not None
                and dt.year == year
                and dt.month == month_number
            )
        ]
    else:
        year_match = re.search(r"\b(20\d{2})\b", q)

        if year_match:
            year = int(year_match.group(1))
            selected = [
                o for o in selected
                if parse_date(o["created_at"]).year == year
            ]

    return selected


def target_currency(question):
    q = question.lower()

    if re.search(r"\b(usd|us dollars?|dollars?)\b", q):
        return "USD"

    if re.search(r"\b(eur|euros?)\b", q):
        return "EUR"

    if re.search(r"\b(inr|rupees?|indian rupees?)\b", q):
        return "INR"

    return "USD"


def is_distinct_customer_question(q):
    return (
        "customer" in q
        and any(
            word in q
            for word in ("distinct", "unique", "different")
        )
    )


def is_average_question(q):
    return (
        "average" in q
        or "on average" in q
        or "mean" in q
        or "per order" in q
    )


def count_question(q):
    return any(
        phrase in q
        for phrase in (
            "how many", "number of", "count of",
            "count the", "number",
        )
    )


def answer_question(question):
    q = question.lower()
    selected = filter_orders(question)

    # Refund questions.
    if "refund" in q or "refunded" in q:
        relevant = [
            o for o in selected if o["status"] == "refunded"
        ]

        if is_distinct_customer_question(q):
            return len({o["customer"] for o in relevant})

        if count_question(q):
            return len(relevant)

        if "quantity" in q or "units" in q:
            return sum(int(o["qty"]) for o in relevant)

        return sum_amount(relevant, target_currency(question))

    # Voided/cancelled orders.
    if any(word in q for word in ("void", "cancelled", "canceled")):
        relevant = [
            o for o in selected if o["status"] == "void"
        ]

        if is_distinct_customer_question(q):
            return len({o["customer"] for o in relevant})

        if count_question(q):
            return len(relevant)

        return sum_amount(relevant, target_currency(question))

    # Only current paid orders count as revenue.
    paid = [
        o for o in selected if o["status"] == "paid"
    ]

    # IMPORTANT: distinct customers must be counted once.
    # This check comes before generic "how many" handling.
    if is_distinct_customer_question(q):
        return len({o["customer"] for o in paid})

    # IMPORTANT: an average is a monetary calculation,
    # even when the question begins "How many US dollars...?"
    if is_average_question(q):
        if not paid:
            return 0

        currency = target_currency(question)
        total = sum(converted_amount(o, currency) for o in paid)

        return money(total / len(paid))

    # Count paid orders.
    if count_question(q) or "how many orders" in q:
        return len(paid)

    # Sum quantities for units/items sold.
    if any(
        phrase in q
        for phrase in (
            "units sold", "items sold", "quantity sold",
            "how many units", "how many items",
        )
    ):
        return sum(int(o["qty"]) for o in paid)

    currency = target_currency(question)

    # Grouped revenue by product.
    if any(
        phrase in q
        for phrase in (
            "by product", "each product",
            "per product", "for each product",
        )
    ):
        return {
            product: sum_amount(
                [o for o in paid if o["product"] == product],
                currency,
            )
            for product in sorted(products)
        }

    # Grouped revenue by region.
    if any(
        phrase in q
        for phrase in (
            "by region", "each region",
            "per region", "for each region",
        )
    ):
        return {
            region: sum_amount(
                [o for o in paid if o["region"] == region],
                currency,
            )
            for region in sorted(regions)
        }

    # Grouped revenue by customer.
    if any(
        phrase in q
        for phrase in (
            "by customer", "each customer",
            "per customer", "for each customer",
        )
    ):
        return {
            customer: sum_amount(
                [o for o in paid if o["customer"] == customer],
                currency,
            )
            for customer in sorted(customers)
        }

    # Highest-revenue product.
    if any(
        phrase in q
        for phrase in (
            "top product", "best-selling product",
            "most popular product", "highest revenue product",
            "product with the highest revenue",
        )
    ):
        if not paid:
            return ""

        totals = {
            product: sum(
                converted_amount(o, currency)
                for o in paid if o["product"] == product
            )
            for product in products
        }

        return max(totals, key=totals.get)

    # Highest-revenue region.
    if any(
        phrase in q
        for phrase in (
            "top region", "best region",
            "highest revenue region",
            "region with the highest revenue",
        )
    ):
        if not paid:
            return ""

        totals = {
            region: sum(
                converted_amount(o, currency)
                for o in paid if o["region"] == region
            )
            for region in regions
        }

        return max(totals, key=totals.get)

    # Default: total revenue of the selected paid orders.
    return sum_amount(paid, currency)


@app.post("/")
def ask(payload: Question):
    if not orders:
        raise HTTPException(
            status_code=503,
            detail="Ledger is not loaded",
        )

    if not payload.question.strip():
        raise HTTPException(
            status_code=400,
            detail="Question cannot be empty",
        )

    try:
        return {"answer": answer_question(payload.question)}
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Unable to answer this question",
        )
