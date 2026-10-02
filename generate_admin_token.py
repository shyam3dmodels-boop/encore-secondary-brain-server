#!/usr/bin/env python3
"""
Secure Admin Token Generator for Secondary Brain 2.0 / Enforcer OS
Generates a cryptographically random, high-entropy 256-bit token for ADMIN_TOKEN.
"""

import secrets
import sys

def generate_secure_token(length_bytes: int = 32) -> str:
    """Generates a URL-safe, base64-encoded cryptographically secure secret token."""
    return secrets.token_urlsafe(length_bytes)

def main():
    token = generate_secure_token(32)
    print("=" * 60)
    print("🔐 Secondary Brain 2.0 — Cryptographic Admin Token Generated")
    print("=" * 60)
    print(f"\nADMIN_TOKEN={token}\n")
    print("=" * 60)
    print("📋 Copy this token into your .env file and Render environment settings.")
    print("=" * 60)

if __name__ == "__main__":
    main()
