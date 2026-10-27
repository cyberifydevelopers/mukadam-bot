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
    carry                         {first_row, last_row, description, balance, opening, received, delivered, formula}:
                                  the template is the previous report — closing balances become the opening ones
    clear                         {first_row, last_row, first_column, last_column}: typed
                                  text and numbers (not formulas) of a filled-in sample's
                                  old stock lines are wiped before ours are written
    values                        [cell, number | text | "=formula", (cell to copy the number format from)]
                                  — top-left cell of a merged block
    date_cells, date              cells whose "dd-mm-yyyy" gets today's date (a bare "Dated:" gets it appended);
                                  only the date's characters change, so the cell's bold / size stay
    text_style                    [target cell, source cell]: the target's text gets the character look (font,
                                  size, bold…) of the first run in the source cell — the sample's own stock line
    rich                          {cell, location, customer}: the sample's paragraph keeps its look — the
                                  words Godown / location / customer / Commander International get the same
                                  bold, underline and size the sample gives them
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


def _carry_balances(sheet, carry):
    """The template is the company's previous report: each goods line's closing balance becomes its
    opening balance and its received / delivered go empty. Rate, margin and the other formulas stay."""
    opening, received, delivered = (_column(carry[k]) if carry[k] else None for k in ("opening", "received", "delivered"))
    for row in range(carry["first_row"] - 1, carry["last_row"]):
        if not sheet.getCellByPosition(_column(carry["description"]), row).getString().strip():
            continue  # an empty line of the sample
        balance = sheet.getCellByPosition(_column(carry["balance"]), row)
        closing = balance.getValue()
        if not _is_formula(balance):
            balance.setFormula(carry["formula"].format(r=row + 1))  # a typed balance would not follow the new figures
        if opening is not None:
            sheet.getCellByPosition(opening, row).setValue(closing)
        for column in (received, delivered):
            if column is not None:
                sheet.getCellByPosition(column, row).clearContents(_VALUE | _STRING)


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


_CHAR_PROPS = ("CharFontName", "CharHeight", "CharWeight", "CharPosture", "CharUnderline", "CharColor")


def _portions(cell):
    """The cell's text as [(text, {character properties})], one entry per differently-formatted run."""
    out = []
    for paragraph in cell.createEnumeration():
        for portion in paragraph.createEnumeration():
            out.append((portion.String, {name: portion.getPropertyValue(name) for name in _CHAR_PROPS}))
    return out


def _style(cell, start, length, props):
    if length <= 0 or not props:
        return
    cursor = cell.createTextCursor()
    cursor.gotoStart(False)
    cursor.goRight(start, False)
    cursor.goRight(length, True)
    for name, value in props.items():
        cursor.setPropertyValue(name, value)


def _restyle_paragraph(cell, snapshot, location, customer):
    """Gives the freshly written paragraph the sample's own look. The sample's runs are told apart by
    their text: the one with 'godown', the one with the customer ('M/S ...'), the one naming
    Commander International, and the bold one after 'situated' (the location)."""
    if len(snapshot) < 2:
        return  # a plain one-style paragraph: nothing to copy
    text = cell.getString()
    _style(cell, 0, len(text), snapshot[0][1])

    def run(test):
        return next((props for t, props in snapshot if t.strip() and test(t.lower())), None)

    godown = run(lambda t: "godown" in t)
    commander = run(lambda t: "commander" in t)
    customer_run = run(lambda t: t.strip().startswith("m/s") and "commander" not in t)
    location_run = None
    for i, (t, props) in enumerate(snapshot):
        if "situated" in t.lower() and i + 1 < len(snapshot):
            location_run = snapshot[i + 1][1]
            if not snapshot[i + 1][0].strip():
                location_run = None
            break
    if location_run is None:
        location_run = next((props for t, props in snapshot[1:] if t.strip() and props["CharWeight"] > 100
                             and "godown" not in t.lower() and "commander" not in t.lower()
                             and not t.strip().lower().startswith("m/s")), None)

    lower = text.lower()
    m = re.search(r"godowns?", lower)
    if godown and m:
        _style(cell, m.start(), m.end() - m.start(), godown)
    if location_run and location and location != "—":
        i = text.find(location, m.end() if m else 0)
        if i >= 0:
            _style(cell, i, len(location), location_run)
    if customer_run and customer:
        i = text.find(customer)
        if i >= 0:
            end = i + len(customer)
            if text[max(i - 4, 0):i].lower() == "m/s ":
                i -= 4  # the sample's run starts at "M/S"
            _style(cell, i, end - i, customer_run)
    if commander:
        i = lower.find("m/s commander")
        if i < 0:
            i = lower.find("commander")
        end = lower.find("ltd", i)
        if i >= 0 and end >= 0:
            _style(cell, i, end + 3 - i, commander)


def _row_heights(sheet):
    last = sheet.createCursor()
    last.gotoEndOfUsedArea(False)
    return [sheet.Rows.getByIndex(r).Height for r in range(last.RangeAddress.EndRow + 1)]


def _keep_row_heights(sheet, heights):
    """LibreOffice measures a row again whenever its text changes, which can shrink a row the sample made
    taller. Every row goes back to at least the sample's height (more only when our text needs it)."""
    for r, height in enumerate(heights):
        row = sheet.Rows.getByIndex(r)
        if row.Height < height:
            row.Height = height


def _fit_to_one_page(doc, sheet):
    """The whole report prints on one page, so no table or cell is split across pages."""
    page_style = doc.StyleFamilies.getByName("PageStyles").getByName(sheet.PageStyle)
    page_style.ScaleToPagesX = 1
    page_style.ScaleToPagesY = 1


def _set_date(cell, date):
    """Replaces only the date's characters (or appends it to a bare 'Dated:'), so the cell keeps its
    own bold / size."""
    text = cell.getString()
    cursor = cell.createTextCursor()
    cursor.gotoStart(False)
    m = _DATE.search(text)
    if m:
        cursor.goRight(m.start(), False)
        cursor.goRight(m.end() - m.start(), True)
        cursor.setString(date)
    else:
        cursor.gotoEnd(False)
        cursor.setString(f" {date}")


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

            if job.get("carry"):
                _carry_balances(sheet, job["carry"])
            if job.get("insert_count"):
                _insert_rows(sheet, job)
            for cells in job.get("merges", []):
                sheet.getCellRangeByName(cells).merge(True)
            # What the sample looks like, noted before its old stock lines are wiped.
            heights = _row_heights(sheet)
            line_looks = {}
            for _target, source in job.get("text_style", []):
                if source not in line_looks:
                    runs = _portions(sheet.getCellRangeByName(source))
                    line_looks[source] = runs[0][1] if runs else None
            if job.get("clear"):
                _clear_sample_lines(sheet, job["clear"])

            rich = job.get("rich")
            snapshot = _portions(sheet.getCellRangeByName(rich["cell"])) if rich else None
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
            for target, source in job.get("text_style", []):
                cell = sheet.getCellRangeByName(target)
                _style(cell, 0, len(cell.getString()), line_looks[source])
            if rich:
                _restyle_paragraph(sheet.getCellRangeByName(rich["cell"]), snapshot, rich["location"], rich["customer"])
            for name in job.get("date_cells", []):
                _set_date(sheet.getCellRangeByName(name), job["date"])
            for name, pattern, replacement in job.get("replacements", []):
                cell = sheet.getCellRangeByName(name)
                cell.setString(re.sub(pattern, replacement, cell.getString()))

            _keep_row_heights(sheet, heights)
            _fit_to_one_page(doc, sheet)
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
