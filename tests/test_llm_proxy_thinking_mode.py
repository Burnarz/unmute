from unmute.llm_proxy.adapters.openai import apply_openai_thinking


def test_apply_openai_thinking_off():
    out = apply_openai_thinking({"model": "x", "messages": []}, "off")
    assert out["thinking"] is False
    assert out["reasoning"] == {"enabled": False}


def test_apply_openai_thinking_level():
    out = apply_openai_thinking({"model": "x", "messages": []}, "medium")
    assert out["thinking"] == "medium"
    assert out["reasoning_effort"] == "medium"
    assert out["reasoning"] == {"effort": "medium"}
