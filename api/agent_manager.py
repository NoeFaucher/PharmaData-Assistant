"""
Gestion du cycle de vie des ExcelDataManager.

Chaque conversation a un fichier temporaire sur disque qui sert de
support au DataManager. Le cache évite de recréer le dm à chaque requête.

Clé du cache : str(conversation_id)
Valeur       : (ExcelDataManager, chemin_tmp)
"""

import os
import tempfile
from pathlib import Path
from typing import Dict, Tuple

from src.data_manager import ExcelDataManager

# conversation_id (str) → (ExcelDataManager, tmp_path)
_dm_cache: Dict[str, Tuple[ExcelDataManager, str]] = {}


def get_or_create_dm(conv_id: str, file_bytes: bytes) -> Tuple[ExcelDataManager, str]:
    """
    Retourne le DataManager associé à une conversation.
    Crée un fichier temporaire à partir des bytes DB si besoin.
    """
    if conv_id in _dm_cache:
        return _dm_cache[conv_id]

    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=".xlsx", prefix=f"pharma_{conv_id[:8]}_"
    )
    tmp.write(file_bytes)
    tmp.close()

    dm = ExcelDataManager(tmp.name)
    _dm_cache[conv_id] = (dm, tmp.name)
    return dm, tmp.name


def get_dm(conv_id: str) -> Tuple[ExcelDataManager, str] | None:
    """Retourne le DataManager depuis le cache uniquement (sans créer)."""
    return _dm_cache.get(conv_id)


def get_current_excel_bytes(conv_id: str) -> bytes | None:
    """
    Lit les bytes du fichier Excel courant (après d'éventuelles modifications
    effectuées par l'agent). Retourne None si le dm n'est pas en cache.
    """
    entry = _dm_cache.get(conv_id)
    if entry is None:
        return None
    dm, path = entry
    dm.save()
    return Path(path).read_bytes()


def evict_dm(conv_id: str) -> None:
    """Supprime le dm du cache et efface le fichier temporaire."""
    entry = _dm_cache.pop(conv_id, None)
    if entry:
        _, path = entry
        try:
            os.unlink(path)
        except OSError:
            pass
