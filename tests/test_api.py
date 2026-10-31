"""API behaviour: health, validation, voice handling, detailed log switch, error capture."""

import main


def test_health_reports_mode_db_and_log_switch(client):
    h = client.get("/health").json()
    assert h["status"] == "healthy"
    assert h["mode"] == "offline-scripted"
    assert h["db"]["backend"] == "sqlite"
    assert h["detailed_log"] is True


def test_unknown_merchant_is_404(client):
    assert client.post("/api/tickets", json={"merchant_id": "M9999", "text": "hi"}).status_code == 404


def test_voice_needs_sarvam_in_offline_mode(client):
    r = client.post("/api/voice", data={"merchant_id": "M1001"}, files={"audio": ("a.webm", b"x" * 5000, "audio/webm")})
    assert r.status_code == 400


class FakeSTT:
    def __init__(self, transcript="", fail=False):
        self.transcript, self.fail, self.calls = transcript, fail, 0

    async def transcribe(self, audio_bytes, filename):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Failed to read the file, please check the audio format.")
        return self.transcript, "hi-IN"


def test_voice_too_short_is_rejected_before_calling_sarvam(client, monkeypatch):
    fake = FakeSTT("kuch bhi")
    monkeypatch.setattr(main, "stt", fake)
    r = client.post("/api/voice", data={"merchant_id": "M1001"}, files={"audio": ("a.webm", b"x" * 200, "audio/webm")})
    assert r.status_code == 422 and "Aawaz samajh nahi aayi" in r.json()["detail"]
    assert fake.calls == 0


def test_voice_stt_failure_gives_friendly_message_and_logs_details(client, monkeypatch, log_dir):
    monkeypatch.setattr(main, "stt", FakeSTT(fail=True))
    r = client.post("/api/voice", data={"merchant_id": "M1001"}, files={"audio": ("a.webm", b"x" * 5000, "audio/webm")})
    assert r.status_code == 422
    assert "headers" not in r.json()["detail"]                     # no raw internals for the merchant
    assert "Failed to read the file" in (log_dir / "errors.log").read_text()


def test_voice_happy_path_runs_the_agent(client, monkeypatch):
    monkeypatch.setattr(main, "stt", FakeSTT("Mera kal ka ₹4,500 ka settlement nahi aaya, TXN240001"))
    r = client.post("/api/voice", data={"merchant_id": "M1001"}, files={"audio": ("a.webm", b"x" * 5000, "audio/webm")})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "RESOLVED" and body["ticket_id"].startswith("V-")
    steps = [e["step"] for e in body["timing"]]
    assert "speech-to-text (Saaras) done" in steps and "response sent" in steps


def test_detailed_log_switch(client, log_dir):
    client.post("/api/debug", json={"enabled": False})
    assert client.get("/health").json()["detailed_log"] is False
    r = client.post("/api/tickets", json={"merchant_id": "M1003", "text": "TXN240004 ka status?", "ticket_id": "LOG-OFF"})
    assert "timing" not in r.json()
    assert "LOG-OFF" not in (log_dir / "timing.log").read_text()

    client.post("/api/debug", json={"enabled": True})
    r = client.post("/api/tickets", json={"merchant_id": "M1003", "text": "TXN240004 ka status?", "ticket_id": "LOG-ON"})
    steps = [e["step"] for e in r.json()["timing"]]
    assert steps[0] == "request received" and steps[-1] == "response sent"
    assert any(s.startswith("AI call #1") for s in steps) and any(s.startswith("tool ") for s in steps)
    assert "LOG-ON" in (log_dir / "timing.log").read_text()


def test_browser_timing_and_errors_reach_backend_logs(client, log_dir):
    client.post("/api/debug/client-timing", json={"trace_id": "V-TEST01", "events": [
        {"step": "button pressed", "at_ms": 0, "ms": None, "detail": ""},
        {"step": "mic ready", "at_ms": 108, "ms": 108, "detail": "MacBook Air Microphone"}]})
    assert "V-TEST01" in (log_dir / "timing.log").read_text()

    client.post("/api/debug/client-error", json={"where": "microphone", "message": "Permission denied", "trace_id": "V-TEST02"})
    assert "browser: microphone trace=V-TEST02 | Permission denied" in (log_dir / "errors.log").read_text()


def test_unexpected_error_is_logged_with_traceback_and_hidden_from_user(client, monkeypatch, log_dir):
    def boom():
        raise ValueError("synthetic failure for test")
    monkeypatch.setattr(main.store, "list_merchants", boom)
    r = client.get("/api/merchants")
    assert r.status_code == 500
    assert "synthetic" not in r.json()["detail"]
    errors = (log_dir / "errors.log").read_text()
    assert "GET /api/merchants" in errors and "Traceback" in errors and "synthetic failure for test" in errors


def test_reset_clears_agent_records(client):
    client.post("/api/tickets", json={"merchant_id": "M1001", "text": "TXN240001 nahi aaya", "ticket_id": "R-1"})
    assert client.get("/api/metrics").json()["tickets"] == 1
    client.post("/api/reset")
    assert client.get("/api/metrics").json()["tickets"] == 0
    assert client.get("/api/audit").json() == []
