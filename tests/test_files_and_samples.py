"""Tests for (1) asking for a report's files on WhatsApp in any wording — "pdf" / "report" / "excel" means the stock
report's PDF *and* Excel — and (2) report formats being read for their layout only: a sample's goods, rates and margins
are never carried into a report.

LibreOffice, OpenWA and the AI service are mocked; the real database is never touched.
"""

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from unittest import mock

from app.config import settings
from app.routers import openwa_webhook
from app.services import openwa_service, stock_report, stock_templates
from tests.test_report_qa import APPROVER, Database, payload


def request(text, quoted=None):
    return openwa_service.parse_pdf_request(payload(text, quoted=quoted))


class FileRequestWording(unittest.TestCase):
    def test_any_wording_with_pdf_report_or_excel_is_a_request(self):
        for text, label in [
            ("share pdf for 496", "496"), ("shared pdf for 4", "4"), ("send me report #496", "496"),
            ("can you send the excel of 12?", "12"), ("pdf 496", "496"), ("get file T12", "T12"), ("report 4", "4"),
            ("please share the xlsx for 7", "7"), ("pdf bhejo 9", "9"),
        ]:
            req = request(text)
            self.assertIsNotNone(req, text)
            self.assertEqual(req.target_label, label, text)
            self.assertFalse(req.original, text)

    def test_a_bare_word_works_only_as_a_reply_to_a_message(self):
        self.assertIsNone(request("pdf"))
        self.assertEqual(request("report", quoted="wamid.R").quoted_message_id, "wamid.R")

    def test_original_asks_for_the_document_as_it_arrived(self):
        self.assertTrue(request("original pdf 4").original)
        self.assertTrue(request("send source file for 4").original)

    def test_questions_and_figures_are_not_file_requests(self):
        for text in ["what is margin in this report?", "what is total value of report 4", "report 16 margin 5",
                     "rate 3800", "give me margin of report 4", "tell me about report 4", "how many reports today"]:
            self.assertIsNone(request(text, quoted="wamid.R"), text)

    def test_long_messages_are_not_file_requests(self):
        self.assertIsNone(request("I was looking at the report 4 yesterday and noticed something odd about it"))


class FileRequestHandling(Database):
    def setUp(self):
        super().setUp()
        for patcher in (
            mock.patch.object(openwa_webhook, "settings", mock.Mock(notify_numbers_list=[APPROVER], pdf_storage_dir="Z:/none")),
            mock.patch.object(openwa_service, "send_text", return_value="t1"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        sender = mock.patch.object(openwa_service, "send_document", return_value="d1")
        self.send_document = sender.start()
        self.addCleanup(sender.stop)
        self.report = stock_report.StockReport(xlsx_path="C:/r/Mian Cotton.xlsx", pdf_path="C:/r/Mian Cotton.pdf", customer="Mian Cotton", template="t.xlsx")

    def handle(self, text, message_id="m1"):
        with mock.patch.object(stock_report, "build", return_value=self.report) as build:
            result = openwa_webhook._handle_payload(self.db, payload(text, message_id=message_id))
        self.db.commit()
        return result, build

    def sent_files(self):
        return [c.args[1] for c in self.send_document.call_args_list]

    def test_pdf_wording_sends_both_the_pdf_and_the_excel(self):
        result, build = self.handle("shared pdf for 3")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.sent_files(), ["C:/r/Mian Cotton.pdf", "C:/r/Mian Cotton.xlsx"])
        self.assertEqual(build.call_args.kwargs, {"regenerate": True})

    def test_the_word_report_or_excel_sends_both_too(self):
        for n, text in enumerate(("send report 3", "excel for 3")):
            self.send_document.reset_mock()
            self.handle(text, message_id=f"m{n}")
            self.assertEqual(len(self.sent_files()), 2, text)

    def test_the_caption_goes_with_the_first_file_only(self):
        self.handle("report 3")
        self.assertIn("Stock report for #3", self.send_document.call_args_list[0].args[3])
        self.assertIsNone(self.send_document.call_args_list[1].args[3])

    def test_original_sends_the_single_document_not_a_report(self):
        with mock.patch("os.path.exists", return_value=True):
            _, build = self.handle("original pdf 3")
        build.assert_not_called()
        self.assertEqual(len(self.sent_files()), 1)
        self.assertTrue(self.sent_files()[0].endswith("h_DO Mian Cotton.pdf"))

    def test_no_format_is_explained_and_nothing_is_sent(self):
        with mock.patch.object(stock_report, "build", side_effect=stock_report.NoFormatError("There is no report format")):
            openwa_webhook._handle_payload(self.db, payload("share pdf for 3"))
        self.send_document.assert_not_called()

    def test_a_stranger_cannot_ask_for_files(self):
        result = openwa_webhook._handle_payload(self.db, payload("share pdf for 3", sender="923009999999@c.us"))
        self.assertEqual(result["status"], "ignored")
        self.send_document.assert_not_called()


class SampleIsLayoutOnly(unittest.TestCase):
    ORDER = {"customer_name": "Alliance Sugar Mills", "currency": "PKR", "pledge_location": "Lahore",
             "rows": [{"description": "Sugar", "opening_balance": "100", "quantity_released": "10"}]}

    def template(self, **kw):
        base = next(t for i, _, t in stock_templates.BUILTIN if i == "alliance-sugar")  # shipped with the code, not the registry
        return replace(base, margin=None, less_margin=None, **kw)

    def dp_cells(self, template, order):
        job = stock_report.build_job(template, order, datetime(2026, 10, 30))
        return [v for v in job["values"] if v[0].startswith(template.dp)]

    def test_an_uploaded_samples_own_margin_is_dropped_when_loaded(self):
        raw = {"id": "x", "name": "x", "source": "uploaded", "layout": stock_templates.template_to_dict(self.template(margin_pct=15))}
        self.assertIsNone(stock_templates._entry_from_json(raw).template.margin_pct)

    def test_a_built_in_formats_own_margin_is_kept(self):
        raw = {"id": "x", "name": "x", "source": "builtin", "layout": stock_templates.template_to_dict(self.template(margin_pct=15))}
        self.assertEqual(stock_templates._entry_from_json(raw).template.margin_pct, 15)

    def test_without_a_margin_given_dp_is_not_worked_out_from_a_sample(self):
        self.assertEqual(self.dp_cells(self.template(margin_pct=None), self.ORDER), [])

    def test_a_margin_given_for_the_goods_is_used(self):
        order = {**self.ORDER, "rows": [{**self.ORDER["rows"][0], "margin_pct": "10"}]}
        (cell,) = self.dp_cells(self.template(margin_pct=None), order)
        self.assertRegex(cell[1], r"\*90(\.0)?/100")

    def test_the_reviewer_never_asks_the_ai_for_a_margin(self):
        from app.services import stock_template_review

        schema = stock_template_review._FUNCTION["parameters"]["properties"]
        self.assertNotIn("margin_pct", schema)

    def test_a_sample_is_not_used_as_the_previous_report(self):
        """The first report of a company is the sample wiped and refilled — its goods, rates and margins are not carried."""

        class Stop(Exception):
            pass

        entry = stock_templates.get("mian-cotton-ginning-pressing-oil-mills-8b783e")
        order = {"customer_name": "Mian Cotton Ginning Pressing & Oil Mills", "currency": "PKR", "pledge_location": "Bahawalpur",
                 "rows": [{"description": "Cotton (Bales)", "opening_balance": "1000"}]}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(settings, "stock_report_dir", tmp), \
             mock.patch.object(stock_report, "_soffice", return_value="soffice"), \
             mock.patch.object(stock_report, "build_job", side_effect=Stop) as build_job:
            with self.assertRaises(Stop):
                stock_report.render(entry.template, order, "k", dated=datetime(2026, 10, 30), monthly_record=5)
        self.assertIsNone(build_job.call_args.args[3])  # no `previous`: the sample is only wiped and refilled


if __name__ == "__main__":
    unittest.main()
