"""Protocol frames: additive versioning, permissive parsing, strict validation."""

from __future__ import annotations

from hermes_watch import protocol as p


def test_envelope_always_carries_the_version():
    assert p.envelope("anything")["v"] == p.PROTOCOL_VERSION


def test_event_frame_carries_correlation_id_only_when_given():
    assert "id" not in p.event("turn.started", turn_id="t1")
    frame = p.event("approval.requested", event_id="apv_1", command="rm -rf /tmp/x")
    assert frame["id"] == "apv_1"
    assert frame["payload"] == {"command": "rm -rf /tmp/x"}


def test_unknown_frame_types_and_fields_are_tolerated():
    # Additive evolution: an older bridge must ignore a newer app's frame.
    frame, error = p.parse_client_frame({"v": 1, "type": "subscribe", "fields": ["stats"], "future": True})
    assert error is None and frame["type"] == "subscribe"


def test_answer_requires_a_known_choice_and_an_id():
    _, error = p.parse_client_frame({"v": 1, "type": "answer", "choice": "once"})
    assert "no id" in error
    _, error = p.parse_client_frame({"v": 1, "type": "answer", "id": "apv_1", "choice": "maybe"})
    assert "unsupported choice" in error
    frame, error = p.parse_client_frame({"v": 1, "type": "answer", "id": "apv_1", "choice": "deny"})
    assert error is None and frame["choice"] == "deny"


def test_malformed_frames_are_rejected_with_a_reason():
    assert p.validation_error("not a dict") == "frame is not a JSON object"
    assert p.validation_error({"v": 1}) == "frame has no type"
    assert "bad protocol version" in p.validation_error({"v": 0, "type": "hello"})


def test_answer_accepts_reply_for_free_text_questions():
    frame, error = p.parse_client_frame({"v": 1, "type": "answer", "id": "qst_1", "choice": "reply"})
    assert error is None and frame["choice"] == "reply"


def test_hello_advertises_the_protocol_so_a_client_can_refuse_to_guess():
    frame = p.hello("0.1.0", 1234.5, "work")
    assert frame == {
        "v": 1, "type": "hello", "bridge_version": "0.1.0", "protocol": 1,
        "server_time": 1234.5, "profile": "work",
    }


def test_approval_choices_match_the_hermes_approval_transport_contract():
    # These four strings are Hermes' ApprovalChoice literals; a rename upstream
    # must fail loudly here rather than silently denying approvals on a watch.
    assert p.APPROVAL_CHOICES == ("once", "session", "always", "deny")
