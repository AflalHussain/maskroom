"""Who is calling, and what they may do.

Two modes, chosen by MASKROOM_AUTH_MODE:

  off   (default) no login. The legacy shared secrets still apply: MASKROOM_API_KEY
        on /api/* (X-API-Key header) and MASKROOM_ADMIN_KEY on the admin APIs
        (X-Admin-Key header). Query-string keys are no longer accepted. For local
        demos and tests.
  oidc  single sign-on. The server runs the OpenID Connect authorization-code
        flow against any provider with a discovery document (Google, Microsoft
        Entra, Okta, Keycloak...) and issues its own session cookie, backed by
        the login_sessions table so a session can be revoked centrally. Roles
        (staff < auditor < admin) live in the users table and are managed here,
        never mapped from the provider.

A request is attributed to one Principal, resolved in this order and never
falling through from a presented-but-invalid credential:
  1. Authorization: Bearer mr_...   a named service key (api_keys table)
  2. Authorization: Bearer <token>  a signed-in user on a native client (the
                                    desktop helper), token from /auth/exchange
  3. X-API-Key                      the deprecated shared MASKROOM_API_KEY
  4. the maskroom_session cookie    a signed-in user
  5. anonymous

Native clients sign in through the system browser: /auth/login?next=
http://127.0.0.1:<port>/... (RFC 8252 loopback) ends with a one-time code on
that URL, and POST /auth/exchange {code} returns a login-session token.

Ownership: sessions and file runs remember the principal that created them and
are served only to that principal (rows without an owner, created with auth
off, are admin-only). Cookie-authenticated requests that change state must
carry `X-Requested-With: maskroom`, which a cross-site form cannot add.
"""
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from flask import Blueprint, Response, g, jsonify, redirect, request, session as flask_session

from maskroom.store import (ROLES, ApiKeyStore, LoginCodeStore, LoginSessionStore, RunStore,
                            UserStore, normalize_email, role_allows)
from maskroom.store.users import MISSING

log = logging.getLogger("maskroom.auth")

COOKIE = "maskroom_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "maskroom"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
OPEN_PREFIXES = ("/ext/", "/auth/", "/static/")
OPEN_PATHS = frozenset({"/", "/staging", "/admin", "/admin/rules", "/admin/users",
                        "/api/config", "/favicon.ico"})
ADMIN_API_PREFIXES = ("/api/policy", "/api/users", "/api/keys")
AUDITOR_API_PREFIXES = ("/api/audit",)


# ------------------------------------------------------------------ model
@dataclass(frozen=True)
class Principal:
    kind: str            # user | service | legacy | anonymous
    id: str | None
    email: str | None
    name: str | None
    role: str | None

    @property
    def label(self):
        """The name the audit trail records."""
        if self.kind == "user":
            return self.email
        if self.kind in ("service", "legacy"):
            return self.name
        return "unknown"

    @property
    def authenticated(self):
        return self.kind != "anonymous"

    def to_dict(self):
        return {"kind": self.kind, "id": self.id, "email": self.email,
                "name": self.name, "role": self.role}


ANONYMOUS = Principal("anonymous", None, None, None, None)


@dataclass(frozen=True)
class Identity:
    email: str
    name: str = ""
    sub: str | None = None
    id_token: str | None = None   # raw id_token, kept for RP-initiated logout


class AuthError(Exception):
    """A login that cannot complete; the message is safe to show."""


# -------------------------------------------------------------- providers
class IdentityProvider:
    """Where users prove who they are. Two calls, both on the server."""

    def authorize_redirect(self, redirect_uri, state):
        raise NotImplementedError

    def exchange(self, req):
        """Identity for the provider's callback request, or raise AuthError."""
        raise NotImplementedError

    def end_session_url(self, id_token, post_logout_redirect_uri):
        """Where to send the browser to end the provider's own session, or
        None when the provider has no such endpoint (e.g. Google)."""
        return None


class OidcProvider(IdentityProvider):
    """Any OpenID Connect issuer, configured by its discovery document."""

    def __init__(self, app, issuer, client_id, client_secret, scopes="openid email profile"):
        from authlib.integrations.flask_client import OAuth
        self.issuer = issuer.rstrip("/")
        self.oauth = OAuth(app)
        self.client = self.oauth.register(
            name="oidc", client_id=client_id, client_secret=client_secret,
            server_metadata_url=f"{self.issuer}/.well-known/openid-configuration",
            client_kwargs={"scope": scopes})

    def authorize_redirect(self, redirect_uri, state):
        return self.client.authorize_redirect(redirect_uri, state=state)

    def exchange(self, req):
        from authlib.integrations.base_client.errors import OAuthError
        try:
            token = self.client.authorize_access_token()
        except OAuthError as e:
            raise AuthError(f"Sign-in failed: {e.description or e.error}") from e
        claims = dict(token.get("userinfo") or {})
        if not claims.get("email"):  # some providers omit email from the id_token
            try:
                claims.update(self.client.userinfo(token=token) or {})
            except Exception as e:  # noqa: BLE001
                log.warning("userinfo call failed: %s", e)
        email = claims.get("email")
        if not email:
            raise AuthError("The identity provider returned no email address; "
                            "grant the 'email' scope/claim to the SafePII client.")
        if claims.get("email_verified") is False:
            raise AuthError("This email address is not verified at the identity provider.")
        name = claims.get("name") or claims.get("preferred_username") or ""
        return Identity(email=email, name=name, sub=claims.get("sub"), id_token=token.get("id_token"))

    def end_session_url(self, id_token, post_logout_redirect_uri):
        try:
            endpoint = self.client.load_server_metadata().get("end_session_endpoint")
        except Exception as e:  # noqa: BLE001
            log.warning("could not load provider metadata for logout: %s", e)
            return None
        if not endpoint:
            return None
        params = {"client_id": self.client.client_id, "post_logout_redirect_uri": post_logout_redirect_uri}
        if id_token:
            params["id_token_hint"] = id_token
        return f"{endpoint}?{urlencode(params)}"


class FakeProvider(IdentityProvider):
    """Test double: signs in as a fixed identity through the real routes."""

    def __init__(self, identity, end_session_endpoint=None):
        self.identity = identity
        self.state = None
        self.end_session_endpoint = end_session_endpoint

    def authorize_redirect(self, redirect_uri, state):
        self.state = state
        return redirect(f"{redirect_uri}?state={state}&code=fake")

    def exchange(self, req):
        if not self.state or req.args.get("state") != self.state:
            raise AuthError("Sign-in failed: state mismatch.")
        self.state = None
        if isinstance(self.identity, Exception):
            raise self.identity
        return self.identity

    def end_session_url(self, id_token, post_logout_redirect_uri):
        if not self.end_session_endpoint:
            return None
        params = {"client_id": "maskroom", "post_logout_redirect_uri": post_logout_redirect_uri}
        if id_token:
            params["id_token_hint"] = id_token
        return f"{self.end_session_endpoint}?{urlencode(params)}"


# ----------------------------------------------------------------- state
class AuthState:
    def __init__(self, mode, db, provider, legacy_key, admin_key, session_hours,
                 admin_emails, public_url, extension_ids):
        self.mode = mode
        self.db = db
        self.provider = provider
        self._legacy_key = legacy_key   # str or callable
        self._admin_key = admin_key
        self.session_hours = session_hours
        self.admin_emails = admin_emails
        self.public_url = public_url
        self.extension_ids = extension_ids
        self.users = UserStore(db)
        self.logins = LoginSessionStore(db, hours=session_hours)
        self.codes = LoginCodeStore(db)
        self.keys = ApiKeyStore(db)
        self.runs = RunStore(db)
        self._warned_legacy = False

    @property
    def legacy_key(self):
        return self._legacy_key() if callable(self._legacy_key) else self._legacy_key

    @property
    def admin_key(self):
        return self._admin_key() if callable(self._admin_key) else self._admin_key


AUTH = None
auth_bp = Blueprint("auth", __name__)


def configure_auth(app, db, mode=None, provider=None, legacy_key=None, admin_key=None,
                   session_hours=None, admin_emails=None, public_url=None, extension_ids=None):
    """Build the process-wide auth state from the environment plus overrides.
    Called once at import by webui.app and again by tests. Idempotent."""
    global AUTH
    env = os.environ.get
    mode = (mode or env("MASKROOM_AUTH_MODE") or "off").strip().lower()
    if mode not in ("off", "oidc"):
        raise RuntimeError(f"MASKROOM_AUTH_MODE must be 'off' or 'oidc', not {mode!r}")
    if not app.secret_key:
        app.secret_key = env("MASKROOM_SECRET_KEY") or secrets.token_hex(32)
    if mode == "oidc" and not env("MASKROOM_SECRET_KEY") and not app.testing:
        log.warning("MASKROOM_SECRET_KEY is not set; login state will not survive a restart "
                    "or be shared between workers.")
    if mode == "oidc" and provider is None:
        issuer, cid = env("MASKROOM_OIDC_ISSUER"), env("MASKROOM_OIDC_CLIENT_ID")
        if not issuer or not cid:
            raise RuntimeError("MASKROOM_AUTH_MODE=oidc needs MASKROOM_OIDC_ISSUER and "
                               "MASKROOM_OIDC_CLIENT_ID (and usually MASKROOM_OIDC_CLIENT_SECRET).")
        provider = OidcProvider(app, issuer, cid, env("MASKROOM_OIDC_CLIENT_SECRET"),
                                env("MASKROOM_OIDC_SCOPES") or "openid email profile")
    emails = admin_emails if admin_emails is not None else env("MASKROOM_ADMIN_EMAILS", "")
    if isinstance(emails, str):
        emails = {normalize_email(e) for e in emails.split(",") if e.strip()}
    ext_ids = set(extension_ids or [])
    ext_ids.update(x.strip() for x in env("MASKROOM_EXTENSION_IDS", "").split(",") if x.strip())
    AUTH = AuthState(
        mode=mode, db=db, provider=provider,
        legacy_key=legacy_key if legacy_key is not None else env("MASKROOM_API_KEY"),
        admin_key=admin_key if admin_key is not None else env("MASKROOM_ADMIN_KEY"),
        session_hours=float(session_hours or env("MASKROOM_SESSION_HOURS") or 12),
        admin_emails=emails, public_url=(public_url or env("MASKROOM_PUBLIC_URL") or "").rstrip("/"),
        extension_ids=ext_ids)
    if "auth" not in app.blueprints:
        app.register_blueprint(auth_bp)
    return AUTH


# --------------------------------------------------------------- helpers
def external_base():
    """Absolute base URL as the client reached us, honouring a proxy/tunnel
    (ngrok, load balancer) via X-Forwarded-* so generated URLs are correct
    wherever the server is exposed."""
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
    host = request.headers.get("X-Forwarded-Host", request.host).split(",")[0].strip()
    return f"{scheme}://{host}"


def client_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "")


def public_base():
    return AUTH.public_url or external_base()


def current_principal():
    return getattr(g, "principal", ANONYMOUS)


def owned(owner_id):
    """Whether the current principal may use a session or run with this owner.
    With auth off everything is shared; an ownerless row (made with auth off,
    or before the upgrade) is admin-only; otherwise the ids must match."""
    if AUTH.mode == "off":
        return True
    p = current_principal()
    if owner_id is None:
        return p.role == "admin"
    return p.id == owner_id


def is_loopback(n):
    """A native client's redirect: http://127.0.0.1:<port>/... or [::1]
    (RFC 8252 section 7.3). Not 'localhost', which can resolve elsewhere."""
    u = urlsplit(n or "")
    return u.scheme == "http" and u.hostname in ("127.0.0.1", "::1") and u.port is not None


def safe_next(n):
    """Where to go after login: a same-origin path, the extension's own
    chromiumapp.org callback, or a native client's loopback URL. Anything
    else becomes '/'."""
    if not n:
        return "/"
    if n.startswith("/") and not n.startswith("//") and not n.startswith("/\\"):
        return n
    u = urlsplit(n)
    if u.scheme == "https" and u.netloc in {f"{eid}.chromiumapp.org" for eid in AUTH.extension_ids}:
        return n
    if is_loopback(n):
        return n
    return "/"


def bearer_token():
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth.startswith("Bearer ") else ""


def cookie_kwargs():
    https = external_base().startswith("https")
    return {"httponly": True, "secure": https, "samesite": "None" if https else "Lax",
            "path": "/", "max_age": int(AUTH.session_hours * 3600)}


def required_role(path):
    if path.startswith(ADMIN_API_PREFIXES):
        return "admin"
    if path.startswith(AUDITOR_API_PREFIXES):
        return "auditor"
    return "staff"


def resolve_principal():
    bearer = bearer_token()
    if bearer.startswith("mr_"):
        key = AUTH.keys.authenticate(bearer)
        return Principal("service", key.id, None, key.name, key.role) if key else ANONYMOUS
    if bearer:
        hit = AUTH.logins.resolve(bearer)
        if hit:
            _login, user = hit
            return Principal("user", user.id, user.email, user.name, user.role)
        return ANONYMOUS
    given = request.headers.get("X-API-Key")
    if given:
        legacy = AUTH.legacy_key
        if legacy and hmac.compare_digest(given, legacy):
            if not AUTH._warned_legacy:
                AUTH._warned_legacy = True
                log.warning("A client authenticated with the deprecated shared MASKROOM_API_KEY; "
                            "move it to a named service key (maskroom-admin key create).")
            return Principal("legacy", "legacy-api-key", None, "legacy-api-key", "staff")
        return ANONYMOUS
    token = request.cookies.get(COOKIE)
    if token:
        hit = AUTH.logins.resolve(token)
        if hit:
            _login, user = hit
            return Principal("user", user.id, user.email, user.name, user.role)
    return ANONYMOUS


def _legacy_gate(path):
    """Auth off: the pre-SSO shared-secret checks, headers only."""
    if path.startswith(ADMIN_API_PREFIXES) or path.startswith(AUDITOR_API_PREFIXES):
        admin_key = AUTH.admin_key
        if not admin_key:
            return jsonify({"error": "Admin surfaces are disabled. Set MASKROOM_ADMIN_KEY "
                                     "on the server to enable them."}), 403
        given = request.headers.get("X-Admin-Key") or ""
        if not hmac.compare_digest(given, admin_key):
            return jsonify({"error": "Admin key required (X-Admin-Key header)."}), 401
        return None
    if path == "/api/me" or not path.startswith("/api/"):
        return None
    if AUTH.legacy_key and current_principal().kind not in ("legacy", "service"):
        return jsonify({"error": "Missing or invalid API key (X-API-Key header)."}), 401
    return None


def authenticate():
    """before_request: attribute the request and enforce the access matrix."""
    g.principal = ANONYMOUS
    path = request.path
    if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
        return None
    g.principal = resolve_principal()
    if AUTH.mode == "off":
        return _legacy_gate(path)
    if not path.startswith("/api/"):
        return None
    p = g.principal
    if not p.authenticated:
        return jsonify({"error": "Sign-in required.", "login_url": "/auth/login"}), 401
    if p.kind == "user" and request.method not in SAFE_METHODS \
            and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        return jsonify({"error": f"Missing {CSRF_HEADER}: {CSRF_VALUE} header."}), 403
    need = required_role(path)
    if not role_allows(p.role, need):
        return jsonify({"error": f"The {need} role is required."}), 403
    return None


def _html(status, title, body):
    return Response(f"<!doctype html><meta charset=utf-8><title>{title}</title>"
                    f"<body style='font-family:system-ui;margin:3rem'><h2>{title}</h2><p>{body}</p>"
                    f"<p><a href='/'>Back to SafePII</a></p></body>", status=status, mimetype="text/html")


# ---------------------------------------------------------------- routes
@auth_bp.get("/auth/login")
def login():
    if AUTH.mode != "oidc":
        return jsonify({"error": "Sign-in is not enabled on this server (MASKROOM_AUTH_MODE=off)."}), 404
    state = secrets.token_urlsafe(16)
    flask_session["auth_next"] = safe_next(request.args.get("next"))
    flask_session["auth_state"] = state
    return AUTH.provider.authorize_redirect(f"{public_base()}/auth/callback", state)


@auth_bp.get("/auth/callback")
def callback():
    if AUTH.mode != "oidc":
        return jsonify({"error": "Sign-in is not enabled on this server."}), 404
    try:
        ident = AUTH.provider.exchange(request)
    except AuthError as e:
        return _html(400, "Sign-in failed", str(e))
    email = normalize_email(ident.email)
    user = AUTH.users.upsert_login(email, name=ident.name, sub=ident.sub,
                                   bootstrap_admin=email in AUTH.admin_emails)
    if user.disabled:
        return _html(403, "Account disabled", f"{user.email} has been disabled by an administrator.")
    token = AUTH.logins.create(user.id, ip=client_ip(), id_token=ident.id_token)
    nxt = flask_session.pop("auth_next", "/")
    flask_session.pop("auth_state", None)
    if is_loopback(nxt):
        # A native client is waiting on 127.0.0.1: hand it a one-time code, not
        # the session. The browser keeps its own cookie session as well.
        code = AUTH.codes.create(user.id, ip=client_ip(), id_token=ident.id_token)
        u = urlsplit(nxt)
        query = (u.query + "&" if u.query else "") + urlencode({"code": code})
        nxt = u._replace(query=query).geturl()
    resp = redirect(nxt)
    resp.set_cookie(COOKIE, token, **cookie_kwargs())
    return resp


@auth_bp.post("/auth/exchange")
def exchange():
    """Native client: {code} -> {token, principal, expires_in}. The code came
    from a loopback login redirect and works once, within a minute. The token
    is a login session, sent back as `Authorization: Bearer <token>`."""
    if AUTH.mode != "oidc":
        return jsonify({"error": "Sign-in is not enabled on this server."}), 404
    body = request.get_json(silent=True) or {}
    hit = AUTH.codes.redeem(body.get("code"))
    if not hit:
        return jsonify({"error": "This sign-in code is invalid, used or expired. Sign in again."}), 400
    user_id, _ip, id_token = hit
    user = AUTH.users.get(user_id)
    if user is None or user.disabled:
        return jsonify({"error": "Account disabled."}), 403
    token = AUTH.logins.create(user.id, ip=client_ip(), id_token=id_token)
    p = Principal("user", user.id, user.email, user.name, user.role)
    return jsonify({"token": token, "principal": p.to_dict(),
                    "expires_in": int(AUTH.session_hours * 3600)})


@auth_bp.post("/auth/logout")
def logout():
    """End the SafePII session and say where the browser should go next:
    the provider's end-session endpoint when it has one (so single sign-on
    does not log the user straight back in), else the signed-out page. An
    optional `next` (same rules as login) is honoured after that."""
    token = request.cookies.get(COOKIE)
    if token and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        return jsonify({"error": f"Missing {CSRF_HEADER}: {CSRF_VALUE} header."}), 403
    if not token:
        token = bearer_token()   # a native client ending its own session
    id_token = None
    if token:
        _removed, id_token = AUTH.logins.revoke(token)
    body = request.get_json(silent=True) or {}
    nxt = safe_next(request.args.get("next") or body.get("next"))
    landing = f"{public_base()}/auth/signed-out"
    if nxt != "/":
        landing += "?" + urlencode({"next": nxt})
    redirect_to = None
    if AUTH.mode == "oidc" and AUTH.provider is not None:
        redirect_to = AUTH.provider.end_session_url(id_token, landing)
    resp = jsonify({"ok": True, "redirect": redirect_to or landing})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@auth_bp.get("/auth/signed-out")
def signed_out():
    """Neutral landing after logout. Never redirects to a login route, so a
    still-alive provider session cannot sign the user back in silently."""
    nxt = safe_next(request.args.get("next"))
    if nxt.startswith("https://"):   # the extension's chromiumapp.org callback
        return redirect(nxt)
    return _html(200, "Signed out",
                 "You have been signed out of SafePII. "
                 f"<a href='/auth/login?next={nxt}'>Sign in again</a>")


@auth_bp.get("/api/me")
def me():
    p = current_principal()
    if AUTH.mode == "off":
        return jsonify({"auth_mode": "off", "principal": p.to_dict() if p.authenticated else None})
    if not p.authenticated:
        return jsonify({"error": "Sign-in required.", "login_url": "/auth/login"}), 401
    return jsonify({"auth_mode": "oidc", "principal": p.to_dict()})


# admin: users and service keys (the role gate lives in authenticate())
@auth_bp.get("/api/users")
def users_list():
    return jsonify({"users": [u.to_dict() for u in AUTH.users.list()], "roles": list(ROLES)})


@auth_bp.patch("/api/users/<user_id>")
def users_patch(user_id):
    data = request.get_json(silent=True) or {}
    me_ = current_principal()
    if me_.kind == "user" and me_.id == user_id:
        return jsonify({"error": "You cannot change your own role or disable yourself."}), 400
    user = AUTH.users.get(user_id)
    if not user:
        return jsonify({"error": "Unknown user."}), 404
    if "role" in data:
        try:
            AUTH.users.set_role(user_id, data["role"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    if "disabled" in data:
        AUTH.users.set_disabled(user_id, bool(data["disabled"]))
        if data["disabled"]:
            AUTH.logins.revoke_user(user_id)
    return jsonify({"user": AUTH.users.get(user_id).to_dict()})


@auth_bp.get("/api/keys")
def keys_list():
    return jsonify({"keys": [k.to_dict() for k in AUTH.keys.list()], "roles": list(ROLES)})


@auth_bp.post("/api/keys")
def keys_create():
    data = request.get_json(silent=True) or {}
    try:
        key, plaintext = AUTH.keys.create(data.get("name"), role=data.get("role") or "staff",
                                          created_by=current_principal().label)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"key": key.to_dict(), "secret": plaintext,
                    "note": "This secret is shown once; store it now."}), 201


@auth_bp.delete("/api/keys/<key_id>")
def keys_revoke(key_id):
    if not AUTH.keys.revoke(key_id):
        return jsonify({"error": "Unknown or already revoked key."}), 404
    return jsonify({"ok": True})
