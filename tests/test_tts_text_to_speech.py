import unmute.tts.text_to_speech as tts_text_to_speech
from unmute.tts.text_to_speech import prepare_text_for_tts


def test_prepare_text_for_tts_converts_common_units():
    text = "Vent: 12km/h, temperature: 21°C, humidite: 40%."
    assert (
        prepare_text_for_tts(text)
        == "Vent: 12 kilometre par heure, temperature: 21 degre, humidite: 40 pourcent."
    )


def test_prepare_text_for_tts_converts_mixed_units():
    text = "Distance 5km, taille 180cm, vitesse 2m/s."
    assert (
        prepare_text_for_tts(text)
        == "Distance 5 kilometre, taille 180 centimetre, vitesse 2 metre par seconde."
    )


def test_prepare_text_for_tts_converts_streamed_unit_chunks():
    assert prepare_text_for_tts("km/h") == "kilometre par heure"
    assert prepare_text_for_tts("°C") == "degre"


def test_prepare_text_for_tts_removes_hyphens_for_gpt_oss(monkeypatch):
    monkeypatch.setattr(tts_text_to_speech, "IS_GPT_OSS_MODEL", True)
    assert prepare_text_for_tts("state-of-the-art") == "state of the art"
