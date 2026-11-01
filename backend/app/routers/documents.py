"""Document byte read (GRPH-1276 / GRPH-P64). Bytes come only from here, never from the row."""
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import errors
from app.db import get_db
from app.models import Document, User
from app.security import authz
from app.security.deps import get_current_user
from app.services import documents as docs_svc

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("/{doc_id}/bytes")
def read_bytes(doc_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    authz.require_readable(db, user.id, doc.project_id)
    try:
        data, mime = docs_svc.read_blob(db, doc.project_id, doc_id)
    except errors.BlobPending as e:
        return JSONResponse(status_code=409, content={
            "error": "conflict", "code": e.code, "detail": str(e), "hint": e.hint})
    except errors.NotFound as e:
        raise HTTPException(404, str(e))
    return Response(content=data, media_type=mime or "application/octet-stream")
