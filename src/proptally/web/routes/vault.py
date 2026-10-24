"""The document vault: the portfolio's documents, navigated as folders.

The model is the one a file manager uses. Folders are opened, not filtered: the
tree down the left shows every folder with what it holds, the middle shows the
folder you are in — its subfolders first, then the documents inside it — and the
pane on the right is the inspector for the one document you have chosen.

Every piece of that state is in the URL and rendered on the server: which folder
(`property`, `type`), which chip (`kind`), how the files are ordered (`sort`),
which document is being inspected (`doc`) and which tab of its inspector is open
(`tab`). Nothing here needs script to work, and any view of it can be shared.

The folder tree is two levels deep and follows the way an owner thinks about a
document: **which property is it for**, then **what kind of document is it**. A
document with no property sits under `Unfiled`; one with no type sits under
`Unclassified`, so a document is never unreachable for want of a field.

Search is the exception to the folder model, deliberately: it looks through every
folder rather than the open one, because a person hunting for a file should not have
to guess where somebody else filed it. The tree's counts follow the search, so the
folder holding a match is visible from the tree itself.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import desc
from sqlmodel import Session, select
from starlette.background import BackgroundTask

from proptally.db.models import (
    NON_STATEMENT_DOCUMENT_TYPES,
    Document,
    DocumentExtraction,
    FinancialAccount,
    Property,
    Statement,
)
from proptally.ingestion.service import MAX_UPLOAD_BYTES, store_document
from proptally.web.auth import current_context

logger = logging.getLogger(__name__)

#: The vocabulary `documents.document_type` uses, and the words the vault shows.
DOCUMENT_TYPES: tuple[tuple[str, str], ...] = (
    ("MORTGAGE_STATEMENT", "Mortgage statement"),
    ("CHECKING_STATEMENT", "Bank statement"),
    ("CREDIT_CARD_STATEMENT", "Credit card statement"),
    ("LEASE_AGREEMENT", "Lease agreement"),
    ("INSURANCE_POLICY", "Insurance policy"),
    ("PROPERTY_TAX", "Property tax"),
    ("PROPERTY_DEED", "Property deed / closing"),
    ("HOA_STATEMENT", "HOA statement"),
    ("OTHER", "Other"),
)
TYPE_LABELS: dict[str, str] = dict(DOCUMENT_TYPES)

#: The two folders a document lands in when the matching field is empty.
UNFILED = "unfiled"
UNCLASSIFIED = "unclassified"

STATUS_LABELS = {
    "review": "Needs review",
    "ready": "Ready to approve",
    "approved": "Approved",
    "none": "No extraction",
    "vaulted": "Vaulted",
}

STATUS_VARIANTS = {
    "review": "warning",
    "ready": "info",
    "approved": "success",
    "none": "neutral",
    "vaulted": "neutral",
}

PAGE_SIZES = ("25", "50", "100", "ALL")

SORTS: tuple[tuple[str, str], ...] = (
    ("date", "Uploaded"),
    ("name", "Document"),
    ("location", "Location"),
    ("size", "Size"),
    ("status", "Status"),
)

INSPECTOR_TABS: tuple[tuple[str, str], ...] = (
    ("preview", "Preview"),
    ("details", "Details"),
)


def human_size(num_bytes: int) -> str:
    """A file size a person can read at a glance."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def _extraction_state(extraction: DocumentExtraction | None) -> str:
    if extraction is None:
        return "none"
    if extraction.approval_status == "APPROVED":
        return "approved"
    if extraction.validation_status == "NEEDS_REVIEW":
        return "review"
    return "ready"


def _build_document_accounts(
    session: Session,
    documents: list[Document],
    latest_extractions: dict[str, DocumentExtraction],
) -> dict[str, dict]:
    """Map each financial document to its FinancialAccount or inferred account grouping."""
    statements = session.exec(select(Statement)).all()
    stmt_by_doc = {s.document_id: s for s in statements}

    accounts = session.exec(select(FinancialAccount)).all()
    acct_map = {a.id: a for a in accounts}

    doc_accounts: dict[str, dict] = {}
    for doc in documents:
        # Check Statement table first
        stmt = stmt_by_doc.get(doc.id)
        if stmt and stmt.financial_account_id in acct_map:
            fa = acct_map[stmt.financial_account_id]
            is_chk = fa.account_type == "CHECKING"
            doc_accounts[doc.id] = {
                "key": fa.id,
                "name": fa.account_name,
                "group": "checking" if is_chk else "credit_card",
                "group_label": "Checking Accounts" if is_chk else "Credit Cards",
                "icon": "bank" if is_chk else "card",
            }
            continue

        # Check Extraction data
        ext = latest_extractions.get(doc.id)
        if ext and ext.extracted_data_json:
            try:
                data = json.loads(ext.extracted_data_json)
                fa_id = data.get("financial_account_id")
                if fa_id and fa_id in acct_map:
                    fa = acct_map[fa_id]
                    is_chk = fa.account_type == "CHECKING"
                    doc_accounts[doc.id] = {
                        "key": fa.id,
                        "name": fa.account_name,
                        "group": "checking" if is_chk else "credit_card",
                        "group_label": "Checking Accounts" if is_chk else "Credit Cards",
                        "icon": "bank" if is_chk else "card",
                    }
                    continue

                last4 = data.get("account_last4")
                parser = (ext.parser_name or "").lower()
                stmt_type = data.get("statement_type")
                fn_lower = (doc.original_filename or "").lower()

                matched_fa = None
                for fa in accounts:
                    if last4 and fa.account_number_masked and last4 in fa.account_number_masked:
                        matched_fa = fa
                        break
                    if ("apple" in parser or "apple" in fn_lower) and "apple" in fa.account_name.lower():
                        matched_fa = fa
                        break

                if matched_fa:
                    is_chk = matched_fa.account_type == "CHECKING"
                    doc_accounts[doc.id] = {
                        "key": matched_fa.id,
                        "name": matched_fa.account_name,
                        "group": "checking" if is_chk else "credit_card",
                        "group_label": "Checking Accounts" if is_chk else "Credit Cards",
                        "icon": "bank" if is_chk else "card",
                    }
                    continue

                if last4:
                    is_chk = (stmt_type == "CHECKING" or doc.document_type == "CHECKING_STATEMENT")
                    acct_name = f"Checking (...{last4})" if is_chk else f"Credit Card (...{last4})"
                    doc_accounts[doc.id] = {
                        "key": f"acct_{last4}",
                        "name": acct_name,
                        "group": "checking" if is_chk else "credit_card",
                        "group_label": "Checking Accounts" if is_chk else "Credit Cards",
                        "icon": "bank" if is_chk else "card",
                    }
                    continue
            except Exception:
                pass

        # Fallback based on document_type
        if doc.document_type == "CHECKING_STATEMENT":
            doc_accounts[doc.id] = {
                "key": "checking_general",
                "name": "Checking Account",
                "group": "checking",
                "group_label": "Checking Accounts",
                "icon": "bank",
            }
        elif doc.document_type == "CREDIT_CARD_STATEMENT":
            doc_accounts[doc.id] = {
                "key": "credit_card_general",
                "name": "Credit Card",
                "group": "credit_card",
                "group_label": "Credit Cards",
                "icon": "card",
            }

    return doc_accounts


def _document_row(
    document: Document,
    extraction: DocumentExtraction | None,
    property_record: Property | None,
    account_info: dict | None = None,
) -> dict:
    """One document, with the section and folder it belongs to worked out."""
    is_non_statement = document.document_type in NON_STATEMENT_DOCUMENT_TYPES
    effective_extraction = None if is_non_statement else extraction
    uploaded: datetime = document.uploaded_at
    state = "vaulted" if is_non_statement else _extraction_state(effective_extraction)
    mime = document.mime_type or ""
    fn = (document.original_filename or "").lower()
    if mime == "application/pdf" or fn.endswith(".pdf"):
        file_icon = "file-pdf"
    elif mime.startswith("image/") or fn.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")):
        file_icon = "image"
    elif "csv" in mime or "sheet" in mime or "excel" in mime or fn.endswith((".csv", ".xlsx", ".xls")):
        file_icon = "file-spreadsheet"
    elif mime.startswith("text/") or fn.endswith((".txt", ".md", ".json")):
        file_icon = "file-text"
    else:
        file_icon = "file"

    type_key = document.document_type or UNCLASSIFIED
    type_label = TYPE_LABELS.get(document.document_type or "", "Unclassified")

    if property_record is not None:
        section = "property"
        property_key = document.property_id
        property_name = property_record.name
        folder_key = document.property_id
        folder_label = property_record.name
        location_label = f"{property_record.name} / {type_label}"
        tag_folder = property_record.name
    elif account_info is not None:
        section = "financial"
        property_key = None
        property_name = account_info["name"]
        folder_key = account_info["key"]
        folder_label = account_info["name"]
        location_label = f"{account_info['name']} / {type_label}"
        tag_folder = account_info["name"]
    else:
        section = "inbox"
        property_key = UNFILED
        property_name = "Unfiled"
        folder_key = UNFILED
        folder_label = "Unfiled"
        location_label = f"Unfiled / {type_label}"
        tag_folder = "Unfiled"

    return {
        "id": document.id,
        "filename": document.original_filename,
        "mime_type": mime,
        "kind": "image" if mime.startswith("image/") else "pdf" if mime == "application/pdf" else "other",
        "file_icon": file_icon,
        "can_preview": mime.startswith("image/") or mime == "application/pdf",
        "section": section,
        "property_key": property_key,
        "property_name": property_name,
        "account_key": account_info["key"] if account_info else None,
        "account_name": account_info["name"] if account_info else None,
        "account_group": account_info["group"] if account_info else None,
        "folder_key": folder_key,
        "folder_label": folder_label,
        "location_label": location_label,
        "tag_folder": tag_folder,
        "type_key": type_key,
        "type_label": type_label,
        "size_bytes": document.file_size_bytes,
        "size": human_size(document.file_size_bytes),
        "type_short": (mime.split("/")[-1].upper() if mime else "—"),
        "uploaded_at": uploaded,
        "uploaded": uploaded.strftime("%d %b %Y"),
        "extraction_id": effective_extraction.id if effective_extraction else None,
        "extraction_status": effective_extraction.validation_status if effective_extraction else None,
        "state": state,
        "state_label": STATUS_LABELS[state],
        "state_variant": STATUS_VARIANTS[state],
    }


def _folder_trees(
    rows: list[dict],
    property_map: dict[str, Property],
) -> tuple[list[dict], list[dict], dict]:
    """Build the three section trees: Properties, Financial Accounts, and Unfiled/Inbox."""
    # 1. Properties tree
    prop_nodes: dict[str, dict] = {}
    for r in rows:
        if r["section"] == "property":
            p_key = r["property_key"]
            node = prop_nodes.setdefault(p_key, {
                "key": p_key,
                "label": r["property_name"],
                "count": 0,
                "bytes": 0,
                "icon": "home",
                "children": {},
            })
            node["count"] += 1
            node["bytes"] += r["size_bytes"]
            child = node["children"].setdefault(r["type_key"], {
                "key": r["type_key"],
                "label": r["type_label"],
                "count": 0,
                "bytes": 0,
            })
            child["count"] += 1
            child["bytes"] += r["size_bytes"]

    # Include registered properties with 0 matching files if in full vault
    for p_id, p_obj in property_map.items():
        if p_id not in prop_nodes:
            prop_nodes[p_id] = {
                "key": p_id,
                "label": p_obj.name,
                "count": 0,
                "bytes": 0,
                "icon": "home",
                "children": {},
            }

    sorted_properties = sorted(
        [
            {**n, "children": sorted(n["children"].values(), key=lambda c: c["label"].lower())}
            for n in prop_nodes.values()
        ],
        key=lambda n: n["label"].lower(),
    )

    # 2. Financial Accounts tree
    fin_groups: dict[str, dict] = {
        "checking": {
            "key": "checking",
            "label": "Checking Accounts",
            "count": 0,
            "bytes": 0,
            "icon": "bank",
            "children": {},
        },
        "credit_card": {
            "key": "credit_card",
            "label": "Credit Cards",
            "count": 0,
            "bytes": 0,
            "icon": "card",
            "children": {},
        },
    }

    for r in rows:
        if r["section"] == "financial":
            grp = fin_groups.get(r["account_group"], fin_groups["checking"])
            grp["count"] += 1
            grp["bytes"] += r["size_bytes"]
            acct_key = r["account_key"]
            child = grp["children"].setdefault(acct_key, {
                "key": acct_key,
                "label": r["account_name"],
                "group": grp["key"],
                "count": 0,
                "bytes": 0,
                "icon": "bank" if grp["key"] == "checking" else "card",
            })
            child["count"] += 1
            child["bytes"] += r["size_bytes"]

    sorted_financial = [
        {
            **g,
            "children": sorted(g["children"].values(), key=lambda c: c["label"].lower()),
        }
        for g in (fin_groups["checking"], fin_groups["credit_card"])
        if g["count"] > 0
    ]

    # 3. Unfiled / Inbox tree
    inbox_children: dict[str, dict] = {}
    inbox_count = 0
    inbox_bytes = 0
    for r in rows:
        if r["section"] == "inbox":
            inbox_count += 1
            inbox_bytes += r["size_bytes"]
            c = inbox_children.setdefault(r["type_key"], {
                "key": r["type_key"],
                "label": r["type_label"],
                "count": 0,
                "bytes": 0,
            })
            c["count"] += 1
            c["bytes"] += r["size_bytes"]

    inbox_node = {
        "key": UNFILED,
        "label": "Unfiled",
        "count": inbox_count,
        "bytes": inbox_bytes,
        "icon": "inbox",
        "children": sorted(inbox_children.values(), key=lambda c: c["label"].lower()),
    }

    return sorted_properties, sorted_financial, inbox_node


def _matches(row: dict, needle: str) -> bool:
    haystack = " ".join((
        row["filename"],
        row["type_label"],
        row["property_name"],
        row.get("account_name") or "",
        row["location_label"],
        row["state_label"],
        row["uploaded"],
        row["uploaded_at"].strftime("%B %Y"),
    )).lower()
    return needle in haystack


def _vault_url(
    *,
    property_key: str | None = None,
    account_key: str | None = None,
    account_group: str | None = None,
    type_key: str | None = None,
    q: str | None = None,
    kind: str | None = None,
    sort: str | None = None,
    dir: str | None = None,
    doc: str | None = None,
    tab: str | None = None,
    per_page: str = "25",
    page: int = 1,
) -> str:
    params: dict[str, str] = {"per_page": per_page}
    if property_key:
        params["property"] = property_key
    if account_key:
        params["account"] = account_key
    if account_group:
        params["account_group"] = account_group
    if type_key:
        params["type"] = type_key
    if q:
        params["q"] = q
    if kind and kind != "all":
        params["kind"] = kind
    if sort and sort != "date":
        params["sort"] = sort
    if dir:
        params["dir"] = dir
    if doc:
        params["doc"] = doc
    if tab and tab != "preview":
        params["tab"] = tab
    if page > 1:
        params["page"] = str(page)
    return "/documents?" + urlencode(params)


def get_vault_router(templates, get_session_dep):
    router = APIRouter()

    @router.get("/documents", response_class=HTMLResponse)
    def view_documents(
        request: Request,
        property: str | None = None,
        account: str | None = None,
        account_group: str | None = None,
        type: str | None = None,
        q: str | None = None,
        kind: str = "all",
        sort: str = "date",
        dir: str | None = None,
        doc: str | None = None,
        tab: str = "preview",
        per_page: str = "25",
        page: int = 1,
        message: str | None = Query(None),
        message_variant: str = Query("info"),
        session: Session = Depends(get_session_dep),
    ):
        """One folder of the vault, with the trees beside it and the inspector for what is chosen."""
        # A page size is either "ALL" or a number; the form offers 25/50/100, but a
        # URL may ask for any size it likes.
        if per_page != "ALL":
            try:
                per_page = str(max(1, min(250, int(per_page))))
            except (TypeError, ValueError):
                per_page = "25"
        sort = sort if sort in dict(SORTS) else "date"

        documents = session.exec(
            select(Document).order_by(desc(Document.uploaded_at), desc(Document.id))
        ).all()

        # The newest extraction of each document is the one that describes it now;
        # older ones stay as history, as on the statements screen.
        latest: dict[str, DocumentExtraction] = {}
        for extraction in session.exec(select(DocumentExtraction)).all():
            current = latest.get(extraction.document_id)
            if current is None or extraction.created_at >= current.created_at:
                latest[extraction.document_id] = extraction

        property_map = {p.id: p for p in session.exec(select(Property).order_by(Property.name)).all()}
        doc_accounts = _build_document_accounts(session, documents, latest)
        all_rows = [
            _document_row(doc_, latest.get(doc_.id), property_map.get(doc_.property_id or ""), doc_accounts.get(doc_.id))
            for doc_ in documents
        ]

        needle = (q or "").strip().lower()
        searched = [row for row in all_rows if _matches(row, needle)] if needle else all_rows

        # The search is deliberately vault-wide: hunting for a file should not turn on
        # guessing which folder somebody filed it in. Opening a folder leaves the search
        # behind, and searching leaves the folder behind — neither silently overrides the
        # other, which is why the folder links below carry no `q`.
        if needle:
            property = None
            account = None
            account_group = None
            type = None

        # Build trees: Properties, Financial Accounts, and Unfiled/Inbox
        property_tree, financial_tree, inbox_node = _folder_trees(searched, property_map)

        # Every node carries its own URL, so the template never assembles one. A folder
        # link drops the inspector: the chosen document belongs to another folder.
        for node in property_tree:
            node["href"] = _vault_url(property_key=node["key"], type_key=None, q=None,
                                      kind=kind, sort=sort, dir=dir, per_page=per_page)
            for child in node["children"]:
                child["href"] = _vault_url(property_key=node["key"], type_key=child["key"], q=None,
                                           kind=kind, sort=sort, dir=dir, per_page=per_page)

        for grp in financial_tree:
            grp["href"] = _vault_url(account_group=grp["key"], type_key=None, q=None,
                                     kind=kind, sort=sort, dir=dir, per_page=per_page)
            for child in grp["children"]:
                child["href"] = _vault_url(account_key=child["key"], type_key=None, q=None,
                                           kind=kind, sort=sort, dir=dir, per_page=per_page)

        inbox_node["href"] = _vault_url(property_key=UNFILED, type_key=None, q=None,
                                        kind=kind, sort=sort, dir=dir, per_page=per_page)
        for child in inbox_node["children"]:
            child["href"] = _vault_url(property_key=UNFILED, type_key=child["key"], q=None,
                                       kind=kind, sort=sort, dir=dir, per_page=per_page)

        is_inbox = (property == UNFILED or property == "inbox")
        current_property = None
        current_account_group = None
        current_account = None
        current_type = None

        if is_inbox:
            property = UNFILED
            if type:
                current_type = next((c for c in inbox_node["children"] if c["key"] == type), None)
        elif property:
            current_property = next((node for node in property_tree if node["key"] == property), None)
            if current_property is not None and type:
                current_type = next((child for child in current_property["children"] if child["key"] == type), None)
        elif account:
            matched_grp = next((g for g in financial_tree if g["key"] == account), None)
            if matched_grp is not None:
                current_account_group = matched_grp
                account_group = matched_grp["key"]
                account = None
            else:
                for g in financial_tree:
                    child = next((c for c in g["children"] if c["key"] == account), None)
                    if child is not None:
                        current_account = child
                        current_account_group = g
                        break
            if current_account is not None and type:
                current_type = {"key": type, "label": TYPE_LABELS.get(type, type)}
        elif account_group:
            current_account_group = next((g for g in financial_tree if g["key"] == account_group), None)
            if current_account_group is not None and type:
                current_type = {"key": type, "label": TYPE_LABELS.get(type, type)}

        # Filter documents for this folder
        if is_inbox:
            in_folder = [
                row for row in searched
                if row["section"] == "inbox"
                and (current_type is None or row["type_key"] == current_type["key"])
            ]
        elif current_property is not None:
            in_folder = [
                row for row in searched
                if row["section"] == "property"
                and row["property_key"] == current_property["key"]
                and (current_type is None or row["type_key"] == current_type["key"])
            ]
        elif current_account is not None:
            in_folder = [
                row for row in searched
                if row["section"] == "financial"
                and row["account_key"] == current_account["key"]
                and (current_type is None or row["type_key"] == current_type["key"])
            ]
        elif current_account_group is not None:
            in_folder = [
                row for row in searched
                if row["section"] == "financial"
                and row["account_group"] == current_account_group["key"]
                and (current_type is None or row["type_key"] == current_type["key"])
            ]
        else:
            in_folder = [
                row for row in searched
                if (current_type is None or row["type_key"] == current_type["key"])
            ]

        # Parent navigation links
        parent_href: str | None = None
        parent_label: str | None = None
        if current_type is not None:
            if current_property is not None:
                parent_href = _vault_url(property_key=current_property["key"], type_key=None, q=None, kind=kind, sort=sort, dir=dir, per_page=per_page)
                parent_label = current_property["label"]
            elif current_account is not None:
                parent_href = _vault_url(account_key=current_account["key"], type_key=None, q=None, kind=kind, sort=sort, dir=dir, per_page=per_page)
                parent_label = current_account["label"]
            elif is_inbox:
                parent_href = _vault_url(property_key=UNFILED, type_key=None, q=None, kind=kind, sort=sort, dir=dir, per_page=per_page)
                parent_label = "Unfiled"
        elif current_account is not None and current_account_group is not None:
            parent_href = _vault_url(account_group=current_account_group["key"], type_key=None, q=None, kind=kind, sort=sort, dir=dir, per_page=per_page)
            parent_label = current_account_group["label"]
        elif current_account_group is not None or current_property is not None or is_inbox:
            parent_href = _vault_url(property_key=None, account_key=None, account_group=None, type_key=None, q=None, kind=kind, sort=sort, dir=dir, per_page=per_page)
            parent_label = "Whole vault"

        # Determine title and icon
        if needle:
            here = "Search results"
            here_icon = "search"
        elif current_type is not None:
            here = current_type["label"]
            here_icon = "folder-open"
        elif current_account is not None:
            here = current_account["label"]
            here_icon = current_account.get("icon", "card")
        elif current_account_group is not None:
            here = current_account_group["label"]
            here_icon = current_account_group.get("icon", "bank")
        elif current_property is not None:
            here = current_property["label"]
            here_icon = "home"
        elif is_inbox:
            here = "Unfiled"
            here_icon = "inbox"
        else:
            here = "The whole vault"
            here_icon = "folder-open"

        # The chips are built from what the folder actually holds: a type nobody filed
        # under is not offered, so a chip never leads to an empty list.
        kind_counts: dict[str, int] = {}
        for row in in_folder:
            kind_counts[row["kind"]] = kind_counts.get(row["kind"], 0) + 1
        kind_labels = {"pdf": "PDFs", "image": "Images", "other": "Other files"}
        kind_chips = [
            {"key": "all", "label": "All", "count": len(in_folder)},
        ] + [
            {"key": key, "label": kind_labels.get(key, key.title()), "count": kind_counts[key]}
            for key in ("pdf", "image", "other") if kind_counts.get(key)
        ]
        for chip in kind_chips:
            chip["href"] = _vault_url(
                property_key=property, account_key=account, account_group=account_group,
                type_key=type, q=q, kind=chip["key"], sort=sort, dir=dir, per_page=per_page)
            chip["active"] = chip["key"] == (kind if kind in {c["key"] for c in kind_chips} else "all")

        if kind not in {chip["key"] for chip in kind_chips}:
            kind = "all"
        visible = [row for row in in_folder if kind == "all" or row["kind"] == kind]

        sort = sort if sort in dict(SORTS) else "date"
        default_dir = "desc" if sort in {"date", "size"} else "asc"
        current_dir = dir if dir in {"asc", "desc"} else default_dir
        reverse = (current_dir == "desc")

        sort_keys = {
            "name": lambda row: (row["filename"] or "").lower(),
            "location": lambda row: (f"{row['location_label']}").lower(),
            "date": lambda row: row["uploaded_at"],
            "size": lambda row: row["size_bytes"],
            "status": lambda row: row["state_label"].lower(),
        }
        ordered = sorted(visible, key=sort_keys[sort], reverse=reverse)

        total = len(ordered)
        total_bytes = sum(row["size_bytes"] for row in ordered)

        # Where each document's own folder is, and its select link preserving view state.
        for row in ordered:
            if row["section"] == "property":
                row["folder_href"] = _vault_url(
                    property_key=row["property_key"], type_key=row["type_key"], q=None,
                    kind="all", sort=sort, dir=current_dir, per_page=per_page)
            elif row["section"] == "financial":
                row["folder_href"] = _vault_url(
                    account_key=row["account_key"], type_key=row["type_key"], q=None,
                    kind="all", sort=sort, dir=current_dir, per_page=per_page)
            else:
                row["folder_href"] = _vault_url(
                    property_key=UNFILED, type_key=row["type_key"], q=None,
                    kind="all", sort=sort, dir=current_dir, per_page=per_page)

            row["select_href"] = _vault_url(
                property_key=property, account_key=account, account_group=account_group,
                type_key=type, q=q, kind=kind, sort=sort, dir=current_dir,
                doc=row["id"], tab=tab, per_page=per_page, page=page)

        per_page_count = max(total, 1) if per_page == "ALL" else int(per_page)
        total_pages = max(1, -(-total // per_page_count))
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page_count
        end = min(start + per_page_count, total)

        # The inspector: the one document being looked at, its prev/next navigation, and its tab.
        selected = next((row for row in in_folder if row["id"] == doc), None)
        if selected is None and doc:
            selected = next((row for row in searched if row["id"] == doc), None)

        prev_doc_url = None
        next_doc_url = None
        doc_position = ""
        inspector_tabs: list[dict] = []

        if selected is not None:
            doc_idx = next((i for i, r in enumerate(ordered) if r["id"] == selected["id"]), -1)
            if doc_idx != -1:
                doc_position = f"{doc_idx + 1} of {total}"
                if doc_idx > 0:
                    prev_r = ordered[doc_idx - 1]
                    prev_doc_url = _vault_url(
                        property_key=property, account_key=account, account_group=account_group,
                        type_key=type, q=q, kind=kind, sort=sort, dir=current_dir,
                        doc=prev_r["id"], tab=tab, per_page=per_page, page=page)
                if doc_idx < total - 1:
                    next_r = ordered[doc_idx + 1]
                    next_doc_url = _vault_url(
                        property_key=property, account_key=account, account_group=account_group,
                        type_key=type, q=q, kind=kind, sort=sort, dir=current_dir,
                        doc=next_r["id"], tab=tab, per_page=per_page, page=page)

            tabs = list(INSPECTOR_TABS) + ([("extraction", "Extraction")] if selected["extraction_id"] else [])
            if tab not in {key for key, _ in tabs}:
                tab = "preview"
            inspector_tabs = [
                {"key": key, "label": label, "href": _vault_url(
                    property_key=property, account_key=account, account_group=account_group,
                    type_key=type, q=q, kind=kind, sort=sort, dir=current_dir,
                    doc=selected["id"], tab=key, per_page=per_page, page=page)}
                for key, label in tabs
            ]

        # The path is the way back up; the last crumb is where you are, so it is not a link.
        breadcrumb: list[dict] = [{"label": "Vault", "href": _vault_url(
            property_key=None, account_key=None, account_group=None, type_key=None, q=None, kind=kind,
            sort=sort, dir=current_dir, per_page=per_page, page=1)}]
        if needle:
            breadcrumb.append({"label": f"search “{q.strip()}”", "href": None})
        elif current_property is not None:
            breadcrumb.append({
                "label": current_property["label"],
                "href": _vault_url(property_key=current_property["key"], type_key=None, q=None,
                                   kind=kind, sort=sort, dir=current_dir, per_page=per_page, page=1) if current_type is not None else None,
            })
            if current_type is not None:
                breadcrumb.append({"label": current_type["label"], "href": None})
        elif current_account_group is not None:
            breadcrumb.append({
                "label": current_account_group["label"],
                "href": _vault_url(account_group=current_account_group["key"], type_key=None, q=None,
                                   kind=kind, sort=sort, dir=current_dir, per_page=per_page, page=1) if (current_account is not None or current_type is not None) else None,
            })
            if current_account is not None:
                breadcrumb.append({
                    "label": current_account["label"],
                    "href": _vault_url(account_key=current_account["key"], type_key=None, q=None,
                                       kind=kind, sort=sort, dir=current_dir, per_page=per_page, page=1) if current_type is not None else None,
                })
            if current_type is not None:
                breadcrumb.append({"label": current_type["label"], "href": None})
        elif is_inbox:
            breadcrumb.append({
                "label": "Unfiled",
                "href": _vault_url(property_key=UNFILED, type_key=None, q=None,
                                   kind=kind, sort=sort, dir=current_dir, per_page=per_page, page=1) if current_type is not None else None,
            })
            if current_type is not None:
                breadcrumb.append({"label": current_type["label"], "href": None})

        sort_urls = {}
        for key, _ in SORTS:
            if key == sort:
                next_dir = "asc" if current_dir == "desc" else "desc"
            else:
                next_dir = "desc" if key in {"date", "size"} else "asc"
            sort_urls[key] = _vault_url(
                property_key=property, account_key=account, account_group=account_group,
                type_key=type, q=q, kind=kind, sort=key, dir=next_dir, per_page=per_page, page=page,
                doc=selected["id"] if selected else None,
            )

        return templates.TemplateResponse(
            request=request,
            name="documents.html",
            context={
                "active_page": "documents",
                "message": message,
                "message_variant": message_variant if message_variant in {"info", "success", "warning", "error"} else "info",
                "tree": property_tree,
                "property_tree": property_tree,
                "financial_tree": financial_tree,
                "inbox_node": inbox_node,
                "breadcrumb": breadcrumb,
                "vault_href": _vault_url(property_key=None, account_key=None, account_group=None, type_key=None, q=None, kind=kind,
                                         sort=sort, dir=current_dir, per_page=per_page, page=1),
                "current_property": current_property,
                "current_account": current_account,
                "current_account_group": current_account_group,
                "is_inbox": is_inbox,
                "current_type": current_type,
                "parent_href": parent_href,
                "parent_label": parent_label,
                "here": here,
                "here_icon": here_icon,
                "rows": ordered[start:end],
                "q": q or "",
                "is_searching": bool(needle),
                "kind": kind,
                "kind_chips": kind_chips,
                "sort": sort,
                "sort_dir": current_dir,
                "sort_urls": sort_urls,
                "is_whole_vault": (current_property is None and current_account is None and current_account_group is None and not is_inbox and current_type is None),
                "sort_chips": [
                    {"label": label, "href": sort_urls[key],
                     "active": key == sort}
                    for key, label in SORTS
                ],
                "selected": selected,
                "prev_doc_url": prev_doc_url,
                "next_doc_url": next_doc_url,
                "doc_position": doc_position,
                "inspector_tabs": inspector_tabs,
                "active_tab": tab,
                "inspector_close_href": _vault_url(property_key=property, account_key=account, account_group=account_group,
                                                   type_key=type, q=q, kind=kind, sort=sort, dir=current_dir, per_page=per_page, page=page),
                "per_page": per_page,
                "page_sizes": PAGE_SIZES,
                "page": page,
                "total_pages": total_pages,
                "start_item": start + 1 if total else 0,
                "end_item": end,
                "total_items": total,
                "folder_size": human_size(total_bytes),
                "folder_documents": len(in_folder),
                "vault_documents": len(all_rows),
                "portfolio_documents": len(all_rows),
                "portfolio_size": human_size(sum(row["size_bytes"] for row in all_rows)),
                "awaiting_review": sum(1 for row in all_rows if row["state"] == "review"),
                "vault_size": human_size(sum(row["size_bytes"] for row in all_rows)),
                "vault_total": len(searched),
                "prev_url": _vault_url(property_key=property, account_key=account, account_group=account_group,
                                       type_key=type, q=q, kind=kind, sort=sort, dir=current_dir,
                                       doc=doc, tab=tab, per_page=per_page, page=page - 1) if page > 1 else None,
                "next_url": _vault_url(property_key=property, account_key=account, account_group=account_group,
                                       type_key=type, q=q, kind=kind, sort=sort, dir=current_dir,
                                       doc=doc, tab=tab, per_page=per_page, page=page + 1) if page < total_pages else None,
                "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
                "per_page_options": [
                    {"value": size, "label": "All" if size == "ALL" else size} for size in PAGE_SIZES
                ],
                "type_options": [{"value": "", "label": "Unclassified — decide from the file"}] + [
                    {"value": key, "label": label} for key, label in DOCUMENT_TYPES
                ],
                "property_options": [{"value": "", "label": "Unfiled — decide later"}] + [
                    {"value": record.id, "label": record.name} for record in property_map.values()
                ],
            },
        )

    @router.post("/documents/upload")
    async def upload_documents(
        files: list[UploadFile] = File(default=[]),
        document_type: str | None = Form(None),
        # The file's own field: which property the document is being filed under.
        property_id: str = Form(""),
        # The view to come back to: the open folder, and the search if there was one.
        property: str = Form(""),
        type: str = Form(""),
        q: str = Form(""),
        per_page: str = Form("25"),
        session: Session = Depends(get_session_dep),
    ):
        """Store one or more uploaded files, then say exactly what happened to each.

        An upload made from inside a folder is filed there, so adding a lease while
        looking at the lease folder does not then need editing. The verdict is per
        file: stored, already in the vault, or rejected with the reason — those are
        different outcomes and one summary sentence would hide which happened.
        """
        context = current_context()
        chosen = [upload for upload in files if upload and upload.filename]
        if not chosen:
            return _back_to_vault(property, type, q, per_page, "Choose at least one file to upload.", "warning")

        # Inside a folder, the folder decides the filing unless the form says otherwise.
        filed_under = property_id if property_id and property_id != UNFILED else ""
        filed_as = document_type or (None if type in ("", UNCLASSIFIED) else type)

        stored: list[str] = []
        duplicates: list[str] = []
        rejected: list[str] = []
        for upload in chosen:
            name = Path(upload.filename).name
            payload = await upload.read(MAX_UPLOAD_BYTES + 1)
            try:
                if len(payload) > MAX_UPLOAD_BYTES:
                    raise ValueError(f"larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
                result = store_document(
                    session,
                    context.paths.vault,
                    payload=payload,
                    original_filename=name,
                    document_type=filed_as,
                    property_id=filed_under or None,
                    parse=True,
                )
            except Exception as exc:  # noqa: BLE001 - every failure belongs against its filename
                rejected.append(f"{name}: {exc}")
                continue

            if not result.created:
                # The bytes are already stored; the upload still files them. An
                # existing classification is never overwritten, only a missing one
                # supplied.
                changed = False
                if filed_under and not result.document.property_id:
                    result.document.property_id = filed_under
                    changed = True
                if filed_as and (not result.document.document_type or result.document.document_type == "OTHER"):
                    result.document.document_type = filed_as
                    changed = True
                if changed:
                    session.add(result.document)
                    session.commit()
                duplicates.append(name)
                continue
            stored.append(f"{name} (staged for review)" if result.extraction_id else name)

        parts: list[str] = []
        if stored:
            parts.append(f"{len(stored)} stored: " + ", ".join(stored[:4]))
            if len(stored) > 4:
                parts.append(f"…and {len(stored) - 4} more")
        if duplicates:
            parts.append("already in the vault: " + ", ".join(duplicates[:4]))
        if rejected:
            parts.append("not stored — " + "; ".join(rejected[:3]))

        if rejected and not stored and not duplicates:
            variant = "error"
        elif rejected or duplicates:
            variant = "warning"
        else:
            variant = "success"
        return _back_to_vault(property, type, q, per_page, " · ".join(parts) or "Nothing to upload.", variant)

    def _back_to_vault(property_key: str, type_key: str, q: str, per_page: str,
                       message: str, variant: str) -> RedirectResponse:
        params: dict[str, str] = {"per_page": per_page or "25", "message": message, "message_variant": variant}
        if property_key:
            params["property"] = property_key
        if type_key:
            params["type"] = type_key
        if q:
            params["q"] = q
        # urlencode escapes the values; escaping the result again mangled the message.
        return RedirectResponse(url="/documents?" + urlencode(params), status_code=303)

    @router.api_route("/documents/bulk/download", methods=["GET", "POST"])
    async def bulk_download_documents(
        request: Request,
        session: Session = Depends(get_session_dep),
    ):
        """Create a zip archive containing the selected or all matching documents with authorization and memory safety."""
        form_data = {}
        if request.method == "POST":
            form_data = dict(await request.form())

        scope = str(form_data.get("scope") or request.query_params.get("scope") or "selected")
        doc_ids_raw = str(form_data.get("doc_ids") or request.query_params.get("doc_ids") or "")

        docs: list[Document] = []
        MAX_DOCS = 500
        MAX_BYTES = 250 * 1024 * 1024  # 250 MB

        if scope == "all_matching":
            property_key = form_data.get("property") or request.query_params.get("property")
            account_key = form_data.get("account") or request.query_params.get("account")
            account_group = form_data.get("account_group") or request.query_params.get("account_group")
            type_key = form_data.get("type") or request.query_params.get("type")
            q = (form_data.get("q") or request.query_params.get("q") or "").strip().lower()
            kind = form_data.get("kind") or request.query_params.get("kind") or "all"

            documents = session.exec(select(Document).order_by(desc(Document.uploaded_at), desc(Document.id))).all()
            latest: dict[str, DocumentExtraction] = {}
            for extraction in session.exec(select(DocumentExtraction)).all():
                current = latest.get(extraction.document_id)
                if current is None or extraction.created_at >= current.created_at:
                    latest[extraction.document_id] = extraction

            property_map = {p.id: p for p in session.exec(select(Property)).all()}
            doc_accounts = _build_document_accounts(session, documents, latest)
            all_rows = [
                _document_row(doc_, latest.get(doc_.id), property_map.get(doc_.property_id or ""), doc_accounts.get(doc_.id))
                for doc_ in documents
            ]

            matching_rows = []
            for r in all_rows:
                if q and not _matches(r, q):
                    continue
                if kind != "all" and r["kind"] != kind:
                    continue
                if type_key and type_key != UNCLASSIFIED and r["type_key"] != type_key:
                    continue

                if property_key == UNFILED or property_key == "inbox":
                    if r["section"] != "inbox":
                        continue
                elif property_key:
                    if r["section"] != "property" or r["property_key"] != property_key:
                        continue
                elif account_key:
                    if r["section"] != "financial" or r["account_key"] != account_key:
                        continue
                elif account_group:
                    if r["section"] != "financial" or r["account_group"] != account_group:
                        continue

                matching_rows.append(r)
                if len(matching_rows) >= MAX_DOCS:
                    break

            doc_map = {d.id: d for d in documents}
            docs = [doc_map[r["id"]] for r in matching_rows if r["id"] in doc_map]
        else:
            ids = [i.strip() for i in doc_ids_raw.split(",") if i.strip()]
            if not ids:
                return RedirectResponse(url="/documents?message=No+documents+selected&message_variant=warning", status_code=303)
            # Scoped strictly to current tenant session
            docs = session.exec(select(Document).where(Document.id.in_(ids[:MAX_DOCS]))).all()

        if not docs:
            return RedirectResponse(url="/documents?message=No+valid+documents+found+to+download&message_variant=warning", status_code=303)

        # Check total size limit
        total_size = sum(d.file_size_bytes for d in docs)
        if total_size > MAX_BYTES:
            return RedirectResponse(url="/documents?message=Selection+exceeds+250MB+limit.+Please+select+fewer+files.&message_variant=warning", status_code=303)

        # Create temporary zip file on disk (memory safe)
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        temp_path = temp_file.name
        temp_file.close()

        included_count = 0
        skipped_files: list[str] = []
        used_names: set[str] = set()

        try:
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for doc in docs:
                    p = Path(doc.storage_path).resolve()
                    if p.exists() and p.is_file():
                        name = doc.original_filename or f"doc_{doc.id}.pdf"
                        base_name = name
                        idx = 1
                        while name in used_names:
                            stem = Path(base_name).stem
                            suffix = Path(base_name).suffix
                            name = f"{stem}_{idx}{suffix}"
                            idx += 1
                        used_names.add(name)
                        zip_file.write(str(p), arcname=name)
                        included_count += 1
                    else:
                        skipped_files.append(f"{doc.original_filename} (ID: {doc.id}) - file missing on disk")

                if skipped_files:
                    summary = f"Export Summary ({datetime.now().isoformat()})\n"
                    summary += f"Total files requested: {len(docs)}\n"
                    summary += f"Total files included: {included_count}\n"
                    summary += f"Files skipped ({len(skipped_files)}):\n"
                    summary += "\n".join(f" - {s}" for s in skipped_files)
                    zip_file.writestr("_export_summary.txt", summary)

            if included_count == 0:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                return RedirectResponse(url="/documents?message=All+selected+files+were+missing+on+disk&message_variant=error", status_code=303)

            return FileResponse(
                path=temp_path,
                filename="documents_export.zip",
                media_type="application/zip",
                background=BackgroundTask(os.unlink, temp_path),
            )
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    return router
