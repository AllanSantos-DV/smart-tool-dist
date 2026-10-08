"""Single source of the Smart Tool version and of the User-Agent sent to public APIs."""
import re

VERSION = "0.9.5"
_CONTACT_RE = re.compile(r"^(?:[^\s@()<>;]+@[^\s@()<>;]+\.[^\s@()<>;]+|https?://[^\s()<>;]+)$")


def check_contact(value):
    """Optional contact (e-mail or http(s) URL) that public APIs such as Wikimedia ask for in the User-Agent."""
    value = str(value or "").strip()
    if value and (len(value) > 200 or not _CONTACT_RE.match(value)):
        raise ValueError("Contact must be an e-mail address or an http(s) URL, without spaces or parentheses.")
    return value


def user_agent(contact=""):
    contact = check_contact(contact)
    return f"SmartTool/{VERSION} (local MCP search tool{'; ' + contact if contact else ''})"
