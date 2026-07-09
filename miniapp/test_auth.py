"""Unit tests for auth.validate_init_data — no network/Telegram client needed,
the initData strings are self-constructed with a known bot token."""
import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from auth import validate_init_data

BOT_TOKEN = "123456:test-token"


def _build_init_data(user: dict, auth_date: int | None = None, token: str = BOT_TOKEN) -> str:
    auth_date = auth_date if auth_date is not None else int(time.time())
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAA",
        "user": json.dumps(user, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    fields["hash"] = computed_hash
    return urlencode(fields)


class TestValidateInitData(unittest.TestCase):
    def test_valid_init_data_returns_user(self):
        user = {"id": 12345, "first_name": "Test", "username": "tester"}
        result = validate_init_data(_build_init_data(user), BOT_TOKEN)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 12345)

    def test_tampered_hash_rejected(self):
        init_data = _build_init_data({"id": 12345}) + "x"
        self.assertIsNone(validate_init_data(init_data, BOT_TOKEN))

    def test_wrong_bot_token_rejected(self):
        init_data = _build_init_data({"id": 12345})
        self.assertIsNone(validate_init_data(init_data, "different-token"))

    def test_expired_auth_date_rejected(self):
        old_date = int(time.time()) - 100000
        init_data = _build_init_data({"id": 12345}, auth_date=old_date)
        self.assertIsNone(validate_init_data(init_data, BOT_TOKEN, max_age_s=86400))

    def test_missing_hash_rejected(self):
        self.assertIsNone(validate_init_data("user=%7B%22id%22%3A1%7D", BOT_TOKEN))

    def test_missing_user_rejected(self):
        fields = {"auth_date": str(int(time.time()))}
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        self.assertIsNone(validate_init_data(urlencode(fields), BOT_TOKEN))

    def test_malformed_input_rejected(self):
        self.assertIsNone(validate_init_data("not a valid query string===", BOT_TOKEN))


if __name__ == "__main__":
    unittest.main()
