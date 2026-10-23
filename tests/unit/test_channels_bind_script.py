"""scripts/channels_bind.py: dry run by default, no children, consent required, never samantha, one owner per number."""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ProviderBinding, ProviderName

SPEC = importlib.util.spec_from_file_location(
    "channels_bind", Path(__file__).parents[2] / "scripts" / "channels_bind.py"
)
channels_bind = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(channels_bind)


def _rows(db: Session) -> list[ProviderBinding]:
    return list(db.scalars(select(ProviderBinding).where(ProviderBinding.provider == ProviderName.INKBOX)))


def test_dry_run_writes_nothing(db: Session, seeded) -> None:
    assert channels_bind.bind(db, "identity", "alex", "uhlexa", consent=None, apply=False).startswith("dry run")
    assert _rows(db) == []


def test_identity_and_phone_bind_and_rebind_replaces(db: Session, seeded) -> None:
    channels_bind.bind(db, "identity", "alex", "Uhlexa", consent=None, apply=True)
    channels_bind.bind(db, "phone", "alex", "+15550000001", consent="Ken, test", apply=True)
    channels_bind.bind(db, "phone", "alex", "+15550000002", consent="Ken, test", apply=True)
    got = sorted((r.binding_type, r.external_id) for r in _rows(db))
    assert got == [("inkbox_identity", "uhlexa"), ("inkbox_phone", "+15550000002")]


@pytest.mark.parametrize(
    ("kind", "handle", "value", "consent"),
    [
        ("identity", "max", "maxbox", None),  # child
        ("phone", "alex", "+15550000001", None),  # no consent
        ("phone", "alex", "555-0001", "ok"),  # not E.164
        ("identity", "alex", "samantha", None),  # Ken's personal identity
    ],
)
def test_refusals(db: Session, seeded, kind: str, handle: str, value: str, consent: str | None) -> None:
    with pytest.raises(SystemExit):
        channels_bind.bind(db, kind, handle, value, consent=consent, apply=True)
    assert _rows(db) == []


def test_a_number_belongs_to_one_person(db: Session, seeded) -> None:
    channels_bind.bind(db, "phone", "alex", "+15550000001", consent="ok", apply=True)
    with pytest.raises(SystemExit):
        channels_bind.bind(db, "phone", "katie", "+15550000001", consent="ok", apply=True)


def _phone_config(db: Session) -> dict:
    return next(r.config for r in _rows(db) if r.binding_type == "inkbox_phone")


def test_pin_is_stored_hashed_and_never_printed(db: Session, seeded, capsys: pytest.CaptureFixture[str]) -> None:
    out = channels_bind.bind(db, "phone", "alex", "+15550000001", consent="ok", pin="4821", apply=True)
    assert "4821" not in out and "4821" not in capsys.readouterr().out
    config = _phone_config(db)
    assert "4821" not in str(config) and config["consent"] == "ok"
    from app.services import channel_service

    assert channel_service.verify_pin(config["pin_hash"], "4821")
    assert not channel_service.verify_pin(config["pin_hash"], "4822")


def test_pin_dry_run_writes_nothing(db: Session, seeded) -> None:
    out = channels_bind.bind(db, "phone", "alex", "+15550000001", consent="ok", pin="4821", apply=False)
    assert out.startswith("dry run") and "4821" not in out
    assert _rows(db) == []


@pytest.mark.parametrize("pin", ["12", "abcd", "1234567890123"])
def test_bad_pin_refused(db: Session, seeded, pin: str) -> None:
    with pytest.raises(SystemExit):
        channels_bind.bind(db, "phone", "alex", "+15550000001", consent="ok", pin=pin, apply=True)
    assert _rows(db) == []


def test_pin_only_for_phone(db: Session, seeded) -> None:
    with pytest.raises(SystemExit):
        channels_bind.bind(db, "identity", "alex", "uhlexa", consent=None, pin="4821", apply=True)
