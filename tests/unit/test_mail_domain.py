"""Mail domain invariants and reconciliation matching (ADR-0020)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from assistant.domain.errors import InvalidMailboxState, InvalidMailMessage
from assistant.domain.mail import (
    MailAttachmentMetadata,
    MailBodyStatus,
    MailboxSyncMode,
    MailboxSyncState,
    MailMessage,
    MailMessageLocation,
    ReconciliationCandidates,
    ReconciliationEvidence,
    choose_reconciliation_match,
    mail_content_fingerprint,
    new_mail_message_id,
    normalize_header_value,
    normalize_message_id,
    validate_account_id,
)

NOW = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64
RAW_SHA = "b" * 64


def _message(**overrides: object) -> MailMessage:
    values: dict[str, object] = {
        "account_id": "smail",
        "content_fingerprint": FINGERPRINT,
        "size_bytes": 1200,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "raw_sha256": RAW_SHA,
        "raw_storage_key": f"mail/raw/bb/{RAW_SHA}.eml",
    }
    values.update(overrides)
    return MailMessage(**values)  # type: ignore[arg-type]


def test_account_ids_are_stable_local_identifiers() -> None:
    assert validate_account_id("smail") == "smail"
    assert validate_account_id("work-mail-2") == "work-mail-2"
    for invalid in ("Smail", "1mail", "mail_1", "student@example.edu", "imap.example.edu", ""):
        with pytest.raises(InvalidMailMessage):
            validate_account_id(invalid)


def test_headers_are_normalised_and_bounded() -> None:
    assert normalize_header_value("  hello\n   world ", limit=50) == "hello world"
    assert normalize_header_value("   ", limit=50) is None
    assert normalize_header_value(None, limit=50) is None
    long_value = normalize_header_value("x" * 5000, limit=2000)
    assert long_value is not None and len(long_value) == 2000
    assert long_value.endswith("\u2026")


def test_a_message_id_is_normalised_but_never_required() -> None:
    assert normalize_message_id("  <a@example.edu>  ") == "<a@example.edu>"
    assert normalize_message_id(None) is None
    assert _message(message_id_header=None).message_id_header is None


def test_a_message_needs_a_valid_account_fingerprint_and_aware_times() -> None:
    message = _message()

    assert message.body_available is True
    assert message.id is not None
    with pytest.raises(InvalidMailMessage):
        _message(account_id="Smail")
    with pytest.raises(InvalidMailMessage):
        _message(content_fingerprint="nope")
    with pytest.raises(InvalidMailMessage):
        _message(raw_sha256="short")
    with pytest.raises(InvalidMailMessage):
        _message(size_bytes=-1)
    with pytest.raises(InvalidMailMessage):
        _message(first_seen_at=datetime(2026, 9, 21))
    with pytest.raises(InvalidMailMessage):
        _message(raw_storage_key="mail/raw/x.eml", raw_sha256=None)


def test_an_oversize_message_has_no_body_and_no_raw_object() -> None:
    oversize = _message(
        body_status=MailBodyStatus.OVERSIZE, raw_sha256=None, raw_storage_key=None
    )

    assert oversize.body_available is False
    assert oversize.body_text is None
    with pytest.raises(InvalidMailMessage):
        _message(
            body_status=MailBodyStatus.OVERSIZE,
            body_text="should not be here",
            raw_sha256=None,
            raw_storage_key=None,
        )


def test_an_available_body_needs_something_to_read() -> None:
    # A message with no text is still available as long as its raw bytes were stored.
    assert _message(body_text=None).body_available is True
    with pytest.raises(InvalidMailMessage):
        _message(body_text=None, raw_sha256=None, raw_storage_key=None)


def test_an_oversize_message_can_still_carry_header_metadata() -> None:
    oversize = _message(
        body_status=MailBodyStatus.OVERSIZE,
        raw_sha256=None,
        raw_storage_key=None,
        subject="A very large attachment",
        size_bytes=30 * 1024 * 1024,
    )

    assert oversize.subject == "A very large attachment"
    assert oversize.size_bytes == 30 * 1024 * 1024


def test_a_location_identity_is_account_mailbox_uidvalidity_and_uid() -> None:
    location = MailMessageLocation(
        message_id=new_mail_message_id(),
        account_id="smail",
        mailbox_name="INBOX",
        uidvalidity=10,
        uid=101,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )

    assert location.identity == ("smail", "INBOX", 10, 101)
    with pytest.raises(InvalidMailboxState):
        MailMessageLocation(
            message_id=location.message_id,
            account_id="smail",
            mailbox_name="INBOX",
            uidvalidity=0,
            uid=1,
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
    with pytest.raises(InvalidMailboxState):
        MailMessageLocation(
            message_id=location.message_id,
            account_id="smail",
            mailbox_name="INBOX",
            uidvalidity=1,
            uid=0,
            first_seen_at=NOW,
            last_seen_at=NOW,
        )


def test_attachment_metadata_is_bounded_and_hashed() -> None:
    attachment = MailAttachmentMetadata(
        message_id=uuid4(),
        ordinal=0,
        size_bytes=10,
        sha256="c" * 64,
        filename="report.pdf",
        content_type="application/pdf",
        content_disposition="attachment",
    )

    assert attachment.filename == "report.pdf"
    with pytest.raises(InvalidMailMessage):
        MailAttachmentMetadata(
            message_id=uuid4(), ordinal=-1, size_bytes=1, sha256="c" * 64
        )
    with pytest.raises(InvalidMailMessage):
        MailAttachmentMetadata(
            message_id=uuid4(), ordinal=0, size_bytes=1, sha256="not-a-hash"
        )


def test_a_malicious_filename_is_only_a_string() -> None:
    """Nothing here is a path: Phase 5A never writes an attachment by name."""
    attachment = MailAttachmentMetadata(
        message_id=uuid4(),
        ordinal=0,
        size_bytes=3,
        sha256="d" * 64,
        filename="../../../../etc/passwd",
    )

    assert attachment.filename == "../../../../etc/passwd"


def test_sync_state_is_a_cursor_not_a_promise() -> None:
    state = MailboxSyncState(
        account_id="smail",
        mailbox_name="INBOX",
        uidvalidity=10,
        last_seen_uid=100,
        updated_at=NOW,
        mode=MailboxSyncMode.NORMAL,
        last_sync_at=NOW,
    )

    assert state.next_uid_to_fetch() == 101
    assert state.last_reconciled_at is None
    with pytest.raises(InvalidMailboxState):
        MailboxSyncState(
            account_id="smail",
            mailbox_name="INBOX",
            uidvalidity=0,
            last_seen_uid=0,
            updated_at=NOW,
        )
    with pytest.raises(InvalidMailboxState):
        MailboxSyncState(
            account_id="smail",
            mailbox_name="INBOX",
            uidvalidity=1,
            last_seen_uid=-1,
            updated_at=NOW,
        )


# ------------------------------------------------------------------ fingerprints


def test_the_content_fingerprint_is_canonical_and_sensitive() -> None:
    base = dict(
        message_id_header="<a@example.edu>",
        from_address="ada@example.edu",
        date_header="Fri, 23 Oct 2026 23:59:00 +0800",
        subject="Deadline",
        body_text="The deadline is Friday.",
        attachment_sha256s=("e" * 64,),
        size_bytes=1200,
    )

    first = mail_content_fingerprint(**base)
    assert first == mail_content_fingerprint(**base)  # deterministic
    assert len(first) == 64
    assert first != mail_content_fingerprint(**{**base, "body_text": "Something else."})
    assert first != mail_content_fingerprint(**{**base, "size_bytes": 1201})
    assert first != mail_content_fingerprint(
        **{**base, "attachment_sha256s": ("f" * 64,)}
    )


def test_the_content_fingerprint_is_not_the_raw_hash() -> None:
    fingerprint = mail_content_fingerprint(
        message_id_header=None,
        from_address=None,
        date_header=None,
        subject=None,
        body_text=None,
        attachment_sha256s=(),
        size_bytes=0,
    )

    assert fingerprint != RAW_SHA


# --------------------------------------------------------------- reconciliation


def _candidates(**overrides: object) -> ReconciliationCandidates:
    values: dict[str, object] = {
        "by_raw_sha256": None,
        "by_message_id": (),
        "by_fingerprint": (),
    }
    values.update(overrides)
    return ReconciliationCandidates(**values)  # type: ignore[arg-type]


def _evidence(**overrides: object) -> ReconciliationEvidence:
    values: dict[str, object] = {
        "content_fingerprint": FINGERPRINT,
        "size_bytes": 1200,
        "raw_sha256": None,
        "message_id_header": None,
        "from_address": None,
        "date_header": None,
    }
    values.update(overrides)
    return ReconciliationEvidence(**values)  # type: ignore[arg-type]


def test_identical_bytes_are_the_same_message() -> None:
    stored = _message()

    decision = choose_reconciliation_match(
        _evidence(raw_sha256=RAW_SHA), _candidates(by_raw_sha256=stored)
    )

    assert decision.matched_message_id == stored.id
    assert decision.conflicted is False


def test_the_same_message_id_and_fingerprint_match() -> None:
    stored = _message(message_id_header="<a@example.edu>")

    decision = choose_reconciliation_match(
        _evidence(message_id_header="<a@example.edu>"),
        _candidates(by_message_id=(stored,)),
    )

    assert decision.matched_message_id == stored.id


def test_the_same_message_id_with_different_content_is_a_conflict() -> None:
    """Two real messages can share a Message-ID; merging them would destroy one."""
    stored = _message(message_id_header="<a@example.edu>", content_fingerprint="9" * 64)

    decision = choose_reconciliation_match(
        _evidence(message_id_header="<a@example.edu>"),
        _candidates(by_message_id=(stored,)),
    )

    assert decision.matched_message_id is None
    assert decision.conflicted is True


def test_a_matching_fingerprint_needs_corroborating_evidence_to_merge() -> None:
    stored = _message(
        message_id_header=None,
        from_address="ada@example.edu",
        date_header="Fri, 23 Oct 2026 23:59:00 +0800",
        size_bytes=1200,
    )

    matching = choose_reconciliation_match(
        _evidence(
            from_address="ada@example.edu",
            date_header="Fri, 23 Oct 2026 23:59:00 +0800",
        ),
        _candidates(by_fingerprint=(stored,)),
    )
    differing = choose_reconciliation_match(
        _evidence(
            from_address="someone-else@example.edu",
            date_header="Fri, 23 Oct 2026 23:59:00 +0800",
        ),
        _candidates(by_fingerprint=(stored,)),
    )

    assert matching.matched_message_id == stored.id
    assert differing.matched_message_id is None
    assert differing.conflicted is False


def test_a_fingerprint_with_a_different_size_does_not_merge() -> None:
    stored = _message(
        message_id_header=None, from_address="a@b", date_header="d", size_bytes=1200
    )

    decision = choose_reconciliation_match(
        _evidence(from_address="a@b", date_header="d", size_bytes=9999),
        _candidates(by_fingerprint=(stored,)),
    )

    assert decision.matched_message_id is None


def test_no_evidence_means_no_match() -> None:
    decision = choose_reconciliation_match(_evidence(), _candidates())

    assert decision.matched_message_id is None
    assert decision.conflicted is False


def test_mail_messages_never_carry_a_raw_imap_uid() -> None:
    fields = set(MailMessage.__dataclass_fields__)

    assert "uid" not in fields
    assert "uidvalidity" not in fields
    assert {"uid", "uidvalidity"} <= set(MailMessageLocation.__dataclass_fields__)


def test_timestamps_are_aware_everywhere() -> None:
    naive = datetime(2026, 9, 21, 0, 0)
    with pytest.raises(InvalidMailMessage):
        _message(last_seen_at=naive)
    with pytest.raises(InvalidMailMessage):
        _message(sent_at=naive)
    with pytest.raises(InvalidMailboxState):
        MailboxSyncState(
            account_id="smail",
            mailbox_name="INBOX",
            uidvalidity=1,
            last_seen_uid=0,
            updated_at=naive,
        )
    assert _message(sent_at=NOW + timedelta(hours=1)).sent_at is not None
