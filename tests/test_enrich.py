from skc.enrich.links import _is_challenge_title, _slug_title


def test_slug_title_strips_medium_hash():
    assert (
        _slug_title("https://dr-arsanjani.medium.com/the-agent-ecosystem-formula-c08c041bb744")
        == "The agent ecosystem formula"
    )


def test_slug_title_handles_publication_domain():
    assert (
        _slug_title("https://ai.gopubby.com/i-ran-codex-and-claude-ee16ea991838")
        == "I ran codex and claude"
    )


def test_slug_title_none_for_opaque_paths():
    assert _slug_title("https://medium.com/@user/p/abc123") is None
    assert _slug_title("https://example.com/") is None
    assert _slug_title("https://example.com") is None


def test_challenge_title_detection():
    assert _is_challenge_title("Just a moment...")
    assert _is_challenge_title("Attention Required! | Cloudflare")
    assert not _is_challenge_title("The Agent Ecosystem Formula")
    assert not _is_challenge_title(None)
