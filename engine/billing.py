"""Stripe checkout. Secret lives on the data volume — never the browser or git."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_FILE = Path("/app/engine/data/stripe.env")
_LOCAL = Path.home() / ".credentials" / "nospeaky-stripe.env"


def _load() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in (_FILE, _LOCAL):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
        break
    for k in ("STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY", "STRIPE_PRICE_ID", "STRIPE_MODE"):
        env = os.environ.get(k, "").strip()
        if env:
            out[k] = env
    return out


def _secret() -> str:
    return _load().get("STRIPE_SECRET_KEY", "").strip()


def ready() -> bool:
    d = _load()
    sk = d.get("STRIPE_SECRET_KEY", "")
    price = d.get("STRIPE_PRICE_ID", "")
    return sk.startswith(("sk_test_", "sk_live_")) and price.startswith("price_")


def mode() -> str:
    d = _load()
    m = (d.get("STRIPE_MODE") or "").strip().lower()
    if m in {"test", "live"}:
        return m
    sk = d.get("STRIPE_SECRET_KEY", "")
    if sk.startswith("sk_live_"):
        return "live"
    if sk.startswith("sk_test_"):
        return "test"
    return "off"


def public() -> dict[str, Any]:
    return {
        "ready": ready(),
        "mode": mode() if ready() else "off",
        "live": ready() and mode() == "live",
    }


def _stripe(method: str, path: str, data: dict[str, str] | None = None) -> dict[str, Any]:
    sk = _secret()
    if not sk:
        raise RuntimeError("Stripe is not connected")
    body = urllib.parse.urlencode(data or {}).encode() if data else None
    req = urllib.request.Request(
        "https://api.stripe.com/v1" + path,
        data=body,
        method=method,
        headers={"Authorization": "Bearer " + sk},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw)
            msg = str(((err.get("error") or {}).get("message")) or raw)[:240]
        except json.JSONDecodeError:
            msg = raw[:240]
        raise RuntimeError(msg) from e


def create_checkout(success_url: str, cancel_url: str) -> str:
    if not ready():
        raise RuntimeError("Stripe is not connected")
    price = _load()["STRIPE_PRICE_ID"]
    sess = _stripe(
        "POST",
        "/checkout/sessions",
        {
            "mode": "subscription",
            "line_items[0][price]": price,
            "line_items[0][quantity]": "1",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "allow_promotion_codes": "true",
        },
    )
    url = str(sess.get("url") or "")
    if not url.startswith("https://"):
        raise RuntimeError("Stripe did not return a checkout link")
    return url


def confirm(session_id: str) -> dict[str, Any]:
    sid = (session_id or "").strip()
    if not sid.startswith("cs_"):
        raise RuntimeError("bad session")
    sess = _stripe("GET", "/checkout/sessions/" + urllib.parse.quote(sid, safe=""))
    paid = str(sess.get("payment_status") or "") == "paid" or str(sess.get("status") or "") == "complete"
    if not paid:
        raise RuntimeError("Payment not finished")
    token = _mint(sid)
    return {"ok": True, "skip_token": token, "mode": mode()}


def skip_ok(token: str | None) -> bool:
    raw = (token or "").strip()
    if not raw or "." not in raw:
        return False
    exp_s, sig = raw.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    sk = _secret()
    if not sk:
        return False
    need = hmac.new(sk.encode(), f"skip|{exp}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, need)


def _mint(_session_id: str) -> str:
    sk = _secret()
    exp = int(time.time()) + 30 * 24 * 3600
    sig = hmac.new(sk.encode(), f"skip|{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"
