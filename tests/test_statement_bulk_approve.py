from sqlmodel import Session, select

from proptally.db.models import Document, DocumentExtraction, Statement


def test_bulk_approve_only_validated_statements(owner):
    signed_in = owner()
    with Session(signed_in.engine) as session:
        for index, status in enumerate(("VALIDATED", "NEEDS_REVIEW")):
            session.add(Document(
                id=f"doc_bulk_{index}",
                file_sha256=f"sha_bulk_{index}",
                original_filename=f"statement-{index}.pdf",
                storage_path=f"/tmp/statement-{index}.pdf",
                file_size_bytes=100,
            ))
            session.add(DocumentExtraction(
                id=f"ext_bulk_{index}",
                document_id=f"doc_bulk_{index}",
                parser_name="TestParser",
                parser_version="1",
                validation_status=status,
                approval_status="PENDING",
                extracted_data_json="{}",
            ))
        session.commit()

    response = signed_in.client.post(
        "/statements/approve-ready",
        data={"csrf_token": signed_in.csrf()},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(signed_in.engine) as session:
        approved = session.get(DocumentExtraction, "ext_bulk_0")
        review = session.get(DocumentExtraction, "ext_bulk_1")
        assert approved.approval_status == "APPROVED"
        assert review.approval_status == "PENDING"
        # Approval does not import a statement or write to the ledger.
        assert session.exec(select(Statement)).all() == []
