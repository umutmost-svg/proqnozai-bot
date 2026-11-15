from __future__ import annotations

import pytest

from miniapp.telegram_auth import InitDataError, validate_init_data
from miniapp.tests.signing import TEST_BOT_TOKEN, encode, make_init_data, sign, user_json

NOW = 1_800_000_000
MAX_AGE = 300
SKEW = 30


def check(init_data: str, *, now: float = NOW, token: str = TEST_BOT_TOKEN):
    return validate_init_data(init_data, token, now=now, max_age_s=MAX_AGE, future_skew_s=SKEW)


def rejected(init_data: str, code: str, **kw) -> None:
    with pytest.raises(InitDataError) as exc:
        check(init_data, **kw)
    assert exc.value.code == code


# Reference vector: data-check-string signed with `openssl dgst -sha256 -mac HMAC`
# (secret = HMAC_SHA256(key="WebAppData", msg=token)), not with any Python helper.
REFERENCE_USER = (
    '{"id":123456789,"first_name":"Ülkər + Co","last_name":"Məmmədova",'
    '"username":"ulkar_m","language_code":"az","allows_write_to_pm":true}'
)
REFERENCE_HASH = "1c94d94c4d933954ba8ac0efdd22796821ed2e976521c81cf0083fb3fd6ec75a"
REFERENCE_FIELDS = {
    "query_id": "AAHtestQuery",
    "user": REFERENCE_USER,
    "auth_date": "1700000000",
    "signature": "dGVzdC1zaWduYXR1cmUtbm90LXZlcmlmaWVk",
    "hash": REFERENCE_HASH,
}


@pytest.mark.parametrize("plus_for_space", [False, True])
def test_reference_vector_from_openssl(plus_for_space: bool) -> None:
    init_data = encode(REFERENCE_FIELDS, plus_for_space=plus_for_space)
    result = check(init_data, now=1_700_000_100)
    assert result.user.id == 123456789
    assert result.user.first_name == "Ülkər + Co"  # Unicode, literal plus and space survive decoding
    assert result.user.language_code == "az"
    assert result.legacy_id == REFERENCE_HASH


def test_test_signer_agrees_with_reference() -> None:
    fields = {k: v for k, v in REFERENCE_FIELDS.items() if k != "hash"}
    assert sign(fields) == REFERENCE_HASH


def test_signature_field_is_part_of_hmac_check() -> None:
    # Dropping `signature` from a payload that was signed with it breaks the HMAC.
    fields = {k: v for k, v in REFERENCE_FIELDS.items() if k != "signature"}
    rejected(encode(fields), "bad_signature", now=1_700_000_100)


def test_valid_payload() -> None:
    result = check(make_init_data({"id": 42, "first_name": "A", "username": "a"}, auth_date=NOW))
    assert result.user.id == 42 and result.user.username == "a" and result.auth_date == NOW


def test_large_telegram_id_accepted() -> None:
    assert check(make_init_data({"id": 2**52 + 7}, auth_date=NOW)).user.id == 2**52 + 7


def test_tampered_user_rejected() -> None:
    good = make_init_data({"id": 42}, auth_date=NOW)
    forged = good.replace("%3A42", "%3A43")
    assert forged != good
    rejected(forged, "bad_signature")


def test_tampered_auth_date_rejected() -> None:
    good = make_init_data(None, auth_date=NOW)
    rejected(good.replace(f"auth_date={NOW}", f"auth_date={NOW + 1}"), "bad_signature")


def test_tampered_hash_rejected() -> None:
    good = make_init_data(None, auth_date=NOW)
    head, _, digest = good.rpartition("hash=")
    flipped = ("0" if digest[0] != "0" else "1") + digest[1:]
    rejected(head + "hash=" + flipped, "bad_signature")


def test_other_bot_token_rejected() -> None:
    rejected(make_init_data(None, auth_date=NOW, token="7000000002:other-bot"), "bad_signature")


@pytest.mark.parametrize("digest", ["", "abc", "Z" * 64, "A" * 64, "0" * 63, "0" * 65])
def test_bad_hash_format(digest: str) -> None:
    rejected(f"auth_date={NOW}&user=%7B%22id%22%3A1%7D&hash={digest}", "bad_hash_format")


def test_missing_hash() -> None:
    rejected(f"auth_date={NOW}&user=%7B%22id%22%3A1%7D", "bad_hash_format")


@pytest.mark.parametrize(
    ("offset", "ok"),
    [(0, True), (-MAX_AGE, True), (-MAX_AGE - 1, False), (SKEW, True), (SKEW + 1, False)],
)
def test_auth_date_boundaries(offset: int, ok: bool) -> None:
    init_data = make_init_data(None, auth_date=NOW + offset)
    if ok:
        check(init_data)
    else:
        rejected(init_data, "expired" if offset < 0 else "from_future")


def test_expired_error_is_flagged_as_expired() -> None:
    with pytest.raises(InitDataError) as exc:
        check(make_init_data(None, auth_date=NOW - 10_000))
    assert exc.value.expired


def _signed(fields: dict[str, str]) -> str:
    return encode({**fields, "hash": sign(fields)})


@pytest.mark.parametrize("auth_date", ["", "-5", "1.5", "abc", "1e9", " 1", "9" * 13])
def test_bad_auth_date(auth_date: str) -> None:
    rejected(_signed({"auth_date": auth_date, "user": user_json({"id": 1})}), "bad_auth_date")


def test_missing_auth_date() -> None:
    rejected(_signed({"user": user_json({"id": 1})}), "missing_auth_date")


def test_missing_user() -> None:
    rejected(_signed({"auth_date": str(NOW)}), "missing_user")


@pytest.mark.parametrize(
    "user",
    [
        '{"id":true}',
        '{"id":1.0}',
        '{"id":12.5}',
        '{"id":"123"}',
        '{"id":-1}',
        '{"id":0}',
        f'{{"id":{2**53}}}',
        '{"first_name":"no id"}',
        '{"id":1,"id":2}',
        '{"id":NaN}',
        '{"id":1,"first_name":5}',
        '{"id":1,"is_premium":"yes"}',
        '{"id":1,"is_bot":true}',
        "[1]",
        "not json",
        '{"id":1',
    ],
)
def test_bad_user(user: str) -> None:
    # Correctly signed, so the rejection is about structure, not the signature.
    rejected(_signed({"auth_date": str(NOW), "user": user}), "bad_user")


def test_user_json_is_not_reserialized() -> None:
    # Whitespace and key order differ from json.dumps output; the signature is
    # over the bytes as received.
    raw = '{ "first_name" : "Ann",  "id" : 77 }'
    assert check(_signed({"auth_date": str(NOW), "user": raw})).user.id == 77


def test_duplicate_field_rejected() -> None:
    fields = {"auth_date": str(NOW), "user": user_json({"id": 1})}
    good = _signed(fields)
    rejected(good + f"&auth_date={NOW}", "duplicate_field")


def test_duplicate_after_url_decoding_rejected() -> None:
    fields = {"auth_date": str(NOW), "user": user_json({"id": 1})}
    rejected(_signed(fields) + "&auth%5Fdate=1", "duplicate_field")


@pytest.mark.parametrize(
    "init_data",
    [
        "no-equals-sign",
        "a=1&&b=2",
        "user=%7B%22id%22%3A1%7D&auth_date=1&hash=%zz",
        "user=%E0%A4%A&hash=" + "0" * 64,
        "user=%C3%28&hash=" + "0" * 64,  # invalid UTF-8
        "bad-key=1&hash=" + "0" * 64,
        "=1&hash=" + "0" * 64,
        "user=a%0Ab&hash=" + "0" * 64,  # newline would break the data-check-string
    ],
)
def test_malformed_encoding(init_data: str) -> None:
    rejected(init_data, "malformed")


def test_empty_and_oversized() -> None:
    rejected("", "empty")
    rejected("a=" + "x" * 5000, "too_large")


def test_error_message_does_not_leak_payload() -> None:
    init_data = make_init_data({"id": 42, "first_name": "Secret Name"}, auth_date=NOW - 10_000)
    with pytest.raises(InitDataError) as exc:
        check(init_data)
    assert str(exc.value) == "expired"
    assert "Secret" not in repr(exc.value)


# --- Ed25519: no bot token in the validator ------------------------------------------------------------

from miniapp.telegram_auth import ed25519_shadow_status  # noqa: E402
from miniapp.tests.signing import (  # noqa: E402
    TEST_BOT_ID,
    TEST_ED25519_PUBLIC_HEX,
    ed25519_signature,
    make_init_data_ed25519,
)
from urllib.parse import parse_qsl, urlencode  # noqa: E402


def ed(init_data: str, *, now: float = NOW, bot_id: int = TEST_BOT_ID, key: str = TEST_ED25519_PUBLIC_HEX):
    return validate_init_data(init_data, now=now, max_age_s=300, future_skew_s=30, ed25519=(bot_id, key))


def ed_rejected(init_data: str, code: str, **kw) -> None:
    with pytest.raises(InitDataError) as e:
        ed(init_data, **kw)
    assert e.value.code == code


def _fields(init_data: str) -> dict[str, str]:
    return dict(parse_qsl(init_data, keep_blank_values=True))


def test_ed25519_valid_without_any_bot_token() -> None:
    result = ed(make_init_data_ed25519({"id": 42, "first_name": "Ann"}, auth_date=int(NOW)))
    assert result.user.id == 42 and len(result.launch_id) == 64


def test_ed25519_ignores_the_unsigned_hash_for_the_launch_id() -> None:
    # `hash` is outside the Ed25519 signature: varying it must neither break nor change the launch id,
    # otherwise a replay could dodge the logout burn by editing it.
    base = make_init_data_ed25519(auth_date=int(NOW))
    other = _fields(base) | {"hash": "0" * 64}
    without = {k: v for k, v in _fields(base).items() if k != "hash"}
    ids = {ed(urlencode(f)).launch_id for f in (_fields(base), other, without)}
    assert len(ids) == 1


@pytest.mark.parametrize("field,value", [("user", '{"id":43,"first_name":"Ann"}'), ("auth_date", str(int(NOW) - 1)),
                                         ("query_id", "AAHother")])
def test_ed25519_tampered_field_rejected(field: str, value: str) -> None:
    f = _fields(make_init_data_ed25519(auth_date=int(NOW)))
    f[field] = value
    ed_rejected(urlencode(f), "bad_signature")


def test_ed25519_other_bot_or_other_key_rejected() -> None:
    data = make_init_data_ed25519(auth_date=int(NOW))
    ed_rejected(data, "bad_signature", bot_id=TEST_BOT_ID + 1)
    from miniapp.telegram_auth import TELEGRAM_PUBLIC_KEYS
    ed_rejected(data, "bad_signature", key=TELEGRAM_PUBLIC_KEYS["production"])


def test_ed25519_missing_signature_rejected() -> None:
    f = {k: v for k, v in _fields(make_init_data_ed25519(auth_date=int(NOW))).items() if k != "signature"}
    ed_rejected(urlencode(f), "missing_signature")


@pytest.mark.parametrize("mutate", [
    lambda s: s[:-1],                      # too short
    lambda s: s + "A",                     # too long
    lambda s: s.replace(s[0], "+", 1),     # standard base64 alphabet, not base64url
    lambda s: s + "=",                     # odd padding
    lambda s: s[:-1] + ("B" if s[-1] != "B" else "C"),  # non-canonical trailing bits or another signature
])
def test_ed25519_signature_format(mutate) -> None:
    f = _fields(make_init_data_ed25519(auth_date=int(NOW)))
    f["signature"] = mutate(f["signature"])
    with pytest.raises(InitDataError) as e:
        ed(urlencode(f))
    assert e.value.code in ("bad_signature_format", "bad_signature")


def test_ed25519_padded_signature_accepted() -> None:
    f = _fields(make_init_data_ed25519(auth_date=int(NOW)))
    f["signature"] += "=="
    assert ed(urlencode(f)).user.id == 123456789


def test_ed25519_freshness_and_user_rules_still_apply() -> None:
    ed_rejected(make_init_data_ed25519(auth_date=int(NOW) - 301), "expired")
    ed_rejected(make_init_data_ed25519({"id": 1, "is_bot": True}, auth_date=int(NOW)), "bad_user")


def test_exactly_one_algorithm_is_configured() -> None:
    data = make_init_data_ed25519(auth_date=int(NOW))
    with pytest.raises(ValueError):
        validate_init_data(data, now=NOW, max_age_s=300, future_skew_s=30)
    with pytest.raises(ValueError):
        validate_init_data(data, TEST_BOT_TOKEN, now=NOW, max_age_s=300, future_skew_s=30,
                           ed25519=(TEST_BOT_ID, TEST_ED25519_PUBLIC_HEX))


def test_hmac_still_accepts_the_same_launch_and_shadow_reports_it() -> None:
    data = make_init_data_ed25519(auth_date=int(NOW))
    assert check(data).user.id == 123456789  # the hash covers the signature field too
    assert ed25519_shadow_status(data, TEST_BOT_ID, TEST_ED25519_PUBLIC_HEX) == "ok"
    assert ed25519_shadow_status(make_init_data(auth_date=int(NOW)), TEST_BOT_ID, TEST_ED25519_PUBLIC_HEX) == "missing"
    assert ed25519_shadow_status(data, TEST_BOT_ID + 1, TEST_ED25519_PUBLIC_HEX) == "bad"
    assert ed25519_shadow_status("%%%", TEST_BOT_ID, TEST_ED25519_PUBLIC_HEX) == "bad"


def test_signer_matches_documented_check_string() -> None:
    from miniapp.telegram_auth import ed25519_check_string
    f = {"auth_date": "1", "query_id": "q", "user": "{}", "hash": "h", "signature": "s"}
    assert ed25519_check_string(f, 12345678) == "12345678:WebAppData\nauth_date=1\nquery_id=q\nuser={}"
    assert ed25519_signature(f)  # the test signer accepts the same fields


def test_launch_id_is_the_same_in_both_modes() -> None:
    data = make_init_data_ed25519(auth_date=int(NOW))
    hmac_result, ed_result = check(data), ed(data)
    assert hmac_result.launch_id == ed_result.launch_id and ed_result.legacy_id is None
    assert hmac_result.legacy_id == _fields(data)["hash"]
