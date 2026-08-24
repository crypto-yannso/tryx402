"""Anonymous authentication for tryx402 SDK.

No email, no account, no API key. The SDK generates a persistent customer_id
locally and the server creates the wallet on first contact.

The only human action is the Stripe card payment.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

__all__ = ["get_or_create_customer_id", "CUSTOMER_ID_FILE"]


CUSTOMER_ID_FILE = Path.home() / ".tryx402_customer_id"


def get_or_create_customer_id() -> str:
    """Get or create a persistent customer ID for this user.

    The ID is stored locally in ~/.tryx402_customer_id and reused across
    all SDK sessions. It is sent as X-Customer-ID header to the server.

    Returns:
        UUID string identifying this customer
    """
    if CUSTOMER_ID_FILE.exists():
        try:
            data = json.loads(CUSTOMER_ID_FILE.read_text())
            cid = data.get("customer_id", "")
            if _is_valid_uuid(cid):
                return cid
        except (json.JSONDecodeError, OSError):
            pass

    # Create new customer ID
    cid = str(uuid.uuid4())
    try:
        CUSTOMER_ID_FILE.write_text(json.dumps({"customer_id": cid}))
        # Set permissions to user-only read/write
        CUSTOMER_ID_FILE.chmod(0o600)
    except OSError:
        # If we can't write, return the ID anyway (it will be regenerated each session)
        pass

    return cid


def _is_valid_uuid(s: str) -> bool:
    """Check if string is a valid UUID."""
    try:
        uuid.UUID(s)
        return True
    except ValueError:
        return False
