"""JSON API behind the dashboard's "Stock Reports" tab: every stock report the bot
has made, kept per firm (app/services/stock_report.py writes them to
STOCK_REPORT_DIR/monthly/<firm>/<year-month>/<firm> dd-mm-yy.xlsx + .pdf + a
small .json). The tab lists them by firm and month and opens a report in the
page; the PDF is the sheet exactly as the Excel file looks.

Like the rest of /api this has no login — it is meant for 127.0.0.1 only.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import settings
from app.services import stock_report

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/inventory", tags=["inventory"])

_EXTENSIONS = {"xlsx": stock_report.XLSX_MIME, "pdf": stock_report.PDF_MIME}
_TRAILING_DATE = re.compile(r"\s+\d\d-\d\d-\d\d(\s+\(\d+\))?$")


def _root() -> Path:
    return Path(settings.stock_report_dir, "monthly")


def _date_key(date: str) -> str:
    try:
        return datetime.strptime(date, "%d-%m-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


@router.get("")
def list_inventory():
    """The firms with their reports, firms by latest report first, each firm's reports newest first."""
    firms: dict[str, dict] = {}
    for path in sorted(_root().glob("*/*/*.json")):
        state = stock_report._read_state(str(path))
        if not state or not path.with_suffix(".pdf").exists():
            continue
        folder = path.parent.parent.name
        key = state.get("firm") or folder
        customer = state.get("customer") or _TRAILING_DATE.sub("", path.stem)
        firm = firms.setdefault(key, {"key": key, "name": customer, "reports": []})
        firm["reports"].append({
            "ref": f"{folder}/{path.parent.name}/{path.stem}",
            "name": path.stem,
            "date": state.get("date", ""),
            "month": path.parent.name,
            "record": state.get("record"),
            "created": state.get("created", ""),
            "xlsx": path.with_suffix(".xlsx").exists(),
        })
        if state.get("customer"):
            firm["name"] = state["customer"]
    for firm in firms.values():
        firm["reports"].sort(key=lambda r: (_date_key(r["date"]), r["created"]), reverse=True)
        firm["latest"] = firm["reports"][0]["date"]
    ordered = sorted(firms.values(), key=lambda f: (_date_key(f["latest"]), f["name"].lower()), reverse=True)
    return {"firms": ordered}


def _file(ref: str, extension: str) -> Path:
    """The report file named by `ref` (<folder>/<year-month>/<name>), only ever inside the inventory folder."""
    if extension not in _EXTENSIONS:
        raise HTTPException(404, "Unknown file type")
    parts = ref.split("/")
    if len(parts) != 3 or any(p in ("", ".", "..") or "\\" in p for p in parts):
        raise HTTPException(404, "Report not found")
    path = (_root() / parts[0] / parts[1] / f"{parts[2]}.{extension}").resolve()
    if _root().resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "Report not found")
    return path


@router.get("/file/{ref:path}.{extension}")
def get_file(ref: str, extension: str, download: bool = False):
    """The report as it is: the PDF opens in the browser, the Excel file downloads."""
    path = _file(ref, extension)
    inline = extension == "pdf" and not download
    return FileResponse(
        path, media_type=_EXTENSIONS[extension], filename=path.name,
        content_disposition_type="inline" if inline else "attachment",
    )


@router.delete("/file/{ref:path}")
def delete_report(ref: str):
    """Removes one report (its Excel, PDF and note). The next report of that firm then carries on from the one before it."""
    pdf = _file(ref, "pdf")
    for extension in ("xlsx", "pdf", "json"):
        pdf.with_suffix(f".{extension}").unlink(missing_ok=True)
    logger.info("Stock report %s deleted", ref)
    return {"deleted": ref}
