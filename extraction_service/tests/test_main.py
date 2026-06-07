from __future__ import annotations

import unittest

from extraction_service.app.main import (
    _extract_json_blob,
    confidence_from_fields,
    deterministic_extract,
    extract_bank_statement,
    extract_contract,
    extract_invoice,
    extract_receipt,
)


class ExtractionServiceTests(unittest.TestCase):
    def test_extract_invoice_fields(self) -> None:
        fields = extract_invoice(
            "From: ACME Ltd\nInvoice Number INV-001\nDate: 2026-06-01\nAmount Due: $500.00\nUSD"
        )

        self.assertEqual(fields["invoice_number"], "INV-001")
        self.assertEqual(fields["total_amount"], "$500.00")
        self.assertEqual(fields["currency"], "USD")
        self.assertEqual(fields["vendor_name"], "ACME Ltd")

    def test_route_aware_deterministic_extract_receipt(self) -> None:
        fields = deterministic_extract(
            "Merchant: Corner Store\nDate: 2026/06/01\nSubtotal: $40.00\nTax: $3.00\nTotal: $43.00\nVisa",
            route="receipt",
        )

        self.assertEqual(fields["merchant_name"], "Corner Store")
        self.assertEqual(fields["tax_amount"], "$3.00")
        self.assertEqual(fields["payment_method"].lower(), "visa")

    def test_extract_contract_fields(self) -> None:
        fields = extract_contract(
            "Service Agreement between Alpha LLC and Beta Inc. Effective Date: June 1, 2026. "
            "Term: 12 months. Governing Law: Lagos."
        )

        self.assertEqual(fields["effective_date"], "June 1, 2026")
        self.assertIn("Alpha", fields["parties"])
        self.assertEqual(fields["term"], "12 months")

    def test_extract_bank_statement_fields(self) -> None:
        fields = extract_bank_statement(
            "Bank Statement Account Number 123456789 Statement Period: 01/01/2026 - 01/31/2026 "
            "Opening Balance: $100.00 Closing Balance: $250.00"
        )

        self.assertEqual(fields["account_number"], "123456789")
        self.assertEqual(fields["opening_balance"], "$100.00")
        self.assertEqual(fields["closing_balance"], "$250.00")

    def test_confidence_from_fields_increases_with_fill_ratio(self) -> None:
        low = confidence_from_fields(0.8, {"a": "", "b": ""})
        high = confidence_from_fields(0.8, {"a": "x", "b": "y"})
        self.assertLess(low, high)

    def test_extract_json_blob_parses_wrapped_json(self) -> None:
        parsed = _extract_json_blob('```json\n{"invoice_number":"INV-1","confidence":0.8}\n```')
        self.assertEqual(parsed["invoice_number"], "INV-1")
        self.assertAlmostEqual(parsed["confidence"], 0.8, places=3)


if __name__ == "__main__":
    unittest.main()
