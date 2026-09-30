# Mukadam Bot

Watches a mailbox for bank payment-advice/invoice emails, extracts the PDF's
data (amount, date, sender, receiver, reference number), asks for a Yes/No
confirmation on WhatsApp, and on "Yes" appends the row to that bank's Excel
workbook under `data/excel/{BANK_CODE}.xlsx`.

Full, non-hand-wavy workflow (real API constraints, sequence diagram, open
decisions that need your input) is in [`docs/WORKFLOW.md`](docs/WORKFLOW.md)
— read that before wiring up real credentials.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env`:
- IMAP credentials for the mailbox that receives the bank emails.
- WhatsApp Cloud API credentials (phone number id, access token, app secret,
  webhook verify token) — see docs/WORKFLOW.md for what has to be set up on
  the Meta side first (a business account, an approved message template).
- `NOTIFY_WHATSAPP_NUMBERS` — who gets the confirmation prompts.

Add at least one real `BankProfile` in [`app/bank_registry.py`](app/bank_registry.py)
before relying on this in production — it ships empty because sender
addresses, PDF password patterns, and PDF field layouts differ per bank and
cannot be guessed. See "Onboarding a new bank" in docs/WORKFLOW.md.

## Run

```bash
uvicorn app.main:app --reload
```

The mailbox poller starts automatically in the background
(`IMAP_POLL_INTERVAL_SECONDS`). To trigger a poll immediately instead of
waiting:

```bash
curl -X POST http://localhost:8000/invoices/poll-now
```

The WhatsApp webhook must be reachable from the internet (use `ngrok` in
development) and registered in the Meta App Dashboard pointing at
`/webhook/whatsapp`.

## Endpoints

- `GET /health` — liveness check.
- `GET /invoices` — list everything the pipeline has seen and its status.
- `POST /invoices/poll-now` — run one mailbox poll cycle immediately.
- `GET|POST /webhook/whatsapp` — Meta webhook verification (GET) and
  incoming button-reply delivery (POST).
