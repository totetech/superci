import base64
import importlib
import json
import sys

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi.testclient import TestClient

AUDIENCE = "https://launcher.example"
ISSUER = "https://plane.example"
SUBJECT = "plane:p1"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwk_of(key: ec.EllipticCurvePrivateKey) -> dict:
    point = key.public_key().public_numbers()
    return {"kty": "EC", "crv": "P-256", "alg": "ES256", "use": "sig",
            "x": b64(point.x.to_bytes(32, "big")), "y": b64(point.y.to_bytes(32, "big"))}


@pytest.fixture
def now() -> float:
    return 1_700_000_000.0


@pytest.fixture
def key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def jwks(key) -> dict:
    return {"keys": [jwk_of(key)]}


@pytest.fixture
def trust(jwks) -> dict:
    return {"issuer": ISSUER, "subject": SUBJECT, "keys": jwks}


@pytest.fixture
def mint(key, trust, now):
    """Signs a token with the plane's key unless another key is given; `claims` override a good token's claims."""
    def mint(claims: dict | None = None, signing_key: ec.EllipticCurvePrivateKey | None = None) -> str:
        good = {"iss": trust["issuer"], "sub": trust["subject"], "aud": AUDIENCE, "exp": now + 60}
        header = b64(json.dumps({"alg": "ES256", "typ": "JWT"}).encode())
        payload = b64(json.dumps({**good, **(claims or {})}).encode())
        der = (signing_key or key).sign(f"{header}.{payload}".encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return f"{header}.{payload}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return mint


@pytest.fixture
def env(monkeypatch, trust, jwks) -> dict:
    """The environment a launcher is started with, with the plane's identity from `trust`."""
    values = {
        "PROJECT": "superci-test", "REGION": "us-west4", "NETWORK": "superci", "SUBNET": "superci-us-west4",
        "IMAGE_FAMILY": "superci-runner", "RUNNER_VERSION": "2.330.0", "ON_DEMAND_FALLBACK": "0",
        "PUBLIC_URL": AUDIENCE, "TRUSTED_ISSUER": trust["issuer"], "TRUSTED_SUBJECT": trust["subject"],
        "TRUSTED_KEYS": json.dumps(jwks), "SWEEP_SECRET": "sweep-secret",
    }
    for name in ("ZONES", "BEHIND_GATEWAY", "EXTERNAL_ADDRESS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


@pytest.fixture
def app_module(env, monkeypatch):
    """`app` imported afresh under `env`, since it reads its configuration when imported."""
    monkeypatch.delitem(sys.modules, "app", raising=False)
    module = importlib.import_module("app")
    yield module
    sys.modules.pop("app", None)


@pytest.fixture
def client(app_module) -> TestClient:
    return TestClient(app_module.api)


@pytest.fixture
def cfg(env):
    """The launcher's configuration as `env` gives it: three us-west4 zones, spot only, a bare subnet name, path A."""
    from launcher import Config
    return Config.from_env()


@pytest.fixture
def fake():
    """A `FakeCompute` that has room nowhere until a test's `plan` says so."""
    from fake_compute import FakeCompute
    return FakeCompute()


@pytest.fixture
def client_with_fake(app_module, client, fake) -> TestClient:
    """`client`, with the app's Compute replaced by `fake` (through FastAPI's dependency overrides)."""
    app_module.api.dependency_overrides[app_module.get_compute] = lambda: fake
    return client
