import re
import tempfile
import os
from uuid import UUID

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user, get_db
from api.models import ExcelFile, User
from api.schemas import ExcelFileOut

router = APIRouter(prefix="/files", tags=["files"])

# ─── Format validation (reprise de app.py) ───────────────────────────────────

REQUIRED_SHEETS: dict[str, list[str]] = {
    "Produits": [
        "ID_Produit", "Nom_Produit", "Categorie",
        "Prix_Unitaire_EUR", "Stock_Actuel", "Seuil_Alerte",
    ],
    "Fournisseurs": [
        "ID_Fournisseur", "Nom_Fournisseur", "Pays",
        "Delai_Livraison_Jours", "Note_Qualite",
    ],
    "Approvisionnements": [
        "ID_Appro", "ID_Produit", "ID_Fournisseur",
        "Date_Livraison", "Quantite_Recue", "Cout_Total_EUR",
    ],
}
VENTES_REQUIRED_COLS = [
    "ID_Vente", "ID_Produit", "Mois",
    "Quantite_Vendue", "Prix_Vente_EUR", "CA_EUR", "Region",
]


def _validate_excel(path: str) -> list[str]:
    errors: list[str] = []
    try:
        xl = pd.ExcelFile(path)
        sheet_names = xl.sheet_names
    except Exception as e:
        return [f"Impossible de lire le fichier : {e}"]

    for sheet, required_cols in REQUIRED_SHEETS.items():
        if sheet not in sheet_names:
            errors.append(f"Onglet manquant : {sheet}")
            continue
        try:
            df = pd.read_excel(path, sheet_name=sheet, header=1)
            missing = [c for c in required_cols if c not in df.columns]
            if missing:
                errors.append(f"Onglet {sheet} — colonnes manquantes : {', '.join(missing)}")
        except Exception as e:
            errors.append(f"Onglet {sheet} — erreur de lecture : {e}")

    ventes_sheets = [
        s for s in sheet_names
        if re.search(r"\d{4}", s) and "vent" in s.lower()
    ]
    if not ventes_sheets:
        errors.append("Aucun onglet Ventes avec une année dans le nom")
    else:
        for sheet in ventes_sheets:
            try:
                df = pd.read_excel(path, sheet_name=sheet, header=1)
                missing = [c for c in VENTES_REQUIRED_COLS if c not in df.columns]
                if missing:
                    errors.append(
                        f"Onglet {sheet} — colonnes manquantes : {', '.join(missing)}"
                    )
            except Exception as e:
                errors.append(f"Onglet {sheet} — erreur de lecture : {e}")

    return errors


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/upload", response_model=ExcelFileOut, status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Seuls les fichiers .xlsx sont acceptés")

    file_bytes = await file.read()

    # Validate format via temp file
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    tmp.write(file_bytes)
    tmp.close()
    try:
        errors = _validate_excel(tmp.name)
    finally:
        os.unlink(tmp.name)

    if errors:
        raise HTTPException(
            status_code=422,
            detail={"message": "Format Excel invalide", "errors": errors},
        )

    excel_file = ExcelFile(
        user_id=current_user.id,
        filename=file.filename,
        file_data=file_bytes,
    )
    db.add(excel_file)
    await db.commit()
    await db.refresh(excel_file)
    return excel_file


@router.get("", response_model=list[ExcelFileOut])
async def list_files(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ExcelFile)
        .where(ExcelFile.user_id == current_user.id)
        .order_by(ExcelFile.uploaded_at.desc())
    )
    return result.scalars().all()


@router.get("/{file_id}/download")
async def download_file(
    file_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    excel_file = await db.get(ExcelFile, file_id)
    if not excel_file or excel_file.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Fichier introuvable")

    return Response(
        content=excel_file.file_data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{excel_file.filename}"'},
    )


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    file_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    excel_file = await db.get(ExcelFile, file_id)
    if not excel_file or excel_file.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Fichier introuvable")

    await db.delete(excel_file)
    await db.commit()
