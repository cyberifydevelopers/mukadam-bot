"""The stock report's table must show everything it holds: a wider substitute font (the server has no Cambria) or a long
description may not cut text off. The LibreOffice-side helpers are tested through their source (the `uno` module only exists
inside LibreOffice); the real thing was checked by rendering the report in LibreOffice."""

import re
import unittest
from datetime import datetime
from pathlib import Path

from app.services import stock_report, stock_templates

UNO_SOURCE = Path("app/services/stock_report_uno.py").read_text(encoding="utf-8")


def load_helpers():
    start = UNO_SOURCE.index("def _char_width")
    end = UNO_SOURCE.index("def _grow_wrapped")
    namespace = {"re": re}
    exec(UNO_SOURCE[start:end], namespace)  # noqa: S102 — our own file
    return namespace["_text_height"]


text_height = load_helpers()
LINE = 12 * 1.2 * 35.28  # one line of 12 pt text, 1/100 mm


def runs(text, pt=12, bold=False):
    return [(text, {"CharHeight": pt, "CharWeight": 150 if bold else 100})]


class TextHeight(unittest.TestCase):
    WIDTH = 3000  # 3 cm

    def lines(self, text, **kw):
        return round((text_height(runs(text, **kw), self.WIDTH) - 120) / LINE)

    def test_short_text_is_one_line(self):
        self.assertEqual(self.lines("Wheat"), 1)

    def test_text_wraps_at_spaces(self):
        self.assertGreater(self.lines("WANNATE TDI-80 L/C 26INSU0201-02371 imported chemical"), 2)

    def test_a_word_longer_than_a_line_is_broken_instead_of_overflowing(self):
        self.assertGreaterEqual(self.lines("26INSU0201026INSU0201026INSU02010"), 2)

    def test_the_wrapped_text_of_the_reported_case_needs_several_lines(self):
        self.assertGreaterEqual(self.lines("WANNATE TDI-80 L/C 26INSU0201-02371", bold=True), 3)

    def test_bold_and_bigger_text_need_more_room(self):
        text = "Cannon Foam Industries (PVT) Ltd, 44-KM, G.T Road"
        self.assertGreaterEqual(text_height(runs(text, bold=True), self.WIDTH), text_height(runs(text), self.WIDTH))
        self.assertGreater(text_height(runs(text, pt=16), self.WIDTH), text_height(runs(text, pt=10), self.WIDTH))

    def test_a_line_break_starts_a_new_line(self):
        self.assertEqual(self.lines("a\nb\nc"), 3)

    def test_empty_text_is_one_small_line(self):
        self.assertLess(text_height(runs(""), self.WIDTH), 2 * LINE)

    def test_a_wider_cell_needs_less_height(self):
        text = "WANNATE TDI-80 L/C 26INSU0201-02371 imported polyurethane chemical"
        self.assertLess(text_height(runs(text), 9000), text_height(runs(text), 3000))


class WhatIsChecked(unittest.TestCase):
    def test_the_goods_descriptions_and_the_godown_sentence_are_checked_for_height(self):
        entry = stock_templates.get("mian-cotton-ginning-pressing-oil-mills-8b783e")
        order = {"customer_name": "Mian Cotton", "currency": "PKR", "pledge_location": "Bahawalpur",
                 "rows": [{"description": "Wheat", "opening_balance": "1"}, {"description": "Cotton (Bales)", "opening_balance": "2"}]}
        job = stock_report.build_job(entry.template, order, datetime(2026, 10, 30))
        t = entry.template
        self.assertIn(t.text_cell, job["grow"])
        self.assertEqual([c for c in job["grow"] if c != t.text_cell], [f"{t.description}{t.first_row}", f"{t.description}{t.first_row + 1}"])


class LibreOfficeSteps(unittest.TestCase):
    def test_rows_only_grow_where_the_text_needs_it(self):
        self.assertRegex(UNO_SOURCE, r"if need > have \* 1\.02:")

    def test_the_date_and_the_replaced_currency_labels_shrink_instead_of_being_cut(self):
        self.assertIn("date_cell.ShrinkToFit = True", UNO_SOURCE)
        self.assertRegex(UNO_SOURCE, r"cell\.setString\(re\.sub\(pattern, replacement, cell\.getString\(\)\)\)\n\s+cell\.IsTextWrapped = False\n\s+cell\.ShrinkToFit = True")

    def test_it_runs_after_the_samples_own_heights_are_restored(self):
        self.assertLess(UNO_SOURCE.index("_keep_row_heights(sheet, heights)"), UNO_SOURCE.index("_grow_wrapped(sheet, job.get"))


class Server(unittest.TestCase):
    def test_the_docker_image_has_fonts_with_cambria_and_calibris_widths(self):
        docker = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("fonts-crosextra-caladea", docker)  # Cambria
        self.assertIn("fonts-crosextra-carlito", docker)  # Calibri
        self.assertIn("fonts-liberation", docker)  # Times New Roman / Arial


if __name__ == "__main__":
    unittest.main()
