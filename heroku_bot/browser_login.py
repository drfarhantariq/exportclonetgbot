"""Telegram OIDC login and opaque, revocable browser sessions."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit

import jwt
from aiohttp import BasicAuth, ClientSession, web

SESSION_COOKIE = "__Host-msz_session"
FLOW_COOKIE = "__Host-msz_login"
SESSION_AGE = 12 * 3600
ISSUER = "https://oauth.telegram.org"


class BrowserLogin:
    def __init__(self, server):
        self.server = server
        self.key = hmac.new(server.token.encode(), b"msz-browser-login-v1", hashlib.sha256).digest()
        self.keys, self.keys_until = {}, 0

    def config(self):
        origin = self.origin()
        client_id = os.getenv("TELEGRAM_LOGIN_CLIENT_ID", "").strip()
        secret = os.getenv("TELEGRAM_LOGIN_CLIENT_SECRET", "").strip()
        if not origin or not client_id or not secret:
            return None
        return origin, client_id, secret

    def origin(self):
        origin = os.getenv("MINIAPP_URL", "").rstrip("/")
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
            return None
        return origin

    @staticmethod
    def cookie(response, name, value, age):
        response.set_cookie(name, value, max_age=age, secure=True, httponly=True, samesite="Lax", path="/")

    def pack(self, value):
        data = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        return data + "." + hmac.new(self.key, data.encode(), hashlib.sha256).hexdigest()

    def unpack(self, raw):
        try:
            data, signature = raw.split(".")
            if not hmac.compare_digest(signature, hmac.new(self.key, data.encode(), hashlib.sha256).hexdigest()):
                raise ValueError
            value = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
            if time.time() > value["exp"]:
                raise ValueError
            return value
        except Exception:
            raise ValueError("Login expired. Please try again.") from None

    async def authenticate(self, request, mutation=False):
        raw = request.cookies.get(SESSION_COOKIE, "")
        if not raw or len(raw) > 128:
            raise ValueError("Log in with Telegram to access your workspace.")
        record = await self.server.engine._load_state(self.server.store, self.session_key(raw)) or {}
        user = record.get("user", {})
        if record.get("expires", 0) < time.time() or user.get("id") not in self.server.admins():
            raise ValueError("Your session expired. Log in with Telegram again.")
        if mutation:
            self.check_origin(request)
            supplied = request.headers.get("X-CSRF-Token", "")
            if not supplied or not hmac.compare_digest(supplied, record["csrf"]):
                raise ValueError("Invalid browser action. Refresh the page and try again.")
        return user, record["csrf"]

    @staticmethod
    def session_key(raw):
        return "browser_session:" + hashlib.sha256(raw.encode()).hexdigest()

    def check_origin(self, request):
        origin = self.origin()
        if not origin or request.headers.get("Origin") != origin:
            raise ValueError("Browser actions must come from this website.")

    async def info(self, request):
        user, csrf = None, None
        try:
            user, csrf = await self.authenticate(request)
        except ValueError:
            pass
        return web.json_response({"enabled": bool(self.origin()), "authenticated": bool(user),
                                  "csrf": csrf, "user": user})

    async def start(self, request):
        config = self.config()
        if not self.origin():
            return web.HTTPFound("/?login_error=setup")
        flow = {"state": secrets.token_urlsafe(32), "verifier": secrets.token_urlsafe(48),
                "nonce": secrets.token_urlsafe(32), "exp": time.time() + 600}
        if not config:
            response = web.HTTPFound("/auth/widget")
            self.cookie(response, FLOW_COOKIE, self.pack(flow), 600)
            return response
        origin, client_id, _ = config
        challenge = base64.urlsafe_b64encode(hashlib.sha256(flow["verifier"].encode()).digest()).decode().rstrip("=")
        response = web.HTTPFound(ISSUER + "/auth?" + urlencode({"client_id": client_id,
            "redirect_uri": origin + "/auth/callback", "response_type": "code", "scope": "openid profile",
            "state": flow["state"], "nonce": flow["nonce"], "code_challenge": challenge,
            "code_challenge_method": "S256"}))
        self.cookie(response, FLOW_COOKIE, self.pack(flow), 600)
        return response

    async def widget(self, request):
        try:
            self.unpack(request.cookies.get(FLOW_COOKIE, ""))
        except ValueError:
            return web.HTTPFound("/auth/login")
        return web.Response(text='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · MSZ Workspace</title><script src="/assets/telegram-web-app.js"></script><script src="/assets/theme.js"></script><link rel="stylesheet" href="/assets/app.css"></head><body><main class="browser-login-page"><section class="panel locked"><span class="brand-icon">↗</span><div class="eyebrow">MSZ WORKSPACE</div><h1>Log in with Telegram</h1><p>Use your bot admin account to open your workspace.</p><div id="telegram-login-widget"></div><p id="widget-help" class="muted history-section">If Telegram shows “Bot domain invalid”, use /setdomain in BotFather to register this website.</p><a class="text-button" href="/">Back to workspace</a></section></main><script src="/assets/login-widget.js"></script></body></html>''', content_type="text/html")

    async def widget_config(self, request):
        flow = self.unpack(request.cookies.get(FLOW_COOKIE, ""))
        origin = self.origin()
        if not origin:
            raise ValueError("Website login is not configured.")
        # The username is confirmed from the running bot, never supplied by the browser.
        username = getattr(getattr(self.server.bot, "me", None), "username", None)
        if not username:
            username = os.getenv("TELEGRAM_LOGIN_BOT_USERNAME", "mszec_bot").lstrip("@")
        return web.json_response({"bot": username, "callback": origin + "/auth/legacy-callback?" + urlencode({"state": flow["state"]})})

    def verify_legacy(self, request, flow):
        if len(request.query) != len(set(request.query)) or len(request.query_string) > 16384:
            raise ValueError("Invalid login response.")
        fields = dict(request.query)
        state = fields.pop("state", "")
        if not state or not hmac.compare_digest(state, flow["state"]):
            raise ValueError("Invalid login state.")
        supplied = fields.pop("hash", "")
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        expected = hmac.new(hashlib.sha256(self.server.token.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
        if not supplied or not hmac.compare_digest(expected, supplied):
            raise ValueError("Invalid Telegram signature.")
        age = time.time() - int(fields.get("auth_date", 0))
        if age < -30 or age > 600:
            raise ValueError("Login expired.")
        return {"id": int(fields["id"]), "first_name": fields.get("first_name", "Admin"),
                "last_name": fields.get("last_name", ""), "username": fields.get("username", "")}

    async def issue_session(self, request, user):
        if user["id"] not in self.server.admins():
            return web.HTTPFound("/?login_error=admin")
        response = web.HTTPFound("/")
        raw = secrets.token_urlsafe(48)
        record = {"user": user, "csrf": secrets.token_urlsafe(32), "expires": time.time() + SESSION_AGE}
        await self.server.engine._save_transfer_doc(self.server.store, self.session_key(raw), record)
        old = request.cookies.get(SESSION_COOKIE)
        if old:
            await self.server.engine._save_transfer_doc(self.server.store, self.session_key(old), {})
        self.cookie(response, SESSION_COOKIE, raw, SESSION_AGE)
        return response

    async def legacy_callback(self, request):
        try:
            flow = self.unpack(request.cookies.get(FLOW_COOKIE, ""))
            user = self.verify_legacy(request, flow)
            response = await self.issue_session(request, user)
        except Exception:
            response = web.HTTPFound("/?login_error=failed")
        self.cookie(response, FLOW_COOKIE, "", 0)
        return response

    async def public_keys(self, session, force=False):
        if force or time.time() > self.keys_until:
            async with session.get(ISSUER + "/.well-known/jwks.json", timeout=20) as response:
                response.raise_for_status()
                data = await response.json()
            self.keys = {key["kid"]: key for key in data["keys"] if key.get("kty") == "RSA"}
            self.keys_until = time.time() + 3600
        return self.keys

    async def exchange(self, code, flow):
        origin, client_id, secret = self.config()
        async with ClientSession() as session:
            async with session.post(ISSUER + "/token", auth=BasicAuth(client_id, secret),
                data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                      "redirect_uri": origin + "/auth/callback", "code_verifier": flow["verifier"]}, timeout=20) as response:
                response.raise_for_status()
                token = (await response.json())["id_token"]
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise ValueError("Unsupported login signature.")
            keys = await self.public_keys(session)
            if header.get("kid") not in keys:
                keys = await self.public_keys(session, force=True)
            return self.verify_token(token, keys[header["kid"]], client_id, flow)

    @staticmethod
    def verify_token(token, jwk, client_id, flow):
        key = jwt.PyJWK.from_dict(jwk, algorithm="RS256").key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=client_id, issuer=ISSUER,
                            options={"require": ["exp", "iat", "iss", "aud", "sub", "id"]}, leeway=30)
        # PKCE and state bind the authorization code even if the provider omits nonce.
        if "nonce" in claims and not hmac.compare_digest(str(claims["nonce"]), flow["nonce"]):
            raise ValueError("Invalid login nonce.")
        return claims

    async def callback(self, request):
        response = web.HTTPFound("/")
        try:
            if not self.config():
                raise ValueError
            flow = self.unpack(request.cookies.get(FLOW_COOKIE, ""))
            state = request.query.get("state", "")
            if not state or not hmac.compare_digest(state, flow["state"]):
                raise ValueError
            if request.query.get("error") or not request.query.get("code"):
                raise ValueError
            claims = await self.exchange(request.query["code"], flow)
            uid = claims.get("id")
            if type(uid) is not int or uid not in self.server.admins():
                response = web.HTTPFound("/?login_error=admin")
            else:
                user = {"id": uid, "first_name": claims.get("given_name") or claims.get("name") or "Admin",
                        "last_name": claims.get("family_name", ""), "username": claims.get("preferred_username", "")}
                response = await self.issue_session(request, user)
        except Exception:
            # Never expose OAuth codes, tokens, or client secrets in logs/errors.
            response = web.HTTPFound("/?login_error=failed")
        self.cookie(response, FLOW_COOKIE, "", 0)
        return response

    async def logout(self, request):
        await self.authenticate(request, mutation=True)
        raw = request.cookies[SESSION_COOKIE]
        await self.server.engine._save_transfer_doc(self.server.store, self.session_key(raw), {})
        response = web.json_response({"ok": True})
        self.cookie(response, SESSION_COOKIE, "", 0)
        return response
