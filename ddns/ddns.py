#!/usr/bin/env python3
"""panel-ddns — keep the NameSilo apex A record pointed at this host's public IPv4.

The default manages the apex only: *.pietrasik.top is a CNAME → the apex
(one-time manual record), so it follows the A record automatically and must
NOT get its own A (a CNAME owns its name).

Runs as user 'panel' from a systemd timer (stdlib only, no root, outbound
only). Reuses NAMESILO_API_KEY via the unit's EnvironmentFile
(/opt/panel/.panel-ddns.env, derived from /opt/panel/.env) — the same key
Traefik uses for DNS-01; the key is never logged.

Purely declarative, no state: every tick it detects the public IPv4 (OpenDNS
resolver first, then HTTPS fallbacks), lists the domain's records, updates
every stale A record and creates missing ones. CNAMEs at a configured host
are respected and skipped. Whatever drifted is fixed within one tick — a
"nothing changed, skip" short-circuit was deliberately rejected: it can
silently trust a stale assumption, for ~6 tiny API calls/hour saved. TTL is
pinned at 3600 (NameSilo's minimum), so worst-case cutover ≈ timer interval
+ 1h of resolver caching.

The domain is fixed at pietrasik.top and hardcoded: every other
domain-aware piece of this project is pinned to it, so an env override here
could only desync the A record from those routes.

Env: DDNS_HOSTS (default "@" — the apex; set "@,*" only if the wildcard is
ever an A record).
"""

from __future__ import annotations

import http.client
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

DOMAIN = "pietrasik.top"  # fixed, not an env knob — see module docstring
HOSTS = [h.strip() for h in os.environ.get("DDNS_HOSTS", "@").split(",") if h.strip()]
TTL = 3600  # NameSilo's minimum TTL
API = "https://www.namesilo.com/api"

IPV4_RE = re.compile(r"^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$")


def log(msg: str) -> None:
    print(datetime.now(timezone.utc).isoformat(timespec="seconds") + " panel-ddns: " + msg, flush=True)


def die(msg: str) -> None:
    log("FATAL: " + msg)
    sys.exit(1)


def public_ip() -> str:
    """Current public IPv4. OpenDNS resolver first (no web service involved)."""
    try:
        out = subprocess.run(
            ["dig", "-4", "+short", "myip.opendns.com", "@resolver1.opendns.com"],
            capture_output=True, text=True, timeout=15,
        ).stdout.split()
        for candidate in out:
            if IPV4_RE.match(candidate):
                return candidate
    except (OSError, subprocess.TimeoutExpired):
        pass  # dig missing or resolver unreachable — fall through to HTTPS
    for url in ("https://api.ipify.org", "https://ipv4.icanhazip.com"):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                ip = r.read().decode().strip()
            if IPV4_RE.match(ip):
                return ip
        except OSError:
            continue
    die("could not determine public IPv4 (dig and all HTTPS fallbacks failed)")


def api(key: str, op: str, **params: object) -> ET.Element:
    """Call a NameSilo API op, return its <reply> element. Exits on any error.
    Only exception types/reasons are surfaced — never the request URL (which
    carries the API key)."""
    q = {"version": "1", "type": "xml", "key": key, "domain": DOMAIN}
    q.update({k: v for k, v in params.items() if v is not None})
    url = f"{API}/{op}?" + urllib.parse.urlencode(q)  # contains the key — never logged
    req = urllib.request.Request(url, headers={"User-Agent": "panel-ddns/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            root = ET.parse(r).getroot()
    except ET.ParseError:
        die(f"{op}: response was not well-formed XML")
    except http.client.HTTPException as e:
        # Wire-level HTTP errors from the socket read (BadStatusLine,
        # RemoteDisconnected, …). A body truncated mid-XML usually surfaces
        # as the ParseError above.
        die(f"{op}: malformed HTTP response ({type(e).__name__})")
    except OSError as e:
        # Structured error text, never str(e): an HTTPError's reason is a
        # server-controlled status phrase; a connection error's is the
        # underlying socket/SSL/gai exception.
        status = getattr(e, "code", None)
        reason = getattr(e, "reason", None)
        reason = str(reason) if isinstance(reason, BaseException) else ""
        detail = (f"HTTP {status}" if status is not None
                  else f"network error ({reason or type(e).__name__})")
        die(f"{op}: request failed ({detail})")
    reply = root.find("reply")
    if reply is None:
        die(f"{op}: malformed response (no <reply> element)")
    code = (reply.findtext("code") or "").strip()
    detail = (reply.findtext("detail") or "").strip()
    if code != "300":
        die(f"{op}: API error {code}: {detail}")
    log(f"{op}: ok ({detail})")
    return reply


def rel_host(host_field: str) -> str:
    """Normalize a record's host field to the relative form we configure.

    NameSilo shows hosts as 'name.domain.tld' (bare domain for '@'); be
    tolerant of relative display too — matching on the wrong form would make
    the reconciler create duplicates.
    """
    h = (host_field or "").strip().lower()
    if h in ("", "@", DOMAIN.lower()):
        return ""
    suffix = "." + DOMAIN.lower()
    if h.endswith(suffix):
        return h[: -len(suffix)]
    return h


def full_host(h: str) -> str:
    return DOMAIN if h == "@" else f"{h}.{DOMAIN}"


def reconcile(key: str, ip: str) -> int:
    """Make the A records match `ip`. Returns the number of changes made."""
    reply = api(key, "dnsListRecords")
    records = reply.findall("resource_record")
    changes = 0
    for h in HOSTS:
        want_rel = "" if h == "@" else h
        cname = [
            rr for rr in records
            if (rr.findtext("type") or "").strip().upper() == "CNAME"
            and rel_host(rr.findtext("host") or "") == want_rel
        ]
        if cname:
            # A CNAME owns the name (RFC 1034) — adding an A here would either
            # fail or break the alias. Log the target and leave it alone.
            log(f"skipping {full_host(h)}: CNAME -> "
                f"{(cname[0].findtext('value') or '?').strip()} (not managed as A)")
            continue
        mine = [
            rr for rr in records
            if (rr.findtext("type") or "").strip().upper() == "A"
            and rel_host(rr.findtext("host") or "") == want_rel
        ]
        # Apex: send rrhost=@ for the root domain. The NameSilo docs are
        # silent on apex handling; @ is what ddns-go's provider sends on both
        # add and update — the best-attested form.
        rrhost = want_rel or "@"
        if not mine:
            # rrtype is a documented dnsAddRecord parameter; pin it explicitly
            # rather than rely on a default.
            api(key, "dnsAddRecord", rrtype="A", rrhost=rrhost, rrvalue=ip, rrttl=str(TTL))
            log(f"created A {full_host(h)} -> {ip}")
            changes += 1
            continue
        for rr in mine:
            rrid = (rr.findtext("record_id") or "").strip()
            value = (rr.findtext("value") or "").strip()
            if value == ip:
                continue
            if not rrid:
                log(f"WARNING: A {full_host(h)}: record without record_id, skipping")
                continue
            api(key, "dnsUpdateRecord", rrid=rrid, rrhost=rrhost, rrvalue=ip, rrttl=str(TTL))
            log(f"updated A {full_host(h)} ({rrid}): {value or '?'} -> {ip}")
            changes += 1
    return changes


def main() -> int:
    key = os.environ.get("NAMESILO_API_KEY", "").strip()
    if not key:
        die("NAMESILO_API_KEY missing (systemd unit reads it from /opt/panel/.panel-ddns.env — re-derive with: make ddns)")
    ip = public_ip()
    changes = reconcile(key, ip)
    log(f"done: {DOMAIN} ({', '.join(HOSTS)}) -> {ip}"
        + ("" if changes else " (already in sync)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
