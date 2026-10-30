"""Tests for answering WhatsApp questions about the reports (app/services/report_qa.py and its webhook wiring).

The AI service and OpenWA are mocked; nothing is sent and the real database is never touched.
"""

import json
import unittest
from datetime import datetime
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import GoodsProfile, InvoiceRecord, InvoiceStatus, ProcessedEmail, WhatsAppMessage
from app.routers import openwa_webhook
from app.services import openwa_service, report_qa, stock_report

APPROVER = "923001234567"


def payload(body, quoted=None, sender=f"{APPROVER}@c.us", message_id="m1"):
    data = {"id": message_id, "from": sender, "body": body, "fromMe": False, "isGroup": False}
    if quoted:
        data["quotedMessage"] = {"id": quoted}
    return {"event": "message.received", "data": data}


class Parsing(unittest.TestCase):
    def test_questions_are_recognised(self):
        for text in ["what is margin", "Which factory is this from?", "kitna rate hai", "rate of putti?", "previous balance of cotton",
                     "margin"]:
            self.assertIsNotNone(openwa_service.parse_question(payload(text)), text)

    def test_acknowledgements_and_noise_get_no_answer(self):
        for text in ["ok", "Thanks!", "thank you", "shukriya", "👍", "hi", "Assalam o Alaikum", "  ", "12"]:
            self.assertIsNone(openwa_service.parse_question(payload(text)), text)

    def test_own_group_and_status_messages_are_ignored(self):
        for flag in ("fromMe", "isGroup", "isStatusBroadcast"):
            p = payload("what is margin")
            p["data"][flag] = True
            self.assertIsNone(openwa_service.parse_question(p), flag)

    def test_quote_and_report_number_are_picked_up(self):
        q = openwa_service.parse_question(payload("what is the margin for report 16", quoted="wamid.X"))
        self.assertEqual((q.quoted_message_id, q.target_label, q.sender_phone), ("wamid.X", "16", APPROVER))

    def test_a_question_is_never_read_as_figures(self):
        for text in ["what is the margin of wheat for report 16", "margin of wheat for report 16?", "kya rate 3800 hai"]:
            self.assertIsNone(openwa_service.parse_figures_request(payload(text)), text)

    def test_real_figures_still_parse(self):
        req = openwa_service.parse_figures_request(payload("margin wheat 5"))
        self.assertEqual(req.figures, [("margin", "wheat", "5")])
        self.assertEqual(openwa_service.parse_figures_request(payload("rate 3800")).figures, [("rate", "", "3800")])

    def test_looks_like_question(self):
        self.assertTrue(openwa_service.looks_like_question("what is margin"))
        self.assertTrue(openwa_service.looks_like_question("margin 5?"))
        self.assertFalse(openwa_service.looks_like_question("margin wheat 5"))


class Database(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, autoflush=False)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.db.add(ProcessedEmail(id=1, message_id="g1", sender="Meezan Bank <ops@meezan.example>", subject="Delivery order Mian Cotton",
                                   received_at=datetime(2026, 10, 7, 9, 30)))
        order = {"customer_name": "Mian Cotton Ginning Pressing & Oil Mills", "pledge_location": "Head Rajkan Road, Bahawalpur",
                 "bank_branch": "Circular Road Branch Bahawalpur", "currency": "PKR",
                 "rows": [{"description": "Putti (munds)", "opening_balance": "16600", "quantity_released": "2600", "rate_per_unit": "3800"},
                          {"description": "Cotton (Bales)", "opening_balance": "1000"}]}
        self.db.add(InvoiceRecord(id=3, email_id=1, pdf_filename="h_DO Mian Cotton.pdf", bank_name="Meezan Bank", bank_code="MEEZAN",
                                  status=InvoiceStatus.CONFIRMED, reference_number="CAD/DQ/03/2026", txn_date="9-Oct-26",
                                  stock_missing=json.dumps(["Margin %"]),
                                  raw_extracted_json=json.dumps({"delivery_order": order, "document_type": "Delivery Order"})))
        self.db.add(GoodsProfile(customer_key="miancottonginningpressingoilmills", goods_key="putti",
                                 customer_name="Mian Cotton Ginning Pressing & Oil Mills", rate_per_unit="3800"))
        self.db.commit()


class Context(Database):
    def build(self, record, text="what is margin"):
        with mock.patch.object(stock_report, "latest", return_value=None):
            return report_qa.build_context(self.db, record, text)

    def test_context_has_the_document_facts_and_source(self):
        ctx = self.build(self.db.get(InvoiceRecord, 3))
        for expected in ("Meezan Bank", "Circular Road Branch Bahawalpur", "ops@meezan.example", "Head Rajkan Road, Bahawalpur",
                         "CAD/DQ/03/2026", "Mian Cotton Ginning Pressing & Oil Mills", "Putti (munds)", "16600"):
            self.assertIn(expected, ctx)

    def test_missing_figures_are_flagged_not_filled(self):
        ctx = self.build(self.db.get(InvoiceRecord, 3))
        self.assertIn('"missing_from_report"', ctx)
        self.assertNotIn("margin_pct", ctx)  # the order itself carries no margin

    def test_saved_figures_per_factory_are_included(self):
        ctx = self.build(self.db.get(InvoiceRecord, 3))
        self.assertIn("FIGURES KEPT PER FACTORY", ctx)
        self.assertIn("putti", ctx)

    def test_with_no_report_named_the_latest_is_used_and_said_so(self):
        ctx = self.build(None)
        self.assertIn("No report was named", ctx)
        self.assertIn("#3", ctx)

    def test_a_report_file_is_read_as_computed_figures(self):
        import openpyxl
        import os
        import tempfile

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Description of Goods", "Balance on Hand", "Rate Per Unit", "Total Value"])
        ws.append(["Putti (munds)", 14000, 3800, 53200000])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.xlsx")
            wb.save(path)
            text = report_qa._sheet_text(path)
        self.assertIn("Putti (munds) | 14000 | 3800 | 53200000", text)
        self.assertIn("Total Value", text)

    def test_a_missing_report_file_gives_no_text_instead_of_an_error(self):
        self.assertEqual(report_qa._sheet_text("Z:/nope.xlsx"), "")


class Answering(Database):
    def reply(self, content):
        response = mock.Mock()
        response.choices = [mock.Mock(message=mock.Mock(content=content))]
        return response

    def test_the_model_gets_the_rules_the_data_and_the_question(self):
        with mock.patch("app.parsers.llm_parser.openrouter_client"), \
             mock.patch("app.parsers.llm_parser.chat_with_fallback", return_value=self.reply("Margin abhi nahi diya gaya.")) as chat, \
             mock.patch.object(stock_report, "latest", return_value=None):
            out = report_qa.answer(self.db, "what is margin", self.db.get(InvoiceRecord, 3))
        self.assertEqual(out, "Margin abhi nahi diya gaya.")
        messages = chat.call_args.kwargs["messages"]
        self.assertIn("Never invent", messages[0]["content"])
        self.assertIn("Circular Road Branch Bahawalpur", messages[0]["content"])
        self.assertEqual(messages[1], {"role": "user", "content": "what is margin"})
        self.assertEqual(chat.call_args.kwargs["temperature"], 0)

    def test_markdown_is_turned_into_whatsapp_formatting(self):
        given = "**Margin** is *5%*\n## Source\n\u2022 **Bank**: Meezan"
        self.assertEqual(report_qa.whatsapp_format(given), "*Margin* is *5%*\nSource\n\u2022 *Bank*: Meezan")

    def test_the_prompt_demands_a_direct_answer_not_an_explanation(self):
        self.assertIn("ANSWER DIRECTLY", report_qa._SYSTEM)
        self.assertIn("asks for the VALUE", report_qa._SYSTEM)
        self.assertIn("unless the person clearly asks what", report_qa._SYSTEM)

    def test_the_prompt_asks_for_roman_urdu_not_urdu_script(self):
        self.assertIn("Roman Urdu", report_qa._SYSTEM)
        self.assertIn("never Urdu script", report_qa._SYSTEM)

    def test_an_empty_reply_is_an_error_for_the_person(self):
        with mock.patch("app.parsers.llm_parser.openrouter_client"), \
             mock.patch("app.parsers.llm_parser.chat_with_fallback", return_value=self.reply("  ")), \
             mock.patch.object(stock_report, "latest", return_value=None):
            with self.assertRaises(report_qa.AnswerError):
                report_qa.answer(self.db, "what is margin", None)

    def test_a_provider_failure_becomes_a_plain_message(self):
        with mock.patch("app.parsers.llm_parser.openrouter_client"), \
             mock.patch("app.parsers.llm_parser.chat_with_fallback", side_effect=RuntimeError("boom")), \
             mock.patch.object(stock_report, "latest", return_value=None):
            with self.assertRaises(report_qa.AnswerError) as ctx:
                report_qa.answer(self.db, "what is margin", None)
        self.assertNotIn("boom", str(ctx.exception))
        self.assertIn("try again", str(ctx.exception))

    def test_missing_api_key_is_explained(self):
        from app.parsers import llm_parser

        with mock.patch("app.parsers.llm_parser.openrouter_client", side_effect=llm_parser.LlmParseError("The AI service isn't set up yet")):
            with self.assertRaises(report_qa.AnswerError) as ctx:
                report_qa.answer(self.db, "what is margin", None)
        self.assertIn("isn't set up", str(ctx.exception))


class Webhook(Database):
    def setUp(self):
        super().setUp()
        spawn = mock.patch.object(openwa_webhook, "_spawn")
        self.spawn = spawn.start()
        self.addCleanup(spawn.stop)
        approvers = mock.patch.object(openwa_webhook, "settings", mock.Mock(notify_numbers_list=[APPROVER]))
        approvers.start()
        self.addCleanup(approvers.stop)

    def handle(self, body, **kw):
        result = openwa_webhook._handle_payload(self.db, payload(body, **kw))
        self.db.commit()
        return result

    def logged(self):
        return self.db.query(WhatsAppMessage).filter_by(direction="in").all()

    def test_a_question_from_the_approver_is_logged_and_answered_in_the_background(self):
        self.assertEqual(self.handle("what is margin for report 3")["status"], "ok")
        self.spawn.assert_called_once_with(openwa_webhook._reply_to_question, APPROVER, "what is margin for report 3", 3)
        self.assertEqual([m.text for m in self.logged()], ["what is margin for report 3"])

    def test_a_question_with_no_report_named_is_still_answered(self):
        self.handle("which factories have margin saved?")
        self.assertEqual(self.spawn.call_args.args[3], None)

    def test_a_stranger_gets_no_answer_and_leaves_no_trace(self):
        result = openwa_webhook._handle_payload(self.db, payload("what is margin", sender="923009999999@c.us"))
        self.assertEqual(result["status"], "ignored")
        self.spawn.assert_not_called()
        self.assertEqual(self.logged(), [])

    def test_a_redelivered_message_is_answered_once(self):
        self.handle("what is margin")
        self.assertEqual(self.handle("what is margin")["status"], "duplicate")
        self.assertEqual(self.spawn.call_count, 1)

    def test_yes_and_no_still_go_to_the_approval_flow_not_to_the_assistant(self):
        self.handle("yes")
        self.spawn.assert_not_called()

    def test_figures_still_go_to_the_figures_flow(self):
        with mock.patch.object(openwa_webhook, "_handle_figures_request", return_value={"status": "ok"}) as figures:
            self.handle("margin wheat 5")
        figures.assert_called_once()
        self.spawn.assert_not_called()

    def test_a_question_containing_a_number_is_not_taken_as_figures(self):
        with mock.patch.object(openwa_webhook, "_handle_figures_request") as figures:
            self.handle("what is the margin of wheat for report 3")
        figures.assert_not_called()
        self.spawn.assert_called_once()

    def test_thanks_is_not_answered(self):
        self.assertEqual(self.handle("thanks")["status"], "ignored")
        self.spawn.assert_not_called()


class Reply(Database):
    def run_reply(self, answer_result, record_id=3):
        sent = []
        with mock.patch.object(openwa_webhook, "SessionLocal", self.Session), \
             mock.patch.object(openwa_webhook, "_send_logged", side_effect=lambda db, to, text, target: sent.append((to, text))), \
             mock.patch.object(report_qa, "answer", **answer_result):
            openwa_webhook._reply_to_question(APPROVER, "what is margin", record_id)
        return sent

    def test_the_answer_is_sent_to_the_asker(self):
        self.assertEqual(self.run_reply({"return_value": "Margin is 5%"}), [(APPROVER, "Margin is 5%")])

    def test_an_answer_error_is_told_to_the_asker(self):
        self.assertEqual(self.run_reply({"side_effect": report_qa.AnswerError("AI is busy")}), [(APPROVER, "AI is busy")])

    def test_an_unexpected_crash_still_gets_a_reply(self):
        (to, text), = self.run_reply({"side_effect": ValueError("bug")})
        self.assertEqual(to, APPROVER)
        self.assertIn("went wrong", text)


if __name__ == "__main__":
    unittest.main()
