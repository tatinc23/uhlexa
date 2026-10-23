"""scripts/keiki_tokens.py: dry run by default, hash-only storage, rotation, no child tokens."""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mcp.auth import TOKEN_BINDING_TYPE, agent_principal, hash_token
from app.models import ProviderBinding

SPEC = importlib.util.spec_from_file_location("keiki_tokens", Path(__file__).parents[2] / "scripts" / "keiki_tokens.py")
keiki_tokens = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(keiki_tokens)


def _rows(db: Session) -> list[ProviderBinding]:
    return list(db.scalars(select(ProviderBinding).where(ProviderBinding.binding_type == TOKEN_BINDING_TYPE)))


def _token(path: Path) -> str:
    (line,) = [line for line in path.read_text().splitlines() if line.startswith("KEIKI_MCP_TOKEN_ALEX=")]
    return line.split("=", 1)[1]


def test_dry_run_writes_nothing(db: Session, seeded, tmp_path: Path) -> None:
    out = keiki_tokens.mint(db, "alex", apply=False, secrets_file=tmp_path / "s")
    assert out.startswith("dry run") and _rows(db) == [] and not (tmp_path / "s").exists()


def test_mint_stores_only_the_hash_and_the_token_works(db: Session, seeded, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_KEIKI", "true")
    from app.settings import get_settings

    get_settings.cache_clear()
    secrets_file = tmp_path / "s"
    keiki_tokens.mint(db, "alex", apply=True, secrets_file=secrets_file)
    token = _token(secrets_file)
    (row,) = _rows(db)
    assert row.external_id == hash_token(token) and token not in repr(row.config)
    assert agent_principal(db, token).user.handle == "alex"
    assert oct(secrets_file.stat().st_mode)[-3:] == "600"


def test_mint_again_rotates_and_the_old_token_stops_working(db: Session, seeded, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_KEIKI", "true")
    from app.errors import Unauthorized
    from app.settings import get_settings

    get_settings.cache_clear()
    secrets_file = tmp_path / "s"
    keiki_tokens.mint(db, "alex", apply=True, secrets_file=secrets_file)
    old = _token(secrets_file)
    keiki_tokens.mint(db, "alex", apply=True, secrets_file=secrets_file)
    assert len(_rows(db)) == 1 and _token(secrets_file) != old
    with pytest.raises(Unauthorized):
        agent_principal(db, old)


def test_child_never_gets_a_token(db: Session, seeded, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        keiki_tokens.mint(db, "max", apply=True, secrets_file=tmp_path / "s")
    assert _rows(db) == []
