"""SMTP presets of common mail providers for Platform Settings → Mail. Choosing one fills in
host, port and encryption; the account's own address and (app) password are still needed."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MailProvider:
    key: str
    label: str
    host: str
    port: int
    security: str  # "starttls", "ssl" or "none"
    hint: str


MAIL_PROVIDERS: tuple[MailProvider, ...] = (
    MailProvider(
        "gmail",
        "Gmail / Google Workspace",
        "smtp.gmail.com",
        587,
        "starttls",
        "Username: the full Gmail address. Password: an app password (Google account → "
        "Security → 2-Step Verification → App passwords), not the normal password.",
    ),
    MailProvider(
        "outlook",
        "Outlook.com / Microsoft 365",
        "smtp-mail.outlook.com",
        587,
        "starttls",
        "Microsoft is phasing out password logins for SMTP; it only works where SMTP AUTH "
        "is still enabled for the account (Microsoft 365: smtp.office365.com).",
    ),
    MailProvider(
        "icloud",
        "iCloud Mail",
        "smtp.mail.me.com",
        587,
        "starttls",
        "Username: the iCloud address. Password: an app-specific password (account.apple.com).",
    ),
    MailProvider(
        "yahoo",
        "Yahoo Mail",
        "smtp.mail.yahoo.com",
        465,
        "ssl",
        "Password: an app password (Yahoo account security settings).",
    ),
    MailProvider(
        "infomaniak",
        "Infomaniak",
        "mail.infomaniak.com",
        587,
        "starttls",
        "Username: the full address; password of the mailbox.",
    ),
    MailProvider(
        "gmx",
        "GMX",
        "mail.gmx.net",
        587,
        "starttls",
        "Enable sending via SMTP in the GMX settings (POP3/IMAP and SMTP) first.",
    ),
    MailProvider(
        "bluewin",
        "Swisscom (bluewin)",
        "smtpauths.bluewin.ch",
        465,
        "ssl",
        "Username: the full bluewin address.",
    ),
    MailProvider(
        "proton",
        "Proton Mail",
        "smtp.protonmail.ch",
        587,
        "starttls",
        "Paid plans with a custom domain only; password: an SMTP token from Proton's settings.",
    ),
    MailProvider(
        "mailpit",
        "Mailpit (test catcher)",
        "mailpit",
        1025,
        "none",
        "Catches every mail instead of delivering it; web view on port 8025. For development.",
    ),
)

PROVIDERS_BY_KEY = {provider.key: provider for provider in MAIL_PROVIDERS}
