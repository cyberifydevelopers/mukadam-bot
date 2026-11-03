"""Public pages Google's OAuth consent screen asks for: a home page, a privacy policy and terms of service.
They are open to anybody (see PUBLIC_PREFIXES in app/routers/google_login.py)."""

from datetime import date
from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.routers.google_login import _shell

router = APIRouter(tags=["legal"])

CONTACT = "cyberifydevelopers@gmail.com"
UPDATED = date(2026, 11, 3).strftime("%d %B %Y")

_NAV = "<p class=foot><a href='/about'>About</a> · <a href='/privacy'>Privacy Policy</a> · <a href='/terms'>Terms of Service</a> · <a href='/login'>Sign in</a></p>"


def _doc(title: str, intro: str, sections: list[tuple[str, str]]) -> HTMLResponse:
    body = "".join(f"<h2>{escape(heading)}</h2>{html}" for heading, html in sections)
    return _shell(title, f"<h1>{escape(title)}</h1><p>{intro}</p>{body}{_NAV}", wide=True)


@router.get("/about", include_in_schema=False)
def about():
    return _doc(
        "Muccaddam Bot",
        "Muccaddam Bot reads the bank emails that arrive in your Gmail, picks out the bank documents (delivery orders, "
        "invoices, letters of credit), and sends their details to your WhatsApp so you can approve or reject them with a "
        "YES / NO. Approved documents are saved to your Google Sheet and a stock report is prepared for you.",
        [
            ("What it does", "<ul><li>Watches your inbox for new mail and reads attachments and email text.</li>"
                             "<li>Finds bank documents and extracts the reference, date, amount, parties and items.</li>"
                             "<li>Asks you on WhatsApp for approval and keeps a dashboard of every email it has seen.</li></ul>"),
            ("Your Gmail", "<p>The app asks for <b>read-only</b> access to Gmail (the <code>gmail.readonly</code> scope). "
                           "It never sends, deletes or changes your email. Each person who signs in sees only their own dashboard.</p>"),
            ("Get started", "<p><a class='btn primary' href='/login'>Sign in with Google</a></p>"),
            ("Contact", f"<p>Questions? Write to <a href='mailto:{CONTACT}'>{CONTACT}</a>.</p>"),
        ],
    )


@router.get("/privacy", include_in_schema=False)
def privacy():
    return _doc(
        "Privacy Policy",
        f"Last updated {UPDATED}. This policy explains what Muccaddam Bot (&ldquo;the app&rdquo;) does with your data.",
        [
            ("What we access", "<p>When you choose &ldquo;Continue with Google&rdquo; the app asks for one permission: read-only access to your "
                               "Gmail (<code>https://www.googleapis.com/auth/gmail.readonly</code>). We also receive your Gmail address, "
                               "which identifies your account.</p>"),
            ("Why we need it", "<p>Reading your mail is the whole purpose of the app: it finds the bank documents in your inbox and sends their details to "
                               "you for approval. We use your Gmail data only to provide this feature to you.</p>"),
            ("What we store", "<ul><li>Your Gmail address and the Google login token that lets the app keep reading your new mail.</li>"
                              "<li>For each email we process: sender, subject, date, attachment names, the attachments and text of mail that "
                              "looks like a bank document, and the details extracted from them.</li>"
                              "<li>Your approvals and your settings (such as the WhatsApp number to notify).</li></ul>"
                              "<p>Your data is kept apart from every other user&rsquo;s.</p>"),
            ("Who it is shared with", "<p>We do not sell your data and we do not use it for advertising. Data is passed only to the services needed to "
                                      "run the app: an AI service that reads a document and extracts its fields, and WhatsApp (through our messaging "
                                      "gateway) to send you the approval message. These providers receive only what is needed for that step.</p>"),
            ("Google API Services User Data Policy", "<p>The use and transfer of information received from Google APIs to any other app will adhere to the "
                                                     "<a href='https://developers.google.com/terms/api-services-user-data-policy#additional_requirements_for_specific_api_scopes'>"
                                                     "Google API Services User Data Policy</a>, including the Limited Use requirements. We do not use Gmail data to "
                                                     "train generalised AI or machine-learning models, we do not let people read your mail except when you ask for "
                                                     "support or the law requires it, and we do not transfer it to anyone else except as described above.</p>"),
            ("Security", "<p>Connections use HTTPS, sessions use signed cookies, and access to the stored data is limited to the app and its operators.</p>"),
            ("Retention and deletion", "<p>We keep your data while your account is active. You can stop access at any time at "
                                       "<a href='https://myaccount.google.com/permissions'>myaccount.google.com/permissions</a> by removing the app. "
                                       f"To have your stored data deleted, email <a href='mailto:{CONTACT}'>{CONTACT}</a> and we will remove it.</p>"),
            ("Contact", f"<p><a href='mailto:{CONTACT}'>{CONTACT}</a></p>"),
        ],
    )


@router.get("/terms", include_in_schema=False)
def terms():
    return _doc(
        "Terms of Service",
        f"Last updated {UPDATED}. By signing in you agree to these terms.",
        [
            ("The service", "<p>Muccaddam Bot reads bank documents in the Gmail account you connect and sends their details to you for approval. "
                            "It is provided as is, and we may change or stop it at any time.</p>"),
            ("Your account", "<p>Connect only a mailbox you are allowed to use. You are responsible for what you approve: the app extracts "
                             "details automatically and may make mistakes, so check every document before you reply YES.</p>"),
            ("Acceptable use", "<p>Do not misuse the service, attempt to access another person&rsquo;s data, or interfere with its operation.</p>"),
            ("Your data", "<p>How your data is handled is described in the <a href='/privacy'>Privacy Policy</a>. You can disconnect the app at any time "
                          "from your Google account permissions.</p>"),
            ("Liability", "<p>To the extent the law allows, we are not liable for losses that result from using the app, including errors in extracted "
                          "figures or a delayed or missed message.</p>"),
            ("Contact", f"<p><a href='mailto:{CONTACT}'>{CONTACT}</a></p>"),
        ],
    )
