from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Per-bank PDF passwords (BankProfile.pdf_password_env) aren't Settings
# fields — they're looked up by name via os.environ, which pydantic-settings'
# env_file does not populate. Load .env into the process environment too.
load_dotenv(".env")


class Settings(BaseSettings):
    # extra="ignore": .env also holds those per-bank password vars, which
    # would otherwise fail validation and crash startup.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Use IPv4 only for outgoing connections — for a network whose IPv6 route
    # breaks TLS handshakes (SSLEOFError talking to Google). See app/network.py.
    force_ipv4: bool = False

    # true = every email is checked by the LLM in full (subject, text, logos,
    # pictures). false = a cheap keyword check first skips emails that name no
    # bank and have no pictures (saves LLM calls on notifications/newsletters).
    scan_all_emails: bool = True
    # Our own company's names / email addresses (comma-separated). A bank
    # document should be addressed to or concern one of them — checked and
    # shown on every WhatsApp prompt (app/verification.py).
    our_company_names: str = ""

    @property
    def our_company_list(self) -> list[str]:
        return [n.strip() for n in self.our_company_names.split(",") if n.strip()]

    # Google OAuth (Gmail + Sheets share one token — see scripts/gmail_oauth_setup.py)
    google_client_secrets_file: str = "./credentials/client_secret.json"
    google_token_file: str = "./credentials/token.json"
    # Full contents of token.json, for hosts with no file upload (Railway):
    # written to google_token_file when that file doesn't exist yet.
    google_token_json: str = ""

    # Gmail API push (real-time ingestion)
    google_pubsub_topic: str = ""  # full form: projects/<project-id>/topics/<topic-name>
    # Pull subscription on that topic, the app listens on it for new mail —
    # no public URL needed. Full form projects/<project-id>/subscriptions/<name>,
    # or just <name> (the project is taken from GOOGLE_PUBSUB_TOPIC).
    google_pubsub_subscription: str = ""
    pubsub_verification_token: str = ""
    gmail_watch_label_ids: str = "INBOX"
    gmail_watch_renew_interval_seconds: int = 43200  # 12h — comfortably inside Gmail's ~7-day watch cap
    # Fallback polling interval, used only when the Pub/Sub listener can't run
    # (see app/worker/scheduler.py). 0 = never poll.
    gmail_reconcile_interval_seconds: int = 900

    # Google Sheets output
    google_sheets_spreadsheet_id: str = ""

    # LLM-first extraction (default parser for any registered bank that
    # doesn't yet have a hand-written rule-based parser — see bank_registry.py).
    # Routed through OpenRouter (OpenAI-compatible API) so llm_extraction_model
    # can point at any model OpenRouter serves, e.g. "anthropic/claude-sonnet-4.5".
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_extraction_model: str = "anthropic/claude-sonnet-4.5"

    # WhatsApp Cloud API
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_business_account_id: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_webhook_verify_token: str = ""
    whatsapp_template_name: str = "invoice_confirmation"
    whatsapp_template_lang: str = "en"

    # Which WhatsApp integration sends the confirmation prompt: "meta" (the
    # official Cloud API above) or "openwa" (self-hosted OpenWA gateway,
    # linked to a phone by QR — see docs/WORKFLOW.md).
    whatsapp_provider: str = "meta"

    # OpenWA (https://github.com/rmyndharis/OpenWA)
    openwa_url: str = "http://localhost:2785"
    openwa_api_key: str = ""
    openwa_session_id: str = ""
    openwa_webhook_secret: str = ""

    notify_whatsapp_numbers: str = ""

    # App/db
    database_url: str = "sqlite:///./data/mukadam_bot.db"
    pdf_storage_dir: str = "./data/pdfs"
    log_level: str = "INFO"

    @property
    def notify_numbers_list(self) -> list[str]:
        return [n.strip() for n in self.notify_whatsapp_numbers.split(",") if n.strip()]


settings = Settings()
