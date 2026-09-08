import imaplib
import os
import re
import smtplib
import time
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

PROVIDER_IMAP = {
    "gmail.com":      "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com":    "outlook.office365.com",
    "hotmail.com":    "outlook.office365.com",
    "live.com":       "outlook.office365.com",
    "wp.pl":          "imap.wp.pl",
    "o2.pl":          "poczta.o2.pl",
    "onet.pl":        "imap.poczta.onet.pl",
    "interia.pl":     "poczta.interia.pl",
}

PROVIDERS_SAVING_SENT_COPY_THEMSELVES = {"gmail.com", "googlemail.com"}

IMAP_SSL_PORT = 993


@dataclass
class Mailbox:
    address: str
    password: str
    smtp_host: str
    smtp_port: int
    imap_host: str
    display_name: str

    @property
    def provider_domain(self) -> str:
        return self.address.rsplit("@", 1)[-1].lower()


def mailbox_for(profile: dict | None) -> Mailbox | None:
    profile = profile or {}
    address = (profile.get("mailbox_address") or os.getenv("SMTP_USER") or "").strip()
    password = profile.get("mailbox_password") or os.getenv("SMTP_PASSWORD") or ""
    if not address or not password:
        return None
    provider_domain = address.rsplit("@", 1)[-1].lower()
    default_host, default_port = PROVIDER_SMTP.get(provider_domain, ("", 587))
    smtp_host = (profile.get("smtp_host") or os.getenv("SMTP_HOST") or default_host).strip()
    configured_imap_host = (profile.get("imap_host") or os.getenv("IMAP_HOST") or "").strip()
    return Mailbox(
        address=address,
        password=password,
        smtp_host=smtp_host,
        smtp_port=_port(profile.get("smtp_port") or os.getenv("SMTP_PORT"), default_port),
        imap_host=configured_imap_host or _default_imap_host(provider_domain, smtp_host),
        display_name=profile.get("name") or address,
    )


def _default_imap_host(provider_domain: str, smtp_host: str) -> str:
    if provider_domain in PROVIDER_IMAP:
        return PROVIDER_IMAP[provider_domain]
    if smtp_host.startswith("smtp."):
        return "imap." + smtp_host[len("smtp."):]
    return ""


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
                  in_reply_to: str = "", references: list[str] | None = None,
                  attachments: list[tuple[str, bytes, str]] | None = None) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((mailbox.display_name, mailbox.address))
    message["To"] = to_address
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain=mailbox.address.rsplit("@", 1)[-1])
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = " ".join(references or [in_reply_to])
    message.set_content(body)
    for filename, content, mime_type in attachments or []:
        main_type, _, sub_type = mime_type.partition("/")
        message.add_attachment(content, maintype=main_type, subtype=sub_type or "octet-stream",
                               filename=filename)
    return message


@dataclass
class SendResult:
    message_id: str
    sent_copy_warning: str = ""


def send(mailbox: Mailbox, to_address: str, subject: str, body: str,
         in_reply_to: str = "", references: list[str] | None = None,
         attachments: list[tuple[str, bytes, str]] | None = None) -> SendResult:
    if not mailbox.smtp_host:
        raise ValueError(f"Brak hosta SMTP dla skrzynki {mailbox.address}, uzupełnij go w profilu")
    message = build_message(mailbox, to_address, subject, body, in_reply_to, references, attachments)
    with _smtp_connection(mailbox) as smtp:
        smtp.login(mailbox.address, mailbox.password)
        smtp.send_message(message)
    sent_copy_warning = save_copy_in_sent_folder(mailbox, message)
    return SendResult(message_id=message["Message-ID"], sent_copy_warning=sent_copy_warning)


def _smtp_connection(mailbox: Mailbox):
    if mailbox.smtp_port == 465:
        return smtplib.SMTP_SSL(mailbox.smtp_host, 465, timeout=30)
    connection = smtplib.SMTP(mailbox.smtp_host, mailbox.smtp_port, timeout=30)
    connection.starttls()
    return connection


def saves_sent_copy_itself(mailbox: Mailbox) -> bool:
    return mailbox.provider_domain in PROVIDERS_SAVING_SENT_COPY_THEMSELVES


def save_copy_in_sent_folder(mailbox: Mailbox, message: EmailMessage) -> str:
    if saves_sent_copy_itself(mailbox):
        return ""
    if not mailbox.imap_host:
        return "brak hosta IMAP w profilu, kopia nie trafiła do folderu Wysłane"
    try:
        with _imap_connection(mailbox) as imap:
            folder = _sent_folder_name(imap)
            quoted_folder = '"' + folder.replace('"', '\\"') + '"'
            status, response = imap.append(quoted_folder, "\\Seen", time.time(), message.as_bytes())
            if status != "OK":
                raise RuntimeError(f"serwer IMAP odrzucił zapis do {folder}: {response}")
        return ""
    except Exception as error:
        return f"kopia nie trafiła do folderu Wysłane: {str(error)[:160]}"


def _imap_connection(mailbox: Mailbox):
    imap = imaplib.IMAP4_SSL(mailbox.imap_host, IMAP_SSL_PORT, timeout=30)
    imap.login(mailbox.address, mailbox.password)
    return imap


LIST_LINE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?:"(?P<quoted>[^"]*)"|(?P<bare>\S+))\s*$')


def _sent_folder_name(imap: imaplib.IMAP4_SSL) -> str:
    status, folders = imap.list()
    if status != "OK":
        return "Sent"
    for raw_line in folders:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        parsed = LIST_LINE.match(line.strip())
        if not parsed:
            continue
        flags = parsed.group("flags").split()
        if "\\Sent" not in flags:
            continue
        return parsed.group("quoted") if parsed.group("quoted") is not None else parsed.group("bare")
    return "Sent"


def test_connection(mailbox: Mailbox) -> dict:
    return {"smtp": _test_smtp(mailbox), "imap": _test_imap(mailbox)}


def _test_smtp(mailbox: Mailbox) -> str:
    if not mailbox.smtp_host:
        return "błąd: brak hosta SMTP"
    try:
        with _smtp_connection(mailbox) as smtp:
            smtp.login(mailbox.address, mailbox.password)
        return "ok"
    except Exception as error:
        return f"błąd: {str(error)[:160]}"


def _test_imap(mailbox: Mailbox) -> str:
    if saves_sent_copy_itself(mailbox):
        return "ok, dostawca sam zapisuje kopie w Wysłane"
    if not mailbox.imap_host:
        return "błąd: brak hosta IMAP, kopie nie trafią do folderu Wysłane"
    try:
        with _imap_connection(mailbox) as imap:
            folder = _sent_folder_name(imap)
        return f"ok, folder {folder}"
    except Exception as error:
        return f"błąd: {str(error)[:160]}"
