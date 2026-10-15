"""Sample data for trying BizDesk. Everything here is fictional.

Dates are relative to today, so the demo always has paid, open and overdue invoices.
"""

from __future__ import annotations

from datetime import timedelta

from .service import BizDesk, BizDeskError


def seed_demo(desk: BizDesk) -> str:
    if desk.find_clients():
        raise BizDeskError("The database already has clients; demo data only goes into an empty database.")
    today = desk.today()

    def ago(days: int) -> str:
        return (today - timedelta(days=days)).isoformat()

    desk.update_profile(
        {
            "business_name": "Brightway Cleaning Co.",
            "address": "214 Market Street\nSpringfield, IL 62701",
            "email": "billing@brightway.example",
            "phone": "(555) 010-2040",
            "currency": "USD",
            "default_tax_rate": "8.25",
            "payment_terms_days": 14,
            "payment_instructions": "Bank transfer to Brightway Cleaning Co. (details on request) "
            "or card payment by phone.",
        }
    )
    harbor = desk.add_client(
        "James Carter",
        "Harbor View Apartments",
        "james@harborview.example",
        address="88 Lakeshore Drive\nSpringfield, IL 62704",
    )["id"]
    elm = desk.add_client(
        "Tom Becker", "Elm & Oak Law Office", "tom@elmandoak.example", address="12 Court Square\nSpringfield, IL 62701"
    )["id"]
    dental = desk.add_client(
        "Maria Lopez",
        "Maple Street Dental",
        "maria@maplestreetdental.example",
        address="401 Maple Street\nSpringfield, IL 62702",
    )["id"]
    yoga = desk.add_client("Priya Shah", "Northside Yoga Studio", "priya@northsideyoga.example")["id"]
    home = desk.add_client("Aisha Bello", email="aisha.bello@mail.example", notes="Prefers Friday mornings")["id"]

    common_area = [
        {"description": "Common-area cleaning (lobby, halls, stairwells), weekly", "quantity": 4, "unit_price": 320},
        {"description": "Window cleaning, ground floor", "quantity": 1, "unit_price": 180},
    ]
    for days_ago, paid in ((100, True), (70, True), (40, False)):
        inv = desk.create_invoice(harbor, common_area, issue_date=ago(days_ago))["number"]
        desk.mark_invoice_sent(inv)
        if paid:
            desk.record_payment(
                inv,
                f"{desk.get_document(inv)['total_cents'] / 100:.2f}",
                paid_on=ago(days_ago - 12),
                method="bank transfer",
            )

    inv = desk.create_invoice(
        elm,
        [
            {"description": "Office cleaning, 3 visits per week", "quantity": 12, "unit_price": 95},
            {"description": "Carpet shampoo, conference room", "quantity": 1, "unit_price": 240},
        ],
        issue_date=ago(75),
    )["number"]
    desk.mark_invoice_sent(inv)
    desk.record_payment(inv, 600, paid_on=ago(50), method="check", reference="#1043")

    inv = desk.create_invoice(
        dental,
        [
            {"description": "Clinical-area disinfection, after hours", "quantity": 8, "unit_price": 140},
            {"description": "Waiting-room deep clean", "quantity": 2, "unit_price": 160},
        ],
        issue_date=ago(30),
    )["number"]
    desk.mark_invoice_sent(inv)
    desk.record_payment(inv, f"{desk.get_document(inv)['total_cents'] / 100:.2f}", paid_on=ago(18), method="card")

    quote = desk.create_quote(
        yoga,
        [
            {"description": "Studio floor deep clean and polish (1,200 sq ft)", "quantity": 1, "unit_price": 450},
            {"description": "Changing rooms and showers, sanitise", "quantity": 2, "unit_price": 85},
        ],
        issue_date=ago(25),
        notes="Work done on a Sunday when the studio is closed.",
    )["number"]
    desk.set_quote_status(quote, "sent")
    inv = desk.convert_quote_to_invoice(quote, issue_date=ago(20))["number"]
    desk.mark_invoice_sent(inv)

    desk.create_quote(
        dental,
        [{"description": "Quarterly HVAC vent cleaning", "quantity": 14, "unit_price": 25}],
        notes="Price holds for 30 days.",
    )
    desk.create_invoice(
        home,
        [
            {"description": "Move-out clean, 2-bedroom apartment", "quantity": 1, "unit_price": 260},
            {"description": "Inside oven and fridge", "quantity": 1, "unit_price": 45},
        ],
    )
    return "5 clients, 7 invoices, 2 quotes"
