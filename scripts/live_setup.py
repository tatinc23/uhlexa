"""One command for docs/LIVE_SETUP.md steps 1, 2, 4 and 5 (everything except the Keiki dashboard paste).

    .venv/bin/python scripts/live_setup.py --phone +15551234567                 # dry run: reads Inkbox, changes nothing
    .venv/bin/python scripts/live_setup.py --phone +15551234567 --apply         # does it

Does, for Alex's agent `uhlexa` and Katie's agent `jordy`:
  * turns the Inkbox channel flags on in `.env.secrets.local` (gitignored);
  * creates the Inkbox webhook subscription (imessage.received, text.received) pointing at this app, once;
  * sets incoming calls to auto-accept with this app's call WebSocket;
  * saves each identity's webhook signing key into INKBOX_SIGNING_KEYS (a key is shown only once, when its first
    subscription is created; if a key already exists and is not saved here the script stops and says so, because
    `--rotate-keys` would break any other consumer of that identity);
  * binds alex -> uhlexa (+ your phone, with consent) and katie -> jordy (no phone);
  * mints Alex's Keiki MCP token if there is none (the plaintext goes to `.env.secrets.local` only).
Never uses the samantha identity. Prints nothing secret.
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / ".env.secrets.local"
PEOPLE = {"alex": "uhlexa", "katie": "jordy"}  # Uhlexa user -> Inkbox handle
EVENTS = ["imessage.received", "text.received"]
FLAGS = ["ENABLE_INKBOX", "ENABLE_INKBOX_IMESSAGE", "ENABLE_INKBOX_SMS", "ENABLE_INKBOX_CALLS"]
REFUSED = {"samantha"}


def read_env(path: Path = SECRETS) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip("'\"")
    return values


def set_env_lines(path: Path, updates: dict[str, str]) -> None:
    """Replace or append `NAME='value'` lines, keeping everything else and the 0600 mode."""
    lines = path.read_text().splitlines() if path.exists() else []
    names = set(updates)
    kept = [ln for ln in lines if ln.split("=", 1)[0].strip() not in names]
    kept += [f"{name}='{value}'" if value.startswith("{") else f"{name}={value}" for name, value in updates.items()]
    path.write_text("\n".join(kept) + "\n")
    os.chmod(path, 0o600)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phone", required=True, help="Ken's own phone, E.164, bound to Alex")
    parser.add_argument("--base-url", default="https://uhlexa.inkboxwire.com")
    parser.add_argument("--consent", default="Ken, 2026-10-23, own phone")
    parser.add_argument("--rotate-keys", action="store_true", help="rotate a signing key that was never saved")
    parser.add_argument("--reseed", action="store_true", help="wipe the database to the fresh demo household (Katie)")
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    args = parser.parse_args(argv)
    if set(PEOPLE.values()) & REFUSED:
        raise SystemExit("refusing the samantha identity")

    env = read_env()
    for name in ("INKBOX_API_KEY",):
        if not env.get(name):
            raise SystemExit(f"{name} is missing from .env.secrets.local")
    os.environ.update(env)
    os.environ["DATABASE_URL"] = os.environ.get("DATABASE_URL") or env.get("NEON_DATABASE_URL", "")
    if not os.environ["DATABASE_URL"]:
        raise SystemExit("No database: NEON_DATABASE_URL is missing from .env.secrets.local")
    sys.path.insert(0, str(ROOT))

    from inkbox import Inkbox  # noqa: PLC0415

    say = print
    mode = "APPLY" if args.apply else "DRY RUN (nothing changes)"
    say(f"== {mode} ==")
    base = args.base_url.rstrip("/")
    ws_base = base.replace("https://", "wss://", 1)
    client = Inkbox(api_key=env["INKBOX_API_KEY"])
    updates: dict[str, str] = {name: "true" for name in FLAGS if env.get(name) != "true"}
    keys: dict[str, str] = json.loads(env.get("INKBOX_SIGNING_KEYS") or "{}")
    problems: list[str] = []

    for handle in PEOPLE.values():
        identity = client.get_identity(handle)
        url = f"{base}/webhooks/inkbox/{handle}"
        existing = [s for s in client.webhooks.subscriptions.list(agent_identity_id=identity.id) if s.url == url]
        if existing:
            say(f"[{handle}] webhook subscription already there")
        elif args.apply:
            created = client.webhooks.subscriptions.create(url=url, event_types=EVENTS, agent_identity_id=identity.id)
            say(f"[{handle}] webhook subscription created")
            if created.signing_key:
                keys[handle] = created.signing_key
        else:
            say(f"[{handle}] would create webhook subscription -> {url}")
        if handle not in keys:
            has_key = client.signing_keys.get_status(handle).configured
            if args.rotate_keys and args.apply:
                keys[handle] = client.signing_keys.create_or_rotate(handle).signing_key
                say(f"[{handle}] signing key rotated and saved")
            elif has_key and not keys.get(handle):
                say(f"[{handle}] STOP: it has a signing key that is not saved here; re-run with --rotate-keys")
                problems.append(handle)
        ws = f"{ws_base}/ws/phone/{handle}"
        if args.apply:
            client.incoming_call_action.set(
                incoming_call_action="auto_accept", client_websocket_url=ws, agent_identity_id=identity.id
            )
            say(f"[{handle}] incoming calls: auto-accept -> {ws}")
        else:
            say(f"[{handle}] would set incoming calls to auto-accept -> {ws}")

    if keys:
        updates["INKBOX_SIGNING_KEYS"] = json.dumps(keys)
    if problems:
        raise SystemExit("Stopped before changing the database or .env.secrets.local.")
    if args.apply:
        if updates:
            set_env_lines(SECRETS, updates)
        say(f"saved {len(updates)} setting(s) to .env.secrets.local")
    else:
        say(f"would save: {', '.join(updates) or 'nothing'}")

    from sqlalchemy import select  # noqa: PLC0415
    from sqlalchemy import select as _select  # noqa: PLC0415

    from app.db.migrate import upgrade_to_head  # noqa: PLC0415
    from app.db.session import SessionLocal  # noqa: PLC0415
    from app.mcp.auth import TOKEN_BINDING_TYPE  # noqa: PLC0415
    from app.models import (  # noqa: PLC0415
        BindingStatus,
        ProviderBinding,
        ProviderName,
        User,  # noqa: PLC0415
    )
    from app.seed import reset_demo  # noqa: PLC0415
    from scripts import channels_bind, keiki_tokens  # noqa: PLC0415

    if args.apply:
        upgrade_to_head()
    with SessionLocal() as db:
        from app.models import Agent  # noqa: PLC0415

        katie = db.scalars(_select(User).where(User.handle == "katie")).first()
        names = {a.name for a in db.scalars(_select(Agent))}
        if katie is None or not {"Uhlexa", "Jordy"} <= names:
            if not (args.apply and args.reseed):
                say("The database has the old seed (no Katie, or agents not named Uhlexa and Jordy): add --reseed.")
                return
            reset_demo(db)
            db.commit()
            say("database reset to the fresh demo household")
        for person, handle in PEOPLE.items():
            say(channels_bind.bind(db, "identity", person, handle, consent=None, apply=args.apply))
        say(channels_bind.bind(db, "phone", "alex", args.phone, consent=args.consent, apply=args.apply))
        has_token = db.scalars(
            select(ProviderBinding).where(
                ProviderBinding.provider == ProviderName.KEIKI,
                ProviderBinding.binding_type == TOKEN_BINDING_TYPE,
                ProviderBinding.status == BindingStatus.ACTIVE,
            )
        ).first()
        if has_token:
            say("alex already has a Keiki MCP token (not rotated)")
        else:
            say(keiki_tokens.mint(db, "alex", apply=args.apply))

    say("")
    say("LAST STEP (Keiki dashboard, by hand): Alex's agent -> MCP connections -> add")
    say(f"  endpoint {base}/mcp   header  Authorization: Bearer <token>")
    say("  copy the token:  grep KEIKI_MCP_TOKEN_ALEX .env.secrets.local | cut -d= -f2 | pbcopy")
    say("Then restart scripts/run_public.sh.")


if __name__ == "__main__":
    main()
