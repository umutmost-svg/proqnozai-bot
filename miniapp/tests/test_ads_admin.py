# ruff: noqa: F811 — pytest fixtures imported from test_admin_web are used as parameters
"""Ads module, stage 1: data model in admin.db, roles, journal, publication of ads-policy.json."""
from __future__ import annotations

import json

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from miniapp.ads import policy, targeting
from miniapp.admin_web.ads import load_signing_key
from miniapp.admin_web.demo import DEMO_ID_BASE
from miniapp.tests.test_admin_web import digest_of, fresh, code, env, login, post  # noqa: F401 — env is a fixture

U1 = DEMO_ID_BASE + 1000
U2 = DEMO_ID_BASE + 1037
DAY = 86400


def partner_body(**extra) -> dict:
    return {"name": "Partner MB", "hosts": ["partner.example.com", "go.partner.example.com"],
            "countries": ["AZ", "TR", "UZ"], "allowUnknownCountry": False, "ageMin": 18,
            "disclosure": {"advertiser": "Partner LLC", "ageLabel": "18+"}, **extra}


def campaign_body(partner_id: int, now: int, **extra) -> dict:
    return {"name": "Осенний бонус", "tier": "general", "priority": 10, "langIn": ["ru", "az"],
            "placements": ["quick_pick"], "countries": [], "allowUnknownCountry": False,
            "startsAt": now - DAY, "endsAt": now + 30 * DAY, "partnerId": partner_id,
            "disclosure": {"adId": "ERID-1"}, "condition": None, "controlPct": 0,
            "variants": [{"key": "A", "weight": 100, "headline": "Бонус 100%", "body": "Текст",
                          "extra": "", "cta": "Забрать", "url": "https://partner.example.com/promo?x=1"}],
            **extra}


def pub(config):
    return load_signing_key(config.admin_db.parent / "ads-signing.key").public_key()


def dangerous(secrets_, clock, email, key="publish-key-1", **extra) -> dict:
    return {"reason": "публикация", "idempotencyKey": key, "code": code(secrets_[email], clock), **extra}


@pytest.fixture()
def ads_env(env):
    config, clock, client, accounts, secrets_ = env
    csrf_owner = login(client, clock, secrets_, "owner@x.test")
    pid = post(client, "/api/admin/ads/partners", csrf_owner, partner_body()).json()["id"]
    return config, clock, client, accounts, secrets_, csrf_owner, pid


def test_roles_partners_owner_only_campaigns_admin_analyst_reads(ads_env):
    config, clock, client, _, secrets_, _, pid = ads_env
    client.cookies.clear()
    csrf = login(client, clock, secrets_, "admin@x.test")
    assert post(client, "/api/admin/ads/partners", csrf, partner_body(name="Other")).status_code == 403
    assert post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).status_code == 200
    client.cookies.clear()
    csrf = login(client, clock, secrets_, "analyst@x.test")
    assert post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).status_code == 403
    assert post(client, "/api/admin/ads/publish", csrf, dangerous(secrets_, clock, "analyst@x.test")).status_code == 403
    assert client.get("/api/admin/ads/campaigns").status_code == 200


@pytest.mark.parametrize("url,error", [
    ("http://partner.example.com/x", "invalid_url"),
    ("https://evil.example.org/x", "url_host_not_allowed"),
    ("https://user@partner.example.com/x", "invalid_url"),
    ("https://partner.example.com:8443/x", "invalid_url"),
    ("https://PARTNER.example.com/x", "invalid_url"),
    ("https://partner.example.com/x y", "invalid_url"),
    ("javascript:alert(1)", "invalid_url"),
])
def test_links_only_https_on_partner_hosts(ads_env, url, error):
    _, clock, client, _, _, csrf, pid = ads_env
    body = campaign_body(pid, int(clock()))
    body["variants"][0]["url"] = url
    assert post(client, "/api/admin/ads/campaigns", csrf, body).json()["error"] == error


def test_partner_and_variant_validation(ads_env):
    _, clock, client, _, _, csrf, pid = ads_env
    assert post(client, "/api/admin/ads/partners", csrf, partner_body(name="X", hosts=["bad host"])).json()["error"] == "invalid_host"
    assert post(client, "/api/admin/ads/partners", csrf, partner_body()).json()["error"] == "name_taken"
    body = campaign_body(pid, int(clock()))
    body["variants"] = [{**body["variants"][0], "key": "A", "weight": 60}, {**body["variants"][0], "key": "B", "weight": 30}]
    assert post(client, "/api/admin/ads/campaigns", csrf, body).json()["error"] == "invalid_weights"
    body = campaign_body(pid, int(clock()), endsAt=int(clock()) - DAY)
    assert post(client, "/api/admin/ads/campaigns", csrf, body).json()["error"] == "invalid_window"
    body = campaign_body(pid, int(clock()))
    body["variants"][0]["headline"] = "x" * 81
    assert post(client, "/api/admin/ads/campaigns", csrf, body).json()["error"] == "invalid_headline"
    assert post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()), langIn=["fr"])).json()["error"] == "invalid_langs"


def test_condition_tree_limits():
    leaf = {"geo_in": ["AZ"]}
    deep = {"all": [{"any": [{"not": {"all": [leaf]}}]}]}  # depth 5
    with pytest.raises(targeting.ConditionError):
        targeting.validate(deep, "editor")
    with pytest.raises(targeting.ConditionError):
        targeting.validate({"any": [leaf] * 31}, "editor")
    with pytest.raises(targeting.ConditionError):
        targeting.validate({"aud": "mb"}, "editor")           # snapshot-only leaf
    with pytest.raises(targeting.ConditionError):
        targeting.validate({"group_in": [1]}, "snapshot")     # editor-only leaf
    with pytest.raises(targeting.ConditionError):
        targeting.validate({"segment": {"name": "active", "params": {"days": 7}}}, "editor")
    ok = targeting.validate({"all": [leaf, {"not": {"user_in": [3, 2, 2]}},
                                     {"segment": {"name": "new", "params": {"days": 7}}}]}, "editor")
    assert ok["all"][1] == {"not": {"user_in": [2, 3]}}
    assert targeting.redact(ok)["all"][1] == {"not": {"user_in": {"count": 2}}}


def test_status_transitions_and_edits_only_when_not_running(ads_env):
    _, clock, client, accounts, _, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
    assert post(client, f"/api/admin/ads/campaigns/{cid}/status", csrf, {"action": "pause"}).json()["error"] == "invalid_transition"
    assert post(client, f"/api/admin/ads/campaigns/{cid}/status", csrf, {"action": "activate"}).json()["status"] == "active"
    edit = post(client, "/api/admin/ads/campaigns", csrf, {**campaign_body(pid, int(clock())), "id": cid})
    assert edit.status_code == 409 and edit.json()["error"] == "not_editable"
    post(client, f"/api/admin/ads/campaigns/{cid}/status", csrf, {"action": "pause"})
    assert post(client, "/api/admin/ads/campaigns", csrf, {**campaign_body(pid, int(clock()), priority=50), "id": cid}).status_code == 200
    copy = post(client, f"/api/admin/ads/campaigns/{cid}/copy", csrf, {}).json()["id"]
    item = client.get(f"/api/admin/ads/campaigns/{copy}").json()
    assert item["status"] == "draft" and item["priority"] == 50 and item["name"].endswith("(копия)")
    actions = [e["action"] for e in accounts.events(action="ads.")]
    assert actions.count("ads.campaign_status") == 2 and actions.count("ads.campaign_save") == 3
    future = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()), startsAt=int(clock()) + DAY,
                                                                          endsAt=int(clock()) + 2 * DAY)).json()["id"]
    post(client, f"/api/admin/ads/campaigns/{future}/status", csrf, {"action": "activate"})
    assert client.get(f"/api/admin/ads/campaigns/{future}").json()["state"] == "scheduled"


def test_journal_keeps_texts_and_links_but_no_identifiers(ads_env):
    _, clock, client, accounts, secrets_, csrf, pid = ads_env
    gid = post(client, "/api/admin/ads/groups", csrf, {"name": "VIP"}).json()["id"]
    assert post(client, f"/api/admin/ads/groups/{gid}/members", csrf, {"add": [U1, U2]}).json()["total"] == 2
    post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": "MB777", "code": fresh(secrets_, clock)})
    cond = {"any": [{"user_in": [U2]}, {"group_in": [gid]}, {"mb_id_in": ["MB777"]}]}
    post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()), condition=cond))
    dump = json.dumps(accounts.events(action="ads."), ensure_ascii=False)
    assert str(U1) not in dump and str(U2) not in dump and "MB777" not in dump
    assert "https://partner.example.com/promo?x=1" in dump and "Бонус 100%" in dump
    save = accounts.events(action="ads.campaign_save")[0]
    assert save["after"]["audience"]["kinds"] == ["group_in", "mb_id_in", "user_in"]


def test_analyst_sees_campaigns_without_identifiers(ads_env):
    _, clock, client, _, secrets_, csrf, pid = ads_env
    post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()), condition={"user_in": [U1, U2]}))
    gid = post(client, "/api/admin/ads/groups", csrf, {"name": "VIP"}).json()["id"]
    post(client, f"/api/admin/ads/groups/{gid}/members", csrf, {"add": [U1]})
    client.cookies.clear()
    login(client, clock, secrets_, "analyst@x.test")
    body = client.get("/api/admin/ads/campaigns").text
    assert str(U1) not in body and '"count":2' in body.replace(" ", "")
    assert client.get(f"/api/admin/ads/groups/{gid}/members").status_code == 403


def publish(client, csrf, secrets_, clock, email="owner@x.test", key="publish-key-1", **extra):
    clock.t += 30  # every step-up needs a fresh TOTP step
    return post(client, "/api/admin/ads/publish", csrf, dangerous(secrets_, clock, email, key, **extra))


def test_publish_writes_a_valid_snapshot_with_compiled_audiences(ads_env):
    config, clock, client, accounts, secrets_, csrf, pid = ads_env
    gid = post(client, "/api/admin/ads/groups", csrf, {"name": "VIP"}).json()["id"]
    post(client, f"/api/admin/ads/groups/{gid}/members", csrf, {"add": [U2]})
    post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": "MB777", "code": fresh(secrets_, clock)})
    cond = {"all": [{"any": [{"group_in": [gid]}, {"mb_id_in": ["mb777"]}, {"mb_verified": True}]},
                    {"not": {"mb_id_in": ["NOBODY"]}}]}
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()), condition=cond,
                                                                      countries=["AZ", "RU"])).json()["id"]
    draft = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
    post(client, f"/api/admin/ads/campaigns/{cid}/status", csrf, {"action": "activate"})

    bad = publish(client, csrf, secrets_, clock, **{"code": "000000"})
    assert bad.status_code == 403 and bad.json()["error"] == "invalid_code"
    ok = publish(client, csrf, secrets_, clock)
    assert ok.status_code == 200, ok.text
    result = ok.json()
    assert result["after"]["campaigns"] == [cid] and result["fileWritten"] is True
    raw = config.ads_policy_file.read_bytes()
    snap = policy.parse(raw, pub(config))
    assert snap.version == result["after"]["version"] and [c.id for c in snap.campaigns] == [cid]
    c = snap.campaigns[0]
    assert c.countries == ("AZ",)  # the campaign narrows the partner's list, never widens it
    assert c.age_min == 18 and c.disclosure == {"advertiser": "Partner LLC", "adId": "ERID-1", "ageLabel": "18+"}
    assert c.condition == {"all": [{"any": [{"aud": f"g:{gid}"}, {"user_in": [U1]}, {"aud": "mb"}]},
                                   {"not": {"user_in": []}}]}
    assert snap.audiences == {f"g:{gid}": frozenset({U2}), "mb": frozenset({U1})}
    assert b"MB777" not in raw and b"internal_name" not in raw and draft not in [x.id for x in snap.campaigns]
    status = client.get("/api/admin/ads/publication").json()
    assert status["latest"]["version"] == snap.version == status["fileVersion"]
    assert accounts.events(action="ads.publish")[0]["after"]["sha256"] == snap.sha256


def test_publish_is_idempotent_and_versions_only_grow(ads_env):
    config, clock, client, _, secrets_, csrf, pid = ads_env
    first = publish(client, csrf, secrets_, clock).json()
    again = publish(client, csrf, secrets_, clock).json()
    assert again["replayed"] is True and again["after"]["version"] == first["after"]["version"]
    assert publish(client, csrf, secrets_, clock, pauseAll=True).json()["error"] == "idempotency_key_reused"
    # The Mini App applied a version far ahead (e.g. admin.db restored from an old copy): the next one is above it.
    config.ads_applied_file.write_text(str(first["after"]["version"] + 10**7))
    nxt = publish(client, csrf, secrets_, clock, key="publish-key-2").json()
    assert nxt["after"]["version"] > first["after"]["version"] + 10**7


def test_emergency_stop_pauses_everything_and_publishes_empty(ads_env):
    config, clock, client, accounts, secrets_, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
    post(client, f"/api/admin/ads/campaigns/{cid}/status", csrf, {"action": "activate"})
    publish(client, csrf, secrets_, clock)
    assert len(policy.parse(config.ads_policy_file.read_bytes(), pub(config)).campaigns) == 1
    stop = publish(client, csrf, secrets_, clock, key="stop-key-1", pauseAll=True).json()
    assert stop["after"]["campaigns"] == []
    assert policy.parse(config.ads_policy_file.read_bytes(), pub(config)).campaigns == ()
    assert client.get(f"/api/admin/ads/campaigns/{cid}").json()["status"] == "paused"


def test_inactive_partner_or_moved_hosts_drop_campaigns_from_the_snapshot(ads_env):
    config, clock, client, _, secrets_, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
    post(client, f"/api/admin/ads/campaigns/{cid}/status", csrf, {"action": "activate"})
    post(client, "/api/admin/ads/partners", csrf, {**partner_body(hosts=["other.example.com"]), "id": pid})
    result = publish(client, csrf, secrets_, clock).json()
    assert result["after"]["campaigns"] == [] and result["after"]["skipped"] == [{"id": cid, "code": "url_host_not_allowed"}]
    post(client, "/api/admin/ads/partners", csrf, {**partner_body(active=False), "id": pid})
    result = publish(client, csrf, secrets_, clock, key="publish-key-2").json()
    assert result["after"]["skipped"] == [{"id": cid, "code": "partner_inactive"}]


def test_lost_or_tampered_file_is_rewritten_from_admin_db(ads_env):
    config, clock, client, _, secrets_, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
    post(client, f"/api/admin/ads/campaigns/{cid}/status", csrf, {"action": "activate"})
    version = publish(client, csrf, secrets_, clock).json()["after"]["version"]
    original = config.ads_policy_file.read_bytes()
    config.ads_policy_file.unlink()
    assert client.get("/api/admin/ads/publication").json()["fileVersion"] == version
    assert config.ads_policy_file.read_bytes() == original
    tampered = json.loads(original)
    tampered["campaigns"][0]["variants"][0]["url"] = "https://evil.example.org/"
    config.ads_policy_file.write_bytes(json.dumps(tampered).encode())
    with pytest.raises(policy.PolicyError, match="bad_checksum"):
        policy.parse(config.ads_policy_file.read_bytes(), pub(config))
    # Whoever can write the volume can recompute the checksum, but cannot sign with the admin's key.
    forged = policy.sign(tampered, Ed25519PrivateKey.generate())
    with pytest.raises(policy.PolicyError, match="bad_signature"):
        policy.parse(policy.canonical(forged), pub(config))
    config.ads_policy_file.write_bytes(policy.canonical(forged))
    client.get("/api/admin/ads/publication")
    assert config.ads_policy_file.read_bytes() == original
    # Even a file signed by the admin cannot point outside the partner's hosts.
    with pytest.raises(policy.PolicyError, match="url_host_not_allowed"):
        policy.parse(policy.canonical(policy.sign(tampered, load_signing_key(config.admin_db.parent / "ads-signing.key"))),
                     pub(config))


def test_same_version_with_other_content_is_repaired(ads_env):
    config, clock, client, _, secrets_, csrf, pid = ads_env
    publish(client, csrf, secrets_, clock)
    original = config.ads_policy_file.read_bytes()
    other = json.loads(original)
    other["generatedAt"] += 1
    key = load_signing_key(config.admin_db.parent / "ads-signing.key")
    config.ads_policy_file.write_bytes(policy.canonical(policy.sign(other, key)))
    client.get("/api/admin/ads/publication")
    assert config.ads_policy_file.read_bytes() == original


def test_multi_group_leaf_at_full_depth_still_publishes(ads_env):
    config, clock, client, _, secrets_, csrf, pid = ads_env
    g1 = post(client, "/api/admin/ads/groups", csrf, {"name": "G1"}).json()["id"]
    g2 = post(client, "/api/admin/ads/groups", csrf, {"name": "G2"}).json()["id"]
    post(client, f"/api/admin/ads/groups/{g1}/members", csrf, {"add": [U1]})
    post(client, f"/api/admin/ads/groups/{g2}/members", csrf, {"add": [U1, U2]})
    deep = {"all": [{"any": [{"not": {"group_in": [g1, g2]}}]}]}  # the leaf sits at depth 4
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()), condition=deep)).json()["id"]
    post(client, f"/api/admin/ads/campaigns/{cid}/status", csrf, {"action": "activate"})
    result = publish(client, csrf, secrets_, clock).json()
    assert result["after"]["campaigns"] == [cid] and result["after"]["skipped"] == []
    snap = policy.parse(config.ads_policy_file.read_bytes(), pub(config))
    assert snap.audiences == {f"g:{g1},{g2}": frozenset({U1, U2})}


def test_unpublished_changes_cover_partners_groups_and_mb_ids(ads_env):
    config, clock, client, _, secrets_, csrf, pid = ads_env
    publish(client, csrf, secrets_, clock)
    assert client.get("/api/admin/ads/publication").json()["unpublishedChanges"] is False
    post(client, f"/api/admin/users/{U1}/mb-id", csrf, {"mbId": "MB1", "code": fresh(secrets_, clock)})
    assert client.get("/api/admin/ads/publication").json()["unpublishedChanges"] is True
    publish(client, csrf, secrets_, clock, key="publish-key-2")
    gid = post(client, "/api/admin/ads/groups", csrf, {"name": "G"}).json()["id"]
    assert client.get("/api/admin/ads/publication").json()["unpublishedChanges"] is True
    publish(client, csrf, secrets_, clock, key="publish-key-3")
    post(client, f"/api/admin/ads/groups/{gid}/members", csrf, {"add": [U1]})
    assert client.get("/api/admin/ads/publication").json()["unpublishedChanges"] is True


def test_publication_beyond_reader_limits_is_refused(ads_env, monkeypatch):
    config, clock, client, accounts, secrets_, csrf, pid = ads_env
    for _ in range(3):
        cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
        post(client, f"/api/admin/ads/campaigns/{cid}/status", csrf, {"action": "activate"})
    monkeypatch.setattr(policy, "MAX_CAMPAIGNS", 2)
    result = publish(client, csrf, secrets_, clock)
    assert result.status_code == 409 and result.json()["error"] == "too_many_campaigns"
    assert not config.ads_policy_file.exists() and accounts.events(action="ads.publish") == []
    monkeypatch.setattr(policy, "MAX_CAMPAIGNS", 200)
    monkeypatch.setattr(policy, "MAX_FILE_BYTES", 500)
    result = publish(client, csrf, secrets_, clock, key="publish-key-2")
    assert result.status_code == 409 and result.json()["error"] == "snapshot_too_large"


def test_copy_keeps_the_suffix_of_a_long_name(ads_env):
    _, clock, client, _, _, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()), name="x" * 120)).json()["id"]
    copy = post(client, f"/api/admin/ads/campaigns/{cid}/copy", csrf, {}).json()["id"]
    name = client.get(f"/api/admin/ads/campaigns/{copy}").json()["name"]
    assert len(name) == 120 and name.endswith(" (копия)")


KEY = Ed25519PrivateKey.generate()


def test_snapshot_reader_rejects_unknown_audience_and_bad_schema():
    body = {"schema": 1, "version": 5, "generatedAt": 1, "audiences": {}, "campaigns": [{
        "id": 1, "tier": "general", "priority": 0, "startsAt": 0, "endsAt": 10, "langIn": [], "placements": ["quick_pick"],
        "countries": ["AZ"], "allowUnknownCountry": False, "ageMin": 0, "partnerId": 1, "hosts": ["a.example.com"],
        "disclosure": {}, "condition": {"aud": "g:9"}, "experiment": {"salt": "s" * 16, "controlPct": 0},
        "variants": [{"key": "A", "weight": 100, "headline": "h", "body": "", "extra": "", "cta": "c",
                      "url": "https://a.example.com/"}]}]}
    pk = KEY.public_key()
    with pytest.raises(policy.PolicyError, match="unknown_audience"):
        policy.parse(policy.canonical(policy.sign(body, KEY)), pk)
    body["audiences"] = {"g:9": [1, 2]}
    assert policy.parse(policy.canonical(policy.sign(body, KEY)), pk).audiences["g:9"] == frozenset({1, 2})
    with pytest.raises(policy.PolicyError, match="bad_schema"):
        policy.parse(policy.canonical(policy.sign({**body, "schema": 2}, KEY)), pk)
    unsigned = policy.sign(body, KEY)
    del unsigned["signature"]
    with pytest.raises(policy.PolicyError, match="bad_signature"):
        policy.parse(policy.canonical(unsigned), pk)


def test_signing_key_creation_never_leaves_a_partial_key(tmp_path, monkeypatch):
    import os

    path = tmp_path / "admin" / "ads-signing.key"
    real_link = os.link

    def crash(*a, **k):
        raise KeyboardInterrupt  # the process dies after writing the temporary file, before installing it
    monkeypatch.setattr(os, "link", crash)
    with pytest.raises(KeyboardInterrupt):
        load_signing_key(path)
    assert not path.exists() and not list(path.parent.iterdir())  # no final file, temporary one removed
    monkeypatch.setattr(os, "link", real_link)
    key = load_signing_key(path)  # the next start creates it normally
    assert load_signing_key(path).public_key().public_bytes_raw() == key.public_key().public_bytes_raw()
    assert path.stat().st_mode & 0o777 == 0o600


def test_file_write_failure_after_commit_is_repaired_later(ads_env, monkeypatch):
    config, clock, client, accounts, secrets_, csrf, pid = ads_env
    real = policy.write_atomic

    def fail(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(policy, "write_atomic", fail)
    result = publish(client, csrf, secrets_, clock).json()
    assert result["fileWritten"] is False and accounts.events(action="ads.publish")  # committed
    assert not config.ads_policy_file.exists()
    monkeypatch.setattr(policy, "write_atomic", real)
    assert client.get("/api/admin/ads/publication").json()["fileVersion"] == result["after"]["version"]


def test_a_repair_waiting_for_the_lock_writes_the_latest_publication(ads_env):
    import threading

    from miniapp.admin_web.ads import AdsStore

    config, clock, client, accounts, secrets_, csrf, pid = ads_env
    first = publish(client, csrf, secrets_, clock).json()["after"]["version"]
    stale = config.ads_policy_file.read_bytes()
    second = publish(client, csrf, secrets_, clock, key="publish-key-2").json()["after"]["version"]
    assert second > first
    ads = AdsStore(accounts, config.ads_policy_file, config.ads_applied_file)
    with ads._file_lock():
        config.ads_policy_file.write_bytes(stale)  # e.g. another repairer that had read the old publication
        waiting = threading.Thread(target=ads.ensure_published)
        waiting.start()
        waiting.join(0.2)
        assert waiting.is_alive()  # blocked on the lock
    waiting.join(5)
    assert policy.parse(config.ads_policy_file.read_bytes(), pub(config)).version == second


def test_an_implausible_applied_mark_cannot_block_the_emergency_stop(ads_env):
    config, clock, client, _, secrets_, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
    post(client, f"/api/admin/ads/campaigns/{cid}/status", csrf, {"action": "activate"})
    config.ads_applied_file.write_text(str(2**62))  # poisoned shared volume
    stop = publish(client, csrf, secrets_, clock, pauseAll=True)
    assert stop.status_code == 200, stop.text
    assert policy.parse(config.ads_policy_file.read_bytes(), pub(config)).campaigns == ()


def test_a_signed_file_far_ahead_is_a_floor_for_the_emergency_stop(ads_env):
    # e.g. admin.db restored from an old copy, or the clock corrected by more than a day
    config, clock, client, _, secrets_, csrf, pid = ads_env
    cid = post(client, "/api/admin/ads/campaigns", csrf, campaign_body(pid, int(clock()))).json()["id"]
    post(client, f"/api/admin/ads/campaigns/{cid}/status", csrf, {"action": "activate"})
    publish(client, csrf, secrets_, clock)
    ahead = json.loads(config.ads_policy_file.read_bytes())
    ahead["version"] = int(clock() * 1000) + 30 * DAY * 1000
    key = load_signing_key(config.admin_db.parent / "ads-signing.key")
    config.ads_policy_file.write_bytes(policy.canonical(policy.sign(ahead, key)))
    stop = publish(client, csrf, secrets_, clock, key="stop-key-1", pauseAll=True).json()
    assert stop["after"]["version"] > ahead["version"] and stop["fileWritten"] is True
    snap = policy.parse(config.ads_policy_file.read_bytes(), pub(config))
    assert snap.version == stop["after"]["version"] and snap.campaigns == ()


def test_a_failed_directory_sync_is_retried_by_the_repair(ads_env, monkeypatch):
    config, clock, client, _, secrets_, csrf, pid = ads_env
    calls = []
    real = policy.sync_dir

    def flaky(directory):
        calls.append(directory)
        if len(calls) == 1:
            raise OSError("fsync failed")
        real(directory)
    monkeypatch.setattr(policy, "sync_dir", flaky)
    first = publish(client, csrf, secrets_, clock).json()
    assert first["fileWritten"] is False
    assert client.get("/api/admin/ads/publication").json()["fileVersion"] == first["after"]["version"]
    assert len(calls) >= 2  # the repair synced the directory again although the content already matched
