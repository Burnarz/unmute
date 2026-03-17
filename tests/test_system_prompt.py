from unittest.mock import patch

from unmute.llm.system_prompt import ConstantInstructions


def test_constant_instructions_include_current_time_context():
    with patch(
        "unmute.llm.system_prompt.get_current_time_context",
        return_value={
            "current_time": "Wednesday, March 18, 2026 at 14:30",
            "timezone": "CET",
        },
    ), patch("unmute.llm.system_prompt.autoselect_model", return_value="test-model"):
        prompt = ConstantInstructions(text="Be concise.").make_system_prompt()

    assert "Be concise." in prompt
    assert "It's currently Wednesday, March 18, 2026 at 14:30 in your timezone (CET)." in prompt
