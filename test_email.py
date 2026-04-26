#!/usr/bin/env python3
"""
Quick email credential test. Run this locally before triggering the workflow.

Usage:
    python test_email.py

Reads GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ALERT_RECIPIENT from .env
Sends a plain-text test email and reports exactly what went wrong if it fails.
"""

import smtplib
import sys
from email.mime.text import MIMEText

from dotenv import load_dotenv
import os

load_dotenv()

gmail   = os.getenv("GMAIL_ADDRESS", "")
pw      = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")   # strip spaces
to      = os.getenv("ALERT_RECIPIENT", "")

print(f"GMAIL_ADDRESS    : {gmail or '(not set)'}")
print(f"GMAIL_APP_PASSWORD: {'*' * len(pw) if pw else '(not set)'} ({len(pw)} chars)")
print(f"ALERT_RECIPIENT  : {to or '(not set)'}")
print()

if not gmail or not pw or not to:
    print("ERROR: one or more credentials are missing from .env")
    sys.exit(1)

if len(pw) != 16:
    print(f"WARNING: app passwords are exactly 16 chars — yours is {len(pw)}. Check for spaces or typos.")

msg = MIMEText("This is a test from AutoDealTracker. If you received this, email is working.", "plain", "utf-8")
msg["Subject"] = "AutoDealTracker — email test"
msg["From"]    = gmail
msg["To"]      = to

print(f"Connecting to smtp.gmail.com:465 ...")
try:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        print("Connected. Logging in ...")
        server.login(gmail, pw)
        print("Login OK. Sending ...")
        server.sendmail(gmail, to, msg.as_string())
    print(f"\nSUCCESS — test email sent to {to}")
except smtplib.SMTPAuthenticationError as e:
    print(f"\nFAILED — authentication error: {e}")
    print("\nMost likely causes:")
    print("  1. GMAIL_APP_PASSWORD is your regular Gmail password — use an App Password instead")
    print("  2. 2-Step Verification is not enabled on the Gmail account (required for App Passwords)")
    print("  3. The App Password was copied with spaces — they've been stripped, but double-check")
    print("\nTo generate an App Password:")
    print("  myaccount.google.com/security → 2-Step Verification → App passwords")
    sys.exit(1)
except Exception as e:
    print(f"\nFAILED — {type(e).__name__}: {e}")
    sys.exit(1)
