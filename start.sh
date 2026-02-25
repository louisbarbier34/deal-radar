#!/bin/bash
set -e

# Bootstrap Google credentials from base64 env vars (cloud only)
if [ -n "$GOOGLE_CREDENTIALS_JSON" ] && [ ! -f "credentials.json" ]; then
    echo "$GOOGLE_CREDENTIALS_JSON" | base64 -d > credentials.json
    echo "credentials.json written from env"
fi

if [ -n "$GOOGLE_TOKEN_JSON" ] && [ ! -f "token.json" ]; then
    echo "$GOOGLE_TOKEN_JSON" | base64 -d > token.json
    echo "token.json written from env"
fi

exec python3 main.py
