from unmute.llm_proxy.main import _iter_text_chunks


def test_iter_text_chunks_preserves_whitespace_exactly():
    text = "le silence s'installe.\nSi vous avez besoin d'autre chose plus tard."
    chunks = _iter_text_chunks(text)

    assert "".join(chunks) == text
    assert all(chunk != "" for chunk in chunks)
