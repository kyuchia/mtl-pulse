"""
Secret lookup: environment first, then the macOS login Keychain.

Nothing here ever writes a secret to disk or echoes one. Keychain is the store of
record so that no credential lives in a launchd plist, a dotfile, or the repo — a
LaunchAgent runs inside the user's GUI session, so the login keychain is already
unlocked and `security` can read it without a password prompt.

    STM_API_KEY            <- service "mtl-pulse-stm"
    OPENSKY_CLIENT_ID      <- service "mtl-pulse-opensky-id"
    OPENSKY_CLIENT_SECRET  <- service "mtl-pulse-opensky-secret"

The environment still wins when set, so an ad-hoc `export STM_API_KEY=...` run keeps
working exactly as before.
"""

import os
import shutil
import subprocess
import sys

SERVICES = {
    "STM_API_KEY": "mtl-pulse-stm",
    "OPENSKY_CLIENT_ID": "mtl-pulse-opensky-id",
    "OPENSKY_CLIENT_SECRET": "mtl-pulse-opensky-secret",
}


def keychain_get(service, account=None):
    """Read one generic password. Returns None if absent or the tool is unavailable."""
    if not shutil.which("security"):
        return None
    cmd = ["security", "find-generic-password", "-w", "-s", service]
    if account:
        cmd += ["-a", account]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value or None


def get_secret(env_var, service=None, required=False, hint=""):
    """Environment first, then Keychain. Never prints the value itself."""
    val = os.environ.get(env_var)
    source = "env"
    if not val:
        val = keychain_get(service or SERVICES.get(env_var, ""))
        source = "keychain"
    if not val:
        if required:
            svc = service or SERVICES.get(env_var, env_var)
            print(f"❌ {env_var} not found in the environment or the Keychain "
                  f"(service '{svc}').", file=sys.stderr)
            if hint:
                print(f"   {hint}", file=sys.stderr)
            sys.exit(1)
        return None, None
    return val, source


def describe(name, value, source):
    """A one-line, non-revealing confirmation that a secret was found."""
    if not value:
        return f"{name}: not set"
    return f"{name}: loaded from {source} ({len(value)} chars, ...{value[-4:]})"
