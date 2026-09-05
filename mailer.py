import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

PROVIDER_SMTP = {
    "gmail.com":      ("smtp.gmail.com", 587),
    "googlemail.com": ("smtp.gmail.com", 587),
    "outlook.com":    ("smtp.office365.com", 587),
    "hotmail.com":    ("smtp.office365.com", 587),
    "live.com":       ("smtp.office365.com", 587),
    "wp.pl":          ("smtp.wp.pl", 465),
    "o2.pl":          ("poczta.o2.pl", 465),
    "onet.pl":        ("smtp.poczta.onet.pl", 465),
    "interia.pl":     ("poczta.interia.pl", 465),
}


@dataclass
class Mailbox:
    address: str
    password: str
    smtp_host: str
    smtp_port: int
    display_name: str


def mailbox_for(profile: dict | None) -> Mailbox | None:
    profile = profile or {}
    address = (profile.get("mailbox_address") or os.getenv("SMTP_USER") or "").strip()
    password = profile.get("mailbox_password") or os.getenv("SMTP_PASSWORD") or ""
    if not address or not password:
        return None
    provider_domain = address.rsplit("@", 1)[-1].lower()
    default_host, default_port = PROVIDER_SMTP.get(provider_domain, ("", 587))
    return Mailbox(
        address=address,
        password=password,
        smtp_host=(profile.get("smtp_host") or os.getenv("SMTP_HOST") or default_host).strip(),
        smtp_port=_port(profile.get("smtp_port") or os.getenv("SMTP_PORT"), default_port),
        display_name=profile.get("name") or address,
    )


def _port(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def describe(mailbox: Mailbox | None) -> str:
    if mailbox is None:
        return "brak skrzynki"
    if not mailbox.smtp_host:
        return f"{mailbox.address}: brak hosta SMTP"
    return f"{mailbox.address}: gotowa do wysyłki"


def build_message(mailbox: Mailbox, to_address: str, subject: str, body: str,
                  in_reply_to: str = "", references: list[str] | None = None) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((mailbox.display_name, mailbox.address))
    message["To"] = to_address
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain=mailbox.address.rsplit("@", 1)[-1])
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = " ".join(references or [in_reply_to])
    message.set_content(body)
    return message


def send(mailbox: Mailbox, to_address: str, subject: str, body: str,
         in_reply_to: str = "", references: list[str] | None = None) -> str:
    if not mailbox.smtp_host:
        raise ValueError(f"Brak hosta SMTP dla skrzynki {mailbox.address}, uzupełnij go w profilu")
    message = build_message(mailbox, to_address, subject, body, in_reply_to, references)
    with _smtp_connection(mailbox) as smtp:
        smtp.login(mailbox.address, mailbox.password)
        smtp.send_message(message)
    return message["Message-ID"]


def _smtp_connection(mailbox: Mailbox):
    if mailbox.smtp_port == 465:
        return smtplib.SMTP_SSL(mailbox.smtp_host, 465, timeout=30)
    connection = smtplib.SMTP(mailbox.smtp_host, mailbox.smtp_port, timeout=30)
    connection.starttls()
    return connection


def test_connection(mailbox: Mailbox) -> dict:
    if not mailbox.smtp_host:
        return {"smtp": "błąd: brak hosta SMTP"}
    try:
        with _smtp_connection(mailbox) as smtp:
            smtp.login(mailbox.address, mailbox.password)
        return {"smtp": "ok"}
    except Exception as error:
        return {"smtp": f"błąd: {str(error)[:160]}"}
