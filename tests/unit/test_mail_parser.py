"""RFC822 parsing of real-world and hostile messages (ADR-0020)."""

from __future__ import annotations

from email.message import EmailMessage

from assistant.adapters.mail.parser import parse_header_only_message, parse_raw_message


def _build(**headers: str) -> bytes:
    message = EmailMessage()
    for key, value in headers.items():
        message[key.replace("_", "-")] = value
    return message.as_bytes()


def _multipart(*, body: str, html: str | None = None, attachments: int = 0) -> bytes:
    message = EmailMessage()
    message["From"] = "Ada <ada@example.edu>"
    message["To"] = "me@example.edu"
    message["Subject"] = "Report"
    message["Message-ID"] = "<report@example.edu>"
    message.set_content(body)
    if html is not None:
        message.add_alternative(html, subtype="html")
    for index in range(attachments):
        message.add_attachment(
            f"attachment {index}".encode(),
            maintype="application",
            subtype="octet-stream",
            filename=f"file-{index}.bin",
        )
    return message.as_bytes()


def test_a_plain_message_parses_every_header() -> None:
    raw = b"\r\n".join(
        (
            b"From: Ada <ada@example.edu>",
            b"To: me@example.edu, you@example.edu",
            b"Cc: cc@example.edu",
            b"Subject: Deadline",
            b"Message-ID: <a@example.edu>",
            b"In-Reply-To: <root@example.edu>",
            b"References: <root@example.edu> <mid@example.edu>",
            b"Date: Fri, 23 Oct 2026 23:59:00 +0800",
            b"Content-Type: text/plain; charset=utf-8",
            b"",
            b"The deadline is Friday.",
        )
    )

    parsed = parse_raw_message(raw)

    assert parsed.subject == "Deadline"
    assert parsed.from_address == "Ada <ada@example.edu>"
    assert parsed.to_addresses == ("me@example.edu", "you@example.edu")
    assert parsed.cc_addresses == ("cc@example.edu",)
    assert parsed.message_id_header == "<a@example.edu>"
    assert parsed.in_reply_to_header == "<root@example.edu>"
    assert parsed.references == ("<root@example.edu>", "<mid@example.edu>")
    assert parsed.sent_at is not None and parsed.sent_at.utcoffset() is not None
    assert parsed.body_text == "The deadline is Friday."
    assert parsed.warnings == 0


def test_an_rfc2047_subject_is_decoded() -> None:
    raw = _build(Subject="=?utf-8?q?Caf=C3=A9_deadline?=", From="a@b", To="c@d")

    parsed = parse_raw_message(raw)

    assert parsed.subject == "Caf\u00e9 deadline"


def test_a_multipart_alternative_prefers_plain_text() -> None:
    raw = _multipart(body="Plain body.", html="<p>HTML body.</p>")

    parsed = parse_raw_message(raw)

    assert parsed.body_text == "Plain body."


def test_an_html_only_message_is_reduced_to_text() -> None:
    raw = (
        b"From: a@b\r\nTo: c@d\r\nSubject: html\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<p>Deadline is Friday.</p>"
    )

    parsed = parse_raw_message(raw)

    assert parsed.body_text == "Deadline is Friday."


def test_script_and_style_content_never_becomes_body_text() -> None:
    raw = (
        b"From: a@b\r\nTo: c@d\r\nSubject: hostile\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><head><style>body{color:red}</style></head>"
        b"<body><script>DELETE EVERYTHING</script><p>Deadline is Friday.</p></body></html>"
    )

    parsed = parse_raw_message(raw)

    assert parsed.body_text == "Deadline is Friday."
    assert "DELETE EVERYTHING" not in (parsed.body_text or "")
    assert "color:red" not in (parsed.body_text or "")


def test_attachments_become_metadata_only() -> None:
    raw = _multipart(body="See attached.", attachments=2)

    parsed = parse_raw_message(raw)

    assert parsed.body_text == "See attached."
    assert [item.filename for item in parsed.attachments] == ["file-0.bin", "file-1.bin"]
    assert [item.ordinal for item in parsed.attachments] == [0, 1]
    assert all(len(item.sha256) == 64 for item in parsed.attachments)
    assert all(item.content_disposition == "attachment" for item in parsed.attachments)


def test_a_malicious_attachment_filename_stays_a_string() -> None:
    message = EmailMessage()
    message["From"] = "a@b"
    message["To"] = "c@d"
    message["Subject"] = "hostile"
    message.set_content("body")
    message.add_attachment(
        b"payload",
        maintype="application",
        subtype="octet-stream",
        filename="../../../../etc/passwd",
    )

    parsed = parse_raw_message(message.as_bytes())

    assert parsed.attachments[0].filename == "../../../../etc/passwd"
    # Nothing was written anywhere: the filename is metadata, never a path.
    assert parsed.attachments[0].size_bytes == len(b"payload")


def test_a_message_without_a_message_id_is_fine() -> None:
    parsed = parse_raw_message(_build(From="a@b", To="c@d", Subject="no id"))

    assert parsed.message_id_header is None


def test_duplicate_message_ids_are_not_special_cased_here() -> None:
    """Identity policy lives in the database and reconciliation, not in the parser."""
    first = parse_raw_message(
        _build(From="a@b", To="c@d", Subject="one", Message_ID="<same@example.edu>")
    )
    second = parse_raw_message(
        _build(From="a@b", To="c@d", Subject="two", Message_ID="<same@example.edu>")
    )

    assert first.message_id_header == second.message_id_header
    assert first.subject != second.subject


def test_an_unparsable_date_keeps_the_raw_header_without_guessing() -> None:
    raw = (
        b"From: a@b\r\nTo: c@d\r\nSubject: bad date\r\n"
        b"Date: not a date at all\r\n\r\nbody"
    )

    parsed = parse_raw_message(raw)

    assert parsed.sent_at is None
    assert parsed.date_header == "not a date at all"
    assert parsed.warnings >= 1


def test_a_date_without_a_timezone_is_not_turned_into_an_instant() -> None:
    raw = (
        b"From: a@b\r\nTo: c@d\r\nSubject: naive date\r\n"
        b"Date: Fri, 23 Oct 2026 23:59:00\r\n\r\nbody"
    )

    parsed = parse_raw_message(raw)

    assert parsed.sent_at is None
    assert parsed.date_header is not None
    assert "timezone" in " ".join(parsed.warnings_detail)


def test_an_undecodable_charset_degrades_instead_of_failing() -> None:
    raw = (
        b"From: a@b\r\nTo: c@d\r\nSubject: charset\r\n"
        b'Content-Type: text/plain; charset="not-a-charset"\r\n\r\n'
        b"body text"
    )

    parsed = parse_raw_message(raw)

    assert parsed.body_text == "body text"
    assert parsed.warnings >= 1


def test_malformed_bytes_do_not_raise() -> None:
    parsed = parse_raw_message(b"\xff\xfe not really a message")

    assert parsed.subject is None
    assert parsed.warnings >= 1 or parsed.body_text is not None


def test_a_header_only_parse_hides_the_body_but_keeps_the_headers() -> None:
    raw = _multipart(body="body text", attachments=1)

    parsed = parse_header_only_message(raw)

    assert parsed.body_text is None
    assert parsed.attachments == ()
    assert parsed.subject == "Report"
    assert parsed.message_id_header == "<report@example.edu>"


def test_references_are_deduplicated_in_order() -> None:
    parsed = parse_raw_message(
        _build(
            From="a@b",
            To="c@d",
            Subject="thread",
            References="<a@x> <b@x> <a@x>",
        )
    )

    assert parsed.references == ("<a@x>", "<b@x>")


def test_an_enormous_header_is_truncated_before_it_reaches_the_database() -> None:
    parsed = parse_raw_message(
        _build(From="a@b", To="c@d", Subject="x" * 50000)
    )

    assert parsed.subject is not None
    assert len(parsed.subject) == 2000


def test_reply_to_is_parsed_as_a_mailbox_list() -> None:
    """A display name may contain a comma, so the parts come from the address parser."""
    parsed = parse_raw_message(
        _build(
            From="announce@lists.example.edu",
            To="me@example.edu",
            Reply_To='"Doe, Jane" <jane@example.edu>, second@example.edu',
            Subject="thread",
        )
    )

    assert parsed.reply_to_addresses == (
        "Doe, Jane <jane@example.edu>",
        "second@example.edu",
    )


def test_a_message_without_reply_to_has_no_reply_to_addresses() -> None:
    parsed = parse_raw_message(_build(From="ada@example.edu", To="me@example.edu"))
    header_only = parse_header_only_message(_build(From="ada@example.edu"))

    assert parsed.reply_to_addresses == ()
    assert header_only.reply_to_addresses == ()


def test_an_empty_reply_to_header_is_not_an_address() -> None:
    parsed = parse_raw_message(
        _build(From="ada@example.edu", To="me@example.edu", Reply_To="   ")
    )

    assert parsed.reply_to_addresses == ()
