"""Business logic for BizDesk: clients, quotes, invoices, payments and reports.

Every public method returns plain JSON-friendly dicts so the MCP layer can hand
them straight to the model. Amounts are shown formatted (e.g. "$1,250.00") and,
for totals, also as integer cents (e.g. ``total_cents``) for exact maths.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import date, timedelta
from typing import Any

from .money import (
    MoneyError,
    format_money,
    format_quantity,
    format_rate,
    line_total_cents,
    rate_to_bp,
    tax_cents,
    to_cents,
    to_milli,
)
from .store import Store, utc_now


class BizDeskError(ValueError):
    """A problem the user (or the model) can fix, with a clear message."""


class ReadOnlyError(PermissionError):
    pass


DEFAULT_PROFILE: dict[str, str] = {
    "business_name": "My Business",
    "address": "",
    "email": "",
    "phone": "",
    "tax_id": "",
    "currency": "USD",
    "default_tax_rate": "0",
    "payment_terms_days": "14",
    "quote_valid_days": "30",
    "invoice_prefix": "INV-",
    "quote_prefix": "Q-",
    "payment_instructions": "",
}

INVOICE_STATUSES = ("draft", "sent", "partially_paid", "paid", "void")
QUOTE_STATUSES = ("draft", "sent", "accepted", "declined", "expired")
OPEN_INVOICE_STATUSES = ("sent", "partially_paid")
MAX_ITEMS = 100
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _parse_date(value: str | None, what: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise BizDeskError(f"{what} must be a date like 2026-10-15, got '{value}'") from None


def _clean(text: object, what: str, max_len: int, required: bool = False) -> str:
    s = "" if text is None else str(text).strip()
    if required and not s:
        raise BizDeskError(f"{what} is required")
    if len(s) > max_len:
        raise BizDeskError(f"{what} is too long (max {max_len} characters)")
    return s


class BizDesk:
    def __init__(
        self,
        store: Store,
        *,
        read_only: bool = False,
        today: Callable[[], date] = date.today,
    ):
        self.store = store
        self.db = store.conn
        self.read_only = read_only
        self._today = today

    # ------------------------------------------------------------------ helpers
    def today(self) -> date:
        return self._today()

    def _require_write(self) -> None:
        if self.read_only:
            raise ReadOnlyError("BizDesk is running in read-only mode (BIZDESK_READ_ONLY=1), so it cannot change data.")

    def _setting(self, key: str) -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else DEFAULT_PROFILE.get(key, "")

    def _next_number(self, kind: str) -> str:
        key = f"next_{kind}_seq"
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        seq = int(row["value"]) if row else 1
        self.db.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(seq + 1)),
        )
        prefix = self._setting("invoice_prefix" if kind == "invoice" else "quote_prefix")
        return f"{prefix}{seq:04d}"

    # ------------------------------------------------------------------ profile
    def get_profile(self) -> dict[str, Any]:
        profile = dict(DEFAULT_PROFILE)
        for row in self.db.execute("SELECT key, value FROM settings"):
            if row["key"] in profile:
                profile[row["key"]] = row["value"]
        profile["default_tax_rate"] = format_rate(rate_to_bp(profile["default_tax_rate"]))
        return profile

    def update_profile(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        self._require_write()
        clean: dict[str, str] = {}
        for key, value in changes.items():
            if value is None:
                continue
            if key not in DEFAULT_PROFILE:
                raise BizDeskError(f"Unknown profile field '{key}'. Allowed: {', '.join(DEFAULT_PROFILE)}")
            if key == "currency":
                v = str(value).strip().upper()
                if not re.fullmatch(r"[A-Z]{3}", v):
                    raise BizDeskError("currency must be a 3-letter ISO code such as USD, EUR, GBP or INR")
            elif key == "default_tax_rate":
                v = str(value).strip().rstrip("%")
                try:
                    rate_to_bp(v)
                except MoneyError as e:
                    raise BizDeskError(str(e)) from None
            elif key in ("payment_terms_days", "quote_valid_days"):
                try:
                    days = int(value)
                except (TypeError, ValueError):
                    raise BizDeskError(f"{key} must be a whole number of days") from None
                if not 0 <= days <= 365:
                    raise BizDeskError(f"{key} must be between 0 and 365")
                v = str(days)
            elif key == "email":
                v = _clean(value, "email", 200)
                if v and not _EMAIL.match(v):
                    raise BizDeskError(f"'{v}' does not look like an email address")
            else:
                v = _clean(value, key, 500)
            clean[key] = v
        if not clean:
            raise BizDeskError("Nothing to update: pass at least one profile field")
        with self.store.transaction():
            for key, v in clean.items():
                self.db.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, v),
                )
            self.store.audit("update", "profile", "profile", **clean)
        return self.get_profile()

    # ------------------------------------------------------------------ clients
    def add_client(
        self,
        name: str,
        company: str = "",
        email: str = "",
        phone: str = "",
        address: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        self._require_write()
        name = _clean(name, "name", 120, required=True)
        email = _clean(email, "email", 200)
        if email and not _EMAIL.match(email):
            raise BizDeskError(f"'{email}' does not look like an email address")
        values = (
            name,
            _clean(company, "company", 120),
            email,
            _clean(phone, "phone", 40),
            _clean(address, "address", 300),
            _clean(notes, "notes", 1000),
            utc_now(),
        )
        with self.store.transaction():
            dup = self.db.execute(
                "SELECT id FROM clients WHERE lower(name) = lower(?) AND lower(company) = lower(?)",
                (values[0], values[1]),
            ).fetchone()
            if dup:
                raise BizDeskError(
                    f"A client named '{name}' at '{values[1] or 'no company'}' already exists (id {dup['id']})"
                )
            cur = self.db.execute(
                "INSERT INTO clients(name, company, email, phone, address, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            self.store.audit("create", "client", cur.lastrowid, name=name)
        return self.get_client(cur.lastrowid)

    def update_client(self, client_id: int, changes: Mapping[str, Any]) -> dict[str, Any]:
        self._require_write()
        self._client_row(client_id)
        limits = {"name": 120, "company": 120, "email": 200, "phone": 40, "address": 300, "notes": 1000}
        clean = {}
        for key, value in changes.items():
            if value is None:
                continue
            if key not in limits:
                raise BizDeskError(f"Unknown client field '{key}'. Allowed: {', '.join(limits)}")
            clean[key] = _clean(value, key, limits[key], required=(key == "name"))
            if key == "email" and clean[key] and not _EMAIL.match(clean[key]):
                raise BizDeskError(f"'{clean[key]}' does not look like an email address")
        if not clean:
            raise BizDeskError("Nothing to update: pass at least one client field")
        sets = ", ".join(f"{k} = ?" for k in clean)
        with self.store.transaction():
            self.db.execute(f"UPDATE clients SET {sets} WHERE id = ?", (*clean.values(), client_id))
            self.store.audit("update", "client", client_id, **clean)
        return self.get_client(client_id)

    def _client_row(self, client_id: int):
        row = self.db.execute("SELECT * FROM clients WHERE id = ?", (int(client_id),)).fetchone()
        if not row:
            raise BizDeskError(f"No client with id {client_id}. Use find_clients to look up the id.")
        return row

    def find_clients(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        q = f"%{_clean(query, 'query', 100)}%"
        limit = max(1, min(int(limit), 100))
        rows = self.db.execute(
            "SELECT * FROM clients WHERE name LIKE ? OR company LIKE ? OR email LIKE ? "
            "ORDER BY name COLLATE NOCASE LIMIT ?",
            (q, q, q, limit),
        ).fetchall()
        return [self._client_summary(r) for r in rows]

    def get_client(self, client_id: int) -> dict[str, Any]:
        row = self._client_row(client_id)
        out = self._client_summary(row)
        out.update({"phone": row["phone"], "address": row["address"], "notes": row["notes"]})
        return out

    def _client_summary(self, row) -> dict[str, Any]:
        currency = self._setting("currency")
        invoiced = paid = outstanding = 0
        overdue = 0
        for doc in self._doc_rows(
            "WHERE d.kind = 'invoice' AND d.client_id = ? AND d.status NOT IN ('void', 'draft')", (row["id"],)
        ):
            t = self._totals(doc)
            invoiced += t["total"]
            paid += t["paid"]
            if doc["status"] in OPEN_INVOICE_STATUSES:
                outstanding += t["balance"]
                if self._days_overdue(doc, t["balance"]) > 0:
                    overdue += 1
        return {
            "id": row["id"],
            "name": row["name"],
            "company": row["company"],
            "email": row["email"],
            "total_invoiced": format_money(invoiced, currency),
            "total_paid": format_money(paid, currency),
            "outstanding": format_money(outstanding, currency),
            "outstanding_cents": outstanding,
            "overdue_invoices": overdue,
        }

    # ------------------------------------------------------------------ documents
    def _parse_items(self, items: Iterable[Mapping[str, Any]]) -> list[tuple[str, int, int]]:
        parsed = []
        items = list(items or [])
        if not items:
            raise BizDeskError("Add at least one line item (description, quantity, unit_price)")
        if len(items) > MAX_ITEMS:
            raise BizDeskError(f"Too many line items (max {MAX_ITEMS})")
        for i, item in enumerate(items, 1):
            if not isinstance(item, Mapping):
                raise BizDeskError(f"Line {i} must be an object with description, quantity and unit_price")
            desc = _clean(item.get("description"), f"line {i} description", 200, required=True)
            try:
                qty = to_milli(item.get("quantity", 1), what=f"line {i} quantity")
                price = to_cents(item.get("unit_price"), what=f"line {i} unit_price")
            except MoneyError as e:
                raise BizDeskError(str(e)) from None
            parsed.append((desc, qty, price))
        return parsed

    def _tax_bp(self, tax_rate: object | None) -> int:
        try:
            if tax_rate is None or tax_rate == "":
                return rate_to_bp(self._setting("default_tax_rate"))
            return rate_to_bp(str(tax_rate).strip().rstrip("%"))
        except MoneyError as e:
            raise BizDeskError(str(e)) from None

    def _insert_document(
        self,
        kind: str,
        client_id: int,
        items: list[tuple[str, int, int]],
        tax_bp: int,
        issue: date,
        due: date | None,
        notes: str,
        source_quote_id: int | None = None,
    ) -> str:
        number = self._next_number(kind)
        cur = self.db.execute(
            "INSERT INTO documents(kind, number, client_id, status, issue_date, due_date, currency, "
            "tax_rate_bp, notes, source_quote_id, created_at) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                number,
                client_id,
                issue.isoformat(),
                due.isoformat() if due else None,
                self._setting("currency"),
                tax_bp,
                notes,
                source_quote_id,
                utc_now(),
            ),
        )
        self.db.executemany(
            "INSERT INTO line_items(document_id, position, description, quantity_milli, unit_price_cents) "
            "VALUES (?, ?, ?, ?, ?)",
            [(cur.lastrowid, pos, d, q, p) for pos, (d, q, p) in enumerate(items, 1)],
        )
        self.store.audit("create", kind, number, client_id=client_id, lines=len(items))
        return number

    def create_quote(
        self,
        client_id: int,
        items: list[Mapping[str, Any]],
        tax_rate: object | None = None,
        valid_days: int | None = None,
        notes: str = "",
        issue_date: str | None = None,
    ) -> dict[str, Any]:
        self._require_write()
        self._client_row(client_id)
        parsed = self._parse_items(items)
        issue = _parse_date(issue_date, "issue_date") or self.today()
        days = int(valid_days) if valid_days is not None else int(self._setting("quote_valid_days"))
        if not 0 <= days <= 365:
            raise BizDeskError("valid_days must be between 0 and 365")
        with self.store.transaction():
            number = self._insert_document(
                "quote",
                client_id,
                parsed,
                self._tax_bp(tax_rate),
                issue,
                issue + timedelta(days=days),
                _clean(notes, "notes", 1000),
            )
        return self.get_document(number)

    def create_invoice(
        self,
        client_id: int,
        items: list[Mapping[str, Any]],
        tax_rate: object | None = None,
        due_in_days: int | None = None,
        notes: str = "",
        issue_date: str | None = None,
    ) -> dict[str, Any]:
        self._require_write()
        self._client_row(client_id)
        parsed = self._parse_items(items)
        issue = _parse_date(issue_date, "issue_date") or self.today()
        days = int(due_in_days) if due_in_days is not None else int(self._setting("payment_terms_days"))
        if not 0 <= days <= 365:
            raise BizDeskError("due_in_days must be between 0 and 365")
        with self.store.transaction():
            number = self._insert_document(
                "invoice",
                client_id,
                parsed,
                self._tax_bp(tax_rate),
                issue,
                issue + timedelta(days=days),
                _clean(notes, "notes", 1000),
            )
        return self.get_document(number)

    def convert_quote_to_invoice(
        self, quote_number: str, due_in_days: int | None = None, issue_date: str | None = None
    ) -> dict[str, Any]:
        self._require_write()
        quote = self._doc_row(quote_number, kind="quote")
        if quote["status"] in ("declined", "expired"):
            raise BizDeskError(f"Quote {quote_number} is {quote['status']} and cannot be invoiced")
        existing = self.db.execute(
            "SELECT number FROM documents WHERE source_quote_id = ? AND status != 'void'", (quote["id"],)
        ).fetchone()
        if existing:
            raise BizDeskError(f"Quote {quote_number} was already invoiced as {existing['number']}")
        items = [
            (r["description"], r["quantity_milli"], r["unit_price_cents"])
            for r in self.db.execute("SELECT * FROM line_items WHERE document_id = ? ORDER BY position", (quote["id"],))
        ]
        issue = _parse_date(issue_date, "issue_date") or self.today()
        days = int(due_in_days) if due_in_days is not None else int(self._setting("payment_terms_days"))
        if not 0 <= days <= 365:
            raise BizDeskError("due_in_days must be between 0 and 365")
        with self.store.transaction():
            self.db.execute("UPDATE documents SET status = 'accepted' WHERE id = ?", (quote["id"],))
            self.store.audit("accept", "quote", quote_number)
            number = self._insert_document(
                "invoice",
                quote["client_id"],
                items,
                quote["tax_rate_bp"],
                issue,
                issue + timedelta(days=days),
                quote["notes"],
                source_quote_id=quote["id"],
            )
        return self.get_document(number)

    def set_quote_status(self, quote_number: str, status: str) -> dict[str, Any]:
        self._require_write()
        if status not in ("sent", "accepted", "declined"):
            raise BizDeskError("status must be one of: sent, accepted, declined")
        quote = self._doc_row(quote_number, kind="quote")
        with self.store.transaction():
            self.db.execute(
                "UPDATE documents SET status = ?, sent_at = COALESCE(sent_at, ?) WHERE id = ?",
                (status, utc_now() if status == "sent" else None, quote["id"]),
            )
            self.store.audit(status, "quote", quote_number)
        return self.get_document(quote_number)

    def mark_invoice_sent(self, invoice_number: str) -> dict[str, Any]:
        self._require_write()
        inv = self._doc_row(invoice_number, kind="invoice")
        if inv["status"] == "void":
            raise BizDeskError(f"Invoice {invoice_number} is void")
        with self.store.transaction():
            self.db.execute(
                "UPDATE documents SET status = CASE WHEN status = 'draft' THEN 'sent' ELSE status END, "
                "sent_at = COALESCE(sent_at, ?) WHERE id = ?",
                (utc_now(), inv["id"]),
            )
            self.store.audit("send", "invoice", invoice_number)
        return self.get_document(invoice_number)

    def void_invoice(self, invoice_number: str, reason: str) -> dict[str, Any]:
        self._require_write()
        inv = self._doc_row(invoice_number, kind="invoice")
        reason = _clean(reason, "reason", 300, required=True)
        paid = self.db.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS p FROM payments WHERE document_id = ?", (inv["id"],)
        ).fetchone()["p"]
        if paid:
            raise BizDeskError(f"Invoice {invoice_number} has payments recorded and cannot be voided")
        with self.store.transaction():
            self.db.execute("UPDATE documents SET status = 'void', void_reason = ? WHERE id = ?", (reason, inv["id"]))
            self.store.audit("void", "invoice", invoice_number, reason=reason)
        return self.get_document(invoice_number)

    # ------------------------------------------------------------------ payments
    def record_payment(
        self,
        invoice_number: str,
        amount: object,
        paid_on: str | None = None,
        method: str = "",
        reference: str = "",
    ) -> dict[str, Any]:
        self._require_write()
        inv = self._doc_row(invoice_number, kind="invoice")
        if inv["status"] == "void":
            raise BizDeskError(f"Invoice {invoice_number} is void and cannot take payments")
        try:
            cents = to_cents(amount, what="amount")
        except MoneyError as e:
            raise BizDeskError(str(e)) from None
        if cents <= 0:
            raise BizDeskError("amount must be greater than zero")
        totals = self._totals(inv)
        if cents > totals["balance"]:
            raise BizDeskError(
                f"Payment of {format_money(cents, inv['currency'])} is more than the balance due "
                f"({format_money(totals['balance'], inv['currency'])}) on {invoice_number}"
            )
        when = _parse_date(paid_on, "paid_on") or self.today()
        with self.store.transaction():
            self.db.execute(
                "INSERT INTO payments(document_id, amount_cents, paid_on, method, reference, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    inv["id"],
                    cents,
                    when.isoformat(),
                    _clean(method, "method", 40),
                    _clean(reference, "reference", 100),
                    utc_now(),
                ),
            )
            new_status = "paid" if cents == totals["balance"] else "partially_paid"
            self.db.execute(
                "UPDATE documents SET status = ?, sent_at = COALESCE(sent_at, ?) WHERE id = ?",
                (new_status, utc_now(), inv["id"]),
            )
            self.store.audit("payment", "invoice", invoice_number, amount_cents=cents, paid_on=when.isoformat())
        return self.get_document(invoice_number)

    # ------------------------------------------------------------------ reading documents
    def _doc_rows(self, where: str = "", params: tuple = ()) -> list:
        return self.db.execute(
            "SELECT d.*, c.name AS client_name, c.company AS client_company, c.email AS client_email, "
            "c.address AS client_address FROM documents d JOIN clients c ON c.id = d.client_id "
            f"{where} ORDER BY d.issue_date, d.id",
            params,
        ).fetchall()

    def _doc_row(self, number: str, kind: str | None = None):
        number = _clean(number, "number", 40, required=True).upper()
        rows = self._doc_rows("WHERE upper(d.number) = ?", (number,))
        if not rows:
            raise BizDeskError(f"No quote or invoice numbered '{number}'. Use list_invoices to find numbers.")
        row = rows[0]
        if kind and row["kind"] != kind:
            raise BizDeskError(f"{number} is a {row['kind']}, not a {kind}")
        return row

    def _totals(self, doc) -> dict[str, int]:
        lines = self.db.execute(
            "SELECT quantity_milli, unit_price_cents FROM line_items WHERE document_id = ?", (doc["id"],)
        ).fetchall()
        subtotal = sum(line_total_cents(r["quantity_milli"], r["unit_price_cents"]) for r in lines)
        tax = tax_cents(subtotal, doc["tax_rate_bp"])
        paid = self.db.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS p FROM payments WHERE document_id = ?", (doc["id"],)
        ).fetchone()["p"]
        total = subtotal + tax
        return {"subtotal": subtotal, "tax": tax, "total": total, "paid": paid, "balance": total - paid}

    def _days_overdue(self, doc, balance: int, as_of: date | None = None) -> int:
        if doc["kind"] != "invoice" or doc["status"] not in OPEN_INVOICE_STATUSES or balance <= 0:
            return 0
        due = date.fromisoformat(doc["due_date"])
        return max(0, ((as_of or self.today()) - due).days)

    def get_document(self, number: str) -> dict[str, Any]:
        doc = self._doc_row(number)
        cur = doc["currency"]
        t = self._totals(doc)
        lines = self.db.execute(
            "SELECT * FROM line_items WHERE document_id = ? ORDER BY position", (doc["id"],)
        ).fetchall()
        payments = self.db.execute(
            "SELECT * FROM payments WHERE document_id = ? ORDER BY paid_on, id", (doc["id"],)
        ).fetchall()
        source = None
        if doc["source_quote_id"]:
            source = self.db.execute("SELECT number FROM documents WHERE id = ?", (doc["source_quote_id"],)).fetchone()[
                "number"
            ]
        out: dict[str, Any] = {
            "number": doc["number"],
            "kind": doc["kind"],
            "status": doc["status"],
            "client": {
                "id": doc["client_id"],
                "name": doc["client_name"],
                "company": doc["client_company"],
                "email": doc["client_email"],
                "address": doc["client_address"],
            },
            "issue_date": doc["issue_date"],
            ("due_date" if doc["kind"] == "invoice" else "valid_until"): doc["due_date"],
            "currency": cur,
            "items": [
                {
                    "description": r["description"],
                    "quantity": format_quantity(r["quantity_milli"]),
                    "unit_price": format_money(r["unit_price_cents"], cur),
                    "line_total": format_money(line_total_cents(r["quantity_milli"], r["unit_price_cents"]), cur),
                }
                for r in lines
            ],
            "subtotal": format_money(t["subtotal"], cur),
            "tax_rate": format_rate(doc["tax_rate_bp"]),
            "tax": format_money(t["tax"], cur),
            "total": format_money(t["total"], cur),
            "total_cents": t["total"],
            "notes": doc["notes"],
        }
        if doc["kind"] == "invoice":
            days = self._days_overdue(doc, t["balance"])
            out.update(
                {
                    "paid": format_money(t["paid"], cur),
                    "balance_due": format_money(t["balance"] if doc["status"] != "void" else 0, cur),
                    "balance_due_cents": t["balance"] if doc["status"] != "void" else 0,
                    "overdue": days > 0,
                    "days_overdue": days,
                    "payments": [
                        {
                            "amount": format_money(p["amount_cents"], cur),
                            "paid_on": p["paid_on"],
                            "method": p["method"],
                            "reference": p["reference"],
                        }
                        for p in payments
                    ],
                    "from_quote": source,
                }
            )
            if doc["status"] == "void":
                out["void_reason"] = doc["void_reason"]
        return out

    def list_documents(
        self,
        kind: str = "invoice",
        status: str | None = None,
        client_id: int | None = None,
        overdue_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if kind not in ("invoice", "quote"):
            raise BizDeskError("kind must be 'invoice' or 'quote'")
        allowed = INVOICE_STATUSES if kind == "invoice" else QUOTE_STATUSES
        where, params = ["d.kind = ?"], [kind]
        if status:
            if status not in allowed:
                raise BizDeskError(f"status must be one of: {', '.join(allowed)}")
            where.append("d.status = ?")
            params.append(status)
        if client_id is not None:
            self._client_row(client_id)
            where.append("d.client_id = ?")
            params.append(int(client_id))
        out = []
        for doc in self._doc_rows("WHERE " + " AND ".join(where), tuple(params)):
            t = self._totals(doc)
            days = self._days_overdue(doc, t["balance"])
            if overdue_only and days <= 0:
                continue
            row = {
                "number": doc["number"],
                "client": doc["client_company"] or doc["client_name"],
                "status": doc["status"],
                "issue_date": doc["issue_date"],
                ("due_date" if kind == "invoice" else "valid_until"): doc["due_date"],
                "total": format_money(t["total"], doc["currency"]),
            }
            if kind == "invoice":
                row["balance_due"] = format_money(t["balance"] if doc["status"] != "void" else 0, doc["currency"])
                row["days_overdue"] = days
            out.append(row)
        limit = max(1, min(int(limit), 500))
        return out[-limit:][::-1]  # newest first

    # ------------------------------------------------------------------ reports
    def overdue_report(self, as_of: str | None = None) -> dict[str, Any]:
        when = _parse_date(as_of, "as_of") or self.today()
        currency = self._setting("currency")
        buckets = {"1-30 days": 0, "31-60 days": 0, "61-90 days": 0, "over 90 days": 0}
        rows = []
        for doc in self._doc_rows("WHERE d.kind = 'invoice' AND d.status IN ('sent', 'partially_paid')"):
            t = self._totals(doc)
            days = self._days_overdue(doc, t["balance"], as_of=when)
            if days <= 0:
                continue
            bucket = (
                "1-30 days"
                if days <= 30
                else "31-60 days"
                if days <= 60
                else "61-90 days"
                if days <= 90
                else "over 90 days"
            )
            if doc["currency"] == currency:
                buckets[bucket] += t["balance"]
            rows.append(
                {
                    "number": doc["number"],
                    "client": doc["client_company"] or doc["client_name"],
                    "email": doc["client_email"],
                    "due_date": doc["due_date"],
                    "days_overdue": days,
                    "balance_due": format_money(t["balance"], doc["currency"]),
                }
            )
        rows.sort(key=lambda r: -r["days_overdue"])
        total = sum(buckets.values())
        return {
            "as_of": when.isoformat(),
            "overdue_invoices": len(rows),
            "total_overdue": format_money(total, currency),
            "total_overdue_cents": total,
            "by_age": {k: format_money(v, currency) for k, v in buckets.items()},
            "invoices": rows,
        }

    def revenue_report(self, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        """Invoiced (by issue date) vs collected (by payment date) per month.

        Draft and void invoices are not counted as invoiced.
        """
        today = self.today()
        end_d = _parse_date(end, "end") or today
        if start:
            start_d = _parse_date(start, "start")
        else:  # first day of the month, five months back: a six-month view
            y, m = end_d.year, end_d.month - 5
            while m <= 0:
                y, m = y - 1, m + 12
            start_d = date(y, m, 1)
        if start_d > end_d:
            raise BizDeskError("start must be on or before end")
        currency = self._setting("currency")
        months: dict[str, dict[str, int]] = {}
        y, m = start_d.year, start_d.month
        while (y, m) <= (end_d.year, end_d.month):
            months[f"{y:04d}-{m:02d}"] = {"invoiced": 0, "collected": 0, "invoices": 0}
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)

        by_client: dict[str, int] = defaultdict(int)
        skipped_other_currency = 0
        for doc in self._doc_rows(
            "WHERE d.kind = 'invoice' AND d.status NOT IN ('void', 'draft') AND d.issue_date BETWEEN ? AND ?",
            (start_d.isoformat(), end_d.isoformat()),
        ):
            if doc["currency"] != currency:
                skipped_other_currency += 1
                continue
            t = self._totals(doc)
            key = doc["issue_date"][:7]
            months[key]["invoiced"] += t["total"]
            months[key]["invoices"] += 1
            by_client[doc["client_company"] or doc["client_name"]] += t["total"]

        for p in self.db.execute(
            "SELECT p.amount_cents, p.paid_on, d.currency FROM payments p JOIN documents d ON d.id = p.document_id "
            "WHERE p.paid_on BETWEEN ? AND ?",
            (start_d.isoformat(), end_d.isoformat()),
        ):
            if p["currency"] == currency:
                months[p["paid_on"][:7]]["collected"] += p["amount_cents"]

        outstanding = 0
        for doc in self._doc_rows("WHERE d.kind = 'invoice' AND d.status IN ('sent', 'partially_paid')"):
            if doc["currency"] == currency:
                outstanding += self._totals(doc)["balance"]

        inv_total = sum(v["invoiced"] for v in months.values())
        col_total = sum(v["collected"] for v in months.values())
        top = sorted(by_client.items(), key=lambda kv: -kv[1])[:5]
        report = {
            "period": {"start": start_d.isoformat(), "end": end_d.isoformat()},
            "currency": currency,
            "invoiced": format_money(inv_total, currency),
            "collected": format_money(col_total, currency),
            "outstanding_now": format_money(outstanding, currency),
            "invoiced_cents": inv_total,
            "collected_cents": col_total,
            "outstanding_now_cents": outstanding,
            "by_month": [
                {
                    "month": k,
                    "invoices": v["invoices"],
                    "invoiced": format_money(v["invoiced"], currency),
                    "collected": format_money(v["collected"], currency),
                }
                for k, v in months.items()
            ],
            "top_clients": [{"client": c, "invoiced": format_money(v, currency)} for c, v in top],
        }
        if skipped_other_currency:
            report["note"] = f"{skipped_other_currency} invoice(s) in other currencies are not included in these totals"
        return report

    def draft_payment_reminder(self, invoice_number: str, tone: str = "friendly") -> dict[str, Any]:
        """Write a reminder email. It is only a draft: BizDesk never sends email."""
        if tone not in ("friendly", "firm", "final"):
            raise BizDeskError("tone must be friendly, firm or final")
        doc = self.get_document(invoice_number)
        if doc["kind"] != "invoice":
            raise BizDeskError(f"{invoice_number} is a quote; reminders are for invoices")
        if doc["status"] in ("paid", "void") or doc["balance_due_cents"] <= 0:
            raise BizDeskError(f"Invoice {invoice_number} has nothing due ({doc['status']})")
        profile = self.get_profile()
        business = profile["business_name"]
        first = (doc["client"]["name"] or "there").split()[0]
        days = doc["days_overdue"]
        late = f"{days} day{'s' if days != 1 else ''} past its due date" if days else "coming due"
        how_to_pay = f"\n\nHow to pay: {profile['payment_instructions']}" if profile["payment_instructions"] else ""
        if tone == "friendly":
            subject = f"Friendly reminder: invoice {doc['number']} ({doc['balance_due']})"
            opening = (
                f"I hope all is well. This is a quick reminder that invoice {doc['number']}, "
                f"issued on {doc['issue_date']}, is {late} (due {doc['due_date']})."
            )
            close = "If you have already sent payment, thank you, and please ignore this note."
        elif tone == "firm":
            subject = f"Payment overdue: invoice {doc['number']} ({doc['balance_due']})"
            opening = (
                f"Our records show that invoice {doc['number']}, due on {doc['due_date']}, "
                f"is now {late}, with {doc['balance_due']} still outstanding."
            )
            close = "Please arrange payment this week, or reply to let us know when we can expect it."
        else:
            subject = f"Final notice: invoice {doc['number']} ({doc['balance_due']})"
            opening = (
                f"Despite earlier reminders, invoice {doc['number']} (due {doc['due_date']}) remains unpaid "
                f"and is {late}."
            )
            close = (
                "Please settle the balance within 7 days. If there is a problem with the invoice, "
                "reply and we will sort it out together."
            )
        body = (
            f"Hi {first},\n\n{opening}\n\nBalance due: {doc['balance_due']}{how_to_pay}\n\n{close}\n\n"
            f"Thank you,\n{business}"
        )
        return {
            "to": doc["client"]["email"],
            "subject": subject,
            "body": body,
            "note": "Draft only. BizDesk does not send email; copy this into your email app.",
        }

    def recent_activity(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT at, action, entity, entity_id, details FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]
