#!/usr/bin/env python3
"""
Check Foxwoods booking results for complimentary / $0 rates on one or more stay dates.

Setup:
  pip install -r requirements.txt
  playwright install chromium

Authentication (for member rates and comp rooms):
  Create foxwoods_config.json (see foxwoods_config.example.json) next to this script,
  or set FOXWOODS_EMAIL / FOXWOODS_PASSWORD. Environment variables override the file.

  To pass the entire config as a single secret (e.g. GitHub Actions):
    FOXWOODS_CONFIG_B64  – full foxwoods_config.json contents, base64-encoded (recommended)
    FOXWOODS_CONFIG      – full foxwoods_config.json contents as a raw JSON string
  Individual overrides (FOXWOODS_EMAIL, FOXWOODS_PASSWORD, FOXWOODS_SMTP_PASSWORD)
  are always applied on top of whichever source was used.

Optional alerts when free rooms are found (only if free/comp rooms are listed):
  notify_email: inbox address. notify_sms_email: carrier SMS gateway address.
  notify_phone: E.164 mobile for Twilio, OR an SMS gateway email like 5551234567@vtext.com (SMTP).
  smtp: required for email or gateway texts. twilio: required for real SMS to notify_phone.
  Override SMTP password with env FOXWOODS_SMTP_PASSWORD.

Usage:
  python check_free_rooms.py 2026-04-15 2026-04-16 2026-04-17
  python check_free_rooms.py 2026-04-15 --nights 2 --headed
  python check_free_rooms.py 2026-04-15 --skip-login
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

LOGIN_URL = "https://www.foxwoods.com/login"
BOOKING_BASE = "https://www.foxwoods.com/booking/reserve"
DEFAULT_HOTELS = "GPT,GCH,TFT"
DEFAULT_CONFIG_NAME = "foxwoods_config.json"


def _config_paths(explicit: str | None) -> list[Path]:
    base = Path(__file__).resolve().parent
    if explicit:
        return [Path(explicit).expanduser().resolve()]
    return [base / DEFAULT_CONFIG_NAME, Path.cwd() / DEFAULT_CONFIG_NAME]


def _load_config(config_arg: str | None) -> dict[str, Any]:
    """Load JSON config; priority order (highest to lowest):
      1. FOXWOODS_CONFIG_B64  – full config as base64-encoded JSON (GitHub Secret friendly)
      2. FOXWOODS_CONFIG      – full config as a raw JSON string
      3. JSON file (--config PATH, or foxwoods_config.json next to script / in cwd)
      4. Individual FOXWOODS_EMAIL / FOXWOODS_PASSWORD / FOXWOODS_SMTP_PASSWORD overrides
    """
    raw: dict[str, Any] = {}

    # 1. Full config from base64 env var (highest priority — ideal for GitHub Secrets)
    config_b64 = os.environ.get("FOXWOODS_CONFIG_B64", "").strip()
    if config_b64:
        try:
            decoded = base64.b64decode(config_b64).decode("utf-8")
            loaded = json.loads(decoded)
        except Exception as e:
            raise SystemExit(f"Could not decode FOXWOODS_CONFIG_B64: {e}") from e
        if not isinstance(loaded, dict):
            raise SystemExit("FOXWOODS_CONFIG_B64 must decode to a JSON object.")
        raw = loaded

    # 2. Full config as raw JSON string env var
    elif os.environ.get("FOXWOODS_CONFIG", "").strip():
        try:
            loaded = json.loads(os.environ["FOXWOODS_CONFIG"])
        except json.JSONDecodeError as e:
            raise SystemExit(f"Could not parse FOXWOODS_CONFIG as JSON: {e}") from e
        if not isinstance(loaded, dict):
            raise SystemExit("FOXWOODS_CONFIG must be a JSON object.")
        raw = loaded

    # 3. JSON file on disk
    else:
        paths = _config_paths(config_arg)
        if config_arg and not paths[0].is_file():
            raise SystemExit(f"Config file not found: {paths[0]}")
        for path in paths:
            if not path.is_file():
                continue
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise SystemExit(f"Could not read config {path}: {e}") from e
            if not isinstance(loaded, dict):
                raise SystemExit(f"Config {path} must be a JSON object.")
            raw = loaded
            break

    # 4. Individual env var overrides (always applied on top of whatever was loaded)
    if os.environ.get("FOXWOODS_EMAIL", "").strip():
        raw["email"] = os.environ["FOXWOODS_EMAIL"].strip()
    if os.environ.get("FOXWOODS_PASSWORD"):
        raw["password"] = os.environ["FOXWOODS_PASSWORD"]
    if os.environ.get("FOXWOODS_SMTP_PASSWORD"):
        smtp = raw.get("smtp")
        if not isinstance(smtp, dict):
            smtp = {}
            raw["smtp"] = smtp
        smtp["password"] = os.environ["FOXWOODS_SMTP_PASSWORD"]

    return raw
    

def _build_notification_body(
    arrival: str,
    departure: str,
    nights: int,
    free_hits: list[str],
) -> str:
    lines = [
        f"Foxwoods: free/comp rooms reported for {arrival} -> {departure} ({nights} night(s)).",
        "",
        *free_hits,
    ]
    return "\n".join(lines)


def _send_smtp_email(
    smtp_cfg: dict[str, Any],
    recipients: list[str],
    subject: str,
    body: str,
) -> None:
    host = str(smtp_cfg.get("host", "")).strip()
    if not host:
        raise ValueError("smtp.host is required to send email or SMS gateway mail.")
    port = int(smtp_cfg.get("port", 587))
    user = str(smtp_cfg.get("user", "") or "").strip()
    pw = str(smtp_cfg.get("password", "") or "")
    if not user or not pw:
        raise ValueError("smtp.user and smtp.password are required.")
    from_addr = str(smtp_cfg.get("from") or user).strip()
    use_tls = bool(smtp_cfg.get("use_tls", True))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=45) as smtp:
        smtp.ehlo()
        if use_tls:
            smtp.starttls()
            smtp.ehlo()
        try:
            smtp.login(user, pw)
        except smtplib.SMTPAuthenticationError as e:
            code = getattr(e, "smtp_code", None)
            err = getattr(e, "smtp_error", b"") or b""
            err_b = err if isinstance(err, bytes) else str(err).encode("utf-8", errors="replace")
            if code == 534 or b"Application-specific password" in err_b:
                raise RuntimeError(
                    "Gmail SMTP needs an App Password in smtp.password (not your normal "
                    "Gmail password). Enable 2-Step Verification, then create one at "
                    "https://myaccount.google.com/apppasswords - use the 16-character code."
                ) from e
            raise
        smtp.send_message(msg)


def _smtp_error_message(e: BaseException) -> str:
    if isinstance(e, RuntimeError) and "Gmail SMTP needs an App Password" in str(e):
        return str(e)
    msg = str(e)
    if "534" in msg and ("Application-specific password" in msg or "InvalidSecondFactor" in msg):
        return (
            "Gmail requires an App Password for smtp.password (16 characters from "
            "https://myaccount.google.com/apppasswords ). "
            f"Raw error: {msg}"
        )
    return msg


def _send_twilio_sms(
    twilio_cfg: dict[str, Any],
    to_e164: str,
    body: str,
) -> None:
    sid = str(twilio_cfg.get("account_sid", "") or "").strip()
    token = str(twilio_cfg.get("auth_token", "") or "").strip()
    from_num = str(twilio_cfg.get("from", "") or "").strip()
    if not sid or not token or not from_num:
        raise ValueError("twilio.account_sid, twilio.auth_token, and twilio.from are required.")
    data = urllib.parse.urlencode(
        {"To": to_e164.strip(), "From": from_num, "Body": body[:1600]}
    ).encode("utf-8")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    req = urllib.request.Request(url, data=data, method="POST")
    cred = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {cred}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Twilio HTTP {e.code}: {detail}") from e


def _notify_free_rooms(
    cfg: dict[str, Any],
    arrival: str,
    departure: str,
    nights: int,
    free_hits: list[str],
) -> list[str]:
    """Send configured notifications; returns list of warning strings (non-fatal)."""
    warnings: list[str] = []
    subject = f"Foxwoods free rooms: {arrival} -> {departure}"
    body = _build_notification_body(arrival, departure, nights, free_hits)

    notify_email = str(cfg.get("notify_email", "") or "").strip()
    notify_sms_email = str(cfg.get("notify_sms_email", "") or "").strip()
    notify_phone = str(cfg.get("notify_phone", "") or "").strip()
    smtp_cfg = cfg.get("smtp")
    twilio_cfg = cfg.get("twilio")

    mail_targets: list[str] = []
    for a in (notify_email, notify_sms_email):
        if a:
            mail_targets.append(a)
    # SMS via carrier email gateway (e.g. 5551234567@vtext.com); same SMTP as email
    if notify_phone and "@" in notify_phone:
        mail_targets.append(notify_phone)
    mail_targets = list(dict.fromkeys(mail_targets))

    twilio_to = (
        notify_phone if (notify_phone and "@" not in notify_phone) else ""
    )
    if mail_targets:
        if not isinstance(smtp_cfg, dict):
            warnings.append(
                "notify_email or notify_sms_email is set but smtp is missing or invalid."
            )
        else:
            try:
                _send_smtp_email(smtp_cfg, mail_targets, subject, body)
            except Exception as e:
                warnings.append(f"SMTP notification failed: {_smtp_error_message(e)}")

    if twilio_to:
        if not isinstance(twilio_cfg, dict):
            warnings.append(
                "notify_phone (SMS number) is set but twilio config is missing or invalid."
            )
        else:
            try:
                _send_twilio_sms(twilio_cfg, twilio_to, body)
            except Exception as e:
                warnings.append(f"Twilio SMS failed: {e}")

    return warnings

FREE_AMOUNT_RE = re.compile(r"^\s*\$?\s*0(?:\.00)?\s*$", re.I)
FREE_KEYWORDS = re.compile(
    r"\b(comp|complimentary|free)\b",
    re.I,
)


def _parse_check_in(s: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    raise SystemExit(
        f"Invalid date {s!r}. Use YYYY-MM-DD or MM/DD/YYYY."
    )


def _maybe_dismiss_overlays(page: Any) -> None:
    for sel in (
        'button:has-text("Accept All")',
        'button:has-text("Accept")',
        "#truste-consent-button",
    ):
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=1200)
            loc.click(timeout=2000)
            break
        except Exception:
            pass


def _login(page: Any, email: str, password: str) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    _maybe_dismiss_overlays(page)
    page.locator("#edit-fox-user-login-email").wait_for(state="visible", timeout=30000)
    page.locator("#edit-fox-user-login-email").fill(email)
    page.locator("#edit-fox-user-login-password").fill(password)
    page.locator("#edit-fox-user-login-submit").click()
    # Do not use "networkidle" here: analytics and long-polling often prevent it from ever settling.
    page.wait_for_load_state("load", timeout=90000)
    path = urlparse(page.url).path.rstrip("/")
    if path.endswith("/login"):
        err = page.locator(".messages--error, .alert-danger, [role='alert']").first
        try:
            if err.is_visible(timeout=2000):
                raise SystemExit(f"Login failed: {err.inner_text()[:500].strip()}")
        except SystemExit:
            raise
        except Exception:
            pass
        raise SystemExit(
            "Still on /login after submit. Check credentials, 2FA, or run with "
            "--headed to complete any security prompts."
        )


def _ensure_results(page: Any) -> None:
    page.wait_for_selector("#fox-booking-search-results", timeout=60000)
    loc = page.locator(".room-details")
    try:
        loc.first.wait_for(state="visible", timeout=15000)
        return
    except Exception:
        pass
    submit = page.locator("#edit-submit")
    if submit.is_visible():
        submit.click()
    loc.first.wait_for(state="visible", timeout=60000)


def _room_is_free(amount_attr: str, price_text: str, block_text: str) -> bool:
    try:
        if float(amount_attr) == 0.0:
            return True
    except ValueError:
        pass
    if FREE_AMOUNT_RE.match(price_text or ""):
        return True
    if FREE_KEYWORDS.search(block_text or ""):
        return True
    return False


def _status(msg: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Find $0 / comp Foxwoods room offers for one or more dates.")
    p.add_argument(
        "check_in",
        nargs="+",
        help="One or more check-in dates (YYYY-MM-DD or MM/DD/YYYY)",
    )
    p.add_argument("--nights", type=int, default=1, help="Length of stay in nights (default: 1)")
    p.add_argument(
        "--hotels",
        default=DEFAULT_HOTELS,
        help=f'Comma hotel codes (default: "{DEFAULT_HOTELS}")',
    )
    p.add_argument("--headed", action="store_true", help="Show browser window")
    p.add_argument(
        "--skip-login",
        action="store_true",
        help="Do not sign in (public rates only; comps usually need login)",
    )
    p.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=f"JSON credentials file (default: search ./{DEFAULT_CONFIG_NAME})",
    )
    p.add_argument(
        "--no-notify",
        action="store_true",
        help="Do not send email/SMS even if configured",
    )
    args = p.parse_args()
    if args.nights < 1:
        raise SystemExit("--nights must be at least 1")

    _status("Parsing date inputs...")

    stays: list[tuple[str, str]] = []
    for raw_date in args.check_in:
        check_in = _parse_check_in(raw_date)
        check_out = check_in + timedelta(days=args.nights)
        arrival = check_in.strftime("%Y-%m-%d")
        departure = check_out.strftime("%Y-%m-%d")
        stays.append((arrival, departure))

    cfg = _load_config(args.config)
    email = str(cfg.get("email", "") or "").strip()
    password = str(cfg.get("password", "") or "")

    if not args.skip_login:
        if not email or not password:
            raise SystemExit(
                f"Add email and password to {DEFAULT_CONFIG_NAME} (see "
                "foxwoods_config.example.json), set FOXWOODS_EMAIL / FOXWOODS_PASSWORD, "
                "or pass --skip-login."
            )

    from playwright.sync_api import sync_playwright

    _status(
        f"Starting browser and preparing to check {len(stays)} date(s) for hotels={args.hotels}"
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            if not args.skip_login:
                _status("Logging in...")
                _login(page, email, password)
                _status("Logged in.")
            else:
                _status("Skipping login (--skip-login).")
            for idx, (arrival, departure) in enumerate(stays):
                _status(f"Checking stay {idx + 1}/{len(stays)}: {arrival} -> {departure}")
                reserve_url = (
                    f"{BOOKING_BASE}?hotels={args.hotels}&arrival={arrival}&departure={departure}"
                )
                page.goto(reserve_url, wait_until="domcontentloaded")
                _maybe_dismiss_overlays(page)
                _ensure_results(page)

                rooms = page.locator(".room-details")
                count = rooms.count()

                if idx > 0:
                    print()
                print(f"Stay: {arrival} -> {departure} ({args.nights} night(s))")
                print(f"Hotels: {args.hotels}")

                if count == 0:
                    print("No room result cards found.")
                    _status(f"No result cards for {arrival} -> {departure}.")
                    continue

                free_hits: list[str] = []
                for i in range(count):
                    card = rooms.nth(i)
                    amt = (card.get_attribute("data-amount") or "").strip()
                    hotel = card.locator(".room-details__hotel-name").inner_text().strip()
                    rname = card.locator(".room-details__room-name").inner_text().strip()
                    try:
                        price_el = card.locator(".room-details__price-amount")
                        price_txt = price_el.inner_text().strip()
                    except Exception:
                        price_txt = ""
                    block = f"{hotel} | {rname} | {price_txt}"
                    if _room_is_free(amt, price_txt, block):
                        line = f"  * {hotel} - {rname} - data-amount={amt!r} display={price_txt!r}"
                        free_hits.append(line)

                print(f"Total result cards: {count}")
                if free_hits:
                    print("Possible free / comp matches:")
                    print("\n".join(free_hits))
                    _status(f"Found {len(free_hits)} free/comp match(es) for {arrival} -> {departure}.")
                    if not args.no_notify:
                        _status("Sending notifications...")
                        warns = _notify_free_rooms(cfg, arrival, departure, args.nights, free_hits)
                        for w in warns:
                            print(w, file=sys.stderr)
                        if warns:
                            _status("Notifications completed with warnings.")
                        else:
                            _status("Notifications sent.")
                else:
                    print(
                        "No $0 or comp-labeled rooms found in listed results. "
                        "(Rates change; try another date or verify account offers.)"
                    )
                    _status(f"No free/comp matches for {arrival} -> {departure}.")
            _status("Finished checking all requested dates.")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
