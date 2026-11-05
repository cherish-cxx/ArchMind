import json

from app.core.events import sse_frame


def test_sse_frame_is_single_data_line():
    frame = sse_frame("step", {"detail": "a\nb", "n": 1})

    lines = frame.split("\n")
    assert lines[0] == "event: step"
    assert lines[1].startswith("data: ")
    assert lines[2] == ""
    assert frame.endswith("\n\n")
    assert json.loads(lines[1][6:]) == {"detail": "a\nb", "n": 1}
