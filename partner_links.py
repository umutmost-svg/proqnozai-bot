"""Signing for partner click links.

The redirect that counts partner clicks is necessarily public — it is a link in
a Telegram message. Without a signature anyone could hit
`/r/<partner>?u=<id>` with an id of their choosing: inventing clicks, or
attributing them to another user. The bot signs each link with the secret it
already shares with the dashboard, and the dashboard records a click only when
the signature checks out.

This does not make click counts unforgeable by the person the link belongs to —
they can replay their own link — but it stops attribution being fabricated for
anyone else, which is what makes the per-partner numbers trustworthy.

Both processes import this module so the two sides can never drift apart.
"""
import hashlib
import hmac

# 16 hex chars = 64 bits. Long enough that guessing is hopeless, short enough to
# keep the URL readable in a Telegram button.
_SIG_LEN = 16


def sign_click(secret: str, partner: str, uid) -> str:
    """Signature binding a user id to one partner. Empty when no secret is
    configured — the caller then omits attribution rather than sending
    something unverifiable."""
    if not secret:
        return ""
    msg = f"{partner}:{uid}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:_SIG_LEN]


def verify_click(secret: str, partner: str, uid, signature: str) -> bool:
    """Whether `signature` was produced by sign_click for this (partner, uid)."""
    if not secret or not signature or uid in (None, ""):
        return False
    return hmac.compare_digest(sign_click(secret, partner, uid), signature)
