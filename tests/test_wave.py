"""Sign-in and API behaviour against the mock cloud."""
from __future__ import annotations

import json
import time
import unittest

from bwwatch.errors import ApiError, AuthError, TransientError
from bwwatch.wave import TokenManager, TokenStore, WaveApi, http_request, parse_redirect

from .helpers import MAC, WaveTestCase
from .mock_wave import REDIRECT

CODE_URL = REDIRECT + "?state=init&code=%s"


class ParseRedirect(unittest.TestCase):
    def test_accepted_forms(self):
        code = "abcdefghijklmnopqrstuvwxyz0123456789"
        for text in (CODE_URL % code, "  " + CODE_URL % code + "\n", '"%s"' % (CODE_URL % code),
                     "code=%s&state=init" % code, code):
            with self.subTest(text[:30]):
                self.assertEqual(parse_redirect(text)[0], code)

    def test_state_is_returned(self):
        self.assertEqual(parse_redirect(CODE_URL % "x" * 30)[1]["state"], "init")

    def test_helpful_failures(self):
        cases = {
            "": "nothing was pasted",
            "https://consumer.bradfordwhiteapps.com/x/B2C_1_Wave_SignIn/api/CombinedSigninAndSignup/confirmed?rememberMe=false": "intermediate",
            "https://consumer.bradfordwhiteapps.com/t/B2C_1_Wave_SignIn/oauth2/v2.0/authorize?client_id=1": "sign-in address itself",
            REDIRECT + "?error=access_denied&error_description=nope": "access_denied",
            REDIRECT + "?state=init": "no 'code='",
        }
        for text, expect in cases.items():
            with self.subTest(expect):
                with self.assertRaises(AuthError) as ctx:
                    parse_redirect(text)
                self.assertIn(expect, str(ctx.exception))


class SignIn(WaveTestCase):
    def manager(self, **extra):
        cfg = self.cfg(**extra)
        return cfg, TokenStore(cfg.data_dir / "token.json"), None

    def test_login_exchange_saves_a_private_refresh_token(self):
        cfg = self.cfg()
        store = TokenStore(cfg.data_dir / "token.json")
        tokens = TokenManager(cfg, store)
        tokens.exchange_code(self.mock.issue_login_code())
        saved = store.load()
        self.assertTrue(saved["refresh_token"].startswith("refresh-token-"))
        self.assertEqual(oct((cfg.data_dir / "token.json").stat().st_mode & 0o777), "0o600")
        form = self.mock.hits("/auth/token")[0]["form"]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["redirect_uri"], REDIRECT)
        self.assertEqual(tokens.account_id, self.mock.oid)

    def test_a_login_code_works_only_once(self):
        cfg = self.cfg()
        code = self.mock.issue_login_code()
        TokenManager(cfg, TokenStore(cfg.data_dir / "token.json")).exchange_code(code)
        with self.assertRaises(AuthError) as ctx:
            TokenManager(cfg, TokenStore(cfg.data_dir / "t2.json")).exchange_code(code)
        self.assertIn("invalid_grant", str(ctx.exception))
        self.assertNotIn(code, str(ctx.exception))
        self.assertNotIn("Trace ID", str(ctx.exception))

    def test_refresh_rotates_and_persists_the_new_token(self):
        cfg = self.cfg()
        store = TokenStore(cfg.data_dir / "token.json")
        first = self.mock.seed_refresh_token("seed-1")
        store.save(first)
        tokens = TokenManager(cfg, store)
        tokens.bearer()
        second = store.load()["refresh_token"]
        self.assertNotEqual(first, second)
        self.assertNotIn(first, self.mock.valid_refresh, "the server rotated it")
        tokens.bearer(force=True)
        self.assertNotEqual(store.load()["refresh_token"], second)

    def test_new_refresh_token_is_saved_before_anything_else_uses_it(self):
        cfg = self.cfg()
        store = TokenStore(cfg.data_dir / "token.json")
        store.save(self.mock.seed_refresh_token("seed-2"))
        tokens = TokenManager(cfg, store)

        def explode(_tokens):
            raise RuntimeError("crash right after the server answered")

        tokens._adopt = explode  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            tokens.refresh()
        saved = store.load()["refresh_token"]
        self.assertIn(saved, self.mock.valid_refresh, "the rotated token must already be on disk")
        self.assertNotEqual(saved, "seed-2")

    def test_id_token_is_used_when_no_access_token_is_issued(self):
        self.mock.id_token_only = True
        svc = self.service()
        self.assertEqual(len(svc.api.list_appliances()), 1)
        self.mock.id_token_only = False
        self.mock.revoke_all_tokens()
        self.sign_in(svc.cfg)
        svc.tokens.invalidate()
        self.assertEqual(len(svc.api.list_appliances()), 1)

    def test_seed_token_from_env_is_adopted_into_the_store(self):
        seed = self.mock.seed_refresh_token("env-seed")
        cfg = self.cfg(BW_REFRESH_TOKEN=seed)
        store = TokenStore(cfg.data_dir / "token.json")
        self.assertIsNone(store.load())
        TokenManager(cfg, store).bearer()
        self.assertTrue(store.load()["refresh_token"].startswith("refresh-token-"), "rotated token replaced the seed")
        # and on the next start the (stale) .env value is NOT used again
        TokenManager(cfg, store).bearer()

    def test_no_credentials_is_a_clear_auth_error(self):
        cfg = self.cfg()
        with self.assertRaises(AuthError) as ctx:
            TokenManager(cfg, TokenStore(cfg.data_dir / "token.json")).bearer()
        self.assertIn("login", str(ctx.exception))
        self.assertEqual(self.mock.requests, [], "no request when there is nothing to send")

    def test_rejected_refresh_token_is_an_auth_error_without_secrets(self):
        cfg = self.cfg()
        store = TokenStore(cfg.data_dir / "token.json")
        store.save("revoked-refresh-token-value-12345")
        with self.assertRaises(AuthError) as ctx:
            TokenManager(cfg, store).bearer()
        message = str(ctx.exception)
        self.assertIn("rejected", message)
        self.assertNotIn("revoked-refresh-token-value-12345", message)

    def test_token_is_reused_until_nearly_expired_then_renewed(self):
        cfg = self.cfg()
        store = TokenStore(cfg.data_dir / "token.json")
        store.save(self.mock.seed_refresh_token())
        offset = [0.0]
        tokens = TokenManager(cfg, store, clock=lambda: time.time() + offset[0])
        tokens.bearer()
        tokens.bearer()
        self.assertEqual(len(self.mock.hits("/auth/token")), 1, "a fresh token is reused")
        offset[0] = 3000  # 600 s of the 3600 s left
        tokens.bearer()
        self.assertEqual(len(self.mock.hits("/auth/token")), 1)
        offset[0] = 3500  # 100 s left: inside the 120 s margin
        tokens.bearer()
        self.assertEqual(len(self.mock.hits("/auth/token")), 2, "a nearly-expired token is renewed first")

    def test_very_short_lived_tokens_do_not_cause_double_refreshes(self):
        self.mock.access_lifetime = 100
        svc = self.service()
        svc.api.list_appliances()
        self.assertEqual(len(self.mock.hits("/auth/token")), 1)


class ApiBehaviour(WaveTestCase):
    def test_401_triggers_one_refresh_and_retry(self):
        svc = self.service()
        svc.api.list_appliances()
        self.mock.valid_access.clear()  # server forgets our access token
        self.assertEqual(len(svc.api.list_appliances()), 1)
        self.assertEqual(len(self.mock.hits("/auth/token")), 2)

    def test_persistent_401_is_an_auth_error_not_a_loop(self):
        svc = self.service()
        svc.api.list_appliances()
        before = len(self.mock.requests)
        self.mock.api_queue.extend([(401, {"message": "no"}, {})] * 5)

        original = self.mock.handle_api

        def always_401(handler, rec):
            handler.reply(401, {"message": "Unauthorized"})

        self.mock.handle_api = always_401  # type: ignore[assignment]
        try:
            with self.assertRaises(AuthError):
                svc.api.list_appliances()
        finally:
            self.mock.handle_api = original  # type: ignore[assignment]
        self.assertLessEqual(len(self.mock.requests) - before, 3, "one try, one refresh, one retry - then stop")

    def test_server_errors_are_retried_a_limited_number_of_times(self):
        svc = self.service()
        svc.api.list_appliances()
        self.mock.api_queue.extend([(503, {"message": "busy"}, {})] * 10)
        with self.assertRaises(TransientError):
            svc.api.list_appliances()
        self.assertEqual(len(self.mock.api_hits("getApplianceList")), 1 + 3, "1 earlier + 3 attempts (1 try + 2 retries)")

    def test_a_flaky_error_followed_by_success_recovers(self):
        svc = self.service()
        self.mock.api_queue.append((502, {"message": "bad gateway"}, {}))
        self.assertEqual(len(svc.api.list_appliances()), 1)

    def test_rate_limit_is_not_retried_and_is_remembered(self):
        svc = self.service()
        svc.api.list_appliances()
        count = len(self.mock.api_hits("getApplianceList"))
        self.mock.api_queue.append((429, {"message": "slow down"}, {"Retry-After": "1800"}))
        with self.assertRaises(TransientError) as ctx:
            svc.api.list_appliances()
        self.assertEqual(len(self.mock.api_hits("getApplianceList")), count + 1, "no retry after a 429")
        self.assertEqual(ctx.exception.retry_after, 1800)
        self.assertEqual(svc.api.retry_after, 1800)

    def test_retry_after_is_bounded(self):
        svc = self.service()
        svc.api.list_appliances()
        self.mock.api_queue.append((429, {}, {"Retry-After": "999999"}))
        with self.assertRaises(TransientError):
            svc.api.list_appliances()
        self.assertEqual(svc.api.retry_after, 6 * 3600)

    def test_sign_in_server_rate_limit_is_remembered_too(self):
        cfg = self.cfg()
        store = TokenStore(cfg.data_dir / "token.json")
        store.save(self.mock.seed_refresh_token())
        self.mock.token_override = (429, {"error": "too_many_requests"}, {"Retry-After": "900"})
        tokens = TokenManager(cfg, store)
        with self.assertRaises(TransientError):
            tokens.bearer()
        self.assertEqual(tokens.retry_after, 900)

    def test_redirects_are_never_followed(self):
        svc = self.service()
        svc.api.list_appliances()
        self.mock.api_queue.append((302, "", {"Location": "https://evil.example/steal"}))
        with self.assertRaises(ApiError):
            svc.api.list_appliances()
        self.assertEqual({r["headers"].get("Host", "").split(":")[0] for r in self.mock.requests}, {"127.0.0.1"})

    def test_the_token_is_never_sent_to_another_host(self):
        from bwwatch.config import RequestSpec

        svc = self.service()
        spec = RequestSpec.parse("GET https://evil.example/wave/getNotifications", label="x")
        with self.assertRaises(ApiError) as ctx:
            svc.api.call(spec)
        self.assertIn("refusing", str(ctx.exception))

    def test_placeholders_are_url_encoded(self):
        svc = self.service()
        svc.api.call(svc.cfg.status_request, {"mac": MAC})
        hit = self.mock.api_hits("getApplianceStatus")[0]
        self.assertEqual(hit["query"]["macAddress"], MAC)
        self.assertEqual(hit["headers"]["User-Agent"], "Dart/3.8 (dart:io)")
        self.assertTrue(hit["headers"]["Authorization"].startswith("Bearer "))

    def test_missing_account_id_is_explained(self):
        from bwwatch.config import RequestSpec

        svc = self.service()
        svc.api.tokens.account_id = None
        svc.api.tokens._bearer = "x.y.z"
        svc.api.tokens._expires_at = time.time() + 1000
        with self.assertRaises(ApiError) as ctx:
            svc.api.call(RequestSpec.parse("GET /wave/getNotifications?username={account_id}", label="x"))
        self.assertIn("BW_ACCOUNT_ID", str(ctx.exception))

    def test_post_with_json_body(self):
        from bwwatch.config import RequestSpec

        svc = self.service()
        self.mock.extra_routes["getEnergyUsage"] = (200, [{"x": 1}])
        spec = RequestSpec.parse('POST /wave/getEnergyUsage {"mac_address": "{mac}", "view_type": "weekly"}', label="x")
        self.assertEqual(svc.api.call(spec, {"mac": MAC}), [{"x": 1}])
        body = json.loads(self.mock.api_hits("getEnergyUsage")[0]["body"])
        self.assertEqual(body, {"mac_address": MAC, "view_type": "weekly"})

    def test_network_failure_is_transient_and_hides_the_url_when_labelled(self):
        with self.assertRaises(TransientError) as ctx:
            http_request("GET", "http://127.0.0.1:9/secret-webhook-id", timeout=2, label="Home Assistant webhook")
        self.assertIn("Home Assistant webhook", str(ctx.exception))
        self.assertNotIn("secret-webhook-id", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
