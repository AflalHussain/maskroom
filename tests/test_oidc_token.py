"""The id_token validation authlib performs for OidcProvider, exercised with a
locally generated RSA key and a JWKS, no live issuer needed."""
import time

import pytest
from authlib.integrations.flask_client import OAuth
from authlib.jose import JsonWebKey, jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask

ISSUER = "https://issuer.test"


def _keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    pub = priv.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)
    return pem, pub


def _client(app, jwks):
    oauth = OAuth(app)
    return oauth.register("oidc", client_id="maskroom", client_secret="s",
                          issuer=ISSUER, jwks=jwks,
                          id_token_signing_alg_values_supported=["RS256"],
                          client_kwargs={"scope": "openid email profile"})


def _token(priv, kid="k1", **over):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": "maskroom", "sub": "s1", "email": "a@b.c",
              "email_verified": True, "nonce": "n1", "exp": now + 60, "iat": now, **over}
    return jwt.encode({"alg": "RS256", "kid": kid}, claims, priv).decode()


def test_id_token_validation_against_jwks():
    app = Flask(__name__)
    app.secret_key = "t"
    app.testing = True
    priv, pub = _keypair()
    other_priv, _ = _keypair()
    jwk = JsonWebKey.import_key(pub, {"kty": "RSA", "kid": "k1", "use": "sig"})
    client = _client(app, {"keys": [jwk.as_dict()]})
    with app.test_request_context():
        claims = client.parse_id_token({"id_token": _token(priv)}, nonce="n1")
        assert claims["email"] == "a@b.c" and claims["sub"] == "s1"
        with pytest.raises(Exception):
            client.parse_id_token({"id_token": _token(priv)}, nonce="wrong-nonce")
        with pytest.raises(Exception):
            client.parse_id_token({"id_token": _token(priv, aud="someone-else")}, nonce="n1")
        with pytest.raises(Exception):
            client.parse_id_token({"id_token": _token(priv, iss="https://evil.test")}, nonce="n1")
        with pytest.raises(Exception):
            client.parse_id_token({"id_token": _token(priv, exp=int(time.time()) - 3600)}, nonce="n1")
        with pytest.raises(Exception):
            client.parse_id_token({"id_token": _token(other_priv)}, nonce="n1")   # unknown key
