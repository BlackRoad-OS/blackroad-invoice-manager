#!/usr/bin/env python3
"""
BlackRoad Invoice Manager
Production-grade invoicing with line items, tax, discounts, PDF text, and overdue tracking.
"""
from __future__ import annotations
import argparse
import csv
import io
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import List, Optional

DB_PATH = os.path.expanduser("~/.blackroad/invoices.db")


@dataclass
class LineItem:
    description: str
    qty: float
    unit_price: float

    @property
    def total(self) -> float:
        return round(self.qty * self.unit_price, 2)

    def to_dict(self) -> dict:
        return {**asdict(self), "total": self.total}


@dataclass
class Invoice:
    id: str
    number: str
    client_name: str
    client_email: str
    line_items: List[LineItem]
    tax_rate: float
    discount: float
    status: str       # draft | sent | paid | overdue
    due_date: str
    created_at: str
    paid_at: Optional[str] = None
    payment_method: Optional[str] = None
    notes: str = ""
    currency: str = "USD"

    @property
    def subtotal(self) -> float:
        return round(sum(li.total for li in self.line_items), 2)

    @property
    def discount_amount(self) -> float:
        return round(self.subtotal * self.discount, 2)

    @property
    def taxable_amount(self) -> float:
        return round(self.subtotal - self.discount_amount, 2)

    @property
    def tax_amount(self) -> float:
        return round(self.taxable_amount * self.tax_rate, 2)

    @property
    def total(self) -> float:
        return round(self.taxable_amount + self.tax_amount, 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["line_items"] = [li.to_dict() for li in self.line_items]
        d["subtotal"] = self.subtotal
        d["discount_amount"] = self.discount_amount
        d["taxable_amount"] = self.taxable_amount
        d["tax_amount"] = self.tax_amount
        d["total"] = self.total
        return d


def _now() -> str:
    return datetime.utcnow().isoformat()


def _invoice_number(conn: sqlite3.Connection) -> str:
    year = datetime.utcnow().year
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM invoices WHERE number LIKE ?",
        (f"INV-{year}-%",),
    ).fetchone()
    seq = (row["cnt"] if row else 0) + 1
    return f"INV-{year}-{seq:05d}"


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

def get_db(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(path: str = DB_PATH) -> None:
    with get_db(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS invoices (
                id             TEXT PRIMARY KEY,
                number         TEXT UNIQUE NOT NULL,
                client_name    TEXT NOT NULL,
                client_email   TEXT NOT NULL,
                tax_rate       REAL NOT NULL DEFAULT 0,
                discount       REAL NOT NULL DEFAULT 0,
                status         TEXT NOT NULL DEFAULT 'draft',
                due_date       TEXT NOT NULL,
                created_at     TEXT NOT NULL,
                paid_at        TEXT,
                payment_method TEXT,
                notes          TEXT NOT NULL DEFAULT '',
                currency       TEXT NOT NULL DEFAULT 'USD'
            );
            CREATE TABLE IF NOT EXISTS line_items (
                id          TEXT PRIMARY KEY,
                invoice_id  TEXT NOT NULL REFERENCES invoices(id),
                description TEXT NOT NULL,
                qty         REAL NOT NULL,
                unit_price  REAL NOT NULL,
                position    INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_li_invoice ON line_items(invoice_id);
            CREATE INDEX IF NOT EXISTS idx_inv_status  ON invoices(status);
            CREATE INDEX IF NOT EXISTS idx_inv_client  ON invoices(client_name);
        """)


# ---------------------------------------------------------------------------
# Core invoice operations
# ---------------------------------------------------------------------------

def create_invoice(
    client_name: str,
    client_email: str,
    items: List[LineItem],
    tax_rate: float = 0.0,
    due_days: int = 30,
    discount: float = 0.0,
    notes: str = "",
    currency: str = "USD",
    path: str = DB_PATH,
) -> Invoice:
    """Create a new invoice in draft status."""
    if not items:
        raise ValueError("Invoice must have at least one line item")
    if not 0 <= tax_rate <= 1:
        raise ValueError("tax_rate must be between 0 and 1")
    if not 0 <= discount <= 1:
        raise ValueError("discount must be between 0 and 1")

    now = datetime.utcnow()
    due_date = (now + timedelta(days=due_days)).date().isoformat()

    with get_db(path) as conn:
        number = _invoice_number(conn)
        inv = Invoice(
            id=str(uuid.uuid4()),
            number=number,
            client_name=client_name,
            client_email=client_email,
            line_items=items,
            tax_rate=tax_rate,
            discount=discount,
            status="draft",
            due_date=due_date,
            created_at=now.isoformat(),
            currency=currency,
            notes=notes,
        )
        conn.execute(
            """INSERT INTO invoices
               (id, number, client_name, client_email, tax_rate, discount,
                status, due_date, created_at, notes, currency)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (inv.id, inv.number, inv.client_name, inv.client_email,
             inv.tax_rate, inv.discount, inv.status, inv.due_date,
             inv.created_at, inv.notes, inv.currency),
        )
        for i, li in enumerate(items):
            conn.execute(
                """INSERT INTO line_items (id, invoice_id, description, qty, unit_price, position)
                   VALUES (?,?,?,?,?,?)""",
                (str(uuid.uuid4()), inv.id, li.description, li.qty, li.unit_price, i),
            )
    return inv


def get_invoice(invoice_id: str, path: str = DB_PATH) -> Invoice:
    with get_db(path) as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if not row:
            raise KeyError(f"Invoice {invoice_id} not found")
        li_rows = conn.execute(
            "SELECT * FROM line_items WHERE invoice_id=? ORDER BY position",
            (invoice_id,),
        ).fetchall()
    items = [LineItem(r["description"], r["qty"], r["unit_price"]) for r in li_rows]
    return _row_to_invoice(row, items)


def send_invoice(invoice_id: str, path: str = DB_PATH) -> Invoice:
    """Mark invoice as sent (would trigger email in production)."""
    with get_db(path) as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if not row:
            raise KeyError(f"Invoice {invoice_id} not found")
        if row["status"] == "paid":
            raise ValueError("Cannot send a paid invoice")
        conn.execute(
            "UPDATE invoices SET status='sent' WHERE id=?", (invoice_id,)
        )
    return get_invoice(invoice_id, path)


def mark_paid(
    invoice_id: str,
    payment_method: str = "bank_transfer",
    path: str = DB_PATH,
) -> Invoice:
    """Mark an invoice as paid."""
    with get_db(path) as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if not row:
            raise KeyError(f"Invoice {invoice_id} not found")
        if row["status"] == "paid":
            raise ValueError("Invoice already paid")
        conn.execute(
            "UPDATE invoices SET status='paid', paid_at=?, payment_method=? WHERE id=?",
            (_now(), payment_method, invoice_id),
        )
    return get_invoice(invoice_id, path)


def generate_pdf_text(invoice_id: str, path: str = DB_PATH) -> str:
    """Generate a text-based invoice suitable for PDF conversion."""
    inv = get_invoice(invoice_id, path)
    lines = [
        "=" * 60,
        f"  INVOICE",
        "=" * 60,
        f"  Invoice #: {inv.number}",
        f"  Date:      {inv.created_at[:10]}",
        f"  Due Date:  {inv.due_date}",
        f"  Status:    {inv.status.upper()}",
        "",
        f"  Bill To:",
        f"  {inv.client_name}",
        f"  {inv.client_email}",
        "",
        "-" * 60,
        f"  {'Description':<30} {'Qty':>6} {'Unit Price':>10} {'Total':>10}",
        "-" * 60,
    ]
    for li in inv.line_items:
        lines.append(
            f"  {li.description:<30} {li.qty:>6.2f} {li.unit_price:>10.2f} {li.total:>10.2f}"
        )
    lines += [
        "-" * 60,
        f"  {'Subtotal':<48} {inv.subtotal:>10.2f}",
    ]
    if inv.discount > 0:
        lines.append(f"  {'Discount (' + str(int(inv.discount * 100)) + '%)':<48} -{inv.discount_amount:>9.2f}")
    if inv.tax_rate > 0:
        lines.append(f"  {'Tax (' + str(int(inv.tax_rate * 100)) + '%)':<48} {inv.tax_amount:>10.2f}")
    lines += [
        "=" * 60,
        f"  {'TOTAL ' + inv.currency:<48} {inv.total:>10.2f}",
        "=" * 60,
    ]
    if inv.paid_at:
        lines.append(f"  PAID on {inv.paid_at[:10]} via {inv.payment_method}")
    if inv.notes:
        lines += ["", f"  Notes: {inv.notes}"]
    return "\n".join(lines)


def calculate_overdue_fee(
    invoice_id: str,
    daily_rate: float = 0.001,
    path: str = DB_PATH,
) -> float:
    """Calculate accumulated overdue fee based on days past due date."""
    inv = get_invoice(invoice_id, path)
    if inv.status == "paid":
        return 0.0
    today = datetime.utcnow().date()
    due = datetime.fromisoformat(inv.due_date).date()
    days_overdue = max(0, (today - due).days)
    return round(inv.total * daily_rate * days_overdue, 2)


def mark_overdue(path: str = DB_PATH) -> List[str]:
    """Scan all sent invoices and mark overdue ones. Returns list of updated IDs."""
    today = datetime.utcnow().date().isoformat()
    with get_db(path) as conn:
        rows = conn.execute(
            "SELECT id FROM invoices WHERE status='sent' AND due_date < ?", (today,)
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            conn.execute(
                f"UPDATE invoices SET status='overdue' WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            )
    return ids


def export_to_csv(path: str = DB_PATH, output: Optional[str] = None) -> str:
    """Export all invoices to CSV. Returns the CSV string."""
    with get_db(path) as conn:
        rows = conn.execute("SELECT * FROM invoices ORDER BY created_at DESC").fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "number", "client_name", "client_email", "status",
        "due_date", "created_at", "paid_at", "payment_method",
        "currency", "tax_rate", "discount",
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["number"], r["client_name"], r["client_email"], r["status"],
            r["due_date"], r["created_at"], r["paid_at"], r["payment_method"],
            r["currency"], r["tax_rate"], r["discount"],
        ])
    csv_str = buf.getvalue()
    if output:
        with open(output, "w") as f:
            f.write(csv_str)
    return csv_str


def summary_report(
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    path: str = DB_PATH,
) -> dict:
    """Generate a summary report for a given period."""
    with get_db(path) as conn:
        if period_start and period_end:
            rows = conn.execute(
                "SELECT * FROM invoices WHERE created_at BETWEEN ? AND ?",
                (period_start, period_end),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM invoices").fetchall()

    all_invoices = []
    for r in rows:
        try:
            inv = get_invoice(r["id"], path)
            all_invoices.append(inv)
        except Exception:
            pass

    total_invoiced = sum(i.total for i in all_invoices)
    paid = [i for i in all_invoices if i.status == "paid"]
    overdue = [i for i in all_invoices if i.status == "overdue"]
    draft = [i for i in all_invoices if i.status == "draft"]
    sent = [i for i in all_invoices if i.status == "sent"]

    return {
        "period_start": period_start,
        "period_end": period_end,
        "total_invoices": len(all_invoices),
        "total_invoiced": round(total_invoiced, 2),
        "paid_count": len(paid),
        "paid_total": round(sum(i.total for i in paid), 2),
        "overdue_count": len(overdue),
        "overdue_total": round(sum(i.total for i in overdue), 2),
        "draft_count": len(draft),
        "sent_count": len(sent),
        "collection_rate": round(len(paid) / len(all_invoices) * 100, 1) if all_invoices else 0,
    }


def list_invoices(
    status: Optional[str] = None,
    client: Optional[str] = None,
    path: str = DB_PATH,
) -> List[Invoice]:
    with get_db(path) as conn:
        query = "SELECT id FROM invoices WHERE 1=1"
        params = []
        if status:
            query += " AND status=?"
            params.append(status)
        if client:
            query += " AND client_name LIKE ?"
            params.append(f"%{client}%")
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
    return [get_invoice(r["id"], path) for r in rows]


def _row_to_invoice(row: sqlite3.Row, items: List[LineItem]) -> Invoice:
    return Invoice(
        id=row["id"], number=row["number"],
        client_name=row["client_name"], client_email=row["client_email"],
        line_items=items, tax_rate=row["tax_rate"], discount=row["discount"],
        status=row["status"], due_date=row["due_date"], created_at=row["created_at"],
        paid_at=row["paid_at"], payment_method=row["payment_method"],
        notes=row["notes"], currency=row["currency"],
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_json(obj) -> None:
    if hasattr(obj, "to_dict"):
        print(json.dumps(obj.to_dict(), indent=2))
    elif isinstance(obj, list):
        print(json.dumps([o.to_dict() if hasattr(o, "to_dict") else o for o in obj], indent=2))
    else:
        print(json.dumps(obj, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invoice", description="BlackRoad Invoice Manager")
    parser.add_argument("--db", default=DB_PATH)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init")

    p = sub.add_parser("create")
    p.add_argument("client_name")
    p.add_argument("client_email")
    p.add_argument("--items", required=True,
                   help='JSON: [{"description":"X","qty":1,"unit_price":100}]')
    p.add_argument("--tax-rate", type=float, default=0.0)
    p.add_argument("--discount", type=float, default=0.0)
    p.add_argument("--due-days", type=int, default=30)
    p.add_argument("--notes", default="")
    p.add_argument("--currency", default="USD")

    p = sub.add_parser("get")
    p.add_argument("id")

    p = sub.add_parser("send")
    p.add_argument("id")

    p = sub.add_parser("pay")
    p.add_argument("id")
    p.add_argument("--method", default="bank_transfer")

    p = sub.add_parser("pdf")
    p.add_argument("id")

    p = sub.add_parser("overdue-fee")
    p.add_argument("id")
    p.add_argument("--rate", type=float, default=0.001)

    sub.add_parser("mark-overdue")

    p = sub.add_parser("export-csv")
    p.add_argument("--output", default=None)

    p = sub.add_parser("report")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)

    p = sub.add_parser("list")
    p.add_argument("--status", default=None)
    p.add_argument("--client", default=None)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    db = args.db
    init_db(db)

    if args.command == "init":
        print("Database initialized.")
    elif args.command == "create":
        raw = json.loads(args.items)
        items = [LineItem(i["description"], i["qty"], i["unit_price"]) for i in raw]
        inv = create_invoice(
            args.client_name, args.client_email, items,
            args.tax_rate, args.due_days, args.discount,
            args.notes, args.currency, db,
        )
        _print_json(inv)
    elif args.command == "get":
        _print_json(get_invoice(args.id, db))
    elif args.command == "send":
        _print_json(send_invoice(args.id, db))
    elif args.command == "pay":
        _print_json(mark_paid(args.id, args.method, db))
    elif args.command == "pdf":
        print(generate_pdf_text(args.id, db))
    elif args.command == "overdue-fee":
        fee = calculate_overdue_fee(args.id, args.rate, db)
        print(json.dumps({"overdue_fee": fee}))
    elif args.command == "mark-overdue":
        ids = mark_overdue(db)
        print(json.dumps({"updated": ids}))
    elif args.command == "export-csv":
        csv_str = export_to_csv(db, args.output)
        if not args.output:
            print(csv_str)
        else:
            print(f"Exported to {args.output}")
    elif args.command == "report":
        print(json.dumps(summary_report(args.start, args.end, db), indent=2))
    elif args.command == "list":
        _print_json(list_invoices(args.status, args.client, db))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
