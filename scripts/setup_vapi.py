"""Provision the Vapi assistant + free US phone number from the config in code.

Usage (from the repo root):
    export VAPI_API_KEY=...            # Vapi dashboard -> API Keys -> Private key
    export PUBLIC_BASE_URL=https://your-app.up.railway.app
    export VAPI_WEBHOOK_SECRET=...     # same value as on the server
    python -m scripts.setup_vapi --area-code 512

    python -m scripts.setup_vapi --dry-run   # print the assistant JSON, call nothing
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from app.config import get_settings
from app.voice import assistant_config, provisioning


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--area-code", help="Preferred US area code for the free Vapi number, e.g. 512")
    parser.add_argument("--no-phone", action="store_true", help="Only create/update the assistant")
    parser.add_argument("--dry-run", action="store_true", help="Print the assistant payload and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = get_settings()
    if args.dry_run:
        print(json.dumps(assistant_config.build_assistant_payload(settings), indent=2))
        return 0
    try:
        result = provisioning.sync(settings, area_code=args.area_code, with_phone=not args.no_phone)
    except (provisioning.VapiError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    if result.get("phone_number"):
        print(f"\nCall {result['phone_number']} to talk to the agent (new numbers can take a few minutes to activate).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
