from skc.util import extract_urls, is_x_url


def test_extract_slack_wrapped_and_bare():
    text = (
        "see <https://x.com/u/status/1|this thread> and "
        "<https://example.com/post> plus bare https://twitter.com/a/status/2."
    )
    urls = extract_urls(text)
    assert urls == [
        "https://x.com/u/status/1",
        "https://example.com/post",
        "https://twitter.com/a/status/2",
    ]


def test_dedup_preserves_order():
    text = "<https://x.com/a> again <https://x.com/a>"
    assert extract_urls(text) == ["https://x.com/a"]


def test_is_x_url():
    assert is_x_url("https://x.com/u/status/1")
    assert is_x_url("https://www.twitter.com/u/status/1")
    assert not is_x_url("https://example.com")
