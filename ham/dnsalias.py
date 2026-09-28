"""DNS alias validation: every domain proven at <domain>.<alias zone>.

A challenge type with an alias zone (say validation.net) validates each
domain of a certificate somewhere other than the domain's own zone. The
domain's owner points _acme-challenge.domain.com at domain.com.validation.net
with a CNAME, once, and acme.sh writes the TXT record there with
--domain-alias. The DNS credentials are then for validation.net only.

The alias name may already be a CNAME of its own, pointing somewhere else,
and a CNAME cannot share its name with the TXT record the challenge needs.
With Route 53 the whole issuance is wrapped: the records at the name are
saved to disk, the CNAME is removed, acme.sh runs, and the name is put back
exactly as it was, whether the issuance worked or not. The saved copy stays
on disk until the name is back, so a crash in between is repaired on the
next issuance or renewal round instead of losing the CNAME.
"""

from datetime import datetime
from datetime import timezone
import json
import os
import threading
import time

from .base import DATA_DIR, log

PENDING_PATH = DATA_DIR / "dns-alias-pending.json"
# One aliased issuance at a time: two certificates can share an alias name
# (domain.com and *.domain.com), and interleaved save/restore would lose it.
lock = threading.Lock()
# How long a resolver may keep the old CNAME. Waited out after removing it,
# so the CA follows the chain to our TXT rather than to a cached target.
MAX_TTL_WAIT = 600
TTL_MARGIN = 5
CHANGE_TIMEOUT = 180

# Replaced by the tests.
_sleep = time.sleep


# --------------------------------------------------------------------------
# names


def alias_zone(ch):
    """The challenge type's alias zone, normalised, or "" when it has none."""
    if not ch or ch.get("method") != "dns01":
        return ""
    return (ch.get("dns_alias_zone") or "").strip().strip(".").lower()


def _base(domain):
    """*.domain.com is validated at the same name as domain.com."""
    d = domain.strip().rstrip(".").lower()
    return d[2:] if d.startswith("*.") else d


def alias_name(domain, zone):
    return "%s.%s" % (_base(domain), zone)


def alias_names(domains, zone):
    """The distinct names acme.sh will write to, in the certificate's order."""
    return list(dict.fromkeys(alias_name(d, zone) for d in domains))


def acme_args(domains, zone):
    """-d/--domain-alias pairs. acme.sh matches aliases to domains by
    position, so every domain gets one -- which is why aliasing is all or
    nothing for a certificate."""
    args = []
    for d in domains:
        args += ["-d", d, "--domain-alias", alias_name(d, zone)]
    return args


def required_cnames(domains, zone):
    """[(name, target)]: the CNAME each domain's owner has to publish."""
    return list(
        dict.fromkeys(("_acme-challenge." + _base(d), alias_name(d, zone)) for d in domains)
    )


# --------------------------------------------------------------------------
# the CNAME check


def _resolver():
    """dnspython, or None when it is not installed."""
    try:
        import dns.resolver
    except ImportError:
        return None
    return dns.resolver


def cname_of(name):
    """The CNAME target at name, "" when there is none.

    Raises LookupError when DNS itself did not answer, and RuntimeError when
    dnspython is missing. Replaced by the tests.
    """
    res = _resolver()
    if res is None:
        raise RuntimeError("python3-dnspython is not installed")
    import dns.exception

    try:
        ans = res.resolve(name, "CNAME", lifetime=10)
    except (res.NXDOMAIN, res.NoAnswer):
        return ""
    except dns.exception.DNSException as e:
        raise LookupError(str(e) or e.__class__.__name__)
    return str(ans[0].target).rstrip(".").lower()


def check_cnames(domains, zone):
    """[{name, target, found, ok, error}] for every CNAME the certificate needs."""
    out = []
    for name, target in required_cnames(domains, zone):
        row = {"name": name, "target": target, "found": "", "ok": False, "error": ""}
        try:
            row["found"] = cname_of(name)
            row["ok"] = row["found"] == target
        except (LookupError, RuntimeError) as e:
            row["error"] = str(e)
        out.append(row)
    return out


def cname_problems(rows):
    """The failed rows of check_cnames as sentences for the issue log."""
    lines = []
    for r in rows:
        if r["ok"]:
            continue
        if r["error"]:
            lines.append("%s could not be checked: %s" % (r["name"], r["error"]))
        elif r["found"]:
            lines.append(
                "%s is a CNAME to %s; it must point to %s" % (r["name"], r["found"], r["target"])
            )
        else:
            lines.append("%s has no CNAME; it must point to %s" % (r["name"], r["target"]))
    return lines


# --------------------------------------------------------------------------
# Route 53


class Route53:
    """The few Route 53 calls the wrap needs. client is replaced by the tests."""

    def __init__(self, env, client=None):
        if client is None:
            import boto3

            kw = {}
            if env.get("AWS_ACCESS_KEY_ID"):
                kw = {
                    "aws_access_key_id": env["AWS_ACCESS_KEY_ID"],
                    "aws_secret_access_key": env.get("AWS_SECRET_ACCESS_KEY", ""),
                    "aws_session_token": env.get("AWS_SESSION_TOKEN") or None,
                }
            # With no keys boto3 falls back to its own chain (an instance
            # role), the same as acme.sh's dns_aws does.
            client = boto3.client("route53", **kw)
        self.c = client

    def zone_for(self, name):
        """The public hosted zone holding name: the longest one that matches."""
        labels = name.rstrip(".").lower().split(".")
        for i in range(1, len(labels) - 1):
            cand = ".".join(labels[i:]) + "."
            r = self.c.list_hosted_zones_by_name(DNSName=cand, MaxItems="1")
            for z in r.get("HostedZones", []):
                if z["Name"].lower() == cand and not z.get("Config", {}).get("PrivateZone"):
                    return z["Id"].split("/")[-1]
        raise LookupError("no public Route 53 hosted zone holds %s" % name)

    def records_at(self, zone_id, name):
        """Every record set at exactly name."""
        fq = name.rstrip(".").lower() + "."
        r = self.c.list_resource_record_sets(
            HostedZoneId=zone_id, StartRecordName=fq, MaxItems="20"
        )
        return [rr for rr in r.get("ResourceRecordSets", []) if rr["Name"].lower() == fq]

    def change(self, zone_id, changes, comment):
        """Apply one batch -- all of it or none of it -- and wait until every
        Route 53 nameserver serves it."""
        r = self.c.change_resource_record_sets(
            HostedZoneId=zone_id, ChangeBatch={"Comment": comment[:256], "Changes": changes}
        )
        cid = r["ChangeInfo"]["Id"]
        deadline = time.time() + CHANGE_TIMEOUT
        while r["ChangeInfo"]["Status"] != "INSYNC":
            if time.time() > deadline:
                raise TimeoutError(
                    "Route 53 did not finish change %s within %ds" % (cid, CHANGE_TIMEOUT)
                )
            _sleep(3)
            r = self.c.get_change(Id=cid)


# Replaced by the tests.
def route53(env):
    return Route53(env)


# --------------------------------------------------------------------------
# what is saved while a name is cleared


def _load_pending():
    try:
        return json.loads(PENDING_PATH.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        log.error("could not read %s: %s", PENDING_PATH, e)
        return []


def _save_pending(entries):
    """Whole file, then renamed into place, like the configuration: a torn
    write here would lose the only copy of a record."""
    if not entries:
        PENDING_PATH.unlink(missing_ok=True)
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PENDING_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, PENDING_PATH)


def pending():
    return _load_pending()


# --------------------------------------------------------------------------
# the wrap


def clear_names(ch, env, names, trace):
    """Make each alias name able to take a TXT record.

    Returns "" when acme.sh may run, or why it may not. A name that is a
    CNAME is saved to disk, then cleared; restore() puts it back.
    """
    provider = ch.get("dns_provider", "")
    if provider != "dns_aws":
        # Only Route 53 can clear and restore a name. Elsewhere, refuse up
        # front rather than let acme.sh fail against a CNAME it cannot move.
        for name in names:
            try:
                found = cname_of(name)
            except (LookupError, RuntimeError):
                continue
            if found:
                return (
                    "%s is a CNAME to %s, and only Route 53 (dns_aws) can move a CNAME "
                    "out of the way and put it back" % (name, found)
                )
        return ""

    r53 = route53(env)
    longest = 0
    for name in names:
        zone_id = r53.zone_for(name)
        recs = r53.records_at(zone_id, name)
        cnames = [rr for rr in recs if rr["Type"] == "CNAME"]
        if not cnames:
            continue
        # Saved before anything changes: from here on the copy on disk is
        # the record, until restore() has put it back.
        entries = [e for e in _load_pending() if e["name"] != name]
        entries.append(
            {
                "name": name,
                "zone_id": zone_id,
                "records": recs,
                "challenge": ch.get("id", ""),
                "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        _save_pending(entries)
        target = (
            cnames[0]["ResourceRecords"][0]["Value"] if cnames[0].get("ResourceRecords") else ""
        )
        r53.change(
            zone_id,
            [{"Action": "DELETE", "ResourceRecordSet": rr} for rr in cnames],
            "haproxy-manager: clear %s for ACME validation" % name,
        )
        ttl = int(cnames[0].get("TTL") or 0)
        longest = max(longest, ttl)
        trace.append(
            "dns alias: saved and removed the CNAME %s -> %s (TTL %ds)" % (name, target, ttl)
        )
        log.info(
            "dns alias: removed the CNAME at %s for validation; it is saved in %s",
            name,
            PENDING_PATH,
        )
    if longest:
        wait = min(longest, MAX_TTL_WAIT) + TTL_MARGIN
        trace.append("dns alias: waiting %ds for cached copies of the CNAME to expire" % wait)
        _sleep(wait)
    return ""


def restore(env_for, trace, challenges):
    """Put every saved name back exactly as it was.

    env_for(challenge) gives the credentials, challenges maps id -> challenge
    type. Returns the names that could not be restored; they stay saved and
    are tried again on the next call.
    """
    entries = _load_pending()
    if not entries:
        return []
    failed = []
    for e in list(entries):
        name = e["name"]
        try:
            ch = challenges.get(e.get("challenge"))
            if not ch:
                raise LookupError(
                    "its challenge type no longer exists, so there are no "
                    "credentials to restore it with"
                )
            r53 = route53(env_for(ch))
            current = r53.records_at(e["zone_id"], name)
            saved = e["records"]
            # The name is made to match the copy exactly: whatever the
            # challenge left behind goes, whatever was saved comes back, in
            # one batch so the name is never half-restored.
            changes = [
                {"Action": "DELETE", "ResourceRecordSet": rr} for rr in current if rr not in saved
            ]
            changes += [
                {"Action": "CREATE", "ResourceRecordSet": rr} for rr in saved if rr not in current
            ]
            if changes:
                r53.change(
                    e["zone_id"],
                    changes,
                    "haproxy-manager: restore %s after ACME validation" % name,
                )
            entries = [x for x in entries if x["name"] != name]
            _save_pending(entries)
            trace.append("dns alias: restored %s" % name)
            log.info("dns alias: restored %s", name)
        except Exception as ex:
            failed.append(name)
            trace.append(
                "dns alias: could NOT restore %s: %s -- it stays saved in %s and is "
                "tried again on the next issuance or renewal round" % (name, ex, PENDING_PATH)
            )
            log.error("dns alias: could not restore %s: %s", name, ex)
    return failed
