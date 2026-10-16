"""
Self-test for api_client.py (the API Client fallback for the bearer token) -
stdlib-only, run it directly:

    python selftest_api_client.py

No real HTTP: urllib's urlopen is replaced by a fake for each test, and
every file lives in a temp dir, so nothing touches RUN/api_clients.json.

What's checked is what would go wrong QUIETLY:
  - the secret never comes back out of get_client_summary (the UI reads it);
  - saving with a blank secret keeps the stored one (editing the URI must
    not wipe it);
  - a non-https Token URI is refused - the secret would go out in the clear;
  - the token request is client_credentials with the id/secret as Basic auth;
  - a minted token is reused until close to expiry, then minted again;
  - the server's own error text reaches the caller on a failed exchange;
  - webui.api_client_token refuses to hand out a token when the Target
    Tenant ID is not the preset's - the token decides the tenant, silently.
"""

import base64
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse

import api_client

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


URI = "https://tenant-abc.auth.eu-west-1.amazoncognito.com/oauth2/token"


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class _FakeUrlopen:
    """Records each request; answers with the next queued body or error."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _FakeResponse(json.dumps(answer).encode("utf-8"))


def _with_fake(fake, fn):
    original = api_client.urllib.request.urlopen
    api_client.urllib.request.urlopen = fake
    try:
        return fn()
    finally:
        api_client.urllib.request.urlopen = original


def _clients_file():
    api_client._cache.clear()
    return os.path.join(tempfile.mkdtemp(), "api_clients.json")


def test_secret_never_leaves_and_blank_keeps_it():
    f = _clients_file()
    api_client.save_client("Dev", "client-1", "s3cret", URI, clients_file=f)
    summary = api_client.get_client_summary("Dev", clients_file=f)
    check("s3cret" not in json.dumps(summary), "the secret must never be in the summary the UI gets")
    check(summary["configured"] and summary["hasSecret"], f"expected configured+hasSecret, got {summary}")

    api_client.save_client("Dev", "client-2", "", URI, clients_file=f)
    with open(f, encoding="utf-8") as fh:
        stored = json.load(fh)["Dev"]
    check(stored["clientSecret"] == "s3cret" and stored["clientId"] == "client-2",
          f"a blank secret on save must keep the stored one, got {stored}")


def test_refusals():
    f = _clients_file()
    for args, why in ((("", "id", "s", URI), "no preset"),
                      (("Dev", "", "s", URI), "no client id"),
                      (("Dev", "id", "", URI), "no secret, none stored"),
                      (("Dev", "id", "s", "http://example.com/oauth2/token"), "plain http")):
        try:
            api_client.save_client(*args, clients_file=f)
            failures.append("save_client should refuse: " + why)
        except ValueError:
            pass
    check(api_client.token_uri_warning(URI) is None, "the confirmed Cognito shape must not warn")
    check(api_client.token_uri_warning("https://example.com/token") is not None,
          "an unfamiliar Token URI shape should warn")
    missing = api_client.mint_token("Nobody", clients_file=f)
    check(not missing["ok"] and "No API Client" in missing["error"], f"unexpected: {missing}")


def test_token_request_shape_and_caching():
    f = _clients_file()
    api_client.save_client("Dev", "client-1", "s3cret", URI, clients_file=f)
    fake = _FakeUrlopen({"access_token": "tok-1", "expires_in": 3600, "token_type": "Bearer"},
                        {"access_token": "tok-2", "expires_in": 3600, "token_type": "Bearer"})

    first = _with_fake(fake, lambda: api_client.mint_token("Dev", clients_file=f))
    check(first["ok"] and first["token"] == "tok-1" and not first["cached"], f"unexpected first mint: {first}")
    req = fake.requests[0]
    check(req.full_url == URI and req.get_method() == "POST", "must POST to the stored Token URI")
    check(urllib.parse.parse_qs(req.data.decode("ascii")) == {"grant_type": ["client_credentials"]},
          f"body must be grant_type=client_credentials, got {req.data!r}")
    expected = "Basic " + base64.b64encode(b"client-1:s3cret").decode("ascii")
    check(req.get_header("Authorization") == expected, "id/secret must go as HTTP Basic auth")

    second = _with_fake(fake, lambda: api_client.mint_token("Dev", clients_file=f))
    check(second["cached"] and second["token"] == "tok-1" and len(fake.requests) == 1,
          "a still-good token must be reused, not re-minted")

    api_client._cache["Dev"]["expiresAt"] = 0   # pretend it expired
    third = _with_fake(fake, lambda: api_client.mint_token("Dev", clients_file=f))
    check(third["token"] == "tok-2" and len(fake.requests) == 2, "an expired token must be minted again")


def test_failed_exchange_keeps_server_text():
    f = _clients_file()
    api_client.save_client("Dev", "client-1", "wrong", URI, clients_file=f)
    error = urllib.error.HTTPError(URI, 400, "Bad Request", {}, io.BytesIO(b'{"error":"invalid_client"}'))
    result = _with_fake(_FakeUrlopen(error), lambda: api_client.mint_token("Dev", clients_file=f))
    check(not result["ok"] and "invalid_client" in result["error"] and "400" in result["error"],
          f"the Token URI's own error must reach the caller, got {result}")


def test_webui_refuses_another_tenants_ids():
    import webui

    original_load, original_mint = webui.load_tenants, api_client.mint_token
    webui.load_tenants = lambda: {"Dev": {"tenantId": "TENANT-" + "a" * 32}}
    api_client.mint_token = lambda preset: {"ok": True, "token": "tok", "expiresAt": 1}
    try:
        refused = webui.api_client_token("Dev", "TENANT-" + "b" * 32)
        allowed = webui.api_client_token("Dev", "TENANT-" + "a" * 32)
        unknown = webui.api_client_token("Nope", "")
    finally:
        webui.load_tenants, api_client.mint_token = original_load, original_mint
    check(not refused["ok"] and "different tenant" in refused["error"],
          f"another tenant's ids must be refused, got {refused}")
    check(allowed["ok"] and allowed["token"] == "tok", f"the preset's own tenant must be allowed, got {allowed}")
    check(not unknown["ok"], "an unsaved preset must be refused")


def main():
    tests = (
        test_secret_never_leaves_and_blank_keeps_it,
        test_refusals,
        test_token_request_shape_and_caching,
        test_failed_exchange_keeps_server_text,
        test_webui_refuses_another_tenants_ids,
    )
    for test in tests:
        try:
            test()
        except Exception as error:
            failures.append(f"{test.__name__} raised: {type(error).__name__}: {error}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"All {len(tests)} api_client self-tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
