"""'Saved to sheet' is shown as done only when nothing is still needed (report format, margin, figures) — in the Overview
table's Progress / Result columns, the document lists and the notification bell. The page's own JavaScript is run under
node (skipped when node isn't installed); the layout is checked for the table fitting the window.
"""

import json
import re
import shutil
import subprocess
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import InvoiceRecord, InvoiceStatus
from app.routers import notifications
from app.services import stock_report

HTML = open("app/static/index.html", encoding="utf-8").read()


def between(start, end):
    a = HTML.index(start)
    return HTML[a:HTML.index(end, a)]


HARNESS = (
    'const esc = (x) => String(x ?? ""); const fmtTime = (x) => x; const statusBadge = (s) => "[" + s + "]";\n'
    + between("// What a saved document still needs", "const needsFormatBadge")  # pendingOf, usdBadge, docStatusBadge
    + between("// One step of the progress bar", "// Where the time went")
    + '\nconst doc = (extra) => ({ status: "CONFIRMED", whatsapp: "read", needs_format: false, stock_missing: [], ...extra });\n'
    + 'const email = (docs) => ({ stage: "done", received_at: "t", attachments: ["a.pdf"], bank_name: "Meezan", documents: docs });\n'
    + 'const labels = (docs) => progressSteps(email(docs)).map((h) => h.replace(/<[^>]+>/g, "").trim());\n'
    + "console.log(JSON.stringify(%s));\n"
)


def run_js(expression):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    out = subprocess.run([node, "-e", HARNESS % expression], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


class ProgressColumn(unittest.TestCase):
    def test_complete_shows_saved_to_sheet_as_done(self):
        labels = run_js("labels([doc({})])")
        self.assertIn("✓ Saved to sheet", labels)
        self.assertFalse([l for l in labels if "missing" in l or "required" in l])

    def test_missing_margin_keeps_saved_to_sheet_pending(self):
        labels = run_js('labels([doc({ stock_missing: ["Margin %", "Rate per unit"] })])')
        self.assertNotIn("✓ Saved to sheet", labels)
        self.assertIn("○ Saved to sheet", labels)
        self.assertIn("! Margin & figures missing", labels)

    def test_a_missing_sample_keeps_it_pending_too(self):
        labels = run_js("labels([doc({ needs_format: true })])")
        self.assertNotIn("✓ Saved to sheet", labels)
        self.assertIn("! Sample required", labels)

    def test_both_are_listed_together(self):
        labels = run_js('labels([doc({ needs_format: true, stock_missing: ["Margin %"] })])')
        self.assertIn("! Sample required", labels)
        self.assertIn("! Margin & figures missing", labels)

    def test_figures_other_than_margin_are_counted(self):
        self.assertIn("! 2 figures missing", run_js('labels([doc({ stock_missing: ["Rate per unit", "Weight per unit"] })])'))

    def test_a_usd_document_is_flagged_and_holds_saved_to_sheet_back(self):
        labels = run_js("labels([doc({ needs_pkr: true })])")
        self.assertNotIn("✓ Saved to sheet", labels)
        self.assertIn("! Amount in USD: PKR amount needed", labels)

    def test_one_incomplete_document_of_two_holds_it_back(self):
        labels = run_js('labels([doc({}), doc({ stock_missing: ["Margin %"] })])')
        self.assertNotIn("✓ Saved to sheet", labels)

    def test_waiting_for_yes_and_skipped_are_unchanged(self):
        self.assertIn("Waiting for your YES / NO", " ".join(run_js('labels([doc({ status: "PENDING_CONFIRMATION" })])')))
        self.assertIn("– Skipped by you", run_js('labels([doc({ status: "REJECTED" })])'))


class StatusBadge(unittest.TestCase):
    def test_complete_documents_say_saved(self):
        self.assertEqual(run_js('docStatusBadge(doc({}))'), "[CONFIRMED]")

    def test_incomplete_documents_say_not_complete_yet(self):
        for extra in ('{ needs_format: true }', '{ stock_missing: ["Margin %"] }'):
            self.assertIn("Not complete yet", run_js(f"docStatusBadge(doc({extra}))"))

    def test_other_statuses_are_untouched(self):
        self.assertEqual(run_js('docStatusBadge(doc({ status: "PENDING_CONFIRMATION", needs_format: true }))'), "[PENDING_CONFIRMATION]")


class UsdAlert(unittest.TestCase):
    def test_the_badge_is_shown_only_for_a_usd_document_still_without_rupees(self):
        self.assertIn("AMOUNT IS IN USD", run_js("usdBadge({ needs_pkr: true })"))
        self.assertEqual(run_js("usdBadge({ needs_pkr: false })"), "")

    def test_the_badge_is_bold_and_on_every_document_list(self):
        self.assertRegex(HTML, r"\.usd-alert \{ font-weight: 800;")
        self.assertEqual(HTML.count("usdBadge("), 4)  # used in the 4 document lists / headers

    def test_the_document_page_has_a_bold_alert(self):
        self.assertIn("THIS AMOUNT IS IN USD.", HTML)
        self.assertIn("Get the PKR amount from the bank", HTML)
        self.assertNotRegex(HTML, r"usd rate \d+")  # the page never asks for a dollar rate


class TableLayout(unittest.TestCase):
    def test_incoming_emails_table_shares_the_window_width(self):
        self.assertIn('<table class="act-table">', HTML)
        self.assertRegex(HTML, r"\.act-table \{ table-layout: fixed; \}")
        cols = re.search(r"<colgroup>(.*?)</colgroup>", between("function activityPanel", "/* ---------- Views"), re.S).group(1)
        percentages = sum(int(n) for n in re.findall(r"width:(\d+)%", cols))
        self.assertLessEqual(percentages, 100)
        self.assertEqual(cols.count("<col"), 8)  # one per column: checkbox, #, received, email, status, progress, result, delete

    def test_the_table_has_no_scrolling_of_its_own_in_either_direction(self):
        self.assertRegex(HTML, r"\.activity-scroll \{ overflow: visible;")
        self.assertNotIn("fitActivityRows", HTML)  # it used to cap the table at 5 rows with an inner scrollbar
        self.assertNotIn("ACTIVITY_VISIBLE_ROWS", HTML)
        self.assertNotIn("activity-scroll\")?.scrollTop", HTML)
        self.assertNotIn("min-width: 820px", HTML)  # no narrow-screen fallback that brings a sideways scrollbar back

    def test_text_wraps_instead_of_forcing_a_scroll(self):
        self.assertRegex(HTML, r"\.act-table td\.nowrap, \.act-table \.step \{ white-space: normal; \}")


class Notifications(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autoflush=False)
        app = FastAPI()
        app.include_router(notifications.router)

        def db():
            s = Session()
            try:
                yield s
            finally:
                s.close()

        app.dependency_overrides[get_db] = db
        self.client = TestClient(app)
        self.Session = Session

    def saved_event(self, stock_missing=None, needs_format=False):
        with self.Session() as s:
            s.add(InvoiceRecord(id=1, email_id=1, pdf_filename="h_DO Mian Cotton.pdf", status=InvoiceStatus.CONFIRMED,
                                stock_missing=json.dumps(stock_missing) if stock_missing else None, raw_extracted_json="{}"))
            s.commit()
        with mock.patch.object(stock_report, "needs_format", return_value=needs_format):
            events = self.client.get("/api/notifications").json()["events"]
        return next(e for e in events if e["kind"] == "saved")

    def test_a_complete_document_is_saved_to_the_sheet(self):
        event = self.saved_event()
        self.assertEqual((event["title"], event["tone"]), ("Saved to the sheet", "ok"))

    def test_missing_margin_makes_it_not_complete_yet(self):
        event = self.saved_event(stock_missing=["Margin %"])
        self.assertEqual((event["title"], event["tone"]), ("Saved, not complete yet", "warn"))
        self.assertIn("margin & figures", event["text"])

    def test_a_missing_sample_is_named(self):
        event = self.saved_event(needs_format=True)
        self.assertEqual(event["title"], "Saved, not complete yet")
        self.assertIn("sample", event["text"])


if __name__ == "__main__":
    unittest.main()
