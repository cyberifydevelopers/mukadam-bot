"""Fills a stock-report template — run by LibreOffice's own Python, which has
the `uno` module (app/services/stock_report.py starts it; not imported by
the app). Working through LibreOffice keeps everything in the sample file
as it is: its formulas, formatting and letterhead picture (openpyxl drops
the picture). Only input cells are written: a formula already in a cell is
never replaced, so the sample's own calculations (and the way it works out
its drawing power) stay exactly as they were.

Usage: python stock_report_uno.py <job.json>, where the job (built by
stock_report.py) names the template, the output files and what to write:

    insert_after / insert_count   add rows below that row (1-based), formatted like it,
    first_row / last_row          and the TOTAL row's SUM(first:last) stretches over them
    merges                        ranges to merge in the new rows
    clear                         {first_row, last_row, first_column, last_column}: typed
                                  text and numbers (not formulas) of a filled-in sample's
                                  old stock lines are wiped before ours are written
    values                        [cell, number | text | "=formula", (cell to copy the number format from)]
                                  — top-left cell of a merged block
    date_cells, date              cells whose "dd-mm-yyyy" gets today's date (a bare "Dated:" gets it appended)
    replacements                  [cell, regex, replacement] applied to a label (e.g. Rs → USD)
"""

import json
import re
import subprocess
import sys
import time
import uuid

import uno
from com.sun.star.beans import PropertyValue
from com.sun.star.connection import NoConnectException
from com.sun.star.table.CellContentType import EMPTY, FORMULA

_DATE = re.compile(r"\d{1,2}[-./]\d{1,2}[-./]\d{2,4}")
_VALUE, _DATETIME, _STRING, _FORMULA = 1, 2, 4, 16  # com.sun.star.sheet.CellFlags
_STARTS = 3  #LibreOffice's launcher sometimes exits while it sets up a fresh profile; start it again


def _prop(name, value):
    p = PropertyValue()
    p.Name = name
    p.Value = value
    return p


def _column(letters):
    """'B' → 1, 'AA' → 26."""
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _start_office(soffice, profile_url):
    pipe = f"mukadam_{uuid.uuid4().hex}"
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
    deadline = time.time() + 120  # the first start creates the profile, which is slow
    for _ in range(_STARTS):
        proc = subprocess.Popen(
            [
                soffice,
                f"-env:UserInstallation={profile_url}",
                "--headless",
                "--invisible",
                "--norestore",
                "--nologo",
                "--nodefault",
                f"--accept=pipe,name={pipe};urp;",
            ]
        )
        while True:
            try:
                ctx = resolver.resolve(f"uno:pipe,name={pipe};urp;StarOffice.ComponentContext")
                return proc, ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
            except NoConnectException:
                if time.time() > deadline:
                    proc.kill()
                    raise RuntimeError("LibreOffice did not start")
                if proc.poll() is not None:
                    print(f"LibreOffice exited early (code {proc.returncode}); starting it again", file=sys.stderr)
                    break  # exited early: start it again
                time.sleep(0.5)
    raise RuntimeError("LibreOffice did not start")


def _insert_rows(sheet, job):
    """Adds copies of the last stock line below it and stretches the TOTAL row's sums over them."""
    after, count, first, last = job["insert_after"], job["insert_count"], job["first_row"], job["last_row"]
    sheet.Rows.insertByIndex(after, count)  # 0-based index `after` = the row just below row `after`
    cursor = sheet.createCursor()
    cursor.gotoEndOfUsedArea(False)
    last_column = cursor.RangeAddress.EndColumn
    source = sheet.getCellRangeByPosition(0, after - 1, last_column, after - 1)
    height = sheet.Rows.getByIndex(after - 1).Height
    for i in range(count):
        target = uno.createUnoStruct("com.sun.star.table.CellAddress")
        target.Sheet, target.Column, target.Row = 0, 0, after + i
        sheet.copyRange(target, source.RangeAddress)
        # Only the look is kept: a copied formula can point at a summary cell (=J14-E21) and end up circular.
        # The new line's own formulas are written with its values.
        sheet.getCellRangeByPosition(0, after + i, last_column, after + i).clearContents(_VALUE | _DATETIME | _STRING | _FORMULA)
        sheet.Rows.getByIndex(after + i).Height = height

    # TOTAL is now `count` rows further down.
    total_row = after + count
    pattern = re.compile(rf"(\$?[A-Z]+\$?){first}:(\$?[A-Z]+\$?){last}(?!\d)")
    for column in range(last_column + 1):
        cell = sheet.getCellByPosition(column, total_row)
        formula = cell.getFormula()
        if formula.startswith("="):
            stretched = pattern.sub(lambda m: f"{m[1]}{first}:{m[2]}{last + count}", formula)
            if stretched != formula:
                cell.setFormula(stretched)


def _clear_sample_lines(sheet, clear):
    """Wipes the old stock lines' typed text and numbers, leaving formulas and formatting."""
    left, right = _column(clear["first_column"]), _column(clear["last_column"])
    for row in range(clear["first_row"] - 1, clear["last_row"]):
        for column in range(left, right + 1):
            cell = sheet.getCellByPosition(column, row)
            if cell.Type != EMPTY and cell.Type != FORMULA:
                cell.setString("")


def _is_formula(cell):
    return cell.Type == FORMULA


def fill(job):
    proc, desktop = _start_office(job["soffice"], job["profile_url"])
    try:
        doc = desktop.loadComponentFromURL(uno.systemPathToFileUrl(job["template"]), "_blank", 0, (_prop("Hidden", True),))
        try:
            sheets = doc.Sheets
            sheet = sheets.getByIndex(0)
            # Only the report sheet goes out (and into the PDF).
            for name in list(sheets.ElementNames)[1:]:
                sheets.removeByName(name)

            if job.get("insert_count"):
                _insert_rows(sheet, job)
            for cells in job.get("merges", []):
                sheet.getCellRangeByName(cells).merge(True)
            if job.get("clear"):
                _clear_sample_lines(sheet, job["clear"])

            for name, value, *format_from in job["values"]:
                cell = sheet.getCellRangeByName(name)
                if isinstance(value, (int, float)):
                    cell.setValue(value)
                elif value.startswith("="):
                    if _is_formula(cell):
                        continue  # the sample's own calculation stays
                    cell.setFormula(value)
                else:
                    cell.setString(value)
                if format_from:
                    # The samples' empty lines aren't all formatted alike (e.g. 0.00 vs 0.000).
                    cell.NumberFormat = sheet.getCellRangeByName(format_from[0]).NumberFormat
            for name in job.get("date_cells", []):
                cell = sheet.getCellRangeByName(name)
                text = cell.getString()
                if _DATE.search(text):
                    cell.setString(_DATE.sub(job["date"], text, count=1))
                else:
                    cell.setString(f"{text.rstrip()} {job['date']}")  # "Dated:" with nothing after it
            for name, pattern, replacement in job.get("replacements", []):
                cell = sheet.getCellRangeByName(name)
                cell.setString(re.sub(pattern, replacement, cell.getString()))

            doc.calculateAll()
            doc.storeToURL(uno.systemPathToFileUrl(job["xlsx_out"]), (_prop("FilterName", "Calc MS Excel 2007 XML"),))
            doc.storeToURL(uno.systemPathToFileUrl(job["pdf_out"]), (_prop("FilterName", "calc_pdf_Export"),))
        finally:
            doc.close(True)
    finally:
        try:
            desktop.terminate()
        except Exception:  # noqa: BLE001 — the bridge drops as it exits
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as f:
        fill(json.load(f))
