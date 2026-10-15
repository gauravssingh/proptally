# ADR-007: Immutable Document Vault & Extraction Provenance

* **Status:** Accepted
* **Date:** October 2026
* **Deciders:** Senior Solution Architect, Security Architect

## Context
PropTally ingests official financial statement PDFs. In the event of a dispute, tax audit, or parsing discrepancy, the system must allow the user and their CPA to verify any extracted transaction or mortgage balance directly against the original unaltered source document.

We must decide how files are stored, deduplicated, and linked to extracted data.

## Decision
1. **Immutable Filesystem Vault:**
   Ingested PDF statements are permanently archived in a local vault directory structured as:
   `~/PropTally/vault/{institution_slug}/{year}/{random_uuid}.pdf`
   Files are set with read-only permissions (`chmod 400`) after ingestion and are never modified or overwritten.
2. **Cryptographic Deduplication:**
   Every document is fingerprinted with a SHA-256 hash upon receipt. If the hash matches an existing document, the upload is rejected as a duplicate.
3. **Extraction Provenance:**
   Every parsed transaction and mortgage period record stores:
   - `statement_id` $\rightarrow$ foreign key to `statements` and `documents` table.
   - `parser_name` and `parser_version`.
   - `source_page_number`.
   - Coordinate bounding boxes (where available from PyMuPDF).

## Consequences
### Positive
* **Defensible Tax Workpapers:** Under IRS audit, every reported deduction or interest expense can be cross-referenced to the exact page of the underlying statement with 1 click.
* **Non-Destructive Re-Extraction:** If a parser is upgraded with improved regex patterns, statements can be re-parsed without re-uploading original files.
* **Storage Efficiency:** SHA-256 deduplication prevents disk bloat from accidental re-uploads.

### Negative / Trade-Offs
* Requires disk storage management on the host, though vector PDF statements typically consume $< 1$ MB per statement.
