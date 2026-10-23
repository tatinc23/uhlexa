"""LiveKit token minting (spec 22.3, 22.7): signature, minimal grants, dispatch metadata, expiry."""

import base64
import hashlib
import hmac
import json

import pytest

from app.voice.tokens import mint_token

KW = {
    "api_key": "APIkey123",
    "api_secret": "secret-that-is-long-enough-for-hs256-0123456789",
    "room": "uhlexa-echo-alex-0a1b2c3d",
    "identity": "human-alex-abc123",
    "name": "Alex Parker",
    "agent_name": "uhlexa-voice",
    "metadata": {"handle": "alex", "surface": "echo", "avatar": False},
    "now": 1_800_000_000,
}


def _decode(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def _claims(token: str) -> dict:
    return _decode(token.split(".")[1])


def test_token_signature_verifies() -> None:
    token = mint_token(**KW)
    header, payload, signature = token.split(".")
    assert _decode(header) == {"alg": "HS256", "typ": "JWT"}
    expected = hmac.new(KW["api_secret"].encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)) == expected


def test_token_grants_are_minimal() -> None:
    video = _claims(mint_token(**KW))["video"]
    assert video["roomJoin"] is True and video["room"] == KW["room"]
    assert video["canPublishSources"] == ["microphone"]
    for admin in ("roomAdmin", "roomCreate", "roomRecord", "roomList", "ingressAdmin", "agent", "hidden"):
        assert admin not in video


def test_token_dispatches_worker_with_metadata() -> None:
    claims = _claims(mint_token(**KW))
    assert claims["sub"] == KW["identity"] and claims["iss"] == KW["api_key"]
    dispatch = claims["roomConfig"]["agents"]
    assert len(dispatch) == 1 and dispatch[0]["agentName"] == "uhlexa-voice"
    assert json.loads(dispatch[0]["metadata"]) == KW["metadata"]


def test_token_expires_after_ttl() -> None:
    claims = _claims(mint_token(**KW))
    assert claims["exp"] - claims["nbf"] == 900


def test_missing_secret_rejected() -> None:
    with pytest.raises(ValueError):
        mint_token(**{**KW, "api_secret": ""})


def test_claims_match_livekit_api_when_installed() -> None:
    api = pytest.importorskip("livekit.api")
    from livekit.protocol.agent_dispatch import RoomAgentDispatch
    from livekit.protocol.room import RoomConfiguration

    reference = (
        api.AccessToken(KW["api_key"], KW["api_secret"])
        .with_identity(KW["identity"])
        .with_name(KW["name"])
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=KW["room"],
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
                can_publish_sources=["microphone"],
            )
        )
        .with_room_config(
            RoomConfiguration(
                agents=[RoomAgentDispatch(agent_name="uhlexa-voice", metadata=json.dumps(KW["metadata"]))]
            )
        )
        .to_jwt()
    )
    ours, theirs = _claims(mint_token(**KW)), _claims(reference)
    assert set(ours) == set(theirs)
    assert ours["video"] == theirs["video"]
    assert ours["roomConfig"]["agents"][0]["agentName"] == theirs["roomConfig"]["agents"][0]["agentName"]
