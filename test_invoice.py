"""Tests for BlackRoad Invoice Manager."""
import pytest
from invoice import (
    init_db, create_invoice, get_invoice, send_invoice, mark_paid,
    generate_pdf_text, calculate_overdue_fee, mark_overdue,
    export_to_csv, summary_report, list_invoices, LineItem,
)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test_invoices.db")
    init_db(path)
    return path


def sample_items():
    return [
        LineItem("Web Development", 10, 150.0),
        LineItem("Design Work", 5, 80.0),
    ]


def test_create_invoice(db):
    inv = create_invoice("Acme Corp", "billing@acme.com", sample_items(), path=db)
    assert inv.client_name == "Acme Corp"
    assert inv.status == "draft"
    assert inv.number.startswith("INV-")
    assert len(inv.line_items) == 2


def test_invoice_calculations(db):
    items = [LineItem("Service A", 2, 100.0), LineItem("Service B", 1, 50.0)]
    inv = create_invoice("TestCo", "t@t.com", items, tax_rate=0.1, discount=0.05, path=db)
    assert inv.subtotal == 250.0
    assert inv.discount_amount == pytest.approx(12.5)
    assert inv.taxable_amount == pytest.approx(237.5)
    assert inv.tax_amount == pytest.approx(23.75)
    assert inv.total == pytest.approx(261.25)


def test_line_item_total():
    li = LineItem("Consulting", 3, 200.0)
    assert li.total == 600.0


def test_send_invoice(db):
    inv = create_invoice("SendCo", "s@s.com", sample_items(), path=db)
    sent = send_invoice(inv.id, db)
    assert sent.status == "sent"


def test_send_paid_invoice_fails(db):
    inv = create_invoice("PaidCo", "p@p.com", sample_items(), path=db)
    send_invoice(inv.id, db)
    mark_paid(inv.id, path=db)
    with pytest.raises(ValueError, match="paid"):
        send_invoice(inv.id, db)


def test_mark_paid(db):
    inv = create_invoice("PayCo", "pay@pay.com", sample_items(), path=db)
    send_invoice(inv.id, db)
    paid = mark_paid(inv.id, "credit_card", db)
    assert paid.status == "paid"
    assert paid.payment_method == "credit_card"
    assert paid.paid_at is not None


def test_mark_paid_twice_fails(db):
    inv = create_invoice("DoublePay", "d@d.com", sample_items(), path=db)
    mark_paid(inv.id, path=db)
    with pytest.raises(ValueError, match="already paid"):
        mark_paid(inv.id, path=db)


def test_generate_pdf_text(db):
    inv = create_invoice("PDFCorp", "pdf@corp.com", sample_items(), path=db)
    text = generate_pdf_text(inv.id, db)
    assert "INVOICE" in text
    assert "PDFCorp" in text
    assert inv.number in text
    assert "Web Development" in text
    assert "TOTAL" in text


def test_generate_pdf_text_with_discount_and_tax(db):
    inv = create_invoice("TaxCo", "tax@tax.com", sample_items(), tax_rate=0.2, discount=0.1, path=db)
    text = generate_pdf_text(inv.id, db)
    assert "Discount" in text
    assert "Tax" in text


def test_overdue_fee_zero_for_paid(db):
    inv = create_invoice("PaidFee", "pf@pf.com", sample_items(), path=db)
    mark_paid(inv.id, path=db)
    fee = calculate_overdue_fee(inv.id, path=db)
    assert fee == 0.0


def test_export_to_csv(db):
    create_invoice("CSV Co", "csv@csv.com", sample_items(), path=db)
    create_invoice("CSV Co 2", "csv2@csv.com", sample_items(), path=db)
    csv_str = export_to_csv(path=db)
    assert "CSV Co" in csv_str
    assert "INV-" in csv_str
    lines = csv_str.strip().split("\n")
    assert len(lines) >= 3  # header + 2 rows


def test_summary_report(db):
    inv1 = create_invoice("Report A", "a@a.com", sample_items(), path=db)
    inv2 = create_invoice("Report B", "b@b.com", sample_items(), path=db)
    send_invoice(inv1.id, db)
    mark_paid(inv1.id, path=db)
    report = summary_report(path=db)
    assert report["total_invoices"] >= 2
    assert report["paid_count"] >= 1
    assert report["paid_total"] > 0


def test_list_invoices(db):
    create_invoice("List A", "la@a.com", sample_items(), path=db)
    create_invoice("List B", "lb@b.com", sample_items(), path=db)
    invs = list_invoices(path=db)
    assert len(invs) >= 2


def test_list_invoices_by_status(db):
    inv = create_invoice("StatusA", "sa@a.com", sample_items(), path=db)
    send_invoice(inv.id, db)
    drafts = list_invoices(status="draft", path=db)
    sents = list_invoices(status="sent", path=db)
    assert all(i.status == "draft" for i in drafts)
    assert any(i.id == inv.id for i in sents)


def test_list_invoices_by_client(db):
    create_invoice("UniqueClient XYZ", "xyz@xyz.com", sample_items(), path=db)
    results = list_invoices(client="UniqueClient", path=db)
    assert any(i.client_name == "UniqueClient XYZ" for i in results)


def test_empty_items_raises(db):
    with pytest.raises(ValueError, match="at least one"):
        create_invoice("NoCo", "no@no.com", [], path=db)


def test_invalid_tax_rate(db):
    with pytest.raises(ValueError, match="tax_rate"):
        create_invoice("TaxErr", "t@t.com", sample_items(), tax_rate=1.5, path=db)


def test_invalid_discount(db):
    with pytest.raises(ValueError, match="discount"):
        create_invoice("DisErr", "d@d.com", sample_items(), discount=-0.1, path=db)


def test_invoice_number_sequential(db):
    inv1 = create_invoice("Seq A", "a@a.com", sample_items(), path=db)
    inv2 = create_invoice("Seq B", "b@b.com", sample_items(), path=db)
    assert inv1.number != inv2.number


def test_get_invoice_not_found(db):
    with pytest.raises(KeyError):
        get_invoice("nonexistent-id", db)


def test_mark_overdue(db):
    from datetime import datetime, timedelta
    import sqlite3
    inv = create_invoice("OverdueCo", "od@od.com", sample_items(), due_days=30, path=db)
    send_invoice(inv.id, db)
    # Manually set due_date to the past
    import os
    conn = sqlite3.connect(db)
    conn.execute("UPDATE invoices SET due_date=? WHERE id=?", ("2020-01-01", inv.id))
    conn.commit()
    conn.close()
    updated = mark_overdue(db)
    assert inv.id in updated
    refreshed = get_invoice(inv.id, db)
    assert refreshed.status == "overdue"
