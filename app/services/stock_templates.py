"""The stock-report formats (Excel samples) the bot fills, and where each
one's layout is written down.

A format is an Excel file plus a `Template`: which rows are the stock lines
and which column is the description, opening balance, quantity received …,
where the date and the godown / customer sentence sit, and which customers
(or, failing that, which bank's customers) it is used for. They live in
data/stock_templates/ (STOCK_TEMPLATE_DIR) with a registry.json beside them.
The first time it is read the registry is seeded from the samples shipped
in app/stock_report_templates/; after that the dashboard's "Report formats"
tab adds and removes them. A format added there is read by the AI
(stock_template_review.py), which writes its Template.

Nothing in a sample's own calculations is touched when it is filled (see
stock_report_uno.py): only the input cells are written.
"""

import json
import logging
import re
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "stock_report_templates"
EXTENSIONS = (".xlsx", ".xls")

_MEEZAN_TEXT = (
    "We submit hereunder the particulars of stock are lying in Godown situated at: {location}, on the "
    "account of M/S {customer}, held in your behalf/name and your account. M/s Commander International (Pvt) Ltd."
)
_ALLIED_TEXT = (
    "The particular of stock in GODOWN situated at {location} on the account of M/S {customer}, held on "
    "your behalf / name and on your account by M/s Commander International Pvt Ltd."
)


@dataclass(frozen=True)
class Template:
    """Where things go in one sample report. Rows are 1-based, as in Excel."""

    file: str  # inside the templates directory
    customers: tuple[str, ...]  # lower-case name fragments that pick this sample
    banks: tuple[str, ...]  # fallback: the sample used for this bank's other customers
    first_row: int  # the stock lines between the header and TOTAL
    last_row: int
    description: str  # columns
    opening: str | None  # a sample may lack the opening / received / delivered columns
    received: str | None
    delivered: str | None
    balance: str
    rate: str
    value: str
    dp: str | None
    date_cell: str | None  # its "dd-mm-yyyy" becomes today's date
    text_cell: str  # the godown / customer line
    text: str  # with {location} and {customer}
    margin: str | None = None  # the line's margin % column (whole number, e.g. 5)
    less_margin: str | None = None
    margin_pct: float | None = None  # the sample's own margin ("Less Margin @ 5%")
    value_divisor: int = 1  # e.g. "Present Value (Rupees in Million)"
    currency_cells: tuple[str, ...] = ()  # value labels whose "Rs" becomes the document's currency
    city_cell: str | None = None  # the addressee's city line
    city_text: str = "{city}."
    row_merges: tuple[tuple[str, str], ...] = ()  # cells merged across on each stock line
    lc_column: str | None = None  # where an "L/C #…" description goes, if not `description`
    # An uploaded sample is a report somebody already filled in: its old stock
    # lines (text and typed numbers — never formulas) are wiped before ours go in.
    clear_sample: bool = False
    last_column: str | None = None  # right edge of the stock lines, for that wipe


def _tuple_fields() -> set[str]:
    return {"customers", "banks", "currency_cells"}


def template_from_dict(data: dict) -> Template:
    known = {f.name for f in fields(Template)}
    kwargs = {k: v for k, v in data.items() if k in known}
    for key in _tuple_fields():
        kwargs[key] = tuple(kwargs.get(key) or ())
    kwargs["row_merges"] = tuple(tuple(pair) for pair in kwargs.get("row_merges") or ())
    return Template(**kwargs)


def template_to_dict(template: Template) -> dict:
    return json.loads(json.dumps(asdict(template)))  # tuples → lists


# The samples shipped with the app: (id, name, Template).
BUILTIN = (
    (
        "cannon-foam", "Cannon Foam Industries (Meezan Bank)",
        Template(
            "cannon_foam.xlsx", ("canon foam", "cannon foam"), ("meezan",),
            first_row=16, last_row=21, description="B", opening="D", received="E", delivered="F", balance="G",
            rate="K", value="L", margin="M", less_margin="N", dp="O", margin_pct=5,
            date_cell="L6", text_cell="B13", text=_MEEZAN_TEXT, currency_cells=("B26", "B28"), city_cell="B9",
            row_merges=(("B", "C"),),
        ),
    ),
    (
        "alliance-sugar", "Alliance Sugar Mills (Meezan Bank)",
        Template(
            "alliance_sugar.xlsx", ("alliance sugar",), (),
            first_row=17, last_row=18, description="B", opening="E", received="F", delivered="G", balance="H",
            rate="J", value="K", margin="L", less_margin="M", dp="N", margin_pct=10,
            date_cell="K6", text_cell="B13", text=_MEEZAN_TEXT, currency_cells=("B21", "B23"), city_cell="B9",
            row_merges=(("B", "C"),),
        ),
    ),
    (
        "thal-industries", "Thal Industries (Allied Bank)",
        Template(
            "thal_industries.xlsx", ("thal",), ("allied",),
            first_row=15, last_row=16, description="A", opening="C", received="E", delivered="F", balance="G",
            rate="I", value="J", dp="K", margin_pct=15,
            date_cell="J5", text_cell="A10", text=_ALLIED_TEXT, currency_cells=("D19", "D21", "D22", "D23"),
            city_cell="A7", city_text="Branch:{city}.", row_merges=(("A", "B"), ("C", "D")),
        ),
    ),
    (
        "jilani-poly", "Jilani Poly (Habib Metro)",
        Template(
            "jilani_poly.xls", ("jilani",), ("habibmetro", "habib metro"),
            first_row=19, last_row=22, description="F", opening="I", received="J", delivered="K", balance="L",
            rate="N", value="O", dp=None, value_divisor=1_000_000,
            date_cell="N5", text_cell="B17", text="{location}", lc_column="D",
        ),
    ),
)
DEFAULT_ID = "cannon-foam"  # used when neither the customer nor the bank has a format of its own


class TemplateError(Exception):
    """A format can't be added, found or removed (the message is shown to the user)."""


@dataclass
class Entry:
    id: str
    name: str
    source: str  # "builtin" | "uploaded"
    original_name: str
    added_at: str
    notes: list[str]
    template: Template

    @property
    def path(self) -> Path:
        return templates_dir() / self.template.file

    def to_json(self) -> dict:
        return {
            "id": self.id, "name": self.name, "source": self.source, "original_name": self.original_name,
            "added_at": self.added_at, "notes": self.notes, "layout": template_to_dict(self.template),
        }


_lock = threading.RLock()


def templates_dir() -> Path:
    return Path(settings.stock_template_dir).resolve()


def _registry_path() -> Path:
    return templates_dir() / "registry.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_from_json(raw: dict) -> Entry:
    layout = template_from_dict(raw["layout"])
    if raw.get("source", "uploaded") != "builtin":
        layout = replace(layout, margin_pct=None)  # a sample only shows the layout: its own margin is never used
    return Entry(
        id=raw["id"], name=raw.get("name") or raw["id"], source=raw.get("source", "uploaded"),
        original_name=raw.get("original_name") or layout.file, added_at=raw.get("added_at", ""),
        notes=list(raw.get("notes") or []), template=layout,
    )


def _seed() -> list[Entry]:
    folder = templates_dir()
    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for ident, name, template in BUILTIN:
        source = BUILTIN_DIR / template.file
        if not source.exists():
            logger.warning("Shipped stock-report sample %s is missing", source)
            continue
        shutil.copyfile(source, folder / template.file)
        entries.append(Entry(ident, name, "builtin", template.file, _now(), [], template))
    _save(entries)
    return entries


def _save(entries: list[Entry]) -> None:
    folder = templates_dir()
    folder.mkdir(parents=True, exist_ok=True)
    tmp = _registry_path().with_suffix(".tmp")
    tmp.write_text(json.dumps([e.to_json() for e in entries], indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_registry_path())


def load() -> list[Entry]:
    with _lock:
        if not _registry_path().exists():
            return _seed()
        try:
            raw = json.loads(_registry_path().read_text(encoding="utf-8"))
            return [_entry_from_json(item) for item in raw]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TemplateError(f"The report-format list ({_registry_path()}) can't be read: {exc}") from exc


def get(ident: str) -> Entry:
    for entry in load():
        if entry.id == ident:
            return entry
    raise TemplateError("That report format no longer exists.")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "format"


def normalise(text: str) -> str:
    """How customer names and match words are compared: lower-case letters and single spaces."""
    return re.sub(r"[^a-z]+", " ", text.lower()).strip()


def clean_words(words) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for word in words or ():
        word = normalise(str(word))
        if len(word) >= 3:
            seen[word] = None
    return tuple(seen)


_BANK_NOISE = {"bank", "limited", "ltd", "pvt", "private", "the", "pakistan"}


def bank_words(name: str | None) -> tuple[str, ...]:
    """'Meezan Bank Limited' → ('meezan',); 'Habib Metropolitan Bank Ltd.' → ('habib metropolitan',): the part of
    a bank's name that tells it apart, to be matched against the bank on a document."""
    words = [w for w in normalise(name or "").split() if w not in _BANK_NOISE]
    return clean_words([" ".join(words)]) if words else ()


def bank_matches(words: tuple[str, ...], bank: str | None) -> bool:
    """True if the bank named on a document is one of `words` ('habib metro' matches 'Habib Metropolitan Bank';
    spaces are ignored, so 'habibmetro' does too)."""
    name = normalise(bank or "")
    squeezed = name.replace(" ", "")
    return any(w in name or w.replace(" ", "") in squeezed for w in words)


def add(name: str, original_name: str, data: bytes, template: Template, notes: list[str]) -> Entry:
    """Stores an uploaded sample (already reviewed into `template`, whose file is set here)."""
    with _lock:
        entries = load()
        ident = f"{_slug(name)}-{uuid.uuid4().hex[:6]}"
        extension = Path(original_name).suffix.lower()
        file = f"{ident}{extension}"
        (templates_dir() / file).write_bytes(data)
        entry = Entry(ident, name, "uploaded", original_name, _now(), notes, replace(template, file=file))
        _save([*entries, entry])
        return entry


def update(ident: str, *, name: str | None = None, customers=None, banks=None, template: Template | None = None,
           notes: list[str] | None = None) -> Entry:
    with _lock:
        entries = load()
        for i, entry in enumerate(entries):
            if entry.id != ident:
                continue
            layout = template or entry.template
            if customers is not None:
                layout = replace(layout, customers=clean_words(customers))
            if banks is not None:
                layout = replace(layout, banks=clean_words(banks))
            entries[i] = Entry(
                entry.id, (name or entry.name).strip() or entry.name, entry.source, entry.original_name,
                entry.added_at, entry.notes if notes is None else notes, layout,
            )
            _save(entries)
            return entries[i]
    raise TemplateError("That report format no longer exists.")


def remove(ident: str) -> None:
    with _lock:
        entries = load()
        keep = [e for e in entries if e.id != ident]
        if len(keep) == len(entries):
            raise TemplateError("That report format no longer exists.")
        gone = next(e for e in entries if e.id == ident)
        _save(keep)
        if not any(e.template.file == gone.template.file for e in keep):
            gone.path.unlink(missing_ok=True)


def default(entries: list[Entry]) -> Entry | None:
    return next((e for e in entries if e.id == DEFAULT_ID), entries[0] if entries else None)
