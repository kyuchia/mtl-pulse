#!/bin/bash
# Import an OpenSky credentials.json into the login Keychain, then remove the file.
#
#   ./scripts/store_opensky_credentials.sh ~/Downloads/credentials.json
#
# Values are piped straight from the JSON into `security`, so neither the client id
# nor the secret is ever typed on a command line or left in shell history.
set -euo pipefail

FILE="${1:-}"
if [[ -z "$FILE" || ! -f "$FILE" ]]; then
    echo "usage: $0 /path/to/credentials.json" >&2
    exit 1
fi

CID=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["clientId"])' "$FILE")
CS=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["clientSecret"])' "$FILE")

if [[ -z "$CID" || -z "$CS" ]]; then
    echo "❌ could not read clientId / clientSecret from $FILE" >&2
    exit 1
fi

# -U updates in place if the entry already exists; -T allows the security CLI to read
# it back without a GUI prompt each time the LaunchAgent starts.
security add-generic-password -a "$USER" -s mtl-pulse-opensky-id \
    -T /usr/bin/security -U -w "$CID"
security add-generic-password -a "$USER" -s mtl-pulse-opensky-secret \
    -T /usr/bin/security -U -w "$CS"

echo "✅ stored mtl-pulse-opensky-id     (${#CID} chars, ...${CID: -4})"
echo "✅ stored mtl-pulse-opensky-secret (${#CS} chars, ...${CS: -4})"

# Verify the round-trip BEFORE destroying the only other copy.
BACK=$(security find-generic-password -w -s mtl-pulse-opensky-secret)
if [[ "$BACK" != "$CS" ]]; then
    echo "❌ read-back mismatch — leaving $FILE in place" >&2
    exit 1
fi
echo "✅ read-back verified"

if command -v rm >/dev/null; then
    rm -P "$FILE" 2>/dev/null || rm -f "$FILE"
fi
echo "🗑️  removed $FILE"
