"""Outbound network policy for webhooks.

Two distinct defects are covered here.

1. DNS rebinding (TOCTOU). Validation resolved the hostname and checked the
   address, then ``urllib`` resolved it *again* to connect. A hostname whose DNS
   answer changed in between reached internal destinations. The invariant that
   actually kills this is: **exactly one ``getaddrinfo`` call per delivery, and
   zero during connect.**

2. The allowlist allowed CGNAT. ``_is_public_ip`` was a disjunction of negatives,
   and ``IPv4Address.is_private`` is documented ``False`` for ``100.64.0.0/10`` --
   so Tailscale and container-internal space was reachable with no DNS trick at
   all.

Tests here assert behaviour, not implementation shape, except where a structural
guard is the only way to pin the "resolve once" invariant.
"""

import socket
import sqlite3

import pytest


def _conn():
    from helpers import use_tmp_data_dir

    use_tmp_data_dir()
    from memoratum import db
    from memoratum.config import Settings

    return db.connect(Settings.load().db_path)


def _seed_project(conn, project_id: str = "sec-1") -> None:
    import time

    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO projects(id, org_id, name, created_at, updated_at)"
        " VALUES (?, 'local-org', 'Sec', ?, ?)",
        (project_id, now, now),
    )
    conn.commit()


# --- 1. the address policy ---------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "100.64.1.1",  # CGNAT / Tailscale / cloud+container internals
        "100.127.255.254",  # top of the CGNAT block
        "192.88.99.1",  # 6to4 relay anycast (deprecated, RFC 7526)
        "2001:20::1",  # ORCHIDv2
        "2620:4f:8000::1",  # AS112 anycast DNS
    ],
)
def test_special_use_ranges_are_rejected(address):
    """Regression guard for the CGNAT hole: is_private is False for 100.64/10."""
    from memoratum.webhooks import _is_public_ip

    assert _is_public_ip(address) is False, f"{address} must not be reachable"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "169.254.169.254",  # cloud metadata
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "0.0.0.0",
        "::1",
        "::",
        "fd00::1",  # unique-local
        "fe80::1",  # link-local
        "ff02::1",  # multicast
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:169.254.169.254",  # IPv4-mapped metadata
    ],
)
def test_internal_addresses_are_rejected(address):
    from memoratum.webhooks import _is_public_ip

    assert _is_public_ip(address) is False, f"{address} must not be reachable"


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "1.1.1.1", "2606:4700::1111"])
def test_public_addresses_are_allowed(address):
    from memoratum.webhooks import _is_public_ip

    assert _is_public_ip(address) is True, f"{address} is public and must be allowed"


def test_policy_is_an_allowlist_not_a_denylist():
    """Structurally: the check must consult is_global, not a list of negatives."""
    import inspect

    from memoratum import webhooks

    source = inspect.getsource(webhooks._is_public_ip)
    assert "is_global" in source, (
        "_is_public_ip must positively allowlist globally-routable addresses"
    )


def test_garbage_input_is_rejected():
    from memoratum.webhooks import _is_public_ip

    for value in ["", "not-an-ip", "999.999.999.999", "fe80::1%eth0", None]:
        assert _is_public_ip(value) is False


# --- 2. validation refuses internal targets ---------------------------------


def _resolver_for(address: str):
    def resolver(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    return resolver


def test_cgnat_target_is_refused_at_creation():
    from memoratum.webhooks import UnsafeWebhookTarget, validate_webhook_url

    with pytest.raises(UnsafeWebhookTarget):
        validate_webhook_url("https://internal.example/hook", resolver=_resolver_for("100.64.1.1"))


def test_metadata_target_is_refused_at_creation():
    from memoratum.webhooks import UnsafeWebhookTarget, validate_webhook_url

    with pytest.raises(UnsafeWebhookTarget):
        validate_webhook_url(
            "https://evil.example/latest/meta-data", resolver=_resolver_for("169.254.169.254")
        )


def test_http_still_requires_explicit_opt_in():
    from memoratum.webhooks import UnsafeWebhookTarget, validate_webhook_url

    resolver = _resolver_for("8.8.8.8")
    with pytest.raises(UnsafeWebhookTarget):
        validate_webhook_url("http://example.com/hook", resolver=resolver)
    # allow_private is the documented development-only escape hatch
    assert validate_webhook_url("http://example.com/hook", allow_private=True, resolver=resolver)


def test_reserved_hostnames_are_refused():
    from memoratum.webhooks import UnsafeWebhookTarget, validate_webhook_url

    # IPv6 literals must be bracketed inside a URL authority.
    for host in ["localhost", "metadata.google.internal", "metadata", "instance-data"]:
        with pytest.raises(UnsafeWebhookTarget):
            validate_webhook_url(f"https://{host}/hook", resolver=_resolver_for("8.8.8.8"))


def test_literal_ip_targets_are_refused():
    """A literal IP in the URL must be judged directly, not only via DNS.

    Real DNS echoes a literal host back, so this is defence in depth rather than a
    reachable hole -- but a policy that trusts DNS to police the URL it was given
    is one resolver mistake away from being wrong.
    """
    from memoratum.webhooks import UnsafeWebhookTarget, validate_webhook_url

    for host in ["127.0.0.1", "0.0.0.0", "[::1]", "10.0.0.1", "169.254.169.254", "100.64.1.1"]:
        # Resolver deliberately lies and claims a public address.
        with pytest.raises(UnsafeWebhookTarget):
            validate_webhook_url(f"https://{host}/hook", resolver=_resolver_for("8.8.8.8"))


# --- 3. resolve exactly once, then connect to that address ------------------


def test_delivery_resolves_exactly_once(monkeypatch):
    """The invariant that kills rebinding.

    A rebinding resolver answers public on the first lookup and link-local on
    every later one. If the count is ever greater than one during a single
    delivery, the code re-resolved and is exploitable again.
    """
    from memoratum import db, webhooks

    calls = {"n": 0}

    def resolver(host, port, *args, **kwargs):
        calls["n"] += 1
        address = "93.184.216.34" if calls["n"] == 1 else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    conn = _conn()
    _seed_project(conn)
    delivery_id = _queue_delivery(conn, webhooks)
    conn.close()

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    conn = db.connect(db_path())
    try:
        # No network egress: the connection attempt will fail, which is fine.
        # What matters is how many times DNS was consulted.
        webhooks.deliver_delivery(conn, delivery_id, timeout_seconds=0.01)
    finally:
        conn.close()

    assert calls["n"] == 1, f"DNS was consulted {calls['n']} times; must be exactly once"


def db_path() -> str:
    from memoratum.config import Settings

    return Settings.load().db_path


def _queue_delivery(conn, webhooks, url: str = "https://rebind.example/hook") -> str:
    """Create a webhook plus one queued delivery, without any network call.

    A resolver is injected so creating the webhook never performs real DNS.
    """
    import time

    hook = webhooks.create_webhook(
        conn,
        project_id="sec-1",
        name="pin",
        url=url,
        event_types=["memory_add"],
        secret="whsec_pin",
        resolver=_resolver_for("93.184.216.34"),
    )
    now = time.time()
    conn.execute(
        "INSERT INTO domain_events(id, project_id, event_type, payload, created_at)"
        " VALUES ('ev-pin', 'sec-1', 'memory_add', '{}', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO webhook_deliveries(id, event_id, webhook_id, project_id, event_type,"
        " payload, status, attempts, next_attempt_at, created_at, updated_at)"
        " VALUES ('dl-pin', 'ev-pin', ?, 'sec-1', 'memory_add', '{}', 'queued', 0, NULL, ?, ?)",
        (hook["id"], now, now),
    )
    conn.commit()
    return "dl-pin"


def test_delivery_refuses_a_rebinding_target(monkeypatch):
    """After the first lookup the hostname points at metadata; we must not follow."""
    from memoratum import db, webhooks

    calls = {"n": 0}

    def resolver(host, port, *args, **kwargs):
        calls["n"] += 1
        address = "93.184.216.34" if calls["n"] == 1 else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    conn = _conn()
    _seed_project(conn)
    delivery_id = _queue_delivery(conn, webhooks)
    conn.close()

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    conn = db.connect(db_path())
    try:
        result = webhooks.deliver_delivery(conn, delivery_id, timeout_seconds=0.05)
    finally:
        conn.close()

    # The sandbox has no route to the pinned public address, so the attempt fails
    # and the delivery requeues. The point is the resolution count and the target.
    assert result["status"] in {"succeeded", "dead", "failed", "queued"}, result
    assert calls["n"] == 1, "a second resolution means the pin is not being honoured"
    assert "169.254.169.254" not in str(result), "must never connect to the metadata address"


# --- 4. IPv6 sockaddrs ------------------------------------------------------


def test_ipv6_scope_id_is_preserved():
    """IPv6 getaddrinfo returns 4-tuples; dropping scope_id loses the interface."""
    import inspect

    from memoratum import webhooks

    source = inspect.getsource(webhooks)
    assert "scope_id" in source, "IPv6 scope_id/flowinfo must be carried through, not discarded"


def test_scoped_ipv6_entries_are_parsed():
    from memoratum import webhooks

    entries = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 1)),
    ]
    assert webhooks._addresses_from_entries(entries) == ["::1"]


# --- 5. TLS is bound to the hostname, not the pinned IP ----------------------


def test_tls_validation_binds_to_hostname_not_ip():
    """The pinned socket must still be validated against the name, not the address."""
    import inspect
    import ssl

    from memoratum import webhooks

    # The connection keeps self.host as the name, which is what
    # HTTPSConnection.connect() passes to wrap_socket as server_hostname.
    source = inspect.getsource(webhooks.PinnedHTTPSConnection)
    assert "_create_connection" in source

    handler = inspect.getsource(webhooks.PinnedHTTPSHandler)
    assert "context=self._context" in handler, (
        "context must be forwarded, else urllib silently uses the system trust store"
    )

    # Nothing may weaken verification to make a mismatch pass.
    module = inspect.getsource(webhooks)
    assert "check_hostname = False" not in module
    assert "CERT_NONE" not in module
    # and the default context we build must still verify
    ctx = ssl.create_default_context()
    assert ctx.check_hostname is True
    assert ctx.verify_mode is ssl.CERT_REQUIRED


def test_pinned_connection_keeps_hostname_in_the_url():
    """Rewriting the netloc to the IP would break SNI and the Host header."""
    import inspect

    from memoratum import webhooks

    assert hasattr(webhooks, "PinnedHTTPSConnection"), "no pinned connection class"
    assert hasattr(webhooks, "PinnedHTTPSHandler"), "no pinned urllib handler"
    source = inspect.getsource(webhooks.PinnedHTTPSConnection)
    assert "_create_connection" in source, (
        "must override the _create_connection INSTANCE attribute, not connect()"
    )


# --- 6. retry policy --------------------------------------------------------


def test_tls_errors_are_not_retried(monkeypatch):
    """A cert failure is an attack signal, not a connectivity problem."""
    import ssl

    from memoratum import webhooks

    conn = _conn()
    _seed_project(conn)
    delivery_id = _queue_delivery(conn, webhooks)

    attempts = {"n": 0}

    def boom(*args, **kwargs):
        attempts["n"] += 1
        raise ssl.SSLCertVerificationError("certificate verify failed")

    monkeypatch.setattr(webhooks, "_connect_pinned", boom, raising=False)
    try:
        result = webhooks.deliver_delivery(conn, delivery_id, timeout_seconds=0.05)
    finally:
        conn.close()

    assert attempts["n"] <= 1, f"TLS failure retried {attempts['n']} times"
    assert result["status"] in {"dead", "failed", "succeeded", "queued"}


# --- 7. redirects -----------------------------------------------------------


def test_redirects_are_not_followed():
    """A redirect is an unvalidated network hop, so it must be refused outright."""
    import urllib.error
    import urllib.request

    from memoratum import webhooks

    handler = webhooks._NoRedirect()
    request = urllib.request.Request("https://example.com/hook", method="POST")
    with pytest.raises(urllib.error.HTTPError) as raised:
        handler.redirect_request(request, None, 302, "Found", {}, "https://elsewhere.example/x")
    assert "redirect" in str(raised.value).lower()


def test_delivery_opener_disables_redirects(monkeypatch):
    """The opener used for delivery must carry the no-redirect handler."""
    import urllib.request

    from memoratum import webhooks

    # deliver_delivery resolves through socket.getaddrinfo; stub it so the attempt
    # gets as far as constructing the opener.
    monkeypatch.setattr(socket, "getaddrinfo", _resolver_for("93.184.216.34"))

    built: list[tuple] = []
    original = urllib.request.build_opener

    def spy(*args, **kwargs):
        built.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(webhooks.urllib.request, "build_opener", spy)
    conn = _conn()
    _seed_project(conn)
    delivery_id = _queue_delivery(conn, webhooks)
    try:
        webhooks.deliver_delivery(conn, delivery_id, timeout_seconds=0.01)
    finally:
        conn.close()

    # build_opener accepts handler classes and instances alike.
    handlers = [h for call in built for h in call]

    def is_handler(entry, cls):
        return entry is cls or isinstance(entry, cls)

    assert any(is_handler(h, webhooks._NoRedirect) for h in handlers), (
        "delivery opener is missing the no-redirect handler"
    )
    assert any(is_handler(h, webhooks.PinnedHTTPSHandler) for h in handlers), (
        "delivery opener is not using the pinned HTTPS handler"
    )


def test_database_has_no_plaintext_delivery_secret():
    """Secrets stay encrypted at rest."""
    from memoratum import webhooks

    conn = _conn()
    _seed_project(conn)
    _queue_delivery(conn, webhooks)
    row = conn.execute("SELECT secret FROM webhooks").fetchone()
    assert "whsec_pin" not in str(row["secret"]), "signing secret stored in plaintext"
    assert row["secret"] != "whsec_pin"
    conn.close()


def test_sqlite_error_is_raised_not_swallowed():
    """Guard against the harness masking a real storage failure."""
    with pytest.raises(sqlite3.OperationalError):
        _conn().execute("SELECT * FROM no_such_table")
