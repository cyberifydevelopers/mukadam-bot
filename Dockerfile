FROM python:3.12-slim-bookworm

# LibreOffice (+ its python3-uno bridge) fills the stock reports; see app/services/stock_report.py
# The approved samples are set in Cambria / Calibri: Caladea / Carlito take exactly their widths. Without them the server
# falls back to DejaVu, which is much wider, and text no longer fits its cells (cut-off descriptions, dates, sentences).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-calc python3-uno fonts-liberation fonts-dejavu-core fonts-crosextra-caladea fonts-crosextra-carlito \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
