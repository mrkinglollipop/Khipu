from khipu import routing


def test_a_pull_namespace_id_never_takes_an_authors_forms():
    # A pull caller names its own session id, so one that names an author's
    # raw id must not match that author's replacement notices.
    for author in ("abc", "claude:abc", "codex:abc", "t3:shared"):
        raw = author.split(":", 1)[1] if author.startswith(("claude:", "codex:")) else author
        forms = routing._session_forms("pull\0" + raw)
        assert forms == {"pull\0" + raw}
        assert not forms & routing._session_forms(author)


def test_hook_forms_still_join_raw_and_canonical():
    assert routing._session_forms("abc") == {"abc", "claude:abc", "codex:abc"}
    assert routing._session_forms("claude:abc") == {"claude:abc", "abc"}
