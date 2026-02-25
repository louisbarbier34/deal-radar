#!/bin/bash
cd "$(dirname "$0")"
exec python3 pipedream/webhook_server.py
