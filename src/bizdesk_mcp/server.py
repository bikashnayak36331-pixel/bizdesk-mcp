"""The MCP server: exposes BizDesk to Claude (or any MCP client) as tools, resources and prompts."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from .pdf import render_document_pdf
from .service import BizDesk, BizDeskError, ReadOnlyError
from .store import Store

T = TypeVar("T")

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

INSTRUCTIONS = """\
BizDesk is a small-business back office: clients, quotes, invoices, payments and reports,
stored in a local SQLite file. Money amounts are plain numbers in the business currency
(e.g. 1250.50). Dates use YYYY-MM-DD. Look up a client's id with find_clients before creating
quotes or invoices. BizDesk never sends email: draft_payment_reminder returns text for the user
to send. Ask the user before voiding invoices or recording payments they did not mention."""


class LineItem(BaseModel):
    description: Annotated[str, Field(description="What was sold, e.g. 'Deep clean, 3-bedroom house'")]
    quantity: Annotated[float | str, Field(description="How many (hours, units...). Up to 3 decimals.")] = 1
    unit_price: Annotated[float | str, Field(description="Price for one unit, e.g. 45 or '45.00'")]


def _call(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Turn expected business errors into clean tool errors the model can act on."""
    try:
        return fn(*args, **kwargs)
    except (BizDeskError, ReadOnlyError) as e:
        raise ToolError(str(e)) from None


def _items(items: list[LineItem]) -> list[dict[str, Any]]:
    return [i.model_dump() for i in items]


def document_markdown(doc: dict[str, Any]) -> str:
    lines = [
        f"# {doc['kind'].title()} {doc['number']} ({doc['status']})",
        f"Client: {doc['client']['name']}" + (f", {doc['client']['company']}" if doc["client"]["company"] else ""),
        f"Issued: {doc['issue_date']}  |  "
        + (f"Due: {doc['due_date']}" if doc["kind"] == "invoice" else f"Valid until: {doc['valid_until']}"),
        "",
        "| Description | Qty | Unit price | Amount |",
        "|---|---:|---:|---:|",
    ]
    lines += [f"| {i['description']} | {i['quantity']} | {i['unit_price']} | {i['line_total']} |" for i in doc["items"]]
    lines += [
        "",
        f"Subtotal: {doc['subtotal']}",
        f"Tax ({doc['tax_rate']}): {doc['tax']}",
        f"**Total: {doc['total']}**",
    ]
    if doc["kind"] == "invoice":
        lines += [f"Paid: {doc['paid']}", f"**Balance due: {doc['balance_due']}**"]
        if doc["overdue"]:
            lines.append(f"Overdue by {doc['days_overdue']} days")
    if doc.get("notes"):
        lines += ["", f"Notes: {doc['notes']}"]
    return "\n".join(lines)


def create_server(desk: BizDesk, output_dir: str | Path) -> MCPServer:
    output_dir = Path(output_dir).expanduser()
    mode = " (read-only)" if desk.read_only else ""
    server = MCPServer(
        name="bizdesk",
        title=f"BizDesk{mode}",
        version=__version__,
        instructions=INSTRUCTIONS,
        website_url="https://github.com/bikashnayak36331-pixel/bizdesk-mcp",
    )

    # ---------------------------------------------------------------- profile
    @server.tool(annotations=READ)
    def get_business_profile() -> dict[str, Any]:
        """Show the business profile used on quotes and invoices (name, currency, tax rate, terms)."""
        return _call(desk.get_profile)

    @server.tool(annotations=WRITE)
    def update_business_profile(
        business_name: str | None = None,
        address: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        tax_id: str | None = None,
        currency: Annotated[str | None, Field(description="3-letter code: USD, EUR, GBP, INR...")] = None,
        default_tax_rate: Annotated[str | None, Field(description="Percent, e.g. '8.25'")] = None,
        payment_terms_days: int | None = None,
        quote_valid_days: int | None = None,
        payment_instructions: Annotated[str | None, Field(description="Printed on unpaid invoices")] = None,
    ) -> dict[str, Any]:
        """Change business details. Only the fields you pass are changed."""
        changes = {k: v for k, v in locals().items() if v is not None}
        return _call(desk.update_profile, changes)

    # ---------------------------------------------------------------- clients
    @server.tool(annotations=WRITE)
    def add_client(
        name: Annotated[str, Field(description="Contact person's name")],
        company: str = "",
        email: str = "",
        phone: str = "",
        address: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        """Add a new client. Returns the client with its id."""
        return _call(desk.add_client, name, company, email, phone, address, notes)

    @server.tool(annotations=READ)
    def find_clients(
        query: Annotated[str, Field(description="Part of a name, company or email. Empty lists everyone.")] = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search clients and see what each has been invoiced, has paid and still owes."""
        return _call(desk.find_clients, query, limit)

    @server.tool(annotations=READ)
    def get_client(client_id: int) -> dict[str, Any]:
        """Full details and balances for one client."""
        return _call(desk.get_client, client_id)

    @server.tool(annotations=WRITE)
    def update_client(
        client_id: int,
        name: str | None = None,
        company: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        address: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Change a client's details. Only the fields you pass are changed."""
        changes = {k: v for k, v in locals().items() if k != "client_id" and v is not None}
        return _call(desk.update_client, client_id, changes)

    # ---------------------------------------------------------------- quotes & invoices
    @server.tool(annotations=WRITE)
    def create_quote(
        client_id: int,
        items: Annotated[list[LineItem], Field(min_length=1, max_length=100)],
        tax_rate: Annotated[str | None, Field(description="Percent; defaults to the profile rate")] = None,
        valid_days: Annotated[int | None, Field(description="Days the quote stays valid")] = None,
        notes: str = "",
    ) -> dict[str, Any]:
        """Create a quote (estimate) for a client. Totals and tax are calculated for you."""
        return _call(desk.create_quote, client_id, _items(items), tax_rate, valid_days, notes)

    @server.tool(annotations=WRITE)
    def create_invoice(
        client_id: int,
        items: Annotated[list[LineItem], Field(min_length=1, max_length=100)],
        tax_rate: Annotated[str | None, Field(description="Percent; defaults to the profile rate")] = None,
        due_in_days: Annotated[int | None, Field(description="Defaults to the profile payment terms")] = None,
        notes: str = "",
        issue_date: Annotated[str | None, Field(description="YYYY-MM-DD; defaults to today")] = None,
    ) -> dict[str, Any]:
        """Create a draft invoice. Use mark_invoice_sent once it has gone to the client."""
        return _call(desk.create_invoice, client_id, _items(items), tax_rate, due_in_days, notes, issue_date)

    @server.tool(annotations=WRITE)
    def convert_quote_to_invoice(quote_number: str, due_in_days: int | None = None) -> dict[str, Any]:
        """Mark a quote as accepted and create the matching invoice (same items and tax)."""
        return _call(desk.convert_quote_to_invoice, quote_number, due_in_days)

    @server.tool(annotations=WRITE)
    def set_quote_status(quote_number: str, status: Literal["sent", "accepted", "declined"]) -> dict[str, Any]:
        """Record that a quote was sent, accepted or declined."""
        return _call(desk.set_quote_status, quote_number, status)

    @server.tool(annotations=WRITE)
    def mark_invoice_sent(invoice_number: str) -> dict[str, Any]:
        """Record that an invoice was sent to the client (it then counts towards overdue reports)."""
        return _call(desk.mark_invoice_sent, invoice_number)

    @server.tool(annotations=DESTRUCTIVE)
    def void_invoice(invoice_number: str, reason: str) -> dict[str, Any]:
        """Cancel an invoice that has no payments. The record is kept with the reason."""
        return _call(desk.void_invoice, invoice_number, reason)

    @server.tool(annotations=READ)
    def get_document(
        number: Annotated[str, Field(description="Quote or invoice number, e.g. INV-0007")],
    ) -> dict[str, Any]:
        """Everything about one quote or invoice: items, totals, payments, balance, overdue days."""
        return _call(desk.get_document, number)

    @server.tool(annotations=READ)
    def list_invoices(
        status: Literal["draft", "sent", "partially_paid", "paid", "void"] | None = None,
        client_id: int | None = None,
        overdue_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List invoices, newest first, optionally filtered by status, client or overdue."""
        return _call(desk.list_documents, "invoice", status, client_id, overdue_only, limit)

    @server.tool(annotations=READ)
    def list_quotes(
        status: Literal["draft", "sent", "accepted", "declined", "expired"] | None = None,
        client_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List quotes, newest first."""
        return _call(desk.list_documents, "quote", status, client_id, False, limit)

    # ---------------------------------------------------------------- payments & reports
    @server.tool(annotations=WRITE)
    def record_payment(
        invoice_number: str,
        amount: Annotated[float | str, Field(description="Amount received, e.g. 500 or '500.00'")],
        paid_on: Annotated[str | None, Field(description="YYYY-MM-DD; defaults to today")] = None,
        method: Annotated[str, Field(description="e.g. bank transfer, card, cash")] = "",
        reference: str = "",
    ) -> dict[str, Any]:
        """Record money received against an invoice. Overpayments are refused."""
        return _call(desk.record_payment, invoice_number, amount, paid_on, method, reference)

    @server.tool(annotations=READ)
    def overdue_report(
        as_of: Annotated[str | None, Field(description="YYYY-MM-DD; defaults to today")] = None,
    ) -> dict[str, Any]:
        """Who owes money past the due date, grouped by how late (1-30, 31-60, 61-90, 90+ days)."""
        return _call(desk.overdue_report, as_of)

    @server.tool(annotations=READ)
    def revenue_report(
        start: Annotated[str | None, Field(description="YYYY-MM-DD; defaults to 6 months ago")] = None,
        end: Annotated[str | None, Field(description="YYYY-MM-DD; defaults to today")] = None,
    ) -> dict[str, Any]:
        """Invoiced vs collected by month (drafts and voids excluded), money still outstanding, top clients."""
        return _call(desk.revenue_report, start, end)

    @server.tool(annotations=READ)
    def draft_payment_reminder(
        invoice_number: str, tone: Literal["friendly", "firm", "final"] = "friendly"
    ) -> dict[str, Any]:
        """Write a payment-reminder email for an unpaid invoice. Draft only: nothing is sent."""
        return _call(desk.draft_payment_reminder, invoice_number, tone)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def export_pdf(number: Annotated[str, Field(description="Quote or invoice number")]) -> dict[str, Any]:
        """Save a quote or invoice as a PDF file and return where it was saved."""
        doc = _call(desk.get_document, number)
        path = render_document_pdf(doc, desk.get_profile(), output_dir / f"{doc['number']}.pdf")
        return {"number": doc["number"], "path": str(path), "bytes": path.stat().st_size}

    @server.tool(annotations=READ)
    def recent_activity(limit: int = 20) -> list[dict[str, Any]]:
        """The audit log: what changed, and when."""
        return _call(desk.recent_activity, limit)

    # ---------------------------------------------------------------- resources
    @server.resource("bizdesk://profile", name="Business profile", mime_type="application/json")
    def profile_resource() -> dict[str, Any]:
        return desk.get_profile()

    @server.resource("bizdesk://reports/overdue", name="Overdue invoices", mime_type="application/json")
    def overdue_resource() -> dict[str, Any]:
        return desk.overdue_report()

    @server.resource("bizdesk://documents/{number}", name="Quote or invoice", mime_type="text/markdown")
    def document_resource(number: str) -> str:
        try:
            return document_markdown(desk.get_document(number))
        except BizDeskError as e:
            raise ResourceNotFoundError(str(e)) from None

    # ---------------------------------------------------------------- prompts
    @server.prompt(title="Month-end review")
    def month_end_review(month: Annotated[str, Field(description="YYYY-MM")]) -> str:
        """Review a month: revenue, collections, overdue invoices and reminder drafts."""
        return (
            f"Do a month-end review for {month} using the BizDesk tools.\n"
            f"1. Run revenue_report for that month and summarise invoiced vs collected.\n"
            f"2. Run overdue_report and list who owes what, oldest first.\n"
            f"3. For each invoice more than 14 days overdue, call draft_payment_reminder "
            f"(friendly under 30 days, firm otherwise) and show me the drafts.\n"
            f"4. Finish with three short, practical suggestions to improve cash flow."
        )

    @server.prompt(title="Quote a new job")
    def quote_new_job(client: str, job: str) -> str:
        """Turn a job description into a quote for a client."""
        return (
            f"Prepare a quote for {client} for this job: {job}\n"
            "Find the client with find_clients (ask me before adding a new one). Break the job into "
            "clear line items with quantities and unit prices, show me the draft lines first, and only "
            "call create_quote after I confirm. Then offer to export the PDF."
        )

    return server


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main(argv: list[str] | None = None) -> None:
    home = Path.home() / ".bizdesk"
    parser = argparse.ArgumentParser(prog="bizdesk-mcp", description="BizDesk MCP server")
    parser.add_argument(
        "--db", default=os.environ.get("BIZDESK_DB", str(home / "bizdesk.db")), help="SQLite file (env BIZDESK_DB)"
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("BIZDESK_OUTPUT_DIR", str(home / "pdfs")),
        help="Where PDFs are saved (env BIZDESK_OUTPUT_DIR)",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        default=_env_flag("BIZDESK_READ_ONLY"),
        help="Refuse all changes (env BIZDESK_READ_ONLY=1)",
    )
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed-demo", action="store_true", help="Load sample data into an empty database, then exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    store = Store(Path(args.db).expanduser())
    if args.seed_demo:
        from .demo import seed_demo

        summary = seed_demo(BizDesk(store))
        print(f"Loaded demo data into {args.db}: {summary}", file=sys.stderr)
        return
    desk = BizDesk(store, read_only=args.read_only)
    server = create_server(desk, args.output_dir)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
