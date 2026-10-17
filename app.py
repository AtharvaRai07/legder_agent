
import json
import os
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Acme Appliances Ledger Agent")

ROOT_URL = os.environ.get("LEDGER_ROOT_URL", "").strip()
TZ = ZoneInfo("Asia/Kolkata")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Acme-Ledger-Agent/1.0"})

orders = []
rates = {}
products = set()
regions = set()
customers = set()


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def parse_date(value):
    if not value:
        return None

    value = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)

    return dt.astimezone(TZ)


def fetch_ledger():
    if not ROOT_URL:
        raise RuntimeError("LEDGER_ROOT_URL is not configured")

    root_response = HTTP.get(ROOT_URL, timeout=10)
    root_response.raise_for_status()
    links = root_response.json()["links"]

    rate_response = HTTP.get(links["rates"], timeout=10)
    rate_response.raise_for_status()
    exchange_rates = rate_response.json()["usd_per_unit"]

    export_response = HTTP.get(links["export"], timeout=20)
    export_response.raise_for_status()

    # The export is newline-delimited JSON.
    rows = [
        json.loads(line)
        for line in export_response.text.splitlines()
        if line.strip()
    ]

    # Latest updated_at wins for each order ID.
    latest = {}

    for row in rows:
        order_id = row["id"]
        previous = latest.get(order_id)

        if previous is None:
            latest[order_id] = row
            continue

        current_updated = parse_date(row["updated_at"])
        previous_updated = parse_date(previous["updated_at"])

        if current_updated > previous_updated:
            latest[order_id] = row

    return list(latest.values()), exchange_rates


@app.on_event("startup")
def startup():
    global orders, rates, products, regions, customers

    orders, rates = fetch_ledger()

    products = {str(o["product"]) for o in orders}
    regions = {str(o["region"]) for o in orders}
    customers = {str(o["customer"]) for o in orders}

    print(f"Ledger loaded: {len(orders)} current orders")
    print(f"Products: {len(products)}")
    print(f"Exchange rates: {rates}")


@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "Acme Ledger Agent",
        "orders_loaded": len(orders),
    }


def dec(value):
    return Decimal(str(value))


def money(value):
    return float(
        dec(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def usd_amount(order):
    currency = order["currency"]

    if currency not in rates:
        raise ValueError(f"Unsupported currency: {currency}")

    return dec(order["amount"]) * dec(rates[currency])


def convert_amount(order, target="USD"):
    if target not in rates:
        target = "USD"

    return usd_amount(order) / dec(rates[target])


def total_amount(rows, target="USD"):
    return money(sum(
        (convert_amount(o, target) for o in rows),
        Decimal("0"),
    ))


def normalize(text):
    return re.sub(r"\s+", " ", text.lower()).strip()


MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def find_period(q):
    month_pattern = "|".join(
        sorted(MONTHS.keys(), key=len, reverse=True)
    )

    match = re.search(
        rf"\b({month_pattern})\s+(20\d{{2}})\b",
        q,
        re.IGNORECASE,
    )

    if match:
        return int(match.group(2)), MONTHS[match.group(1).lower()]

    # Also support "2026-04".
    match = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", q)

    if match:
        return int(match.group(1)), int(match.group(2))

    return None


def filter_orders(question):
    q = normalize(question)
    result = list(orders)

    # Exact product name from the API.
    for product in sorted(products, key=len, reverse=True):
        if product.lower() in q:
            result = [
                o for o in result
                if o["product"] == product
            ]
            break

    # Exact region name from the API.
    for region in sorted(regions, key=len, reverse=True):
        if re.search(r"\b" + re.escape(region.lower()) + r"\b", q):
            result = [
                o for o in result
                if o["region"] == region
            ]
            break

    # Customer identifiers such as C1080.
    customer_match = re.search(r"\bC\d+\b", question, re.IGNORECASE)

    if customer_match:
        cid = customer_match.group().upper()
        result = [
            o for o in result
            if str(o["customer"]).upper() == cid
        ]

    # Filter using business creation dates in Asia/Kolkata.
    period = find_period(q)

    if period:
        year, month = period
        filtered = []

        for order in result:
            dt = parse_date(order.get("created_at"))

            if dt and dt.year == year and dt.month == month:
                filtered.append(order)

        result = filtered
    else:
        year_match = re.search(r"\b(20\d{2})\b", q)

        if year_match:
            year = int(year_match.group(1))
            result = [
                o for o in result
                if (dt := parse_date(o.get("created_at"))) is not None
                and dt.year == year
            ]

    return result


def target_currency(question):
    q = normalize(question)

    if re.search(r"\b(usd|us dollars?|u\.s\. dollars?|dollars?)\b", q):
        return "USD"

    if re.search(r"\b(eur|euros?|euro)\b", q):
        return "EUR"

    if re.search(r"\b(inr|rupees?|indian rupees?)\b", q):
        return "INR"

    # Default currency for unqualified revenue questions.
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
    return any(
        phrase in q
        for phrase in (
            "average",
            "on average",
            "mean order",
            "mean value",
            "per order",
            "average order value",
        )
    )


def is_count_question(q):
    return any(
        phrase in q
        for phrase in (
            "how many",
            "number of",
            "count of",
            "count the",
            "count all",
        )
    )


def is_top_product_question(q):
    return any(
        phrase in q
        for phrase in (
            "top-selling product",
            "top selling product",
            "best-selling product",
            "best selling product",
            "which product earned the most",
            "which product made the most",
            "product earned the most",
            "product made the most",
            "highest revenue product",
            "product with the highest revenue",
            "product with the most revenue",
            "product generated the most revenue",
        )
    ) or (
        "product" in q
        and "revenue" in q
        and any(word in q for word in ("top", "highest", "most"))
    )


def is_top_region_question(q):
    return any(
        phrase in q
        for phrase in (
            "top region",
            "best region",
            "highest revenue region",
            "region with the highest revenue",
            "region earned the most",
        )
    )


def aggregate_by(rows, key, currency="USD"):
    result = {}

    for value in sorted({str(o[key]) for o in rows}):
        matching = [o for o in rows if str(o[key]) == value]
        result[value] = total_amount(matching, currency)

    return result


def answer_question(question):
    q = normalize(question)
    selected = filter_orders(question)

    # Refund status is handled separately from paid revenue.
    if "refund" in q or "refunded" in q:
        refunded = [
            o for o in selected
            if o["status"] == "refunded"
        ]

        if is_distinct_customer_question(q):
            return len({o["customer"] for o in refunded})

        if is_count_question(q):
            return len(refunded)

        if "unit" in q or "quantity" in q or "items" in q:
            return sum(int(o["qty"]) for o in refunded)

        return total_amount(refunded, target_currency(question))

    # Void/cancelled order questions.
    if any(word in q for word in ("void", "cancelled", "canceled")):
        voided = [
            o for o in selected
            if o["status"] == "void"
        ]

        if is_distinct_customer_question(q):
            return len({o["customer"] for o in voided})

        if is_count_question(q):
            return len(voided)

        return total_amount(voided, target_currency(question))

    # Revenue questions operate on current paid orders only.
    paid = [
        o for o in selected
        if o["status"] == "paid"
    ]

    # Distinct customer counts must precede generic counts.
    if is_distinct_customer_question(q):
        return len({o["customer"] for o in paid})

    # Rank products by revenue, but return the product name.
    # This check must precede grouped revenue and default totals.
    if is_top_product_question(q):
        if not paid:
            return ""

        totals = {
            product: sum(
                (usd_amount(o) for o in paid
                 if o["product"] == product),
                Decimal("0"),
            )
            for product in products
        }

        return max(totals, key=totals.get)

    if is_top_region_question(q):
        if not paid:
            return ""

        totals = {
            region: sum(
                (usd_amount(o) for o in paid
                 if o["region"] == region),
                Decimal("0"),
            )
            for region in regions
        }

        return max(totals, key=totals.get)

    # Averages must be checked before "how many" counts.
    if is_average_question(q):
        if not paid:
            return 0

        currency = target_currency(question)
        total = sum(
            (convert_amount(o, currency) for o in paid),
            Decimal("0"),
        )

        return money(total / Decimal(len(paid)))

    # Explicit customer spending or revenue totals.
    if "customer" in q and any(
        word in q for word in ("spent", "spending", "revenue", "sales")
    ):
        if is_count_question(q):
            return len({o["customer"] for o in paid})

    # Number of orders.
    if is_count_question(q) or "how many orders" in q:
        return len(paid)

    # Number of customers when distinctness is implied.
    if "customers" in q and any(
        word in q for word in ("bought", "purchased", "placed")
    ):
        return len({o["customer"] for o in paid})

    # Quantity questions.
    if any(
        phrase in q
        for phrase in (
            "units sold",
            "items sold",
            "quantity sold",
            "how many units",
            "how many items",
            "total quantity",
        )
    ):
        return sum(int(o["qty"]) for o in paid)

    currency = target_currency(question)

    # Revenue grouped by product.
    if any(
        phrase in q
        for phrase in (
            "by product",
            "each product",
            "per product",
            "for each product",
        )
    ):
        return aggregate_by(paid, "product", currency)

    # Revenue grouped by region.
    if any(
        phrase in q
        for phrase in (
            "by region",
            "each region",
            "per region",
            "for each region",
        )
    ):
        return aggregate_by(paid, "region", currency)

    # Revenue grouped by customer.
    if any(
        phrase in q
        for phrase in (
            "by customer",
            "each customer",
            "per customer",
            "for each customer",
        )
    ):
        return aggregate_by(paid, "customer", currency)

    # Default: total revenue of matching paid orders.
    return total_amount(paid, currency)


@app.post("/")
def ask(payload: Question):
    if not orders:
        raise HTTPException(
            status_code=503,
            detail="Ledger is not loaded",
        )

    try:
        return {"answer": answer_question(payload.question)}
    except Exception:
        # Do not expose the signed URL or internal details.
        raise HTTPException(
            status_code=500,
            detail="Unable to answer this question",
        )
