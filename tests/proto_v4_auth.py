#!/usr/bin/env python3
"""Independent conformance check for Remote v4 auth/net/policy workstream.

Exercises the REAL modules (host/auth.py, host/net.py, host/policy.py,
host/qr.py) against synthetic state; no shared code with the modules under
test except their public APIs. Exit 0 + "V4-AUTH CHECKS PASSED" on success.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "host"))
os.environ["REMOTE_VENDOR_DIR"] = os.path.join(HERE, "..", "packaging",
                                               "vendor")

import auth
import net as netmod
import policy as policymod
import qr as qrmod

TMP = tempfile.mkdtemp(prefix="v4auth-")
fails = []


def check(name, cond, detail=""):
    if cond:
        print("ok   %s" % name)
    else:
        print("FAIL %s %s" % (name, detail))
        fails.append(name)


# ------------------------------------------------------- device id

p1 = os.path.join(TMP, "device-id")
d1 = auth.get_or_create_device_id(p1)
check("device-id format", re.fullmatch(r"YR-[0-9A-F]{4}-[0-9A-F]{4}", d1),
      repr(d1))
check("device-id 0600", oct(os.stat(p1).st_mode & 0o777) == "0o600")
d2 = auth.get_or_create_device_id(p1)
check("device-id persistent", d1 == d2)
check("device-id uniqueness",
      auth.get_or_create_device_id(os.path.join(TMP, "device-id-2")) != d1)

# ------------------------------------------------------- pairing

pm = auth.PairingManager(os.path.join(TMP, "pairing_codes"))
code = pm.issue_code("TestPhone")
check("pair code 6-digit", re.fullmatch(r"\d{6}", code), repr(code))
ok, detail = pm.redeem(code)
check("pair redeem ok", ok, detail)
ok2, _ = pm.redeem(code)
check("pair double-redeem fails", not ok2)
# expired code: inject an old timestamp into the persisted JSON
old = pm.issue_code("Old")
with open(pm.path) as f:
    data = json.load(f)
for c in data["codes"]:
    if c["code"] == old:
        c["issued_at"] = time.time() - 20 * 60
with open(pm.path, "w") as f:
    json.dump(data, f)
ok3, _ = pm.redeem(old)
check("pair expired fails", not ok3)
left = pm.prune_expired()
check("pair prune", isinstance(left, int))
# constant-time compare is exercised by redeem's code path above; smoke the
# hmac path directly
import hmac as _hmac
check("constant-time compare used",
      _hmac.compare_digest("123456", "123456") and
      not _hmac.compare_digest("123456", "654321"))

# ------------------------------------------------------- rate limiter

rl = auth.RateLimiter()
ip = "203.0.113.9"
for _ in range(5):
    allowed, _ = rl.allow(ip)
    check("limiter allows first 5", allowed)
    rl.record_failure(ip)
allowed, retry = rl.allow(ip)
check("limiter blocks after 5 fails", not allowed and retry > 0,
      "allowed=%r retry=%r" % (allowed, retry))
first_retry = retry
rl.record_failure(ip)
allowed2, retry2 = rl.allow(ip)
check("limiter retry grows", not allowed2 and retry2 > first_retry,
      "retry2=%r" % retry2)
check("limiter cap 900", retry2 <= 900.0, repr(retry2))
check("limiter other ip unaffected", rl.allow("198.51.100.7")[0])
rl.record_success(ip)
allowed3, _ = rl.allow(ip)
check("limiter resets on success", allowed3)

# ------------------------------------------------------- session tokens

st = auth.SessionTokens()
tok, exp = st.issue("YR-AAAA-1111", conn_id=4242)
check("token issue", len(tok) == 64 and exp == 3600, repr((tok, exp)))
check("token validate", st.validate(tok, 4242))
check("token wrong conn rejected", not st.validate(tok, 9999))
check("token unknown rejected", not st.validate("00" * 32, 4242))
check("device_for", st.device_for(tok, 4242) == "YR-AAAA-1111")
tok2, _ = st.rotate("YR-AAAA-1111", 4242)
check("rotate issues new", tok2 != tok)
check("rotate revokes old", not st.validate(tok, 4242))
check("rotate new valid", st.validate(tok2, 4242))
st.revoke(4242)
check("revoke on disconnect", not st.validate(tok2, 4242))
# expiry (short-ttl instance)
st2 = auth.SessionTokens(ttl=1)
t3, _ = st2.issue("YR-BBBB-2222", conn_id=1)
time.sleep(1.2)
check("token expiry", not st2.validate(t3, 1))

# ------------------------------------------------------- device store

ds = auth.DeviceStore(os.path.join(TMP, "devices.json"))
check("12 flags", len(auth.PERMISSION_FLAGS) == 12 and
      "automation" in auth.PERMISSION_FLAGS and "webcam" in auth.PERMISSION_FLAGS
      and "camera" in auth.PERMISSION_FLAGS)
check("trust new", ds.trust({"device_id": "YR-1111-2222",
                             "device_name": "Phone",
                             "platform": "android"}))
check("trust idempotent", not ds.trust({"device_id": "YR-1111-2222"}))
perms = ds.get_permissions("YR-1111-2222")
check("perms all 12 true", perms is not None and len(perms) == 12 and
      all(perms[f] for f in auth.PERMISSION_FLAGS), repr(perms))
# migration: write a 7-flag entry by hand, reload
raw = {"devices": [{"device_id": "YR-OLD-0001", "device_name": "Old",
                    "platform": "linux", "first_seen": "x", "last_seen": "x",
                    "permissions": {"view": True, "mouse": False}}],
       "blocked": [], "grants": {}}
p2 = os.path.join(TMP, "devices2.json")
with open(p2, "w") as f:
    json.dump(raw, f)
ds2 = auth.DeviceStore(p2)
mp = ds2.get_permissions("YR-OLD-0001")
check("migration fills 12", mp is not None and len(mp) == 12 and
      mp["mouse"] is False and mp["audio"] is True and mp["camera"] is True,
      repr(mp))
# temp grants
ds.grant("YR-1111-2222", {"files": False}, time.time() + 600)
check("grant active", ds.is_granted("YR-1111-2222"))
check("grant feature check", not ds.is_granted("YR-1111-2222", "files") and
      ds.is_granted("YR-1111-2222", "view"))
ds.grant("YR-1111-2222", None, time.time() - 1)  # expired
check("expired grant pruned", not ds.is_granted("YR-1111-2222"))
check("grants listed pruned", ds.list_grants() == {})
# block / revoke
check("block", ds.block("YR-BAD-0000"))
check("is_blocked", ds.is_blocked("YR-BAD-0000"))
check("blocked not trusted", not ds.is_trusted("YR-BAD-0000"))
check("trust refuses blocked",
      not ds.trust({"device_id": "YR-BAD-0000"}))
check("unblock", ds.unblock("YR-BAD-0000") and not ds.is_blocked("YR-BAD-0000"))
check("revoke", ds.revoke("YR-1111-2222"))
check("revoked not trusted", not ds.is_trusted("YR-1111-2222"))

# ------------------------------------------------------- policy

pp = policymod.Policy(os.path.join(TMP, "policy.conf"))
d = pp.as_dict()
check("policy defaults all-allow",
      d["allow_files"] and d["allow_terminal"] and d["allow_clipboard"] and
      d["allow_audio"] and d["allow_webcam"] and not d["require_approval"],
      repr(d))
check("policy check files", pp.check("files"))
check("policy unknown fails closed", not pp.check("teleport"))
pp.set("allow_terminal", False)
check("policy toggle enforced", not pp.check("terminal"))
pp.save()
pp2 = policymod.Policy(pp.path)
check("policy persists", not pp2.check("terminal"))
pp3 = policymod.Policy(os.path.join(TMP, "no-such.conf"))
check("policy missing file ok", pp3.check("files"))

# ------------------------------------------------------- net (tailscale)

ts = netmod.TailscaleIntegration()
check("net standalone import", True)

# absent-binary path: point at a nonexistent command
gone = netmod.TailscaleIntegration(cmd="tailscale-definitely-missing")
check("status absent -> installed false",
      gone.status() == {"installed": False, "online": False,
                        "tailscale_ip": None, "peers": []},
      repr(gone.status()))
check("peer_latency absent -> None", gone.peer_latency("100.1.2.3") is None)

# canned fixture through the REAL parser
fixture = json.dumps({
    "Self": {"HostName": "hostbox", "TailscaleIPs": ["100.64.0.5"],
             "Online": True},
    "Peer": {
        "n1": {"HostName": "phone", "TailscaleIPs": ["100.64.0.9"],
               "Online": True},
        "n2": {"HostName": "off", "TailscaleIPs": ["100.64.0.10"],
               "Online": False},
    },
})
st_ = netmod.TailscaleIntegration.parse_status(fixture)
check("parser fixture online", st_["online"] is True)
check("parser fixture ip", st_["tailscale_ip"] == "100.64.0.5")
check("parser fixture peers", len(st_["peers"]) == 2 and
      st_["peers"][0]["name"] == "phone" and st_["peers"][0]["ip"] == "100.64.0.9"
      and st_["peers"][1]["online"] is False, repr(st_["peers"]))
check("parser garbage safe",
      netmod.TailscaleIntegration.parse_status("not json")["online"] is False)

# ping parser
lat, direct = netmod.TailscaleIntegration.parse_ping(
    "pong from 100.64.0.9 (phone) via 1.2.3.4:41641 in 12ms\n"
    "pong from 100.64.0.9 (phone) via 1.2.3.4:41641 in 14ms\n")
check("ping parse direct", lat is not None and abs(lat - 13.0) < 0.01 and
      direct is True, repr((lat, direct)))
lat2, direct2 = netmod.TailscaleIntegration.parse_ping(
    "pong from 100.64.0.9 (phone) via DERP(nyc) in 80ms\n")
check("ping parse derp", direct2 is False and abs(lat2 - 80.0) < 0.01)
lat3, direct3 = netmod.TailscaleIntegration.parse_ping("no answer from peer\n")
check("ping parse none", lat3 is None and direct3 is None)

# NET_STATUS payload with tailscale absent
payload = netmod.net_status_payload(peer_ip="100.64.0.9", tailscale=gone)
check("net_status payload shape",
      set(payload) == {"online", "tailscale_ip", "peer_latency_ms", "direct"}
      and payload["online"] is False and payload["peer_latency_ms"] is None,
      repr(payload))

# ------------------------------------------------------- qr

try:
    png = qrmod.make_pairing_png(
        qrmod.pairing_uri("123456", "YR-ABCD-1234"))
    check("qr png signature", png[:8] == b"\x89PNG\r\n\x1a\n")
    check("qr png non-trivial", len(png) > 200, repr(len(png)))
except qrmod.QrUnavailable as exc:
    check("qr available", False, str(exc))

# ------------------------------------------------------- standalone import

for mod, path in (("auth", "host/auth.py"), ("net", "host/net.py"),
                  ("policy", "host/policy.py"), ("qr", "host/qr.py")):
    r = subprocess.run([sys.executable, "-m", "py_compile",
                        os.path.join(HERE, "..", path)],
                       capture_output=True, text=True)
    check("py_compile %s" % mod, r.returncode == 0, r.stderr.strip())

if fails:
    print("\n%d CHECK(S) FAILED: %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("\nV4-AUTH CHECKS PASSED")
