import json

from app import voice_log
from app.config import settings
from app.logic import decide


def test_log_voice_order_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "voice_log_dir", str(tmp_path))
    audio = voice_log.save_audio("abc", b"RIFF....", "phrase.wav", "audio/wav")
    voice_log.log_event("voice", id="abc", audio=audio, transcription="Tonto, coge la pieza.", mission="mission_2")
    voice_log.log_event("order", mission="mission_2", orderId="mm-1", status="ASSIGNED")
    voice_log.log_event("cancel", orderId="mm-1", serial="sim-1")

    assert (tmp_path / audio).read_bytes() == b"RIFF...."
    events = [json.loads(line) for line in (tmp_path / "voice.jsonl").read_text().splitlines()]
    assert [e["type"] for e in events] == ["voice", "order", "cancel"]
    assert events[0]["audio"] == audio and events[0]["mission"] == "mission_2" and "ts" in events[0]


def test_logs_full_decide_result(tmp_path, monkeypatch):
    """decide() returns a "kind" field; it must not clash with log_event's own argument."""
    monkeypatch.setattr(settings, "voice_log_dir", str(tmp_path))
    result = decide("Tonto, coge la pieza.")
    voice_log.log_event("voice", id="abc", **result)
    event = json.loads((tmp_path / "voice.jsonl").read_text())
    assert event["type"] == "voice" and event["kind"] == "mission" and event["mission"] == result["mission"]


def test_audio_off(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "voice_log_dir", str(tmp_path))
    monkeypatch.setattr(settings, "voice_log_audio", False)
    assert voice_log.save_audio("abc", b"x", "phrase.webm", "audio/webm") is None
    assert not (tmp_path / "audio").exists()


def test_log_off(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "voice_log_dir", "")
    voice_log.log_event("voice", id="abc")
    assert voice_log.save_audio("abc", b"x", "phrase.wav", "audio/wav") is None
    assert list(tmp_path.iterdir()) == []


def test_unwritable_dir_does_not_raise(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    monkeypatch.setattr(settings, "voice_log_dir", str(blocker / "logs"))
    voice_log.log_event("voice", id="abc")  # only a warning
    assert voice_log.save_audio("abc", b"x", "phrase.wav", "audio/wav") is None
