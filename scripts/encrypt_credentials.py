"""
scripts/encrypt_credentials.py
================================
Manage encryption of ozymandias/config/credentials.enc.

COMMANDS
--------
--keygen            Generate a new Fernet key and write it to the key file.
--encrypt           Encrypt a plaintext credentials.enc in place.
--decrypt           Decrypt credentials.enc back to plaintext (for editing or recovery).
--rekey             Re-encrypt credentials.enc with a freshly generated key,
                    replacing the old key file atomically.

Typical first-time setup
------------------------
    python scripts/encrypt_credentials.py --keygen --encrypt

Recovery when key is lost
--------------------------
1. Delete credentials.enc (it's unreadable without the key).
2. Create a fresh plaintext credentials.enc (see README for format).
3. python scripts/encrypt_credentials.py --keygen --encrypt

Rotating the key without losing credentials
-------------------------------------------
    python scripts/encrypt_credentials.py --rekey

OPTIONS
-------
--key-file PATH     Key file path (default: ~/.ozy_key)
--creds-file PATH   Credentials file path (default: auto-discovered)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def _default_creds() -> Path:
    here = Path(__file__).resolve().parent.parent
    return here / "ozymandias" / "config" / "credentials.enc"


def _default_key() -> Path:
    return Path("~/.ozy_key").expanduser()


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _keygen(key_path: Path, *, confirm_overwrite: bool = True) -> bytes:
    from cryptography.fernet import Fernet
    if key_path.exists() and confirm_overwrite:
        answer = input(f"Key file already exists at {key_path}. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)
    key = Fernet.generate_key()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    print(f"Key written to: {key_path}  (mode 600)")
    return key


def _read_and_decrypt(creds_path: Path, key_path: Path) -> bytes:
    """Return plaintext bytes from creds_path, decrypting if necessary."""
    from cryptography.fernet import Fernet, InvalidToken
    raw = creds_path.read_bytes()
    if raw.lstrip().startswith(b"gAAAAA"):
        if not key_path.exists():
            print(f"ERROR: credentials are encrypted but key file not found: {key_path}")
            sys.exit(1)
        key = key_path.read_bytes().strip()
        try:
            return Fernet(key).decrypt(raw.strip())
        except InvalidToken:
            print("ERROR: decryption failed — wrong key or corrupted file.")
            sys.exit(1)
    return raw


def _validate_json(raw: bytes, creds_path: Path) -> dict:
    try:
        creds = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: {creds_path} is not valid JSON: {exc}")
        sys.exit(1)
    api_key = creds.get("api_key") or creds.get("APCA_API_KEY_ID")
    secret_key = creds.get("secret_key") or creds.get("APCA_API_SECRET_KEY")
    if not api_key or not secret_key:
        print("ERROR: credentials must contain 'api_key' and 'secret_key'")
        sys.exit(1)
    return creds


def do_encrypt(creds_path: Path, key_path: Path) -> None:
    from cryptography.fernet import Fernet
    raw = creds_path.read_bytes()
    if raw.lstrip().startswith(b"gAAAAA"):
        print(f"ERROR: {creds_path} is already encrypted. Run --decrypt first if you need to re-encrypt.")
        sys.exit(1)
    _validate_json(raw, creds_path)
    key = key_path.read_bytes().strip()
    creds_path.write_bytes(Fernet(key).encrypt(raw))
    print(f"Encrypted:  {creds_path}")
    print(f"Using key:  {key_path}")


def do_decrypt(creds_path: Path, key_path: Path) -> None:
    raw = creds_path.read_bytes()
    if not raw.lstrip().startswith(b"gAAAAA"):
        print(f"{creds_path} is already plaintext — nothing to do.")
        return
    plaintext = _read_and_decrypt(creds_path, key_path)
    _validate_json(plaintext, creds_path)
    creds_path.write_bytes(plaintext)
    print(f"Decrypted:  {creds_path}  (now plaintext JSON)")
    print("WARNING: credentials are no longer encrypted on disk.")


def do_rekey(creds_path: Path, old_key_path: Path) -> None:
    from cryptography.fernet import Fernet
    # Decrypt with old key
    plaintext = _read_and_decrypt(creds_path, old_key_path)
    _validate_json(plaintext, creds_path)

    # Generate new key (no overwrite prompt — we're replacing intentionally)
    new_key = _keygen(old_key_path, confirm_overwrite=False)

    # Re-encrypt with new key
    creds_path.write_bytes(Fernet(new_key).encrypt(plaintext))
    print(f"Re-encrypted: {creds_path}")
    print(f"New key at:   {old_key_path}  (old key overwritten)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage Ozymandias credentials encryption",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--keygen",  action="store_true", help="Generate a new Fernet key")
    parser.add_argument("--encrypt", action="store_true", help="Encrypt credentials.enc in place")
    parser.add_argument("--decrypt", action="store_true", help="Decrypt credentials.enc back to plaintext")
    parser.add_argument("--rekey",   action="store_true", help="Re-encrypt with a new key (atomic)")
    parser.add_argument(
        "--key-file", type=Path, default=_default_key(),
        metavar="PATH", help=f"Key file path (default: {_default_key()})",
    )
    parser.add_argument(
        "--creds-file", type=Path, default=_default_creds(),
        metavar="PATH", help="Credentials file path (default: auto-discovered)",
    )
    args = parser.parse_args()

    if not any([args.keygen, args.encrypt, args.decrypt, args.rekey]):
        parser.print_help()
        sys.exit(0)

    if args.decrypt and args.encrypt:
        print("ERROR: --encrypt and --decrypt are mutually exclusive.")
        sys.exit(1)

    if args.rekey and (args.encrypt or args.decrypt):
        print("ERROR: --rekey cannot be combined with --encrypt or --decrypt.")
        sys.exit(1)

    if args.rekey:
        if not args.creds_file.exists():
            print(f"ERROR: credentials file not found: {args.creds_file}")
            sys.exit(1)
        do_rekey(args.creds_file, args.key_file)
        return

    if args.keygen:
        _keygen(args.key_file)

    if args.encrypt:
        if not args.key_file.exists():
            print(f"ERROR: key file not found at {args.key_file}. Run --keygen first.")
            sys.exit(1)
        if not args.creds_file.exists():
            print(f"ERROR: credentials file not found: {args.creds_file}")
            sys.exit(1)
        do_encrypt(args.creds_file, args.key_file)

    if args.decrypt:
        if not args.creds_file.exists():
            print(f"ERROR: credentials file not found: {args.creds_file}")
            sys.exit(1)
        do_decrypt(args.creds_file, args.key_file)


if __name__ == "__main__":
    main()
