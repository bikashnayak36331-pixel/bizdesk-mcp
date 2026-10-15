from datetime import date

import pytest

from bizdesk_mcp.service import BizDesk, BizDeskError, ReadOnlyError
from bizdesk_mcp.store import Store

from .conftest import TODAY


def add(desk, name="Ana Diaz", company="Diaz Bakery", **kw):
    return desk.add_client(name, company, kw.get("email", "ana@diaz.example"))


ITEMS = [{"description": "Deep clean", "quantity": 2, "unit_price": "150"}, {"description": "Oven", "unit_price": 45}]


def test_profile_defaults_and_update(desk):
    p = desk.get_profile()
    assert p["currency"] == "USD" and p["default_tax_rate"] == "0%"
    p = desk.update_profile({"business_name": "Acme", "currency": "eur", "default_tax_rate": "20%"})
    assert p["business_name"] == "Acme" and p["currency"] == "EUR" and p["default_tax_rate"] == "20%"
    with pytest.raises(BizDeskError):
        desk.update_profile({"currency": "EURO"})
    with pytest.raises(BizDeskError):
        desk.update_profile({"payment_terms_days": 999})
    with pytest.raises(BizDeskError):
        desk.update_profile({"colour": "red"})
    with pytest.raises(BizDeskError):
        desk.update_profile({})


def test_clients(desk):
    c = add(desk)
    assert c["id"] == 1 and c["outstanding"] == "$0.00"
    with pytest.raises(BizDeskError, match="already exists"):
        add(desk)
    with pytest.raises(BizDeskError, match="email"):
        desk.add_client("Bob", email="not-an-email")
    with pytest.raises(BizDeskError, match="required"):
        desk.add_client("  ")
    assert [x["name"] for x in desk.find_clients("bakery")] == ["Ana Diaz"]
    assert desk.find_clients("nobody") == []
    c = desk.update_client(1, {"phone": "555-0100", "email": None})
    assert c["phone"] == "555-0100" and c["email"] == "ana@diaz.example"
    with pytest.raises(BizDeskError, match="No client"):
        desk.get_client(99)


def test_invoice_totals_and_numbering(desk):
    desk.update_profile({"default_tax_rate": "8.25", "payment_terms_days": 30})
    cid = add(desk)["id"]
    inv = desk.create_invoice(cid, ITEMS)
    assert inv["number"] == "INV-0001" and inv["status"] == "draft"
    assert inv["subtotal"] == "$345.00" and inv["tax"] == "$28.46" and inv["total"] == "$373.46"
    assert inv["due_date"] == "2026-11-14" and inv["balance_due_cents"] == 37346
    assert desk.create_invoice(cid, ITEMS, tax_rate=0)["number"] == "INV-0002"
    assert desk.create_quote(cid, ITEMS)["number"] == "Q-0001"


@pytest.mark.parametrize(
    "items, message",
    [
        ([], "at least one"),
        ([{"description": "", "unit_price": 1}], "description is required"),
        ([{"description": "x", "unit_price": -1}], "negative"),
        ([{"description": "x", "unit_price": 1, "quantity": 0}], "greater than zero"),
        ([{"description": "x", "unit_price": "1.234"}], "2 decimal places"),
        (["not a dict"], "must be an object"),
    ],
)
def test_bad_items(desk, items, message):
    cid = add(desk)["id"]
    with pytest.raises(BizDeskError, match=message):
        desk.create_invoice(cid, items)


def test_quote_to_invoice(desk):
    cid = add(desk)["id"]
    q = desk.create_quote(cid, ITEMS, tax_rate="10", notes="Sunday work")
    assert q["valid_until"] == "2026-11-14"
    inv = desk.convert_quote_to_invoice(q["number"])
    assert inv["from_quote"] == "Q-0001" and inv["total"] == q["total"] and inv["notes"] == "Sunday work"
    assert desk.get_document("Q-0001")["status"] == "accepted"
    with pytest.raises(BizDeskError, match="already invoiced"):
        desk.convert_quote_to_invoice("q-0001")  # numbers are case-insensitive
    q2 = desk.create_quote(cid, ITEMS)
    desk.set_quote_status(q2["number"], "declined")
    with pytest.raises(BizDeskError, match="declined"):
        desk.convert_quote_to_invoice(q2["number"])
    with pytest.raises(BizDeskError, match="is a quote"):
        desk.record_payment(q2["number"], 10)


def test_payments_status_and_overpayment(desk):
    cid = add(desk)["id"]
    inv = desk.create_invoice(cid, [{"description": "Job", "unit_price": 100}])["number"]
    with pytest.raises(BizDeskError, match="more than the balance"):
        desk.record_payment(inv, "100.01")
    doc = desk.record_payment(inv, 40, method="cash")
    assert doc["status"] == "partially_paid" and doc["balance_due"] == "$60.00"
    doc = desk.record_payment(inv, "60.00", paid_on="2026-10-20")
    assert doc["status"] == "paid" and doc["balance_due_cents"] == 0 and len(doc["payments"]) == 2
    with pytest.raises(BizDeskError):
        desk.record_payment(inv, 1)
    with pytest.raises(BizDeskError, match="date"):
        desk.record_payment(desk.create_invoice(cid, ITEMS)["number"], 1, paid_on="15/10/2026")


def test_void_rules(desk):
    cid = add(desk)["id"]
    a = desk.create_invoice(cid, ITEMS)["number"]
    b = desk.create_invoice(cid, ITEMS)["number"]
    desk.record_payment(b, 10)
    with pytest.raises(BizDeskError, match="payments recorded"):
        desk.void_invoice(b, "mistake")
    with pytest.raises(BizDeskError, match="reason is required"):
        desk.void_invoice(a, "")
    v = desk.void_invoice(a, "Duplicate")
    assert v["status"] == "void" and v["balance_due"] == "$0.00" and v["void_reason"] == "Duplicate"
    with pytest.raises(BizDeskError, match="void"):
        desk.record_payment(a, 5)


def test_overdue_needs_sent_status(desk):
    cid = add(desk)["id"]
    inv = desk.create_invoice(cid, ITEMS, issue_date="2026-08-01", due_in_days=14)["number"]
    assert desk.overdue_report()["overdue_invoices"] == 0  # drafts are never overdue
    desk.mark_invoice_sent(inv)
    rep = desk.overdue_report()
    assert rep["overdue_invoices"] == 1 and rep["invoices"][0]["days_overdue"] == 61
    assert rep["by_age"]["61-90 days"] == "$345.00"
    assert desk.overdue_report(as_of="2026-08-10")["overdue_invoices"] == 0


def test_demo_reports(demo_desk):
    rep = demo_desk.overdue_report()
    assert rep["total_overdue"] == "$3,145.45"
    assert [r["number"] for r in rep["invoices"]] == ["INV-0004", "INV-0003", "INV-0006"]
    rev = demo_desk.revenue_report()
    assert rev["period"] == {"start": "2026-05-01", "end": "2026-10-15"}
    assert rev["invoiced"] == "$8,465.15" and rev["collected"] == "$5,319.70"  # drafts are not counted
    assert rev["outstanding_now"] == "$3,145.45"
    assert rev["top_clients"][0]["client"] == "Harbor View Apartments"
    assert sum(m["invoices"] for m in rev["by_month"]) == 6
    with pytest.raises(BizDeskError):
        demo_desk.revenue_report(start="2026-10-01", end="2026-09-01")
    clients = {c["company"] or c["name"]: c for c in demo_desk.find_clients()}
    assert clients["Aisha Bello"]["total_invoiced"] == "$0.00"  # her only invoice is still a draft
    assert clients["Elm & Oak Law Office"]["outstanding"] == "$893.85"
    assert clients["Elm & Oak Law Office"]["overdue_invoices"] == 1


@pytest.mark.parametrize(
    "tone, word", [("friendly", "Friendly reminder"), ("firm", "Payment overdue"), ("final", "Final notice")]
)
def test_reminder_drafts(demo_desk, tone, word):
    r = demo_desk.draft_payment_reminder("INV-0004", tone)
    assert r["to"] == "tom@elmandoak.example" and word in r["subject"]
    assert "Hi Tom" in r["body"] and "$893.85" in r["body"] and "61 days past" in r["body"]
    assert "Bank transfer" in r["body"] and "does not send email" in r["note"]


def test_reminder_refuses_paid(demo_desk):
    with pytest.raises(BizDeskError, match="nothing due"):
        demo_desk.draft_payment_reminder("INV-0005")


def test_read_only_mode(demo_desk):
    ro = BizDesk(demo_desk.store, read_only=True, today=lambda: TODAY)
    assert ro.overdue_report()["overdue_invoices"] == 3  # reads still work
    with pytest.raises(ReadOnlyError):
        ro.add_client("New Person")
    with pytest.raises(ReadOnlyError):
        ro.record_payment("INV-0004", 1)


def test_audit_log(demo_desk):
    log = demo_desk.recent_activity(500)
    actions = {(e["action"], e["entity"]) for e in log}
    assert ("payment", "invoice") in actions and ("create", "client") in actions and ("accept", "quote") in actions


def test_data_survives_restart(tmp_path):
    path = tmp_path / "biz.db"
    d1 = BizDesk(Store(path), today=lambda: date(2026, 1, 2))
    cid = add(d1)["id"]
    d1.create_invoice(cid, ITEMS)
    d1.store.close()
    d2 = BizDesk(Store(path))
    assert d2.get_document("INV-0001")["client"]["name"] == "Ana Diaz"
    assert d2.create_invoice(cid, ITEMS)["number"] == "INV-0002"


def test_failed_write_rolls_back(desk, monkeypatch):
    cid = add(desk)["id"]
    original = desk.store.audit

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(desk.store, "audit", boom)
    with pytest.raises(RuntimeError):
        desk.create_invoice(cid, ITEMS)
    monkeypatch.setattr(desk.store, "audit", original)
    assert desk.list_documents() == []
    assert desk.create_invoice(cid, ITEMS)["number"] == "INV-0001"  # number was not burned
