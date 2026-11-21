"""READ-ONLY Gmail inbox access (IMAP, same app password). Only the owner's own inbox, only when a task asks.
Nothing is deleted, moved or marked read (mailbox selected readonly)."""
import email
import imaplib
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from app.config import settings


def _h(v: str | None) -> str:
    return str(make_header(decode_header(v))) if v else ""


def find_latest_pdf(from_hint: str | None = None, subject_hint: str | None = None, days: int = 7,
                    max_scan: int = 60) -> dict[str, Any] | None:
    """Newest mail (last `days`) with a PDF attachment, optionally matching sender/subject hints.
    Returns {from, subject, date, filename, pdf: bytes} or None."""
    s = settings()
    m = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    try:
        m.login(s.smtp_user, s.smtp_app_password)
        m.select("INBOX", readonly=True)
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        crit = ["SINCE", since]
        if from_hint and "@" in from_hint:
            crit += ["FROM", from_hint]
        typ, data = m.search(None, *crit)
        ids = (data[0].split() if data and data[0] else [])[::-1][:max_scan]
        fh = (from_hint or "").lower().strip()
        sh = (subject_hint or "").lower().strip()
        for i in ids:
            typ, msg_data = m.fetch(i, "(BODY.PEEK[])")  # PEEK: does not mark as read
            msg = email.message_from_bytes(msg_data[0][1])
            frm, subj = _h(msg.get("From")), _h(msg.get("Subject"))
            if fh and "@" not in fh and fh not in frm.lower():
                continue
            if sh and sh not in subj.lower():
                continue
            for part in msg.walk():
                fname = _h(part.get_filename())
                if part.get_content_type() == "application/pdf" or fname.lower().endswith(".pdf"):
                    try:
                        from datetime import timezone
                        ist = timezone(timedelta(hours=5, minutes=30))
                        date = parsedate_to_datetime(msg.get("Date")).astimezone(ist).strftime("%d %b %Y, %I:%M %p IST")
                    except Exception:  # noqa: BLE001
                        date = msg.get("Date")
                    return {"from": frm, "from_email": parseaddr(frm)[1], "subject": subj, "date": date,
                            "filename": fname or "attachment.pdf", "pdf": part.get_payload(decode=True)}
        return None
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass
