import json
import subprocess
import sys

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from bizdesk_mcp.server import create_server
from bizdesk_mcp.service import BizDesk

from .conftest import TODAY

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "get_business_profile",
    "update_business_profile",
    "add_client",
    "find_clients",
    "get_client",
    "update_client",
    "create_quote",
    "create_invoice",
    "convert_quote_to_invoice",
    "set_quote_status",
    "mark_invoice_sent",
    "void_invoice",
    "get_document",
    "list_invoices",
    "list_quotes",
    "record_payment",
    "overdue_report",
    "revenue_report",
    "draft_payment_reminder",
    "export_pdf",
    "recent_activity",
}


def data(result):
    assert not result.is_error, result.content
    return result.structured_content.get("result", result.structured_content)


async def test_tools_listed_with_hints(demo_desk, tmp_path):
    async with Client(create_server(demo_desk, tmp_path)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == EXPECTED_TOOLS
    assert tools["overdue_report"].annotations.read_only_hint is True
    assert tools["void_invoice"].annotations.destructive_hint is True
    items = tools["create_invoice"].input_schema["properties"]["items"]
    assert items["minItems"] == 1 and items["maxItems"] == 100


async def test_end_to_end_flow(demo_desk, tmp_path):
    async with Client(create_server(demo_desk, tmp_path)) as c:
        client = data(await c.call_tool("find_clients", {"query": "yoga"}))[0]
        quote = data(
            await c.call_tool(
                "create_quote",
                {
                    "client_id": client["id"],
                    "items": [{"description": "Mirror wall polish", "quantity": 1, "unit_price": "120"}],
                },
            )
        )
        inv = data(await c.call_tool("convert_quote_to_invoice", {"quote_number": quote["number"]}))
        assert inv["total"] == "$129.90"
        paid = data(await c.call_tool("record_payment", {"invoice_number": inv["number"], "amount": 129.9}))
        assert paid["status"] == "paid"
        pdf = data(await c.call_tool("export_pdf", {"number": inv["number"]}))
        assert pdf["path"].endswith(f"{inv['number']}.pdf") and pdf["bytes"] > 1000


async def test_errors_come_back_as_tool_errors(demo_desk, tmp_path):
    async with Client(create_server(demo_desk, tmp_path)) as c:
        r = await c.call_tool("record_payment", {"invoice_number": "INV-0004", "amount": 5000})
        assert r.is_error and "more than the balance" in r.content[0].text
        r = await c.call_tool("create_invoice", {"client_id": 1, "items": []})
        assert r.is_error


async def test_read_only_server_refuses_writes(demo_desk, tmp_path):
    ro = BizDesk(demo_desk.store, read_only=True, today=lambda: TODAY)
    async with Client(create_server(ro, tmp_path)) as c:
        r = await c.call_tool("add_client", {"name": "New"})
        assert r.is_error and "read-only" in r.content[0].text
        assert data(await c.call_tool("overdue_report", {}))["overdue_invoices"] == 3


async def test_resources_and_prompts(demo_desk, tmp_path):
    async with Client(create_server(demo_desk, tmp_path)) as c:
        uris = {str(r.uri) for r in (await c.list_resources()).resources}
        assert uris == {"bizdesk://profile", "bizdesk://reports/overdue"}
        md = (await c.read_resource("bizdesk://documents/INV-0004")).contents[0].text
        assert "Balance due: $893.85" in md and "Overdue by 61 days" in md
        prof = json.loads((await c.read_resource("bizdesk://profile")).contents[0].text)
        assert prof["business_name"] == "Brightway Cleaning Co."
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        assert prompts == {"month_end_review", "quote_new_job"}
        msg = (await c.get_prompt("month_end_review", {"month": "2026-09"})).messages[0].content.text
        assert "2026-09" in msg and "draft_payment_reminder" in msg


async def test_stdio_subprocess(tmp_path):
    """Start the real command-line server over stdio, like Claude Desktop does."""
    db = tmp_path / "biz.db"
    subprocess.run(
        [sys.executable, "-m", "bizdesk_mcp", "--db", str(db), "--seed-demo"], check=True, capture_output=True
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "bizdesk_mcp", "--db", str(db), "--output-dir", str(tmp_path / "pdfs"), "--read-only"],
    )
    async with Client(params) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == EXPECTED_TOOLS
        rep = data(await c.call_tool("revenue_report", {}))
        assert rep["currency"] == "USD" and len(rep["by_month"]) == 6
        r = await c.call_tool("add_client", {"name": "Blocked"})
        assert r.is_error


async def test_streamable_http_transport(tmp_path):
    """Run the server over HTTP (for remote/hosted use) and talk to it with a real client."""
    import socket

    import anyio

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    db = tmp_path / "http.db"
    subprocess.run(
        [sys.executable, "-m", "bizdesk_mcp", "--db", str(db), "--seed-demo"], check=True, capture_output=True
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bizdesk_mcp",
            "--db",
            str(db),
            "--transport",
            "streamable-http",
            "--port",
            str(port),
            "--read-only",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):  # wait up to 10 s for the port to open
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            await anyio.sleep(0.1)
        async with Client(f"http://127.0.0.1:{port}/mcp") as c:
            assert len((await c.list_tools()).tools) == len(EXPECTED_TOOLS)
            assert data(await c.call_tool("overdue_report", {}))["overdue_invoices"] == 3
    finally:
        proc.terminate()
        proc.wait(timeout=10)
