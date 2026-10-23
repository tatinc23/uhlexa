"""Bind a family member to their agent's Inkbox identity / Primitive inbox and to their own phone and email address,
so real texts, calls and emails reach their agent.

    .venv/bin/python scripts/channels_bind.py identity alex uhlexa                       # dry run
    .venv/bin/python scripts/channels_bind.py identity alex uhlexa --apply
    .venv/bin/python scripts/channels_bind.py phone alex +15551234567 --consent "Ken, 2026-10-23, in person" --apply
    .venv/bin/python scripts/channels_bind.py phone alex +15551234567 --consent "..." --pin 4821 --apply  # + call PIN
    .venv/bin/python scripts/channels_bind.py email-inbox alex alex@agentmail.tatinc.us --apply
    .venv/bin/python scripts/channels_bind.py email alex you@example.com --consent "Ken, 2026-10-23" --apply
    .venv/bin/python scripts/channels_bind.py list

`identity` says which Inkbox handle is this person's agent (`inkbox_identity`). `phone` says which number is this
person's own phone (`inkbox_phone`, also accepted for iMessage): a text or call from that number to their agent's line
is answered as them. `email-inbox` says which Primitive address is this person's agent (`primitive_inbox`); `email`
says which address is their own (`primitive_email`): an email from it to their agent's inbox is answered as them, but
only when Primitive verified the From address. Anyone else is ignored (fail closed). Children are never bound. A phone
or email binding needs `--consent` (who agreed, when, how); it is stored with the binding. Re-running replaces that
person's binding. Uses `DATABASE_URL`.
"""

import argparse
import re
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.auth.demo_auth import resolve_principal  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models import BindingStatus, MembershipRole, ProviderBinding, ProviderName  # noqa: E402
from app.services.channel_service import hash_pin  # noqa: E402

KINDS = {
    "identity": (ProviderName.INKBOX, "inkbox_identity"),
    "phone": (ProviderName.INKBOX, "inkbox_phone"),
    "email-inbox": (ProviderName.PRIMITIVE, "primitive_inbox"),
    "email": (ProviderName.PRIMITIVE, "primitive_email"),
}
NEEDS_CONSENT = {"phone", "email"}
EMAIL = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}")
PIN = re.compile(r"\d{4,8}")
E164 = re.compile(r"\+[1-9]\d{6,14}")
HANDLE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
REFUSED_HANDLES = {"samantha"}  # Ken's personal Inkbox identity is never a Uhlexa agent


def _mask(value: str) -> str:
    return value if not value.startswith("+") else f"{value[:3]}…{value[-2:]}"


def bind(
    db: Session, kind: str, handle: str, value: str, *, consent: str | None, apply: bool, pin: str | None = None
) -> str:
    principal = resolve_principal(db, handle)
    if principal is None:
        raise SystemExit(f"Unknown user {handle!r}")
    if principal.membership.role == MembershipRole.CHILD:
        raise SystemExit(f"{handle} is a child: children are never bound to a real channel")
    provider, binding_type = KINDS[kind]
    value = value.strip() if kind == "phone" else value.strip().lower()
    if kind == "identity" and (not HANDLE.fullmatch(value) or value in REFUSED_HANDLES):
        raise SystemExit(f"{value!r} is not an allowed Inkbox handle")
    if kind == "phone" and not E164.fullmatch(value):
        raise SystemExit("phone must be E.164, e.g. +15551234567")
    if kind in ("email", "email-inbox") and not EMAIL.fullmatch(value):
        raise SystemExit(f"{value!r} is not an email address")
    if pin is not None and kind != "phone":
        raise SystemExit("--pin only applies to a phone binding")
    if pin is not None and not PIN.fullmatch(pin):
        raise SystemExit("--pin must be 4 to 8 digits")
    if kind in NEEDS_CONSENT and not (consent or "").strip():
        raise SystemExit(f"--consent is required for a {kind} binding (who agreed, when, how)")
    taken = db.scalars(
        select(ProviderBinding).where(
            ProviderBinding.provider == provider,
            ProviderBinding.binding_type == binding_type,
            ProviderBinding.external_id == value,
            ProviderBinding.user_id != principal.user.id,
        )
    ).first()
    if taken is not None:
        raise SystemExit(f"{_mask(value)} is already bound to someone else")
    existing = db.scalars(
        select(ProviderBinding).where(
            ProviderBinding.provider == provider,
            ProviderBinding.binding_type == binding_type,
            ProviderBinding.user_id == principal.user.id,
        )
    ).first()
    plan = f"{'replace' if existing else 'add'} {binding_type} {_mask(value)} for {handle}" + (
        " with a call PIN" if pin else ""
    )
    if not apply:
        return f"dry run: would {plan}; add --apply"
    config = {"consent": consent.strip()} if consent else {}
    if pin:
        config["pin_hash"] = hash_pin(pin)  # a fresh config also clears any PIN lockout (the early admin unlock)
    if existing:
        existing.external_id, existing.status, existing.config = value, BindingStatus.ACTIVE, config
        existing.agent_id = principal.agent.id
    else:
        db.add(
            ProviderBinding(
                household_id=principal.household.id,
                user_id=principal.user.id,
                agent_id=principal.agent.id,
                provider=provider,
                binding_type=binding_type,
                external_id=value,
                status=BindingStatus.ACTIVE,
                config=config,
            )
        )
    db.commit()
    return f"done: {plan}"


def list_bindings(db: Session) -> list[str]:
    rows = db.scalars(
        select(ProviderBinding).where(ProviderBinding.provider.in_((ProviderName.INKBOX, ProviderName.PRIMITIVE)))
    ).all()
    return [f"{row.binding_type} {_mask(row.external_id)} user={row.user_id} {row.status.value}" for row in rows] or [
        "no channel bindings"
    ]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=[*KINDS, "list"])
    parser.add_argument("handle", nargs="?", help="the family member's Uhlexa handle, e.g. alex")
    parser.add_argument("value", nargs="?", help="Inkbox handle, E.164 number, or email address")
    parser.add_argument("--consent", help="phone and email: who agreed, when, how")
    parser.add_argument("--pin", help="phone only: a 4-8 digit PIN the caller must say on a call (stored hashed)")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args(argv)
    with SessionLocal() as db:
        if args.command == "list":
            print("\n".join(list_bindings(db)))
            return
        if not (args.handle and args.value):
            parser.error("handle and value are required")
        print(
            bind(
                db, args.command, args.handle.lower(), args.value, consent=args.consent, apply=args.apply, pin=args.pin
            )
        )


if __name__ == "__main__":
    main()
