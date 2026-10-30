"""A document in US dollars: the WhatsApp prompt asks the approver for the PKR amount, "usd rate 280" / "pkr amount 1500000"
answers it, and the stock report, the sheet and the caption follow. OpenWA and LibreOffice are mocked; in-memory database."""

import json
import unittest
from datetime import datetime
from unittest import mock

from app import pipeline
from app.config import settings
from app.models import InvoiceRecord, InvoiceStatus, WhatsAppMessage
from app.routers import openwa_webhook
from app.services import openwa_service, pkr, sheets_service, stock_report
from tests.test_report_qa import APPROVER, Database, payload

USD_ORDER = {
    "customer_name": "Mian Cotton Ginning Pressing & Oil Mills", "currency": "USD", "pledge_location": "Bahawalpur",
    "rows": [{"description": "Cotton (Bales)", "opening_balance": "100", "quantity_released": "10", "rate_per_unit": "2"}],
}


def request(text, quoted=None):
    return openwa_service.parse_pkr_request(payload(text, quoted=quoted))


class Parsing(unittest.TestCase):
    def test_the_dollar_rate_is_read(self):
        for text, rate in [("usd rate 280", 280.0), ("USD rate is 281.5", 281.5), ("dollar rate = 280", 280.0), ("$ rate: 279", 279.0),
                           ("1 usd = 280", 280.0), ("usd rate rs 280 for #12", 280.0)]:
            req = request(text)
            self.assertIsNotNone(req, text)
            self.assertEqual((req.rate, req.amount), (rate, None), text)

    def test_the_pkr_amount_is_read(self):
        for text, amount in [("pkr amount 1500000", 1500000.0), ("PKR 1,500,000", 1500000.0), ("rs 1500000", 1500000.0),
                             ("amount in pkr 1,400,000.50", 1400000.5)]:
            req = request(text)
            self.assertIsNotNone(req, text)
            self.assertEqual((req.rate, req.amount), (None, amount), text)

    def test_the_document_can_be_named_or_quoted(self):
        self.assertEqual(request("usd rate 280 for #12").target_label, "12")
        self.assertEqual(request("pkr amount 1500000 for #12").target_label, "12")
        self.assertEqual(request("pkr 1,500,000 for report 12").amount, 1500000.0)
        self.assertEqual(request("usd rate 280", quoted="wamid.P").quoted_message_id, "wamid.P")

    def test_questions_and_goods_figures_are_not_taken_for_it(self):
        for text in ["what is the usd rate?", "rate 3800", "margin wheat 5", "pkr?", "hello", "send pdf for 4", "the rate is 280"]:
            self.assertIsNone(request(text), text)

    def test_a_dollar_rate_is_not_read_as_a_goods_rate(self):
        self.assertIsNone(openwa_service.parse_pkr_request(payload("rate 280")))


class Calculations(Database):
    def usd_record(self, amount="5,000.00", extra=None, status=InvoiceStatus.PENDING_CONFIRMATION):
        record = InvoiceRecord(id=20, email_id=1, pdf_filename="h_usd.pdf", status=status, amount=amount, bank_name="Meezan Bank",
                               raw_extracted_json=json.dumps({"currency": "USD", **(extra or {})}))
        self.db.add(record)
        self.db.commit()
        return record

    def test_a_rate_gives_the_rupee_amount(self):
        record = self.usd_record()
        self.assertEqual(pkr.apply(record, rate=280), {"rate": 280.0, "amount": 1400000.0})
        self.assertEqual(json.loads(record.raw_extracted_json)["pkr"]["amount"], 1400000.0)

    def test_a_rupee_amount_gives_the_rate(self):
        record = self.usd_record()
        self.assertEqual(pkr.apply(record, amount=1500000), {"rate": 300.0, "amount": 1500000.0})

    def test_without_the_dollar_amount_only_a_rate_can_be_used(self):
        record = self.usd_record(amount=None)
        self.assertIsNone(pkr.apply(record, amount=1500000))
        self.assertEqual(pkr.apply(record, rate=280)["rate"], 280.0)

    def test_a_delivery_order_carries_the_rate_for_its_stock_report(self):
        record = self.usd_record(extra={"delivery_order": dict(USD_ORDER)})
        pkr.apply(record, rate=280)
        self.assertEqual(stock_report.delivery_order(record)["usd_pkr_rate"], 280.0)

    def test_the_settings_rate_is_used_when_none_was_given(self):
        extra = {"currency": "USD"}
        with mock.patch.object(settings, "usd_pkr_rate", 279.3):
            self.assertEqual(pkr.rate_for(extra), 279.3)
            self.assertEqual(pkr.rate_for({"pkr": {"rate": 300}}), 300.0)  # the approver's own wins
        with mock.patch.object(settings, "usd_pkr_rate", 0):
            self.assertEqual(pkr.rate_for(extra), 0.0)

    def test_the_latest_usd_document_without_a_rate_is_found(self):
        self.usd_record()
        self.assertEqual(pkr.latest_usd_record(self.db).id, 20)
        pkr.apply(self.db.get(InvoiceRecord, 20), rate=280)
        self.db.commit()
        self.assertIsNone(pkr.latest_usd_record(self.db))

    def test_only_usd_documents_count(self):
        for currency, expected in [("USD", True), ("usd", True), ("US$", True), ("PKR", False), ("EUR", False), (None, False)]:
            self.assertEqual(pkr.is_usd({"currency": currency}), expected, currency)


class Prompt(Calculations):
    def prompt_for(self, record):
        sent = []
        with mock.patch.object(pipeline, "_deliver", side_effect=lambda rec, text, params, **kw: sent.append(text)), \
             mock.patch.object(pipeline.stock_report, "needs_format", return_value=False):
            pipeline._send_confirmation(record)
        return "\n".join(sent)

    def test_a_usd_document_asks_for_the_pkr_amount(self):
        with mock.patch.object(settings, "usd_pkr_rate", 0):
            text = self.prompt_for(self.usd_record())
        self.assertIn("in USD", text)
        self.assertIn("pkr amount 1500000", text)
        self.assertIn("usd rate 280", text)

    def test_it_shows_the_rupee_amount_instead_once_known(self):
        record = self.usd_record()
        pkr.apply(record, rate=280)
        text = self.prompt_for(record)
        self.assertIn("PKR 1,400,000", text)
        self.assertNotIn("Please send the *PKR amount*", text)

    def test_a_pkr_document_is_not_asked(self):
        record = self.usd_record(extra={"currency": "PKR"})
        self.assertNotIn("USD", self.prompt_for(record))


class SheetAndReport(Calculations):
    def test_the_sheets_currency_cell_shows_the_rupee_amount_once_known(self):
        record = self.usd_record()
        extra = json.loads(record.raw_extracted_json)
        with mock.patch.object(settings, "usd_pkr_rate", 0):
            self.assertEqual(pkr.currency_cell(extra, record), "USD")
        pkr.apply(record, rate=280)
        self.assertEqual(pkr.currency_cell(json.loads(record.raw_extracted_json), record), "USD → PKR 1,400,000 (at 280)")

    def test_a_pkr_documents_currency_cell_is_untouched(self):
        record = self.usd_record(extra={"currency": "PKR"})
        self.assertEqual(pkr.currency_cell({"currency": "PKR"}, record), "PKR")

    def test_the_report_converts_by_the_rate_given_for_the_document(self):
        template = stock_report.stock_templates.get("mian-cotton-ginning-pressing-oil-mills-8b783e").template
        with mock.patch.object(settings, "usd_pkr_rate", 0):
            plain = stock_report.build_job(template, USD_ORDER, datetime(2026, 10, 30))
            converted = stock_report.build_job(template, {**USD_ORDER, "usd_pkr_rate": 280.0}, datetime(2026, 10, 30))
        rate_cell = lambda job: next(v[1] for v in job["values"] if v[0] == f"{template.rate}{template.first_row}")
        self.assertEqual(rate_cell(plain), 2.0)  # dollars, no rate known
        self.assertEqual(rate_cell(converted), 560.0)  # 2 USD × 280, per unit

    def test_the_report_says_what_is_missing_while_no_rate_is_known(self):
        template = stock_report.stock_templates.get("mian-cotton-ginning-pressing-oil-mills-8b783e").template
        report = stock_report.StockReport(xlsx_path="Z:/none.xlsx", pdf_path="x.pdf", customer="c", template="t")
        with mock.patch.object(settings, "usd_pkr_rate", 0):
            notes = stock_report.missing_data(USD_ORDER, template, report, datetime(2026, 10, 30))
            known = stock_report.missing_data({**USD_ORDER, "usd_pkr_rate": 280.0}, template, report, datetime(2026, 10, 30))
        self.assertTrue(any("dollar rate" in n for n in notes), notes)
        self.assertFalse(any("dollar rate" in n for n in known), known)


class Reply(Calculations):
    def setUp(self):
        super().setUp()
        for patcher in (
            mock.patch.object(openwa_webhook, "settings", mock.Mock(notify_numbers_list=[APPROVER])),
            mock.patch.object(openwa_service, "send_text", return_value="t1"),
            mock.patch.object(openwa_service, "send_document", return_value="d1"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def say(self, text, **kw):
        with mock.patch.object(openwa_webhook, "_send_stock_report") as remake:
            result = openwa_webhook._handle_payload(self.db, payload(text, **kw))
        self.db.commit()
        return result, remake

    def replies(self):
        return [m.text for m in self.db.query(WhatsAppMessage).filter_by(direction="out")]

    def test_a_rate_for_a_waiting_document_is_kept_and_acknowledged(self):
        self.usd_record()
        result, remake = self.say("usd rate 280")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(json.loads(self.db.get(InvoiceRecord, 20).raw_extracted_json)["pkr"], {"rate": 280.0, "amount": 1400000.0})
        self.assertIn("PKR 1,400,000", self.replies()[-1])
        self.assertIn("YES", self.replies()[-1])
        remake.assert_not_called()  # nothing sent yet: it still waits for YES

    def test_the_report_of_a_confirmed_delivery_order_is_made_again_in_rupees(self):
        self.usd_record(extra={"delivery_order": dict(USD_ORDER)}, status=InvoiceStatus.CONFIRMED)
        _, remake = self.say("pkr amount 1400000 for #20", message_id="m2")
        remake.assert_called_once()
        self.assertEqual(stock_report.delivery_order(self.db.get(InvoiceRecord, 20))["usd_pkr_rate"], 280.0)

    def test_a_reply_to_the_prompt_goes_to_that_document(self):
        self.usd_record()
        self.db.add(WhatsAppMessage(direction="out", provider="openwa", peer_number=APPROVER, text="prompt", status="sent",
                                    message_id="wamid.PROMPT", invoice_id=20))
        self.db.commit()
        self.say("usd rate 281", quoted="wamid.PROMPT")
        self.assertEqual(json.loads(self.db.get(InvoiceRecord, 20).raw_extracted_json)["pkr"]["rate"], 281.0)

    def test_no_usd_document_means_asking_which_one(self):
        self.say("usd rate 280")
        self.assertIn("Which document", self.replies()[-1])

    def test_a_document_in_rupees_is_not_converted(self):
        self.usd_record(extra={"currency": "PKR"})
        self.say("usd rate 280 for #20")
        self.assertIn("not in USD", self.replies()[-1])
        self.assertNotIn("pkr", json.loads(self.db.get(InvoiceRecord, 20).raw_extracted_json))

    def test_an_amount_with_no_dollar_figure_asks_for_the_rate(self):
        self.usd_record(amount=None)
        self.say("pkr amount 1500000 for #20")
        self.assertIn("usd rate 280", self.replies()[-1])

    def test_a_stranger_changes_nothing(self):
        self.usd_record()
        result = openwa_webhook._handle_payload(self.db, payload("usd rate 280", sender="923009999999@c.us"))
        self.assertEqual(result["status"], "ignored")
        self.assertNotIn("pkr", json.loads(self.db.get(InvoiceRecord, 20).raw_extracted_json))

    def test_a_redelivered_message_is_applied_once(self):
        self.usd_record()
        self.say("usd rate 280")
        self.assertEqual(self.say("usd rate 280")[0]["status"], "duplicate")

    def test_goods_rates_still_go_to_the_figures_flow(self):
        with mock.patch.object(openwa_webhook, "_handle_figures_request", return_value={"status": "ok"}) as figures:
            self.say("rate wheat 3800", quoted="wamid.R")
        figures.assert_called_once()


if __name__ == "__main__":
    unittest.main()
