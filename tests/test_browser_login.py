"""Browser login signatures, flow binding, persistence, and mutation protection."""
import hashlib
import hmac
import json
import asyncio
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from aiohttp.test_utils import TestClient, TestServer

from test_miniapp import fixture_server, TOKEN, USER
from browser_login import FLOW_COOKIE, SESSION_COOKIE, ISSUER

ORIGIN = "https://workspace.example"


class BrowserLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict("os.environ", {"MINIAPP_URL": ORIGIN,
            "TELEGRAM_LOGIN_CLIENT_ID": "", "TELEGRAM_LOGIN_CLIENT_SECRET": ""})
        self.environment.start()
        self.server, self.docs = fixture_server()
        self.client = TestClient(TestServer(self.server.app))
        await self.client.start_server()
        self.login = self.server.browser_login
        self.flow = {"state": "fixture-state", "nonce": "fixture-nonce", "verifier": "fixture-verifier", "exp": time.time() + 600}
        self.flow_headers = {"Cookie": FLOW_COOKIE + "=" + self.login.pack(self.flow)}

    async def asyncTearDown(self):
        await self.server.close()
        await self.client.close()
        self.environment.stop()

    def legacy_query(self, **changes):
        fields = {"id": str(USER["id"]), "first_name": USER["first_name"], "auth_date": str(int(time.time()))}
        fields.update(changes)
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        fields["hash"] = hmac.new(hashlib.sha256(TOKEN.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
        fields["state"] = self.flow["state"]
        return fields

    async def login_response(self, query=None, headers=None):
        return await self.client.get("/auth/legacy-callback?" + urlencode(query or self.legacy_query()),
            headers=self.flow_headers if headers is None else headers, allow_redirects=False)

    async def session_headers(self):
        response = await self.login_response()
        self.assertEqual(response.headers["Location"], "/")
        cookie = response.cookies[SESSION_COOKIE]
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["max-age"], "43200")
        raw = cookie.value
        record = self.docs[self.login.session_key(raw)]
        self.assertNotIn(raw, json.dumps(self.docs))
        return {"Cookie": SESSION_COOKIE + "=" + raw, "Origin": ORIGIN, "X-CSRF-Token": record["csrf"]}

    async def test_login_session_shared_api_and_revocable_logout(self):
        headers = await self.session_headers()
        response = await self.client.get("/api/state", headers=headers)
        self.assertEqual(response.status, 200)
        info = await (await self.client.get("/auth/session", headers=headers)).json()
        self.assertTrue(info["authenticated"])
        self.assertEqual(info["csrf"], headers["X-CSRF-Token"])
        response = await self.client.post("/api/command", headers=headers, json={"command": "/help"})
        self.assertEqual(response.status, 202)
        await asyncio.sleep(.02)
        self.server.handlers["help"].assert_awaited_once()
        response = await self.client.post("/auth/logout", headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.cookies[SESSION_COOKIE]["max-age"], "0")
        self.assertEqual((await self.client.get("/api/state", headers=headers)).status, 401)

    async def test_tampered_stale_future_and_non_admin_logins(self):
        tampered = self.legacy_query()
        tampered["id"] = "999"
        for query, target in [(tampered, "failed"), (self.legacy_query(auth_date=str(int(time.time()) - 601)), "failed"),
                              (self.legacy_query(auth_date=str(int(time.time()) + 61)), "failed"),
                              (self.legacy_query(id="999"), "admin")]:
            response = await self.login_response(query)
            self.assertIn("login_error=" + target, response.headers["Location"])
            self.assertNotIn(SESSION_COOKIE, response.cookies)

    async def test_missing_changed_duplicate_and_expired_flow(self):
        bad = self.legacy_query()
        bad["state"] = "wrong"
        for query, headers in [(bad, self.flow_headers), (self.legacy_query(), {}),
                               (self.legacy_query(), {"Cookie": FLOW_COOKIE + "=" + self.login.pack(dict(self.flow, exp=1))})]:
            response = await self.login_response(query, headers)
            self.assertIn("login_error=failed", response.headers["Location"])
        response = await self.client.get("/auth/legacy-callback?" + urlencode(self.legacy_query()) + "&id=123",
                                         headers=self.flow_headers, allow_redirects=False)
        self.assertNotIn(SESSION_COOKIE, response.cookies)

    async def test_cookie_sessions_require_csrf_and_same_origin(self):
        headers = await self.session_headers()
        for modified in [dict(headers, Origin="https://other.example"), dict(headers, **{"X-CSRF-Token": "wrong"}),
                         {"Cookie": headers["Cookie"]}]:
            response = await self.client.post("/api/command", json={"command": "/restart"}, headers=modified)
            self.assertNotEqual(response.status, 200)
            self.assertNotEqual((await self.client.post("/auth/logout", headers=modified)).status, 200)
        self.server.handlers["restart"].assert_not_awaited()

    async def test_expired_removed_admin_and_fake_sessions_are_rejected(self):
        headers = await self.session_headers()
        raw = headers["Cookie"].split("=", 1)[1]
        key = self.login.session_key(raw)
        self.docs[key]["expires"] = 1
        self.assertEqual((await self.client.get("/api/state", headers=headers)).status, 401)
        self.docs[key]["expires"] = time.time() + 100
        self.server.admins = lambda: set()
        self.assertEqual((await self.client.get("/api/state", headers=headers)).status, 401)
        self.assertEqual((await self.client.get("/api/state", headers={"Cookie": SESSION_COOKIE + "=fake"})).status, 401)

    async def test_sessions_survive_server_recreation(self):
        headers = await self.session_headers()
        from browser_login import BrowserLogin
        self.server.browser_login = BrowserLogin(self.server)
        user, csrf = await self.server.browser_login.authenticate(type("Request", (), {"cookies": {SESSION_COOKIE: headers["Cookie"].split("=", 1)[1]}})())
        self.assertEqual(user["id"], USER["id"])
        self.assertEqual(csrf, headers["X-CSRF-Token"])

    async def test_legacy_widget_and_oidc_use_browser_bound_flow(self):
        response = await self.client.get("/auth/login", allow_redirects=False)
        self.assertEqual(response.headers["Location"], "/auth/widget")
        self.assertIn(FLOW_COOKIE, response.cookies)
        config = await (await self.client.get("/auth/widget-config", headers=self.flow_headers)).json()
        self.assertEqual(config["bot"], "mszec_bot")
        self.assertIn("state=fixture-state", config["callback"])
        with patch.dict("os.environ", {"TELEGRAM_LOGIN_CLIENT_ID": "12345", "TELEGRAM_LOGIN_CLIENT_SECRET": "fixture-secret"}):
            response = await self.client.get("/auth/login", allow_redirects=False)
            self.assertTrue(response.headers["Location"].startswith(ISSUER + "/auth?"))
            self.assertIn("code_challenge_method=S256", response.headers["Location"])
            self.assertNotIn("fixture-secret", response.headers["Location"])

    def test_oidc_token_signature_audience_issuer_expiry_and_nonce(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
        claims = {"id": 123, "sub": "fixture-sub", "aud": "12345", "iss": ISSUER,
                  "iat": int(time.time()), "exp": int(time.time()) + 300, "nonce": self.flow["nonce"]}
        token = jwt.encode(claims, private, algorithm="RS256")
        self.assertEqual(self.login.verify_token(token, jwk, "12345", self.flow)["id"], 123)
        for changed in [dict(claims, aud="other"), dict(claims, iss="other"), dict(claims, exp=1), dict(claims, nonce="other")]:
            with self.assertRaises((jwt.InvalidTokenError, ValueError)):
                self.login.verify_token(jwt.encode(changed, private, algorithm="RS256"), jwk, "12345", self.flow)
        wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(jwt.InvalidTokenError):
            self.login.verify_token(jwt.encode(claims, wrong, algorithm="RS256"), jwk, "12345", self.flow)
