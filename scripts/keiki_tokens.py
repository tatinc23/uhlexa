"""Mint, rotate or revoke the per-agent MCP bearer token a Keiki agent uses to call Uhlexa (addendum 21.4/21.6).

    .venv/bin/python scripts/keiki_tokens.py mint alex            # dry run: shows what would change
    .venv/bin/python scripts/keiki_tokens.py mint alex --apply    # writes the hash, saves the token locally
    .venv/bin/python scripts/keiki_tokens.py revoke alex --apply
    .venv/bin/python scripts/keiki_tokens.py list

Only the token's sha256 is stored (`provider_bindings`, provider keiki, binding_type mcp_token). The plaintext is shown
nowhere: `--apply` appends it to the gitignored `.env.secrets.local` as `KEIKI_MCP_TOKEN_<HANDLE>`. Copy it from there
into 1Password (team vault) and into the Keiki agent's MCP connection header `Authorization: Bearer <token>`.
Minting again rotates the token (the old one stops working). Children never get a token. Uses `DATABASE_URL`.
"""

import argparse
import os
import secrets
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.auth.demo_auth import resolve_principal  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.mcp.auth import TOKEN_BINDING_TYPE, hash_token  # noqa: E402
from app.models import BindingStatus, MembershipRole, ProviderBinding, ProviderName  # noqa: E402

SECRETS_FILE = ROOT / ".env.secrets.local"


def _binding(db: Session, user_id: object) -> ProviderBinding | None:
    return db.scalars(
        select(ProviderBinding).where(
            ProviderBinding.provider == ProviderName.KEIKI,
            ProviderBinding.binding_type == TOKEN_BINDING_TYPE,
            ProviderBinding.user_id == user_id,
        )
    ).first()


def _save_token(handle: str, token: str, path: Path = SECRETS_FILE) -> str:
    name = f"KEIKI_MCP_TOKEN_{handle.upper()}"
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if not line.startswith(f"{name}=")] + [f"{name}={token}"]
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return name


def mint(db: Session, handle: str, *, apply: bool, secrets_file: Path = SECRETS_FILE) -> str:
    principal = resolve_principal(db, handle)
    if principal is None:
        raise SystemExit(f"Unknown user {handle!r}")
    if principal.membership.role == MembershipRole.CHILD:
        raise SystemExit(f"{handle} is a child: children never get an agent token")
    existing = _binding(db, principal.user.id)
    action = "rotate" if existing else "mint"
    if not apply:
        return f"dry run: would {action} the MCP token for {handle} (agent {principal.agent.name!r}); add --apply"
    token = secrets.token_urlsafe(32)
    if existing:
        existing.external_id, existing.status, existing.agent_id = (
            hash_token(token),
            BindingStatus.ACTIVE,
            principal.agent.id,
        )
    else:
        db.add(
            ProviderBinding(
                household_id=principal.household.id,
                user_id=principal.user.id,
                agent_id=principal.agent.id,
                provider=ProviderName.KEIKI,
                binding_type=TOKEN_BINDING_TYPE,
                external_id=hash_token(token),
                status=BindingStatus.ACTIVE,
                config={"label": f"{principal.agent.name} MCP token"},
            )
        )
    db.commit()
    name = _save_token(handle, token, secrets_file)
    return f"{action}ed the MCP token for {handle}; saved as {name} in {secrets_file.name} (copy it to 1Password)"


def revoke(db: Session, handle: str, *, apply: bool) -> str:
    principal = resolve_principal(db, handle)
    binding = _binding(db, principal.user.id) if principal else None
    if binding is None:
        return f"{handle} has no MCP token"
    if not apply:
        return f"dry run: would revoke the MCP token for {handle}; add --apply"
    binding.status = BindingStatus.INACTIVE
    db.commit()
    return f"revoked the MCP token for {handle}"


def list_tokens(db: Session) -> list[str]:
    rows = db.scalars(
        select(ProviderBinding).where(
            ProviderBinding.provider == ProviderName.KEIKI, ProviderBinding.binding_type == TOKEN_BINDING_TYPE
        )
    ).all()
    return [f"{row.config.get('label', row.agent_id)}: {row.status.value}" for row in rows] or ["no MCP tokens"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["mint", "revoke", "list"])
    parser.add_argument("handle", nargs="?")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args(argv)
    with SessionLocal() as db:
        if args.command == "list":
            print("\n".join(list_tokens(db)))
            return
        if not args.handle:
            parser.error("a user handle is required")
        print((mint if args.command == "mint" else revoke)(db, args.handle.lower(), apply=args.apply))


if __name__ == "__main__":
    main()
