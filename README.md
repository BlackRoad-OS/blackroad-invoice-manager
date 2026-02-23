# blackroad-invoice-manager

Production-grade invoice management with line items, tax, discounts, PDF text generation, and overdue tracking.

## Features
- Sequential invoice numbering (INV-YYYY-NNNNN)
- Line items with qty, unit price, and auto-calculated totals
- Tax rates and percentage discounts
- Status lifecycle: draft → sent → paid | overdue
- Text-based PDF/print generation
- Overdue fee calculation (daily compound)
- CSV export and summary reports

## Usage
```bash
python invoice.py init
python invoice.py create "Acme Corp" "billing@acme.com" \
  --items '[{"description":"Web Dev","qty":10,"unit_price":150}]' \
  --tax-rate 0.1 --due-days 30
python invoice.py send <id>
python invoice.py pay <id> --method stripe
python invoice.py pdf <id>
python invoice.py report --start 2025-01-01 --end 2025-12-31
python invoice.py export-csv --output invoices.csv
```

## Testing
```bash
pip install pytest
pytest test_invoice.py -v
```
