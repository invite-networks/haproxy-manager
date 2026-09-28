#!/usr/bin/env python3
"""DNS alias validation, and the Route 53 wrap that puts a CNAME back.

    HAM_DATA_DIR=/tmp/x python3 tools/test-dnsalias.py

Route 53, DNS and acme.sh are all replaced, so nothing here reaches the
network. The rules under test: every domain gets a --domain-alias, a
wildcard shares its base name's; a missing or wrong CNAME stops issuance
before anything changes; a CNAME at the alias name is saved to disk before
it is removed, the TTL is waited out, and the name is put back exactly as it
was whether acme.sh succeeded or not; a restore that fails keeps the saved
copy and the next run repairs it; a name left cleared by a crash comes back
before acme.sh runs again; and only Route 53 may move a CNAME.
"""

import copy
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("HAM_DATA_DIR", tempfile.mkdtemp(prefix="ham-alias-"))
os.environ["HAM_DRY_RUN"] = "1"

import ham

ham  # noqa: E402
from ham import acme, dnsalias, validate  # noqa: E402
from ham.config import load_config, save_config  # noqa: E402

fails = []


def ok(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        fails.append(msg)


# ---- the fakes -------------------------------------------------------------


class FakeRoute53:
    """One public zone, validation.net, holding whatever `records` says."""

    def __init__(self):
        self.records = {}  # name. -> [rrset]
        self.batches = []  # every change batch, in order
        self.fail_changes = 0  # refuse this many change calls

    def list_hosted_zones_by_name(self, DNSName, MaxItems):
        zones = [
            {"Id": "/hostedzone/Z1", "Name": "validation.net.", "Config": {"PrivateZone": False}}
        ]
        return {"HostedZones": [z for z in zones if z["Name"] == DNSName]}

    def list_resource_record_sets(self, HostedZoneId, StartRecordName, MaxItems):
        return {"ResourceRecordSets": copy.deepcopy(self.records.get(StartRecordName, []))}

    def change_resource_record_sets(self, HostedZoneId, ChangeBatch):
        if self.fail_changes:
            self.fail_changes -= 1
            raise RuntimeError("Throttling: Rate exceeded")
        changes = ChangeBatch["Changes"]
        self.batches.append(copy.deepcopy(changes))
        for c in changes:
            rr = c["ResourceRecordSet"]
            have = self.records.setdefault(rr["Name"], [])
            if c["Action"] == "DELETE":
                have.remove(rr)
            else:
                have.append(copy.deepcopy(rr))
        return {"ChangeInfo": {"Id": "/change/C%d" % len(self.batches), "Status": "PENDING"}}

    def get_change(self, Id):
        return {"ChangeInfo": {"Id": Id, "Status": "INSYNC"}}


r53 = FakeRoute53()
dnsalias.route53 = lambda env: dnsalias.Route53(env, client=r53)

published = {}  # the CNAMEs the domain owners have published


def fake_cname_of(name):
    return published.get(name, "")


dnsalias.cname_of = fake_cname_of

slept = []
dnsalias._sleep = lambda s: slept.append(s)

acme_calls = []
acme_rc = [0]


def fake_acme_run(args, env_extra=None):
    """acme.sh as dns_aws drives it: a TXT at each alias name, gone again after."""
    acme_calls.append(
        {
            "args": list(args),
            "env": dict(env_extra or {}),
            "cname_present": {n: [r["Type"] for r in rrs] for n, rrs in r53.records.items()},
        }
    )
    if "--issue" in args:
        # dns_aws leaves a TXT behind now and then; the restore must clear it.
        for i, a in enumerate(args):
            if a == "--domain-alias":
                name = args[i + 1] + "."
                txt = {
                    "Name": name,
                    "Type": "TXT",
                    "TTL": 60,
                    "ResourceRecords": [{"Value": '"challenge-token"'}],
                }
                if txt not in r53.records.setdefault(name, []):
                    r53.records[name].append(txt)
    return acme_rc[0], "acme.sh ran"


acme.acme_run = fake_acme_run
acme.deploy_cert = lambda cfg, cert: {"ok": True, "log": "deployed"}
acme.ensure_account = lambda acc: (0, "account ok")


def cname_rr(name, target, ttl=60):
    return {"Name": name + ".", "Type": "CNAME", "TTL": ttl, "ResourceRecords": [{"Value": target}]}


def reset(cnames=None):
    r53.records.clear()
    r53.batches.clear()
    r53.fail_changes = 0
    published.clear()
    slept.clear()
    acme_calls.clear()
    acme_rc[0] = 0
    dnsalias.PENDING_PATH.unlink(missing_ok=True)
    for name, target in (cnames or {}).items():
        r53.records[name + "."] = [cname_rr(name, target)]


# ---- a configuration to issue from ---------------------------------------

cfg = load_config()
cfg["acme"]["accounts"] = [
    {"id": "acc", "name": "le", "email": "ops@example.com", "ca": "letsencrypt_test"}
]
cfg["acme"]["challenges"] = [
    {
        "id": "r53",
        "name": "route53",
        "method": "dns01",
        "dns_provider": "dns_aws",
        "dns_credentials": "AWS_ACCESS_KEY_ID=AKIA1\nAWS_SECRET_ACCESS_KEY=secret",
        "dns_alias_zone": "Validation.NET.",
    },
    {
        "id": "cf",
        "name": "cloudflare",
        "method": "dns01",
        "dns_provider": "dns_cf",
        "dns_credentials": "CF_Token=t",
        "dns_alias_zone": "validation.net",
    },
    {
        "id": "plain",
        "name": "plain",
        "method": "dns01",
        "dns_provider": "dns_aws",
        "dns_credentials": "AWS_ACCESS_KEY_ID=AKIA1",
    },
]
cert = {
    "id": "c1",
    "name": "domain",
    "domains": "domain.com *.domain.com www.other.org",
    "account": "acc",
    "challenge": "r53",
    "key_type": "ec-256",
    "auto_renew": True,
}
cfg["acme"]["certificates"] = [cert]
save_config(cfg)
cfg = load_config()
cert = cfg["acme"]["certificates"][0]
ch = cfg["acme"]["challenges"][0]

GOOD = {
    "_acme-challenge.domain.com": "domain.com.validation.net",
    "_acme-challenge.www.other.org": "www.other.org.validation.net",
}

# ---- names and arguments --------------------------------------------------
print("names")
ok(dnsalias.alias_zone(ch) == "validation.net", "the zone is normalised: case and trailing dot")
ok(dnsalias.alias_zone(dict(ch, method="http01")) == "", "an HTTP-01 challenge type never aliases")
doms = ["domain.com", "*.domain.com", "www.other.org"]
ok(
    dnsalias.acme_args(doms, "validation.net")
    == [
        "-d",
        "domain.com",
        "--domain-alias",
        "domain.com.validation.net",
        "-d",
        "*.domain.com",
        "--domain-alias",
        "domain.com.validation.net",
        "-d",
        "www.other.org",
        "--domain-alias",
        "www.other.org.validation.net",
    ],
    "every domain gets its own --domain-alias, and a wildcard shares its base name's",
)
ok(
    dnsalias.alias_names(doms, "validation.net")
    == ["domain.com.validation.net", "www.other.org.validation.net"],
    "the names to clear are distinct",
)
ok(
    dnsalias.required_cnames(doms, "validation.net") == list(GOOD.items()),
    "each domain's owner needs one CNAME, the wildcard none of its own",
)

# ---- validation of the setting --------------------------------------------
print("the setting")
for bad in ("validation net", "https://validation.net", "-bad.net", "net"):
    try:
        validate.check_challenge({"method": "dns01", "dns_alias_zone": bad})
        ok(False, "refuses %r" % bad)
    except ValueError:
        ok(True, "refuses %r" % bad)
validate.check_challenge({"method": "dns01", "dns_alias_zone": "validation.net."})
ok(True, "takes validation.net. with its trailing dot")
try:
    validate.check_challenge({"method": "http01", "dns_alias_zone": "validation.net"})
    ok(False, "refuses an alias zone on HTTP-01")
except ValueError:
    ok(True, "refuses an alias zone on HTTP-01")

# ---- the CNAME check ------------------------------------------------------
print("the CNAME check")
reset()
published.update(
    {
        "_acme-challenge.domain.com": "domain.com.validation.net",
        "_acme-challenge.www.other.org": "elsewhere.example",
    }
)
res = acme._acme_issue(cfg, cert)
ok(not res["ok"] and "CNAME is missing or wrong" in res["error"], "a wrong CNAME stops issuance")
ok(
    "_acme-challenge.www.other.org is a CNAME to elsewhere.example; it must point to "
    "www.other.org.validation.net" in res["log"],
    "and the log names the record and what it must be",
)
ok(not acme_calls and not r53.batches, "before acme.sh runs or anything in Route 53 changes")
published.pop("_acme-challenge.www.other.org")
res = acme._acme_issue(cfg, cert)
ok("_acme-challenge.www.other.org has no CNAME" in res["log"], "a missing CNAME is named too")

# ---- the Route 53 wrap ----------------------------------------------------
print("clearing and restoring a CNAME")
reset({"domain.com.validation.net": "domain-com.elsewhere.example"})
published.update(GOOD)
before = copy.deepcopy(r53.records)
res = acme._acme_issue(cfg, cert)
ok(res["ok"], "issuance succeeds")
issue = [c for c in acme_calls if "--issue" in c["args"]]
ok(len(issue) == 1, "acme.sh ran once")
ok(
    "CNAME" not in issue[0]["cname_present"].get("domain.com.validation.net.", []),
    "the CNAME was gone while acme.sh ran",
)
ok(issue[0]["env"].get("AWS_ACCESS_KEY_ID") == "AKIA1", "acme.sh got the Route 53 credentials")
ok(
    r53.batches[0]
    == [
        {
            "Action": "DELETE",
            "ResourceRecordSet": cname_rr(
                "domain.com.validation.net", "domain-com.elsewhere.example"
            ),
        }
    ],
    "the first change removes only the CNAME",
)
ok(60 + dnsalias.TTL_MARGIN in slept, "the CNAME's TTL is waited out before acme.sh runs")
last = r53.batches[-1]
ok(
    [c["Action"] for c in last] == ["DELETE", "CREATE"]
    and last[0]["ResourceRecordSet"]["Type"] == "TXT"
    and last[1]["ResourceRecordSet"]["Type"] == "CNAME",
    "the restore removes the leftover TXT and brings the CNAME back in one batch",
)
ok(
    r53.records["domain.com.validation.net."] == before["domain.com.validation.net."],
    "the name ends exactly as it started",
)
ok(not dnsalias.PENDING_PATH.exists(), "and the saved copy is gone")
ok("restored domain.com.validation.net" in res["log"], "the issue log says so")
ok(
    not [
        n
        for n, rrs in r53.records.items()
        if n.startswith("www.other.org")
        for r in rrs
        if r["Type"] == "CNAME"
    ]
    and len(r53.batches) == 2,
    "a name with no CNAME is left to acme.sh: no save, no change",
)

print("acme.sh fails")
reset({"domain.com.validation.net": "domain-com.elsewhere.example"})
published.update(GOOD)
before = copy.deepcopy(r53.records)
acme_rc[0] = 1
res = acme._acme_issue(cfg, cert)
ok(not res["ok"], "the issuance fails")
ok(
    r53.records["domain.com.validation.net."] == before["domain.com.validation.net."]
    and not dnsalias.PENDING_PATH.exists(),
    "and the CNAME is still put back",
)

print("the saved copy on disk")
reset({"domain.com.validation.net": "domain-com.elsewhere.example"})
published.update(GOOD)
seen_on_disk = []
real_change = dnsalias.Route53.change


def spy_change(self, zone_id, changes, comment):
    if changes[0]["Action"] == "DELETE" and changes[0]["ResourceRecordSet"]["Type"] == "CNAME":
        seen_on_disk.append(json.loads(dnsalias.PENDING_PATH.read_text()))
    return real_change(self, zone_id, changes, comment)


dnsalias.Route53.change = spy_change
acme._acme_issue(cfg, cert)
dnsalias.Route53.change = real_change
ok(
    seen_on_disk and seen_on_disk[0][0]["records"][0]["Type"] == "CNAME",
    "the CNAME is on disk before it is removed",
)

print("a restore that fails")
reset({"domain.com.validation.net": "domain-com.elsewhere.example"})
published.update(GOOD)
before = copy.deepcopy(r53.records)
real_records_at = dnsalias.Route53.records_at


def flaky_records_at(self, zone_id, name):
    # The save works; once acme.sh has run, the restore's read does not.
    if any("--issue" in c["args"] for c in acme_calls):
        raise RuntimeError("Throttling: Rate exceeded")
    return real_records_at(self, zone_id, name)


dnsalias.Route53.records_at = flaky_records_at
res = acme._acme_issue(cfg, cert)
dnsalias.Route53.records_at = real_records_at
ok("could NOT restore domain.com.validation.net" in res["log"], "the log says the restore failed")
ok(dnsalias.PENDING_PATH.exists(), "the saved copy is kept")
failed = acme.restore_dns_aliases(load_config())
ok(
    not failed
    and r53.records["domain.com.validation.net."] == before["domain.com.validation.net."]
    and not dnsalias.PENDING_PATH.exists(),
    "and the next round puts the name back",
)

print("after a crash")
reset({"domain.com.validation.net": "domain-com.elsewhere.example"})
published.update(GOOD)
before = copy.deepcopy(r53.records)
# What a node that died mid-issuance leaves behind: the CNAME saved, then
# removed, and nothing restored.
saved = copy.deepcopy(r53.records["domain.com.validation.net."])
dnsalias._save_pending(
    [
        {
            "name": "domain.com.validation.net",
            "zone_id": "Z1",
            "records": saved,
            "challenge": "r53",
            "since": "2026-09-28T00:00:00+00:00",
        }
    ]
)
r53.records["domain.com.validation.net."] = []
acme_calls.clear()
res = acme._acme_issue(cfg, cert)
ok(
    res["ok"] and r53.records["domain.com.validation.net."] == before["domain.com.validation.net."],
    "the name is restored and the issuance still works",
)
first_issue = next(i for i, b in enumerate(r53.batches) if b[0]["Action"] == "DELETE")
ok(
    r53.batches[0][0]["Action"] == "CREATE" and first_issue > 0,
    "the leftover is restored before this run clears anything",
)

print("only Route 53 moves a CNAME")
reset()
published.update(GOOD)
published["domain.com.validation.net"] = "domain-com.elsewhere.example"
res = acme._acme_issue(cfg, dict(cert, challenge="cf"))
ok(
    not res["ok"] and "only Route 53 (dns_aws) can move a CNAME" in res["log"],
    "another provider with a CNAME at the alias name is refused",
)
ok(not [c for c in acme_calls if "--issue" in c["args"]], "without running acme.sh")
published.pop("domain.com.validation.net")
res = acme._acme_issue(cfg, dict(cert, challenge="cf"))
ok(res["ok"], "another provider is fine when the alias name is free")

print("without an alias zone")
reset()
res = acme._acme_issue(cfg, dict(cert, challenge="plain"))
args = next(c["args"] for c in acme_calls if "--issue" in c["args"])
ok(
    res["ok"] and "--domain-alias" not in args and not r53.batches and not slept,
    "nothing changes for a challenge type without one",
)

# ---- the API --------------------------------------------------------------
print("the API")
cur = load_config()
cur["local"]["api_key"] = "alias-key"
save_config(cur)
client = ham.app.test_client()
H = {"X-API-Key": "alias-key"}
reset()
published.update({"_acme-challenge.domain.com": "domain.com.validation.net"})
r = client.get("/api/acme/cnames/c1", headers=H).get_json()
ok(
    r["ok"] and r["zone"] == "validation.net" and [x["ok"] for x in r["records"]] == [True, False],
    "/api/acme/cnames lists each record and whether it is in place",
)
cur = load_config()
cur["acme"]["certificates"][0]["challenge"] = "plain"
save_config(cur)
r = client.get("/api/acme/cnames/c1", headers=H).get_json()
ok(not r["ok"], "and says so when the certificate does not alias")
r = client.post(
    "/api/acme/challenges",
    headers=H,
    json={"name": "bad", "method": "dns01", "dns_alias_zone": "not a zone"},
)
ok(r.status_code == 400, "saving a challenge type refuses a bad alias zone")

print()
if fails:
    print("%d failed" % len(fails))
    sys.exit(1)
print("all passed")
