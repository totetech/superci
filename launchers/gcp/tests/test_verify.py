import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from conftest import jwk_of
from launcher import Config, verify

AUDIENCE = "https://launcher.example"


def test_a_good_token_yields_its_claims(trust, mint, now):
    claims = verify("Bearer " + mint({"iss": trust["issuer"], "sub": trust["subject"], "aud": "https://launcher.example", "exp": now + 60}), "https://launcher.example", trust, now)
    assert claims["sub"] == trust["subject"]
    assert claims["exp"] == now + 60


@pytest.mark.parametrize("claims,why", [
    ({"aud": "https://other.example"}, "token for another agent or control plane"),
    ({"sub": "plane:other"}, "token for another agent or control plane"),
    ({"iss": "https://other-plane.example"}, "token for another agent or control plane"),
    ({"exp": -60}, "expired"),
])
def test_a_token_for_another_agent_or_expired_is_refused(trust, mint, now, claims, why):
    with pytest.raises(PermissionError, match=f"^{why}$"):
        verify("Bearer " + mint(claims), AUDIENCE, trust, now)


def test_a_token_that_expired_a_second_ago_is_refused_and_one_that_expires_in_a_second_is_not(trust, mint, now):
    verify("Bearer " + mint({"exp": now + 1}), AUDIENCE, trust, now)
    with pytest.raises(PermissionError, match="^expired$"):
        verify("Bearer " + mint({"exp": now - 1}), AUDIENCE, trust, now)


def test_no_token_and_a_token_signed_by_another_key_are_refused(trust, mint, now):
    for header in ("", "Basic abc", mint()):  # the last has no "Bearer " in front
        with pytest.raises(PermissionError, match="^no token$"):
            verify(header, AUDIENCE, trust, now)
    other = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(PermissionError, match="^bad token$"):
        verify("Bearer " + mint(signing_key=other), AUDIENCE, trust, now)


@pytest.mark.parametrize("token", ["garbage", "a.b", "a.b.c.d", "a.b.!", "a.b.", ".."])
def test_a_malformed_token_is_a_bad_token(trust, now, token):
    with pytest.raises(PermissionError, match="^bad token$"):
        verify("Bearer " + token, AUDIENCE, trust, now)


def test_a_tampered_payload_is_a_bad_token(trust, mint, now):
    header, _, signature = mint().split(".")
    forged = mint({"sub": "plane:other"}).split(".")[1]
    with pytest.raises(PermissionError, match="^bad token$"):
        verify(f"Bearer {header}.{forged}.{signature}", AUDIENCE, trust, now)


def test_any_key_of_the_plane_s_set_may_have_signed(trust, mint, now):
    other = ec.generate_private_key(ec.SECP256R1())
    trust = {**trust, "keys": {"keys": [jwk_of(other), *trust["keys"]["keys"]]}}
    assert verify("Bearer " + mint(), AUDIENCE, trust, now)["aud"] == AUDIENCE


def test_health_needs_no_token(client): assert client.get("/health").json() == {"agent": "gcp"}


def test_launch_without_a_token_is_401(client):
    r = client.post("/launch", json={})
    assert r.status_code == 401
    assert r.json() == {"detail": "not allowed: no token"}


def test_a_token_that_does_not_verify_is_401_with_the_reason(client, mint):
    r = client.post("/launch", json={}, headers={"authorization": "Bearer " + mint({"sub": "plane:other", "exp": time.time() + 60})})
    assert r.status_code == 401
    assert r.json() == {"detail": "not allowed: token for another agent or control plane"}
    r = client.post("/launch", json={}, headers={"authorization": "Bearer garbage"})
    assert (r.status_code, r.json()) == (401, {"detail": "not allowed: bad token"})


@pytest.fixture
def probe(app_module):
    """A client of a route that does nothing but run `checked`, reached at the machine's own address."""
    probe = FastAPI()

    @probe.post("/probe")
    async def run(body: dict = Depends(app_module.checked)):
        return body

    return TestClient(probe, base_url="http://10.1.2.3")


def bearer(mint, **claims) -> dict:
    return {"authorization": "Bearer " + mint({"exp": time.time() + 60, **claims})}


def test_the_check_wants_the_registered_url_as_audience_not_the_address_the_request_came_to(probe, mint):
    got = probe.post("/probe", json={"n": 1}, headers=bearer(mint, aud=AUDIENCE))
    assert (got.status_code, got.json()) == (200, {"n": 1})
    for aud in ("http://10.1.2.3", "https://10.1.2.3"):
        r = probe.post("/probe", json={}, headers=bearer(mint, aud=aud))
        assert (r.status_code, r.json()) == (401, {"detail": "not allowed: token for another agent or control plane"}), aud


def test_a_body_that_is_not_a_json_object_is_400_once_the_token_is_good(probe, mint):
    headers = bearer(mint)
    assert probe.post("/probe", content=b"not json", headers=headers).status_code == 400
    assert probe.post("/probe", json=[1, 2], headers=headers).status_code == 400
    assert probe.post("/probe", content=b"not json").status_code == 401  # the token is checked first


def test_the_configuration_is_read_from_the_environment(env, trust, jwks):
    cfg = Config.from_env()
    assert (cfg.project, cfg.region, cfg.network, cfg.subnet) == ("superci-test", "us-west4", "superci", "superci-us-west4")
    assert (cfg.image_family, cfg.runner_version, cfg.public_url, cfg.sweep_secret) == ("superci-runner", "2.330.0", AUDIENCE, "sweep-secret")
    assert cfg.zones == ("us-west4-a", "us-west4-b", "us-west4-c")
    assert cfg.plane_id == "p1"
    assert cfg.trust == trust and cfg.trust["keys"] == jwks
    assert (cfg.on_demand_fallback, cfg.behind_gateway, cfg.external_address) == (False, False, True)
    assert (cfg.budget_s, cfg.insert_wait_s) == (8.0, 5.0)


def test_the_optional_settings_are_read_when_given(env, monkeypatch):
    monkeypatch.setenv("ON_DEMAND_FALLBACK", "1")
    monkeypatch.setenv("BEHIND_GATEWAY", "1")
    monkeypatch.setenv("EXTERNAL_ADDRESS", "0")
    monkeypatch.setenv("ZONES", "us-west4-b, us-west4-c")
    cfg = Config.from_env()
    assert (cfg.on_demand_fallback, cfg.behind_gateway, cfg.external_address) == (True, True, False)
    assert cfg.zones == ("us-west4-b", "us-west4-c")


def test_zones_follow_the_region_unless_given(env, monkeypatch):
    monkeypatch.setenv("REGION", "us-east1")
    assert Config.from_env().zones == ("us-east1-a", "us-east1-b", "us-east1-c")


def test_zones_that_name_no_zone_stop_the_start(env, monkeypatch):
    for zones in (",", " , ", " ,, "):
        monkeypatch.setenv("ZONES", zones)
        with pytest.raises(ValueError, match="ZONES names no zone"):
            Config.from_env()


def test_a_missing_setting_stops_the_start(env, monkeypatch):
    monkeypatch.delenv("PUBLIC_URL")
    with pytest.raises(KeyError, match="PUBLIC_URL"):
        Config.from_env()
