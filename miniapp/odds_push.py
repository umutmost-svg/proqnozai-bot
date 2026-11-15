"""Manual signed odds push for local and stand checks — NOT part of the image, pytest or CI.

    MINIAPP_ODDS_FEED_SECRET=... miniapp/.venv/bin/python -m miniapp.odds_push <url> <lines.json>

<lines.json>: a JSON list of feed lines in the wire format of miniapp/odds/feed.py
({"id", "country", "league", "home", "away", "kickoff", "live", "fetchedAt", "odds"}).
`fetchedAt` may be "now" (replaced by the current time). Signs exactly as the bot's
mostbet_feed.py does and prints only the HTTP status and the answer's counts.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

from miniapp.odds.feed import sign


def main(url: str, path: str) -> int:
    secret = os.environ.get("MINIAPP_ODDS_FEED_SECRET", "").strip()
    if len(secret) < 32:
        print("MINIAPP_ODDS_FEED_SECRET (>= 32 chars) is not set: nothing sent.")
        return 2
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(path) as f:
        lines = json.load(f)
    for line in lines:
        if line.get("fetchedAt") == "now":
            line["fetchedAt"] = now
    body = json.dumps({"v": 1, "nonce": secrets.token_hex(16), "lines": lines}).encode()
    stamp = str(int(time.time()))
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Odds-Timestamp": stamp,
        "X-Odds-Signature": sign(secret.encode(), stamp, body)})
    try:
        with urllib.request.urlopen(request, timeout=10) as r:
            print(r.status, r.read().decode()[:300])
    except urllib.error.HTTPError as e:
        print(e.code, e.read().decode()[:300])
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
