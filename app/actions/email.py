"""SMTP send (Gmail app password). Only ever called after the user confirmed the draft."""
import smtplib
from email.message import EmailMessage

from app.config import settings


def send(to: str, subject: str, body: str, image: tuple[bytes, str] | None = None) -> str:
    """Returns the address actually used. EMAIL_DEMO_OVERRIDE (default on) delivers to the user's own inbox."""
    s = settings()
    real_to = to
    if s.email_demo_override:
        to = s.smtp_user
        subject = f"[DEMO — would go to {real_to}] {subject}"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.smtp_user, to, subject
    msg["Reply-To"] = s.smtp_user
    msg.set_content(body)
    if image:
        data, mime = image
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=f"product.{subtype}")
    last: Exception | None = None
    for _ in range(2):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
                smtp.login(s.smtp_user, s.smtp_app_password)
                smtp.send_message(msg)
            return to
        except (smtplib.SMTPException, OSError) as e:
            last = e
    raise RuntimeError(f"email send failed: {last}")
