#!/usr/bin/env python3
"""panel-ddns — keep the NameSilo apex A record pointed at this host's public IPv4.

The default manages the apex only: *.pietrasik.top is a CNAME → the apex
(one-time manual record), so it follows the A record automatically and must
NOT get its own A — a CNAME owns its name and no other record can coexist
with it.

Runs as user 'panel' from a systemd timer (stdlib only, no venv, no root,
outbound network only). Reuses NAMESILO_API_KEY via the unit's EnvironmentFile
(/opt/panel/.env) — the same key Traefik uses for the DNS-01 challenge. No new
secrets, and the key is never logged (URLs are built, never printed).

Purely declarative, no state: every tick detects the public IPv4 (OpenDNS
resolver first — plain DNS, no third-party web service — then HTTPS
fallbacks), lists the domain's records, updates every stale A record and
creates missing ones. CNAMEs at a configured host are respected and skipped
(never overwritten, never shadowed by a new A). Whatever drifted — a changed
IP, a record edited or deleted by hand, an IP that flipped and flipped back
between ticks — is fixed within one tick. A "nothing changed, skip" short-circuit was deliberately
rejected: it can silently trust a stale assumption, and the API load it saves
(~6 tiny list calls/hour) is not worth having a stale-assumption failure mode.
TTL is pinned at 3600 (NameSilo's minimum), so worst-case cutover after an IP
change ≈ timer interval + 1h of resolver caching.

Env: DDNS_DOMAIN (default pietrasik.top), DDNS_HOSTS (default "@" — the apex;
set "@,*" only if the wildcard is ever an A record).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

DOMAIN = os.environ.get("DDNS_DOMAIN", "pietrasik.top")
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
            ["dig", "+short", "myip.opendns.com", "@resolver1.opendns.com"],
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
    """Call a NameSilo API op, return its <reply> element. Exits on any error."""
    q = {"version": "1", "type": "xml", "key": key, "domain": DOMAIN}
    q.update({k: v for k, v in params.items() if v is not None})
    url = f"{API}/{op}?" + urllib.parse.urlencode(q)  # contains the key — never logged
    req = urllib.request.Request(url, headers={"User-Agent": "panel-ddns/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            root = ET.parse(r).getroot()
    except (OSError, ET.ParseError) as e:
        die(f"{op}: request failed: {e}")
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

    NameSilo shows hosts as 'name.domain.tld' (bare domain for '@'), but be
    tolerant of relative display too — matching on the wrong form would make
    the reconciler think a record is missing and create duplicates.
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
        if not mine:
            api(key, "dnsAddRecord", rrhost=want_rel, rrvalue=ip, rrttl=str(TTL))
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
            api(key, "dnsRecordUpdate", rrid=rrid, rrhost=want_rel, rrvalue=ip, rrttl=str(TTL))
            log(f"updated A {full_host(h)} ({rrid}): {value or '?'} -> {ip}")
            changes += 1
    return changes


def main() -> int:
    key = os.environ.get("NAMESILO_API_KEY", "").strip()
    if not key:
        die("NAMESILO_API_KEY missing (systemd unit reads it from /opt/panel/.env)")
    ip = public_ip()
    changes = reconcile(key, ip)
    log(f"done: {DOMAIN} ({', '.join(HOSTS)}) -> {ip}"
        + ("" if changes else " (already in sync)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
