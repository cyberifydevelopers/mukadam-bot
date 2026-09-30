"""Renders a saved email body (gmail_service.save_body_document()) as a
readable HTML page for the dashboard's "Open file" link.

The .txt is headers + the plain-text body, where a forwarded email repeats
"---------- Forwarded message ---------" blocks and HTML tables arrive as
"cell | cell" lines. Here the forward chain is folded away, the original
message is put first, and the table lines become real tables.
"""

import html
import re

_FWD = re.compile(r"^-{5,}\s*Forwarded message\s*-{5,}\s*$", re.M)
_HEADER = re.compile(r"^(From|To|Cc|Date|Subject):\s*(.*)$")
_KV = re.compile(r"^([A-Z][\w ./&()-]{1,40}?)\s*:\s+(\S.{0,200})$")
_DISCLAIMER = re.compile(r"^\s*(DISCLAIMER|CONFIDENTIALITY)\b", re.I)


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _split_headers(block: str) -> tuple[dict[str, str], str]:
    """Leading "Key: value" header lines → dict, and the rest of the block."""
    headers: dict[str, str] = {}
    lines = block.strip("\n").split("\n")
    i = 0
    while i < len(lines):
        m = _HEADER.match(lines[i].strip())
        if not m:
            break
        headers[m.group(1)] = m.group(2).strip()
        i += 1
    return headers, "\n".join(lines[i:]).strip()


def _paragraphs(body: str) -> list[list[str]]:
    paras, cur = [], []
    for line in body.split("\n"):
        if line.strip():
            cur.append(line.rstrip())
        elif cur:
            paras.append(cur)
            cur = []
    if cur:
        paras.append(cur)
    return paras


def _render_table(rows: list[list[str]]) -> str:
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head, body = rows[0], rows[1:]
    numeric = [
        bool(body) and all(re.fullmatch(r"[\d,.\s%()-]*", r[c]) for r in body) for c in range(width)
    ]
    out = ["<div class='tbl'><table><thead><tr>"]
    out += [f"<th class='{'num' if numeric[c] else ''}'>{_esc(h)}</th>" for c, h in enumerate(head)]
    out.append("</tr></thead><tbody>")
    for r in body:
        total = r[0].strip().lower() in ("total", "grand total", "sub total", "subtotal")
        out.append(f"<tr class='{'total' if total else ''}'>")
        out += [f"<td class='{'num' if numeric[c] else ''}'>{_esc(v)}</td>" for c, v in enumerate(r)]
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _render_body(body: str) -> str:
    """Paragraphs → <p>; runs of "a | b" lines (blank lines between them
    allowed) → one table; "Key: value" lines → a highlighted field."""
    out: list[str] = []
    table: list[list[str]] = []

    def flush():
        if len(table) >= 2:
            out.append(_render_table(table))
        elif table:  # a lone "a | b" line is a signature, not a table
            out.append(f"<p>{_esc(' | '.join(table[0]))}</p>")
        table.clear()

    for para in _paragraphs(body):
        if all(" | " in line for line in para):
            table.extend([c.strip() for c in line.split(" | ")] for line in para)
            continue
        flush()
        text = " ".join(line.strip() for line in para)
        if _DISCLAIMER.match(text):
            out.append(f"<p class='disclaimer'>{_esc(text)}</p>")
        elif len(para) == 1 and (m := _KV.match(text)):
            out.append(f"<div class='field'><span>{_esc(m.group(1))}</span><b>{_esc(m.group(2))}</b></div>")
        else:
            out.append("<p>" + "<br>".join(_esc(line.strip()) for line in para) + "</p>")
    flush()
    return "\n".join(out)


def _header_rows(h: dict[str, str]) -> str:
    return "".join(
        f"<div class='hk'>{k}</div><div class='hv'>{_esc(h[k])}</div>" for k in ("From", "To", "Cc", "Date") if h.get(k)
    )


def render_email_html(text: str, title: str, image_urls: list[str]) -> str:
    """The whole page. `image_urls` are the email's pictures (logos)."""
    parts = _FWD.split(text)
    received, top_body = _split_headers(parts[0])
    messages = [_split_headers(p) for p in parts[1:]]  # oldest last

    # The original message is the deepest forward that has real content;
    # the forwards above it that add a note of their own are kept too.
    with_content = [(h, b) for h, b in messages if b]
    original = with_content[-1] if with_content else (received, top_body)
    notes = [(h, b) for h, b in with_content[:-1]]
    if top_body and with_content:
        notes.insert(0, (received, top_body))
    chain = [h for h, _ in messages]

    subject = original[0].get("Subject") or received.get("Subject") or title
    main = f"""
      <article class="card">
        <h1>{_esc(subject)}</h1>
        <div class="hdr">{_header_rows(original[0])}</div>
        <div class="body">{_render_body(original[1])}</div>
        {"".join(f"<img class='pic' src='{_esc(u)}' alt=''>" for u in image_urls)}
      </article>"""

    notes_html = "".join(
        f"""<article class="card note"><div class="hdr">{_header_rows(h)}</div>
            <div class="body">{_render_body(b)}</div></article>"""
        for h, b in notes
    )
    chain_html = ""
    if chain:
        items = "".join(
            f"<li><b>{_esc(h.get('From', '?'))}</b> → {_esc(h.get('To', '?'))}<span>{_esc(h.get('Date', ''))}</span></li>"
            for h in chain
        )
        chain_html = f"""<details class="card chain"><summary>Forwarded {len(chain)} time{'s' if len(chain) != 1 else ''}
            before reaching us · received {_esc(received.get('Date', ''))} from {_esc(received.get('From', ''))}</summary>
            <ol reversed>{items}</ol></details>"""
    notes_block = f"<h2>Notes added while forwarding</h2>{notes_html}" if notes_html else ""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>
:root {{ --bg:#f4f5f7; --card:#fff; --text:#1c2024; --muted:#6b7280; --line:#e5e7eb; --accent:#0f766e; --head:#f8fafc; --total:#ecfdf5; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --card:#181b21; --text:#e6e8eb; --muted:#9aa1ab; --line:#2a2f37; --accent:#2dd4bf; --head:#1f232a; --total:#12302b; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:860px; margin:0 auto; padding:24px 16px 48px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:22px 24px; margin-bottom:16px; }}
h1 {{ font-size:20px; line-height:1.3; margin:0 0 14px; }}
h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); margin:28px 0 10px; }}
.hdr {{ display:grid; grid-template-columns:max-content 1fr; gap:4px 14px; font-size:13px; padding-bottom:14px; border-bottom:1px solid var(--line); margin-bottom:16px; }}
.hk {{ color:var(--muted); }} .hv {{ overflow-wrap:anywhere; }}
.body p {{ margin:0 0 12px; overflow-wrap:anywhere; }}
.field {{ display:flex; flex-wrap:wrap; gap:4px 12px; align-items:baseline; background:var(--head); border-left:3px solid var(--accent); border-radius:6px; padding:8px 12px; margin:14px 0 8px; }}
.field span {{ color:var(--muted); font-size:13px; }}
.tbl {{ overflow-x:auto; margin:4px 0 18px; border:1px solid var(--line); border-radius:8px; }}
table {{ border-collapse:collapse; width:100%; font-size:14px; }}
th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid var(--line); }}
th {{ background:var(--head); font-weight:600; font-size:13px; }}
tbody tr:last-child td {{ border-bottom:0; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
tr.total td {{ background:var(--total); font-weight:700; }}
.disclaimer {{ font-size:11.5px; color:var(--muted); border-top:1px solid var(--line); padding-top:12px; margin-top:20px; }}
.pic {{ max-width:220px; max-height:90px; display:block; margin-top:8px; }}
.note {{ font-size:14px; }}
.chain summary {{ cursor:pointer; color:var(--muted); font-size:13px; }}
.chain ol {{ margin:12px 0 0; padding-left:22px; font-size:13px; }}
.chain li {{ margin:4px 0; overflow-wrap:anywhere; }} .chain li span {{ color:var(--muted); margin-left:8px; }}
</style></head>
<body><main>{main}{notes_block}{chain_html}</main></body></html>"""
