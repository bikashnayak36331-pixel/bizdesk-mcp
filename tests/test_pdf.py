from pypdf import PdfReader

from bizdesk_mcp.pdf import render_document_pdf


def test_invoice_pdf_contents(demo_desk, tmp_path):
    doc = demo_desk.get_document("INV-0004")
    path = render_document_pdf(doc, demo_desk.get_profile(), tmp_path / "out" / "inv.pdf")
    reader = PdfReader(str(path))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    for expected in (
        "Brightway Cleaning Co.",
        "INVOICE",
        "INV-0004",
        "Elm & Oak Law Office",
        "$1,493.85",
        "$893.85",
        "PART PAID",
        "check",
        "HOW TO PAY",
    ):
        assert expected in text, expected
    assert reader.metadata.title == "Invoice INV-0004"


def test_quote_pdf(demo_desk, tmp_path):
    number = demo_desk.list_documents("quote")[0]["number"]
    doc = demo_desk.get_document(number)
    text = PdfReader(str(render_document_pdf(doc, demo_desk.get_profile(), tmp_path / "q.pdf"))).pages[0].extract_text()
    assert "QUOTE" in text and "VALID UNTIL" in text and "HOW TO PAY" not in text


def test_long_invoice_spills_to_second_page(desk, tmp_path):
    cid = desk.add_client("Big Co")["id"]
    items = [{"description": f"Line {i}: weekly service", "unit_price": 10} for i in range(60)]
    doc = desk.get_document(desk.create_invoice(cid, items)["number"])
    reader = PdfReader(str(render_document_pdf(doc, desk.get_profile(), tmp_path / "long.pdf")))
    assert len(reader.pages) >= 2
    assert "$600.00" in "".join(p.extract_text() for p in reader.pages)
