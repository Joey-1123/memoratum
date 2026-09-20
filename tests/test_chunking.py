"""Chunking contract (RED)."""


def test_recursive_split_respects_size_and_overlap() -> None:
    from memoratum.chunking import split_text

    text = "Para one is here. " * 60 + "\n\n" + "Para two here. " * 60
    chunks = split_text(text, chunk_size=200, overlap=40)
    assert len(chunks) > 1
    assert all(len(c) <= 240 for c in chunks)
    assert chunks[0] in text


def test_markdown_chunks_carry_headings() -> None:
    from memoratum.chunking import split_markdown

    md = "# Setup\n\nInstall with pip.\n\n## Config\n\nSet the key.\n"
    chunks = split_markdown(md, chunk_size=200)
    assert len(chunks) == 2
    assert chunks[0]["heading"] == "Setup"
    assert chunks[1]["heading"] == "Config"
    assert chunks[1]["text"].startswith("# Config")


def test_code_fence_kept_intact_when_small() -> None:
    from memoratum.chunking import split_markdown

    md = "# T\n\nSome intro text here.\n\n```python\nprint(1)\n```\n"
    chunks = split_markdown(md, chunk_size=500)
    assert any("```python" in c["text"] and "print(1)" in c["text"] for c in chunks)
