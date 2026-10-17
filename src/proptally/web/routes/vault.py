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

from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc
from sqlmodel import Session, select

from proptally.db.models import Document, DocumentExtraction, Property
from proptally.ingestion.service import MAX_UPLOAD_BYTES, store_document
from proptally.web.auth import current_context

#: The vocabulary `documents.document_type` uses, and the words the vault shows.
DOCUMENT_TYPES: tuple[tuple[str, str], ...] = (
    ("MORTGAGE_STATEMENT", "Mortgage statement"),
    ("CHECKING_STATEMENT", "Bank statement"),
    ("CREDIT_CARD_STATEMENT", "Credit card statement"),
    ("LEASE_AGREEMENT", "Lease agreement"),
    ("INSURANCE_POLICY", "Insurance policy"),
    ("PROPERTY_TAX", "Property tax"),
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
}

STATUS_VARIANTS = {
    "review": "warning",
    "ready": "info",
    "approved": "success",
    "none": "neutral",
}

PAGE_SIZES = ("25", "50", "100", "ALL")

SORTS: tuple[tuple[str, str], ...] = (
    ("date", "Uploaded"),
    ("name", "Name"),
    ("size", "Size"),
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


def _document_row(
    document: Document,
    extraction: DocumentExtraction | None,
    property_record: Property | None,
) -> dict:
    """One document, with the folder it lives in already worked out."""
    uploaded: datetime = document.uploaded_at
    state = _extraction_state(extraction)
    mime = document.mime_type or ""
    return {
        "id": document.id,
        "filename": document.original_filename,
        "mime_type": mime,
        "kind": "image" if mime.startswith("image/") else "pdf" if mime == "application/pdf" else "other",
        "can_preview": mime.startswith("image/") or mime == "application/pdf",
        "property_key": document.property_id or UNFILED,
        "property_name": property_record.name if property_record else "Unfiled",
        "type_key": document.document_type or UNCLASSIFIED,
        "type_label": TYPE_LABELS.get(document.document_type or "", "Unclassified"),
        "size_bytes": document.file_size_bytes,
        "size": human_size(document.file_size_bytes),
        "type_short": (mime.split("/")[-1].upper() if mime else "—"),
        "uploaded_at": uploaded,
        "uploaded": uploaded.strftime("%d %b %Y"),
        "extraction_id": extraction.id if extraction else None,
        "extraction_status": extraction.validation_status if extraction else None,
        "state": state,
        "state_label": STATUS_LABELS[state],
        "state_variant": STATUS_VARIANTS[state],
    }


def _folder_tree(rows: list[dict], property_map: dict[str, Property]) -> list[dict]:
    """The property → type tree, counted from the rows currently in scope."""
    tree: dict[str, dict] = {}
    for row in rows:
        node = tree.setdefault(row["property_key"], {
            "key": row["property_key"],
            "label": property_map[row["property_key"]].name if row["property_key"] in property_map else "Unfiled",
            "count": 0,
            "bytes": 0,
            "children": {},
        })
        node["count"] += 1
        node["bytes"] += row["size_bytes"]
        child = node["children"].setdefault(row["type_key"], {
            "key": row["type_key"],
            "label": TYPE_LABELS.get(row["type_key"], "Unclassified"),
            "count": 0,
            "bytes": 0,
        })
        child["count"] += 1
        child["bytes"] += row["size_bytes"]

    def order(nodes: list[dict], empty_key: str) -> list[dict]:
        # Real names first, the catch-all folder last, both alphabetical.
        return sorted(
            ({**n, "children": order(list(n["children"].values()), UNCLASSIFIED)} if "children" in n else n
             for n in nodes),
            key=lambda n: (n["key"] == empty_key, n["label"].lower()),
        )

    return order(list(tree.values()), UNFILED)


def _matches(row: dict, needle: str) -> bool:
    haystack = " ".join((
        row["filename"], row["type_label"], row["property_name"],
        row["state_label"], row["uploaded"], row["uploaded_at"].strftime("%B %Y"),
    )).lower()
    return needle in haystack


def _vault_url(*, property_key: str | None, type_key: str | None, q: str | None,
               kind: str | None = None, sort: str | None = None,
               doc: str | None = None, tab: str | None = None,
               per_page: str, page: int = 1) -> str:
    params: dict[str, str] = {"per_page": per_page}
    if property_key:
        params["property"] = property_key
    if type_key:
        params["type"] = type_key
    if q:
        params["q"] = q
    if kind and kind != "all":
        params["kind"] = kind
    if sort and sort != "date":
        params["sort"] = sort
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
        type: str | None = None,
        q: str | None = None,
        kind: str = "all",
        sort: str = "date",
        doc: str | None = None,
        tab: str = "preview",
        per_page: str = "25",
        page: int = 1,
        message: str | None = Query(None),
        message_variant: str = Query("info"),
        session: Session = Depends(get_session_dep),
    ):
        """One folder of the vault, with the tree beside it and the inspector for what is chosen."""
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
        all_rows = [_document_row(doc_, latest.get(doc_.id), property_map.get(doc_.property_id or "")) for doc_ in documents]

        needle = (q or "").strip().lower()
        searched = [row for row in all_rows if _matches(row, needle)] if needle else all_rows

        # The search is deliberately vault-wide: hunting for a file should not turn on
        # guessing which folder somebody filed it in. Opening a folder leaves the search
        # behind, and searching leaves the folder behind — neither silently overrides the
        # other, which is why the folder links below carry no `q`.
        if needle:
            property = None
            type = None

        # The tree counts what the search found, so a match's folder is obvious.
        tree = _folder_tree(searched, property_map)

        # Every node carries its own URL, so the template never assembles one. A folder
        # link drops the inspector: the chosen document belongs to another folder.
        for node in tree:
            node["href"] = _vault_url(property_key=node["key"], type_key=None, q=None,
                                      kind=kind, sort=sort, per_page=per_page)
            for child in node["children"]:
                child["href"] = _vault_url(property_key=node["key"], type_key=child["key"], q=None,
                                           kind=kind, sort=sort, per_page=per_page)

        current_property = next((node for node in tree if node["key"] == property), None)
        current_type = None
        if current_property is not None and type:
            current_type = next((child for child in current_property["children"] if child["key"] == type), None)

        # The folder you are in: its subfolders first, then the documents inside it.
        subfolders: list[dict] = []
        if current_property is None:
            subfolders = [{"key": node["key"], "label": node["label"], "count": node["count"],
                           "size": human_size(node["bytes"]), "href": node["href"]} for node in tree]
        elif current_type is None:
            subfolders = [{"key": child["key"], "label": child["label"], "count": child["count"],
                           "size": human_size(child["bytes"]), "href": child["href"]}
                          for child in current_property["children"]]

        in_folder = [
            row for row in searched
            if (current_property is None or row["property_key"] == current_property["key"])
            and (current_type is None or row["type_key"] == current_type["key"])
        ]

        # The chips are built from what the folder actually holds: a type nobody filed
        # under is not offered, so a chip never leads to an empty list.
        kind_counts: dict[str, int] = {}
        for row in in_folder:
            kind_counts[row["kind"]] = kind_counts.get(row["kind"], 0) + 1
        kind_labels = {"pdf": "PDFs", "image": "Images", "other": "Other files"}
        kind_chips = [
            {"key": "all", "label": "All", "count": len(in_folder)},
            {"key": "folders", "label": "Folders", "count": len(subfolders)},
        ] + [
            {"key": key, "label": kind_labels.get(key, key.title()), "count": kind_counts[key]}
            for key in ("pdf", "image", "other") if kind_counts.get(key)
        ]
        for chip in kind_chips:
            chip["href"] = _vault_url(property_key=property, type_key=type, q=q, kind=chip["key"],
                                      sort=sort, per_page=per_page)
            chip["active"] = chip["key"] == (kind if kind in {c["key"] for c in kind_chips} else "all")

        if kind not in {chip["key"] for chip in kind_chips}:
            kind = "all"
        visible = [row for row in in_folder if kind == "all" or (kind != "folders" and row["kind"] == kind)]
        if kind == "folders":
            visible = []

        ordered = sorted(
            visible,
            key={
                "name": lambda row: row["filename"].lower(),
                "size": lambda row: -row["size_bytes"],
                "date": lambda row: row["uploaded_at"],
            }[sort],
            reverse=(sort == "date"),
        )

        total = len(ordered)
        total_bytes = sum(row["size_bytes"] for row in ordered)

        # Where each document's own folder is, so the table can link into it.
        for row in ordered:
            row["folder_href"] = _vault_url(
                property_key=row["property_key"], type_key=row["type_key"], q=None,
                kind="all", sort=sort, per_page=per_page)

        per_page_count = max(total, 1) if per_page == "ALL" else int(per_page)
        total_pages = max(1, -(-total // per_page_count))
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page_count
        end = min(start + per_page_count, total)

        # The inspector: the one document being looked at, and its tab.
        selected = next((row for row in in_folder if row["id"] == doc), None)
        inspector_tabs: list[dict] = []
        if selected is not None:
            tabs = list(INSPECTOR_TABS) + ([("extraction", "Extraction")] if selected["extraction_id"] else [])
            if tab not in {key for key, _ in tabs}:
                tab = "preview"
            inspector_tabs = [
                {"key": key, "label": label, "href": _vault_url(
                    property_key=property, type_key=type, q=q, kind=kind, sort=sort,
                    doc=selected["id"], tab=key, per_page=per_page)}
                for key, label in tabs
            ]

        # The path is the way back up; the last crumb is where you are, so it is not a link.
        breadcrumb: list[dict] = [{"label": "Vault", "href": _vault_url(
            property_key=None, type_key=None, q=None, kind=kind, sort=sort, per_page=per_page)}]
        if needle:
            breadcrumb.append({"label": f"search “{q.strip()}”", "href": None})
        if current_property is not None:
            breadcrumb.append({
                "label": current_property["label"],
                "href": _vault_url(property_key=current_property["key"], type_key=None, q=None,
                                   kind=kind, sort=sort, per_page=per_page) if current_type is not None else None,
            })
        if current_type is not None:
            breadcrumb.append({"label": current_type["label"], "href": None})

        return templates.TemplateResponse(
            request=request,
            name="documents.html",
            context={
                "active_page": "documents",
                "message": message,
                "message_variant": message_variant if message_variant in {"info", "success", "warning", "error"} else "info",
                "tree": tree,
                "breadcrumb": breadcrumb,
                "vault_href": _vault_url(property_key=None, type_key=None, q=None, kind=kind,
                                         sort=sort, per_page=per_page),
                "current_property": current_property,
                "current_type": current_type,
                "subfolders": subfolders,
                "rows": ordered[start:end],
                "q": q or "",
                "is_searching": bool(needle),
                "kind": kind,
                "kind_chips": kind_chips,
                "sort": sort,
                "sort_chips": [
                    {"label": label, "href": _vault_url(property_key=property, type_key=type, q=q,
                                                        kind=kind, sort=key, per_page=per_page),
                     "active": key == sort}
                    for key, label in SORTS
                ],
                "selected": selected,
                "inspector_tabs": inspector_tabs,
                "active_tab": tab,
                "inspector_close_href": _vault_url(property_key=property, type_key=type, q=q,
                                                   kind=kind, sort=sort, per_page=per_page),
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
                # The tree's counts move with the search, root row included, so the tree
                # never claims three documents while showing one match inside a folder.
                "vault_total": len(searched),
                "prev_url": _vault_url(property_key=property, type_key=type, q=q, kind=kind, sort=sort,
                                       doc=doc, tab=tab, per_page=per_page, page=page - 1) if page > 1 else None,
                "next_url": _vault_url(property_key=property, type_key=type, q=q, kind=kind, sort=sort,
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

    return router
