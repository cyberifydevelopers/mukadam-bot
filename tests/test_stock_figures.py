"""Tests for the margin / rate / weight figures: matching goods across spellings, a bank's margin table, merging a
follow-up into an order, the stock-report sentence, and the dashboard endpoints behind the "Stock report figures" panel.

Run from the project root:  venv\\Scripts\\python.exe -m unittest discover -s tests -v
LibreOffice is never started (stock_report.build is mocked) and the real database is never touched.
"""

import json
import re
import unittest
from datetime import datetime
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import InvoiceRecord, InvoiceStatus
from app.routers import dashboard_api
from app.services import goods_profiles, stock_report, stock_templates, stock_update


def _order(*goods, customer="Mian Cotton Ginning Pressing & Oil Mills"):
    return {"customer_name": customer, "currency": "PKR",
            "rows": [{"description": g, "opening_balance": "100", "quantity_released": "10", "balance_after": "90"} for g in goods]}


class GoodsMatching(unittest.TestCase):
    """Names that differ only by spelling must pair up when a follow-up's figures are added to an order."""

    def score(self, a, b):
        return stock_report._similarity(stock_report._words(a), stock_report._words(b))

    def test_spelling_variants_are_the_same_goods(self):
        for a, b in [("Putty", "Putti"), ("Phutty (season 2026-27)", "Putti (munds)"), ("Cotton Bales", "COTTON"), ("PUTTI", "putti")]:
            self.assertEqual(self.score(a, b), 1.0, (a, b))

    def test_different_goods_do_not_match(self):
        self.assertLess(self.score("Putti", "Cotton Bales"), 0.5)
        self.assertLess(self.score("Wheat", "Rice"), 0.5)

    def test_oil_cake_written_as_one_word_still_pairs(self):
        self.assertGreaterEqual(self.score("Oil Cake", "Oilcake"), 0.5)

    def test_pair_putty_to_putti_line(self):
        old = [{"description": "Putti (munds)", "opening_balance": "16600"}, {"description": "Cotton (Bales)", "opening_balance": "1000"}]
        new = [{"description": "Putty", "rate_per_unit": "3800"}]
        pairs = stock_update._pair(old, new)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0]["description"], "Putti (munds)")

    @unittest.expectedFailure  # KNOWN GAP: profile keys use the raw first word, so spellings don't share saved figures
    def test_saved_figures_are_shared_across_spellings(self):
        self.assertEqual(goods_profiles.goods_key("Putty"), goods_profiles.goods_key("Putti"))

    @unittest.expectedFailure  # KNOWN GAP: same, for "Oil Cake" / "Oilcake"
    def test_saved_figures_oil_cake_one_or_two_words(self):
        self.assertEqual(goods_profiles.goods_key("Oil Cake"), goods_profiles.goods_key("Oilcake"))


class MarginTable(unittest.TestCase):
    """The pledge-site / margin table of the sample email (samples/margin-email-mian-cotton.html)."""

    TABLE = {
        "columns": ["Sr. No.", "Name of Borrower", "Location of Site", "Approved Commodities", "Margin", "Rate Per Unit", "Weight Per Unit"],
        "rows": [["1", "Mian Cotton Ginning Pressing & Oil Mills", "Head Rajkan Road, Bahawalpur", "Putti, Cotton Bales, Oil Cake",
                  "Putti 20% Cotton Bales 25% Oil Cake 20%", "Putti 3,800 Cotton Bales 85,000 Oil Cake 4200", "Putti 40 Cotton Bales 170 Oil Cake 40"]],
    }

    def test_each_commodity_gets_its_own_figures(self):
        (order,) = stock_update.orders_from_table(self.TABLE)
        by = {r["description"]: r for r in order["rows"]}
        self.assertEqual(by["Putti"], {"description": "Putti", "margin_pct": "20", "rate_per_unit": "3800", "weight_per_unit": "40"})
        self.assertEqual(by["Cotton Bales"]["rate_per_unit"], "85000")  # thousands separator removed
        self.assertEqual(by["Oil Cake"]["margin_pct"], "20")

    def test_a_single_bare_margin_applies_to_every_commodity(self):
        table = {"columns": ["Name of Borrower", "Approved Commodities", "Margin"],
                 "rows": [["Mian Cotton", "Putti, Cotton", "20%"]]}
        (order,) = stock_update.orders_from_table(table)
        self.assertEqual({r["description"]: r["margin_pct"] for r in order["rows"]}, {"Putti": "20", "Cotton": "20"})

    def test_a_table_without_borrower_or_figures_is_ignored(self):
        self.assertEqual(stock_update.orders_from_table({"columns": ["Item", "Qty"], "rows": [["a", "1"]]}), [])
        self.assertEqual(stock_update.orders_from_table({"columns": ["Name of Borrower", "Location"], "rows": [["x", "y"]]}), [])
        self.assertEqual(stock_update.orders_from_table(None), [])


class Merging(unittest.TestCase):
    def record(self, rows):
        r = InvoiceRecord(id=1)
        r.raw_extracted_json = json.dumps({"delivery_order": {"customer_name": "Mian Cotton", "rows": rows}})
        return r

    def test_figures_go_into_the_matching_lines(self):
        rec = self.record([{"description": "Putti (munds)"}, {"description": "Cotton (Bales)"}, {"description": "Oil Cake (munds)"}])
        new = {"rows": [{"description": "Putty", "rate_per_unit": "3800", "margin_pct": "20"},
                        {"description": "Cotton Bales", "rate_per_unit": "85000"}]}
        applied = stock_update.merge(rec, new)
        rows = json.loads(rec.raw_extracted_json)["delivery_order"]["rows"]
        self.assertEqual((rows[0]["rate_per_unit"], rows[0]["margin_pct"]), ("3800", "20"))
        self.assertEqual(rows[1]["rate_per_unit"], "85000")
        self.assertNotIn("rate_per_unit", rows[2])
        self.assertIn("Rate per unit: 3800", applied)

    def test_blank_figures_never_erase_existing_ones(self):
        rec = self.record([{"description": "Putti", "rate_per_unit": "3800"}])
        stock_update.merge(rec, {"rows": [{"description": "Putti", "rate_per_unit": "", "margin_pct": None}]})
        self.assertEqual(json.loads(rec.raw_extracted_json)["delivery_order"]["rows"][0]["rate_per_unit"], "3800")

    def test_figures_only_message_detection(self):
        self.assertTrue(stock_update.figures_only({"rows": [{"description": "Putti", "margin_pct": "20"}]}))
        self.assertFalse(stock_update.figures_only({"rows": [{"description": "Putti", "margin_pct": "20", "quantity_released": "5"}]}))
        self.assertFalse(stock_update.figures_only({"rows": []}))


class StockSentence(unittest.TestCase):
    """The bank sample's hand-typed runs of spaces must not show up as a gap in the sentence."""

    def test_long_space_runs_become_line_breaks(self):
        entry = stock_templates.get("mian-cotton-ginning-pressing-oil-mills-8b783e")
        order = {**_order("Putti (munds)"), "pledge_location": "Head Rajkan Road, Bahawalpur"}
        job = stock_report.build_job(entry.template, order, datetime(2026, 10, 7))
        sentence = next(v[1] for v in job["values"] if v[0] == entry.template.text_cell)
        self.assertNotRegex(sentence, r" {10,}")
        self.assertEqual(sentence.count("\n"), 2)
        self.assertIn("Head Rajkan Road, Bahawalpur, account of\n", sentence)
        self.assertTrue(sentence.endswith("M/s COMMANDER INTERNATIONAL PVT LIMITED."))

    def test_a_sentence_without_space_runs_is_unchanged(self):
        entry = stock_templates.get("thal-industries-271cb4")
        job = stock_report.build_job(entry.template, {**_order("Wheat"), "pledge_location": "Lahore"}, datetime(2026, 10, 7))
        sentence = next(v[1] for v in job["values"] if v[0] == entry.template.text_cell)
        self.assertNotIn("\n", sentence)


class FiguresEndpoints(unittest.TestCase):
    """GET / PUT /api/invoices/{id}/stock-figures against an in-memory database; no LibreOffice."""

    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, autoflush=False)
        app = FastAPI()
        app.include_router(dashboard_api.router)

        def db():
            s = self.Session()
            try:
                yield s
            finally:
                s.close()

        app.dependency_overrides[get_db] = db
        self.client = TestClient(app)
        with self.Session() as s:
            s.add(InvoiceRecord(id=1, email_id=1, pdf_filename="h_DO.pdf", status=InvoiceStatus.CONFIRMED,
                                raw_extracted_json=json.dumps({"delivery_order": _order("Putti (munds)", "Cotton (Bales)")})))
            s.add(InvoiceRecord(id=2, email_id=1, pdf_filename="h_other.pdf", status=InvoiceStatus.CONFIRMED,
                                raw_extracted_json=json.dumps({"is_account_statement": True})))
            s.commit()
        patch = mock.patch.object(goods_profiles, "SessionLocal", self.Session)
        patch.start()
        self.addCleanup(patch.stop)

    def saved_rows(self):
        with self.Session() as s:
            return json.loads(s.get(InvoiceRecord, 1).raw_extracted_json)["delivery_order"]["rows"]

    def put(self, rows, **kw):
        return self.client.put("/api/invoices/1/stock-figures", json={"rows": rows}, **kw)

    def test_get_lists_the_goods_with_blank_figures(self):
        r = self.client.get("/api/invoices/1/stock-figures").json()
        self.assertEqual([x["description"] for x in r["rows"]], ["Putti (munds)", "Cotton (Bales)"])
        self.assertEqual(r["rows"][0]["margin_pct"], "")

    def test_get_404_for_a_document_without_a_stock_report(self):
        self.assertEqual(self.client.get("/api/invoices/2/stock-figures").status_code, 404)
        self.assertEqual(self.client.get("/api/invoices/99/stock-figures").status_code, 404)

    def test_put_saves_the_figures_and_remakes_the_report(self):
        with mock.patch.object(stock_report, "build", return_value=mock.Mock(missing=[])) as build:
            r = self.put([{"rate_per_unit": "3,800", "weight_per_unit": "40", "margin_pct": "20"},
                          {"rate_per_unit": "85000", "margin_pct": "25"}])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(build.call_args.kwargs, {"regenerate": True})
        rows = self.saved_rows()
        self.assertEqual((rows[0]["rate_per_unit"], rows[0]["weight_per_unit"], rows[0]["margin_pct"]), ("3800", "40", "20"))  # comma removed
        self.assertIsNone(rows[0]["weight_measurement"])  # left blank
        self.assertEqual(rows[1]["margin_pct"], "25")

    def test_put_rejects_text_instead_of_a_number(self):
        with mock.patch.object(stock_report, "build") as build:
            r = self.put([{"margin_pct": "twenty"}, {}])
        self.assertEqual(r.status_code, 422)
        self.assertIn("number", r.json()["detail"].lower())
        build.assert_not_called()

    def test_put_rejects_a_wrong_number_of_lines(self):
        with mock.patch.object(stock_report, "build") as build:
            self.assertEqual(self.put([{"margin_pct": "5"}]).status_code, 422)
        build.assert_not_called()

    def test_failed_report_is_an_error_and_nothing_is_saved(self):
        with mock.patch.object(stock_report, "build", side_effect=stock_report.StockReportError("LibreOffice failed")):
            r = self.put([{"margin_pct": "5"}, {}])
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"], "LibreOffice failed")
        self.assertNotIn("margin_pct", self.saved_rows()[0])  # rolled back

    def test_stock_report_pdf_404_until_one_exists(self):
        with mock.patch.object(stock_report, "latest", return_value=None):
            self.assertEqual(self.client.get("/api/invoices/1/stock-report").status_code, 404)


class DashboardBulkUpload(unittest.TestCase):
    """The Report formats page is plain JS served as-is; these guard what the bulk upload relies on."""

    @classmethod
    def setUpClass(cls):
        with open("app/static/index.html", encoding="utf-8") as f:
            cls.html = f.read()

    def test_file_input_accepts_several_files(self):
        self.assertRegex(self.html, r'<input[^>]*type="file"[^>]*name="file"[^>]*\bmultiple\b')

    def test_each_file_is_posted_on_its_own(self):
        self.assertRegex(self.html, r"for \(const \[i, file\] of files\.entries\(\)\)")
        self.assertIn('data.append("file", file)', self.html)

    def test_figures_form_fields_match_the_api(self):
        for field in ("rate_per_unit", "weight_per_unit", "weight_measurement", "margin_pct"):
            self.assertIn(field, dashboard_api._FIGURE_FIELDS)
            self.assertIn(f'"{field}"', self.html)
        self.assertTrue(re.search(r"/invoices/\$\{form\.dataset\.id\}/stock-figures", self.html))


if __name__ == "__main__":
    unittest.main()
