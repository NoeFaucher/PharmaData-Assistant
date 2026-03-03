"""
tools.py — Outils LangChain pour l'assistant Excel PharmaTech.

Catégories d'outils :
  1. get_*          — lectures segmentées pour les questions courantes
  2. query_data     — requête pandas libre pour les cas non couverts
  3. write_*        — écriture dans le fichier Excel (CRUD complet)
  4. generate_chart — visualisation Plotly sauvegardée en image
"""

import os
import traceback
from typing import Literal, Annotated
import tempfile
import base64
import io

import pandas as pd
from langchain.tools import tool, InjectedToolCallId
from langchain_core.runnables import RunnableConfig
from langchain.messages import ToolMessage
from langgraph.types import Command

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
matplotlib.use("Agg")

from src.logging_config import get_logger

logger = get_logger(__name__)

_MAX_ROWS_DISPLAY = 50
CHARTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "charts")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_eval(code: str, namespace: dict = {}) -> object:
    """
    Évalue une expression pandas contre les onglets Excel.
    Expression simple → eval. Multi-lignes → exec avec résultat dans `result`.
    """
    namespace["pd"] = pd
    namespace["__builtins__"] = {
        "len": len, "range": range, "str": str, "int": int, "float": float,
        "list": list, "dict": dict, "sum": sum, "min": min, "max": max,
        "round": round, "abs": abs, "bool": bool, "enumerate": enumerate,
        "zip": zip, "sorted": sorted, "print": print,
        "True": True, "False": False, "None": None,
    }
    try:
        return eval(compile(code, "<query>", "eval"), namespace)
    except SyntaxError:
        exec(compile(code, "<query>", "exec"), namespace)
        result = namespace.get("result")
        if result is None:
            raise ValueError(
                "Le code multi-lignes doit assigner le résultat final "
                "à une variable nommée 'result'."
            )
        return result


# ===========================================================================
# 1. LECTURES SEGMENTÉES
# ===========================================================================

@tool
def get_low_stock_products(config: RunnableConfig) -> str:
    """
    Retourne les produits dont le stock actuel est inférieur au seuil d'alerte.
    Utile pour : 'quels produits sont en rupture / sous le seuil ?'
    """
    logger.info("[tool] get_low_stock_products")
    try:
        dm = config["configurable"]["data_manager"]
        df = dm.get("Produits")
        low = df[df["Stock_Actuel"] < df["Seuil_Alerte"]][
            ["ID_Produit", "Nom_Produit", "Stock_Actuel", "Seuil_Alerte"]
        ]
        if low.empty:
            return "Aucun produit n'est en dessous du seuil d'alerte."
        return low
    except Exception as e:
        logger.error("[tool] get_low_stock_products erreur : %s", e)
        return f"Erreur : {e}"


@tool
def get_best_supplier(config: RunnableConfig) -> str:
    """
    Retourne le fournisseur avec la meilleure note qualité (Note_Qualite).
    Utile pour : 'quel fournisseur a la meilleure note qualité ?'
    """
    logger.info("[tool] get_best_supplier")
    try:
        dm = config["configurable"]["data_manager"]
        df = dm.get("Fournisseurs")
        best = df.loc[df["Note_Qualite"].idxmax()]
        return (
            f"Meilleur fournisseur : {best['Nom_Fournisseur']} ({best['ID_Fournisseur']})\n"
            f"  Pays            : {best['Pays']}\n"
            f"  Note qualité    : {best['Note_Qualite']}/10\n"
            f"  Délai livraison : {best['Delai_Livraison_Jours']} jours"
        )
    except Exception as e:
        logger.error("[tool] get_best_supplier erreur : %s", e)
        return f"Erreur : {e}"


@tool
def get_product_by_name(name: str, config: RunnableConfig) -> str:
    """
    Recherche un produit par nom (correspondance partielle, insensible à la casse).
    Passer une chaîne vide pour lister TOUS les produits, à utiliser quand un produit n'est pas trouvé
    ou quand l'utilisateur veut voir tous les produits.
    Toujours appeler cet outil avant une écriture quand l'utilisateur n'a pas fourni d'ID produit explicitement.

    Args:
        name: nom partiel ou complet, ex: 'Doliprane', 'ibu', ou '' pour tout lister
    """
    logger.info("[tool] get_product_by_name : name=%s", name)
    try:
        dm = config["configurable"]["data_manager"]
        df = dm.get("Produits")
        mask = df["Nom_Produit"].str.contains(name, case=False, na=False)
        matches = df[mask]
        if matches.empty:
            return f"Aucun produit trouvé avec le nom '{name}'."
        return matches
    except Exception as e:
        logger.error("[tool] get_product_by_name erreur : %s", e)
        return f"Erreur : {e}"


@tool
def get_supplier_by_name(name: str, config: RunnableConfig) -> str:
    """
    Recherche un fournisseur par nom (correspondance partielle, insensible à la casse).
    Passer une chaîne vide pour lister TOUS les fournisseurs, à utiliser quand un fournisseur n'est pas trouvé
    ou quand l'utilisateur veut voir tous les fournisseurs.
    Toujours appeler cet outil avant une écriture quand l'utilisateur n'a pas fourni d'ID fournisseur explicitement.

    Args:
        name: nom partiel ou complet, ex: 'Sanofi', 'pfi', ou '' pour tout lister
    """
    logger.info("[tool] get_supplier_by_name : name=%s", name)
    try:
        dm = config["configurable"]["data_manager"]
        df = dm.get("Fournisseurs")
        mask = df["Nom_Fournisseur"].str.contains(name, case=False, na=False)
        matches = df[mask]
        if matches.empty:
            return f"Aucun fournisseur trouvé avec le nom '{name}'."
        return matches
    except Exception as e:
        logger.error("[tool] get_supplier_by_name erreur : %s", e)
        return f"Erreur : {e}"


@tool
def get_sales_by_month(mois: str, annee: int, config: RunnableConfig) -> str:
    """
    Retourne les ventes d'un mois donné avec le CA total et les unités vendues.

    Args:
        mois  : nom du mois en français, ex: 'Janvier', 'Mars', 'Décembre'
        annee : année sur 4 chiffres, ex: 2025
    
    À utiliser pour les questions comme:
      'Quel est le CA total en Janvier 2025 ?'
      'Combien d'unites vendues en Mars 2025 ?'
    """
    logger.info("[tool] get_sales_by_month : mois=%s, annee=%d", mois, annee)
    try:
        dm = config["configurable"]["data_manager"]
        df = dm.get("Ventes")
        mask = (df["Mois"].str.lower() == mois.lower()) & (df["Année"] == annee)
        filtered = df[mask]
        if filtered.empty:
            return f"Aucune vente trouvée pour {mois} {annee}."
        total_ca = filtered["CA_EUR"].sum()
        total_units = filtered["Quantite_Vendue"].sum()
        detail = filtered[["ID_Vente", "ID_Produit", "Region", "Quantite_Vendue", "CA_EUR"]]
        return (
            f"Ventes pour {mois} {annee} :\n"
            f"  CA total       : {total_ca:.2f} EUR\n"
            f"  Unités vendues : {int(total_units)}\n"
            f"  Nb lignes      : {len(filtered)}\n\n"
            f"{detail}"
        )
    except Exception as e:
        logger.error("[tool] get_sales_by_month erreur : %s", e)
        return f"Erreur : {e}"


@tool
def get_sales_by_region(region: str, config: RunnableConfig, annee: int | None = None) -> str:
    """
    Retourne le CA total et les unités vendues pour une région, avec détail par mois, filtré optionellement par année.

    Args:
        region : nom de la région, ex: 'Bretagne', 'Île-de-France'
        annee  : année (optionnel) pour restreindre les résultats
                 Si ommise, récupère pour toutes les années - faire attention si les données sont sur plusieurs années

    À utiliser pour les questions comme:
      'Quel est le total des ventes en Bretagne ?'
      'Combien a-t-on vendu en Ile-de-France en 2025 ?'
    """
    logger.info("[tool] get_sales_by_region : region=%s, annee=%s", region, annee)
    try:
        dm = config["configurable"]["data_manager"]
        df = dm.get("Ventes")
        mask = df["Region"].str.lower() == region.lower()
        if annee is not None:
            mask &= df["Année"] == annee
        filtered = df[mask]
        if filtered.empty:
            suffix = f" en {annee}" if annee else ""
            return (f"Aucune vente trouvée pour la région '{region}'{suffix}."
                    f"La région demandée ne correspond peut-être pas exactement à une valeur existante. "
                    f"Appelle get_all_regions() pour voir les régions disponibles, "
                    f"puis demande à l'utilisateur de confirmer."
                    )
        total_ca = filtered["CA_EUR"].sum()
        total_units = filtered["Quantite_Vendue"].sum()
        by_month = filtered.groupby(["Année", "Mois"])["CA_EUR"].sum().reset_index()
        suffix = f" ({annee})" if annee else " (toutes années)"
        return (
            f"Région : {region}{suffix}\n"
            f"  CA total       : {total_ca:.2f} EUR\n"
            f"  Unités vendues : {int(total_units)}\n\n"
            f"Détail par mois :\n{by_month}"
        )
    except Exception as e:
        logger.error("[tool] get_sales_by_region erreur : %s", e)
        return f"Erreur : {e}"


@tool
def get_top_products(
    n: int,
    type: Literal["ca","unite"],
    config: RunnableConfig,
    annee: int | None = None,
    mois: str | None = None,
    region: str | None = None,
) -> str:
    """
    Retourne les N produits avec le meilleur chiffre d'affaire ou nombre de produit vendu, avec filtres optionnels.

    Args:
        n      : nombre de produits à retourner, ex: 3, 5
        type   : definir le classement en fonction, "ca" pour le chiffre d'affaire ou "unite" pour le nombre vendu
        annee  : filtrer par année (optionnel, obligatoire si mois est fourni)
        mois   : filtrer par mois en français (optionnel, nécessite annee) ex: "Janvier", "Décembre", ...
        region : filtrer par région (optionnel), ex: 'Île-de-France'

    À utiliser pour les questions comme:
      'Quels sont les 3 produits les plus vendus en terme de quantité en Ile-de-France ?'
      'Top 5 des produits par CA en Mars 2025 ?'
      'Quel est le produit le plus rentable sur Q1 2025 ?'
    """
    logger.info("[tool] get_top_products : n=%d, type=%s, annee=%s, mois=%s, region=%s", n, type, annee, mois, region)
    try:
        dm = config["configurable"]["data_manager"]
        
        if type != "ca" and type != "unite":
            return "Erreur : classement en fonction du chiffre d'affaire ('ca') ou du nombre d'unité vendu uniquement ('unite')"
        
        if mois is not None and annee is None:
            return "Erreur : 'annee' est obligatoire quand 'mois' est spécifié."

        ventes = dm.get("Ventes")
        produits = dm.get("Produits")[["ID_Produit", "Nom_Produit", "Categorie"]]

        mask = pd.Series([True] * len(ventes), index=ventes.index)
        if annee is not None:
            mask &= ventes["Année"] == annee
        if mois is not None:
            mask &= ventes["Mois"].str.lower() == mois.lower()
        if region is not None:
            mask &= ventes["Region"].str.lower() == region.lower()

        filtered = ventes[mask]
        if filtered.empty:
            return "Aucune vente trouvée pour les filtres spécifiés."

        
        sort_var = "CA_Total" if type == "ca" else "Unités_Total"
        grouped = (
            filtered.groupby("ID_Produit")
            .agg(CA_Total=("CA_EUR", "sum"), Unités_Total=("Quantite_Vendue", "sum"))
            .reset_index()
            .merge(produits, on="ID_Produit")
            .sort_values(sort_var, ascending=False)
            .head(n)
        )
        grouped["CA_Total"] = grouped["CA_Total"].round(2)

        filters = []
        if region:
            filters.append(f"région={region}")
        if mois and annee:
            filters.append(f"{mois} {annee}")
        elif annee:
            filters.append(str(annee))
        filter_str = f" ({', '.join(filters)})" if filters else " (toutes périodes)"

        return (
            f"Top {n} produits par {type.upper()}{filter_str} :\n"
            + grouped[["Nom_Produit", "Categorie", "CA_Total", "Unités_Total"]].to_string()
        )
    except Exception as e:
        logger.error("[tool] get_top_products erreur : %s", e)
        return f"Erreur : {e}"


@tool
def get_stock_summary(config: RunnableConfig) -> str:
    """
    Retourne une vue d'ensemble des stocks : stock actuel, seuil d'alerte,
    statut alerte et  calcule ratio du stock par rapport au seuil (stock / seuil).

    À utiliser pour les questions comme:
      'Quel est le stock total disponible tous produits confondus ?'
      'Donne moi un apercu des stocks actuels.'
      'Quels produits risquent une rupture ?'
    """
    logger.info("[tool] get_stock_summary")
    try:
        dm = config["configurable"]["data_manager"]
        df = dm.get("Produits")[
            ["ID_Produit", "Nom_Produit", "Categorie", "Stock_Actuel", "Seuil_Alerte"]
        ].copy()
        df["En_Alerte"] = df["Stock_Actuel"] < df["Seuil_Alerte"]
        df["Ratio_Stock_Seuil"] = (
            df["Stock_Actuel"] / df["Seuil_Alerte"].replace(0, float("nan"))
        ).round(2)
        total_stock = int(df["Stock_Actuel"].sum())
        nb_alertes = int(df["En_Alerte"].sum())
        return (
            f"Stock total tous produits : {total_stock} unités\n"
            f"Produits en alerte        : {nb_alertes}/{len(df)}\n\n"
            + df.to_string()
        )
    except Exception as e:
        logger.error("[tool] get_stock_summary erreur : %s", e)
        return f"Erreur : {e}"


@tool
def get_ca_by_region(
    config: RunnableConfig,
    mois: str | None = None,
    annee: int | None = None,
) -> str:
    """
    Retourne le CA total par région, trié par ordre décroissant, 
    avec filtres optionnels par année et/ou mois.

    Args:
        mois  : nom du mois en français (optionnel) ex: "Janvier", ..., "Décembre"
        annee : année sur 4 chiffres (optionnel)

    À utiliser pour les questions comme:
      - 'Quelle region genere le plus de CA ?'
      - 'Compare le CA par region en 2025.'
      - 'Repartition du CA par region en Mars 2025.'
    """
    logger.info("[tool] get_ca_by_region : annee=%s, mois=%s", annee, mois)
    try:
        dm = config["configurable"]["data_manager"]
        df = dm.get("Ventes")
        if annee is not None:
            df = df[df["Année"] == annee]
        if mois is not None:
            mois_lower = mois.strip().lower()
            if mois_lower not in df["Mois"].str.lower().unique():
                return (
                    f"Mois '{mois}' invalide. "
                    f"Mois disponibles : {sorted(df['Mois'].unique())}"
                )
            df = df[df["Mois"].str.lower() == mois_lower]

        if df.empty:
            parts = []
            if mois:
                parts.append(mois)
            if annee:
                parts.append(str(annee))
            suffix = f" pour {' '.join(parts)}" if parts else ""
            return (f"Aucune vente trouvée{suffix}."
                    f"La région demandée ne correspond peut-être pas exactement à une valeur existante. "
                    f"Appelle get_all_regions() pour voir les régions disponibles, "
                    f"puis demande à l'utilisateur de confirmer."
                    )
        grouped = (
            df.groupby("Region")
            .agg(
                CA_Total=("CA_EUR", "sum"),
                Unités_Total=("Quantite_Vendue", "sum"),
                Nb_Ventes=("ID_Vente", "count"),
            )
            .reset_index()
            .sort_values("CA_Total", ascending=False)
        )
        grouped["CA_Total"] = grouped["CA_Total"].round(2)
        total = grouped["CA_Total"].sum()
        grouped["Part_%"] = ((grouped["CA_Total"] / total) * 100).round(1)

        suffix_parts = []
        if mois:
            suffix_parts.append(mois)
        if annee:
            suffix_parts.append(str(annee))
        suffix = f" ({' '.join(suffix_parts)})" if suffix_parts else " (toutes années)"

        return (
            f"CA par région{suffix} — total : {total:.2f} EUR\n\n"
            + grouped.to_string()
        )
    except Exception as e:
        logger.error("[tool] get_ca_by_region erreur : %s", e)
        return f"Erreur : {e}"


@tool
def get_supplier_by_product(product_id: str, config: RunnableConfig) -> str:
    """
    Retourne tous les fournisseurs ayant livré un produit donné, avec quantités reçus et coûts totaux.

    Args:
        product_id : ID du produit, ex: 'P001'
        
    À utiliser pour les questions comme:
      'Qui fournit le Doliprane ?'
      'Quels fournisseurs ont livre le produit P001 ?'
    """
    logger.info("[tool] get_supplier_by_product : product_id=%s", product_id)
    try:
        dm = config["configurable"]["data_manager"]
        produits = dm.get("Produits")
        prod_mask = produits["ID_Produit"] == product_id
        if not prod_mask.any():
            return f"Produit '{product_id}' introuvable."
        nom_produit = produits[prod_mask].iloc[0]["Nom_Produit"]
        
        appros = dm.get("Approvisionnements")
        fournisseurs = dm.get("Fournisseurs")
        filtered = appros[appros["ID_Produit"] == product_id]
        if filtered.empty:
            return f"Aucun approvisionnement trouvé pour '{nom_produit}' ({product_id})."

        grouped = (
            filtered.groupby("ID_Fournisseur")
            .agg(
                Qté_Totale=("Quantite_Recue", "sum"),
                Coût_Total=("Cout_Total_EUR", "sum"),
                Nb_Livraisons=("ID_Appro", "count"),
            )
            .reset_index()
            .merge(
                fournisseurs[["ID_Fournisseur", "Nom_Fournisseur", "Pays", "Note_Qualite"]],
                on="ID_Fournisseur",
            )
            .sort_values("Qté_Totale", ascending=False)
        )
        grouped["Coût_Total"] = grouped["Coût_Total"].round(2)
        return (
            f"Fournisseurs pour '{nom_produit}' ({product_id}) :\n"
            + grouped[["Nom_Fournisseur", "Pays", "Note_Qualite", "Qté_Totale", "Coût_Total", "Nb_Livraisons"]].to_string()
        )
    except Exception as e:
        logger.error("[tool] get_supplier_by_product erreur : %s", e)
        return f"Erreur : {e}"


@tool
def get_supply_by_supplier(supplier_id: str, config: RunnableConfig) -> str:
    """
    Retourne toutes les livraisons d'un fournisseur donné, avec totaux.

    Args:
        supplier_id : ID du fournisseur, ex: 'F001'

    À utiliser pour les questions comme:
      'Quel est le total des approvisionnements commandes chez Sanofi ?'
      'Historique des livraisons du fournisseur F007.'
      'Quels produits le fournisseur F002 approvisionne-t-il ?'
    """
    logger.info("[tool] get_supply_by_supplier : supplier_id=%s", supplier_id)
    try:
        dm = config["configurable"]["data_manager"]
        fournisseurs = dm.get("Fournisseurs")
        sup_mask = fournisseurs["ID_Fournisseur"] == supplier_id
        if not sup_mask.any():
            return f"Fournisseur '{supplier_id}' introuvable."
        nom_fournisseur = fournisseurs[sup_mask].iloc[0]["Nom_Fournisseur"]

        appros = dm.get("Approvisionnements")
        produits = dm.get("Produits")[["ID_Produit", "Nom_Produit"]]
        filtered = appros[appros["ID_Fournisseur"] == supplier_id]
        if filtered.empty:
            return f"Aucun approvisionnement trouvé pour '{nom_fournisseur}' ({supplier_id})."

        detail = filtered.merge(produits, on="ID_Produit")[
                ["ID_Appro", "Nom_Produit", "Date_Livraison", "Quantite_Recue", "Cout_Total_EUR"]
            ]
        
        total_qte = int(filtered["Quantite_Recue"].sum())
        total_cout = filtered["Cout_Total_EUR"].sum()
        return (
            f"Approvisionnements de '{nom_fournisseur}' ({supplier_id}) :\n"
            f"  Quantité totale : {total_qte} unités\n"
            f"  Coût total      : {total_cout:.2f} EUR\n"
            f"  Nb commandes    : {len(filtered)}\n\n"
            f"{detail}"
        )
    except Exception as e:
        logger.error("[tool] get_supply_by_supplier erreur : %s", e)
        return f"Erreur : {e}"


@tool
def get_sales_velocity(config: RunnableConfig) -> str:
    """
    Calcule le ratio de rotation par produit : unités vendues / stock actuel.
    Un ratio élevé indique un produit qui se vend vite par rapport à son stock.
    Les produits sans stock sont mis de coté.
    
    À utiliser pour les questions comme:
      'Calcule le ratio ventes/stock par produit.'
      'Quels produits ont une rotation lente ?'
      'Quels produits se vendent le plus vite par rapport a leur stock ?'
    """
    logger.info("[tool] get_sales_velocity")
    try:
        dm = config["configurable"]["data_manager"]
        ventes = dm.get("Ventes")
        produits = dm.get("Produits")[["ID_Produit", "Nom_Produit", "Stock_Actuel", "Seuil_Alerte"]]

        total_sold = (
            ventes.groupby("ID_Produit")["Quantite_Vendue"]
            .sum()
            .reset_index()
            .rename(columns={"Quantite_Vendue": "Total_Vendu"})
        )
        df = produits.merge(total_sold, on="ID_Produit", how="left")
        df["Total_Vendu"] = df["Total_Vendu"].fillna(0).astype(int)
        df["Ratio_Rotation"] = (
            df["Total_Vendu"] / df["Stock_Actuel"].replace(0, float("nan"))
        ).round(3)
        df = df.sort_values("Ratio_Rotation", ascending=False, na_position="last")

        zero_stock = df[df["Stock_Actuel"] == 0]
        flag = (
            f"\nProduits avec stock=0 (ratio non calculable) : {len(zero_stock)}"
            if not zero_stock.empty
            else ""
        )
        return (
            "Ratio de rotation (Total_Vendu / Stock_Actuel) :\n"
            + df[["Nom_Produit", "Stock_Actuel", "Total_Vendu", "Ratio_Rotation"]].to_string()
            + flag
        )
    except Exception as e:
        logger.error("[tool] get_sales_velocity erreur : %s", e)
        return f"Erreur : {e}"


@tool
def get_monthly_ca_trend(annee: int, config: RunnableConfig) -> str:
    """
    Retourne le CA mensuel pour une année donnée, avec variation mois par mois (en pourcentage du CA et d'unités)
    Utilse en entrée pour les questions sur les tendances et pour les projections.

    Args:
        annee : année sur 4 chiffres, ex: 2025

    À utiliser pour les questions comme:
      'Quelle est la moyenne mensuelle du CA en 2025 ?'
      'Quel est le taux de croissance du CA entre Janvier et Mars 2025 ?'
      'Projette le CA du prochain mois.'
    """
    logger.info("[tool] get_monthly_ca_trend : annee=%d", annee)
    try:
        dm = config["configurable"]["data_manager"]
        df = dm.get("Ventes")
        filtered = df[df["Année"] == annee]
        if filtered.empty:
            return f"Aucune vente trouvée pour l'année {annee}."

        MONTH_ORDER = {
            "janvier": 1, "février": 2, "mars": 3, "avril": 4,
            "mai": 5, "juin": 6, "juillet": 7, "août": 8,
            "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
        }

        monthly = (
            filtered.groupby("Mois")
            .agg(CA=("CA_EUR", "sum"), Unités=("Quantite_Vendue", "sum"))
            .reset_index()
        )
        monthly["_order"] = monthly["Mois"].str.lower().map(MONTH_ORDER)
        monthly = monthly.sort_values("_order").drop(columns="_order")
        monthly["CA"] = monthly["CA"].round(2)
        monthly["Var_%"] = monthly["CA"].pct_change().mul(100).round(1)

        avg_ca = monthly["CA"].mean()
        total_ca = monthly["CA"].sum()

        return (
            f"Tendance CA mensuel {annee} :\n"
            f"  CA total : {total_ca:.2f} EUR\n"
            f"  CA moyen : {avg_ca:.2f} EUR/mois\n\n"
            + monthly.to_string()
        )
    except Exception as e:
        logger.error("[tool] get_monthly_ca_trend erreur : %s", e)
        return f"Erreur : {e}"


@tool
def get_all_regions(config: RunnableConfig) -> str:
    """
    Retourne la liste de toutes les régions présentes dans la table Ventes.
    À appeler dès qu'une région semble ambiguë ou abrégée avant toute opération
    de lecture ou d'écriture filtrant sur une région.

    À utiliser pour les questions comme:
      'Quelles régions existent dans les données ?'
      'Est-ce que PACA correspond à une région dans la base ?'
    """
    logger.info("[tool] get_all_regions")
    try:
        dm = config["configurable"]["data_manager"]
        regions = sorted(dm.get("Ventes")["Region"].dropna().unique().tolist())
        if not regions:
            return "Aucune région trouvée dans les données."
        return "Régions disponibles :\n" + "\n".join(f"  - {r}" for r in regions)
    except Exception as e:
        logger.error("[tool] get_all_regions erreur : %s", e)
        return f"Erreur : {e}"


# ===========================================================================
# 2. REQUÊTE PANDAS LIBRE
# ===========================================================================

@tool
def query_data(code: str, config: RunnableConfig) -> str:
    """
    Exécute une expression pandas sur les données Excel et retourne le résultat en chaine de caractère (as a string).

    DataFrames disponibles :
      - Produits           : catalogue de produit (ID_Produit, Nom_Produit, Categorie, Prix_Unitaire_EUR, Stock_Actuel, Seuil_Alerte)
      - Ventes             : historique de vente (ID_Vente, ID_Produit, Mois, Année, Quantite_Vendue, Prix_Vente_EUR, CA_EUR, Region)
      - Fournisseurs       : fournisseurs (ID_Fournisseur, Nom_Fournisseur, Pays, Delai_Livraison_Jours, Note_Qualite)
      - Approvisionnements : commande de stock (ID_Appro, ID_Produit, ID_Fournisseur, Date_Livraison, Quantite_Recue, Cout_Total_EUR)

    Module disponible : pd (pandas).

    Types importants :
      - Ventes['Mois']   : nom du mois en français (ex: 'Janvier', 'Mars', 'Décembre').
                           TOUJOURS filtrer avec Année en même temps pour éviter les plagues de plusieurs années.
                           Ventes[(Ventes['Mois'] == 'Mars') & (Ventes['Année'] == 2025)]
      - Ventes['Année']  : entier (2025), jamais une string.
      - Approvisionnements['Date_Livraison'] : datetime pandas (datetime64).
                           Utiliser .dt.month / .dt.year, JAMAIS .str ou .startswith().
                           JAMAIS utiliser .str.startswith() ou une comparaison de string dans cette colonne.
                           Filtrer par mois : Approvisionnements[Approvisionnements['Date_Livraison'].dt.month == 3]
                           Filtrer by année : Approvisionnements[Approvisionnements['Date_Livraison'].dt.year == 2025]
                           Filtrer les deux : Approvisionnements[(Approvisionnements['Date_Livraison'].dt.month == 3) & (Approvisionnements['Date_Livraison'].dt.year == 2025)]

    Usage :
      - Expression simple -> ecrit directement, ex:
            Produits[Produits['Stock_Actuel'] < Produits['Seuil_Alerte']]
      - Multi-line code   -> assigne le résultat final à une variable `result`, ex:
            ventes_mars = Ventes[(Ventes['Mois'] == 'Mars') & (Ventes['Année'] == 2025)]
            appro_mars  = Approvisionnements[(Approvisionnements['Date_Livraison'].dt.month == 3) & (Approvisionnements['Date_Livraison'].dt.year == 2025)]
            result = pd.DataFrame({
                'CA_Ventes': [ventes_mars['CA_EUR'].sum()],
                'Cout_Appro': [appro_mars['Cout_Total_EUR'].sum()]
            })
    """
    logger.info("[tool] query_data")
    logger.debug("[tool] query_data code :\n%s", code)
    try:
        dm = config["configurable"]["data_manager"]
        namespace = {
            name: dm.get(name)
            for name in ["Produits", "Ventes", "Fournisseurs", "Approvisionnements"]
        }
        result = _safe_eval(code,namespace)
        out = str(result)
        logger.debug("[tool] query_data résultat : %s", out[:500])
        return out
    except Exception as e:
        logger.error("[tool] query_data erreur : %s", e)
        return f"Erreur : {e}"


# ===========================================================================
# 3. ÉCRITURE — CRUD complet
# ===========================================================================

# --- Produits ---

@tool
def write_update_product(
    product_id: str,
    config: RunnableConfig,
    nom: str | None = None,
    categorie: str | None = None,
    prix_unitaire: float | None = None,
    stock: int | None = None,
    seuil_alerte: int | None = None,
) -> str:
    """
    Met à jour un ou plusieurs champs d'un produit existant dans le fichier Excel.
    Seuls les champs fournis sont modifiés, les autres ne le sont pas.

    Args:
        product_id    : ID du produit à modifier, ex: 'P001'
        nom           : nouveau nom (optionnel)
        categorie     : nouvelle catégorie (optionnel)
        prix_unitaire : nouveau prix unitaire en EUR (optionnel, >= 0)
        stock         : nouveau stock (optionnel, >= 0)
        seuil_alerte  : nouveau seuil d'alerte (optionnel, >= 0)

    À utiliser pour les questions comme:
      'Mets à jour le stock du Doliprane 1000mg à 18 000 unités.'
      'Change le prix de l'Ibuprofène à 2.50 €.'
    """
    logger.info("[tool] write_update_product : product_id=%s", product_id)
    try:
        dm = config["configurable"]["data_manager"]
        result = dm.update_product(
            product_id, nom=nom, categorie=categorie,
            prix_unitaire=prix_unitaire, stock=stock, seuil_alerte=seuil_alerte,
        )
        fields_str = ", ".join(f"{k}={v}" for k, v in result["updated_fields"].items())
        msg = f"Produit {product_id} mis à jour : {fields_str}."
        if result["stock_alert"]:
            msg += "\nALERTE : stock sous le seuil d'alerte."
        return msg
    except ValueError as e:
        return f"Erreur : {e}"
    except Exception as e:
        logger.error("[tool] write_update_product erreur : %s\n%s", e, traceback.format_exc())
        return f"Erreur inattendue : {e}"


@tool
def write_add_product(
    nom: str,
    categorie: str,
    prix_unitaire: float,
    stock: int,
    seuil_alerte: int,
    config: RunnableConfig,
) -> str:
    """
    Ajoute un nouveau produit au catalogue. L'ID est auto-généré.

    Args:
        nom           : nom du produit, ex: 'Paracétamol 500mg'
        categorie     : catégorie, ex: 'Antalgique', 'Antibiotique', ...
        prix_unitaire : prix unitaire en EUR (>= 0)
        stock         : stock initial (>= 0)
        seuil_alerte  : seuil d'alerte stock pour qu'un alerte soit déclencher (>= 0)

    À utiliser pour les questions comme:
      'Ajoute un nouveau produit Paracétamol 500mg dans la catégorie Antalgique.'
    """
    logger.info("[tool] write_add_product : nom=%s", nom)
    try:
        dm = config["configurable"]["data_manager"]
        result = dm.add_product(nom, categorie, prix_unitaire, stock, seuil_alerte)
        msg = (
            f"Produit ajouté ({result['product_id']}) : {nom}\n"
            f"   Catégorie : {categorie}\n"
            f"   Prix      : {prix_unitaire:.2f} €\n"
            f"   Stock     : {stock} unités (seuil : {seuil_alerte})"
        )
        if result["stock_alert"]:
            msg += "\nALERTE : stock initial déjà sous le seuil d'alerte."
        return msg
    except ValueError as e:
        return f"Erreur : {e}"
    except Exception as e:
        logger.error("[tool] write_add_product erreur : %s\n%s", e, traceback.format_exc())
        return f"Erreur inattendue : {e}"


@tool
def write_delete_product(product_id: str, config: RunnableConfig) -> str:
    """
    Supprime un produit du catalogue.
    Refusé si des ventes ou approvisionnements le référencent encore.

    Args:
        product_id : ID du produit à supprimer, ex: 'P001'

    Use this for requests like:
      'Supprime le produit P015 du catalogue.'
    """
    logger.info("[tool] write_delete_product : product_id=%s", product_id)
    try:
        dm = config["configurable"]["data_manager"]
        result = dm.delete_product(product_id)
        return f"Produit {product_id} ('{result['nom']}') supprimé du catalogue."
    except ValueError as e:
        return f"Erreur : {e}"
    except Exception as e:
        logger.error("[tool] write_delete_product erreur : %s\n%s", e, traceback.format_exc())
        return f"Erreur inattendue : {e}"


# --- Ventes ---

@tool
def write_add_sale(
    product_id: str,
    mois: str,
    annee: int,
    quantity: int,
    prix_vente_eur: float,
    region: str,
    config: RunnableConfig,
) -> str:
    """
    Enregistre une nouvelle vente dans le fichier Excel.
    Le stock est automatiquement décrémenté. Le CA est calculé automatiquement.

    Args:
        product_id     : ID du produit, ex: 'P001'
        mois           : nom du mois en français, ex: 'Janvier', 'Mars'
        annee          : année sur 4 chiffres, ex: 2025
        quantity       : nombre d'unités vendues (> 0)
        prix_vente_eur : prix de vente unitaire en EUR (>= 0)
        region         : région de vente, ex: 'Île-de-France', 'Bretagne'

    À utiliser pour les questions comme:
      'Ajoute une vente de 200 unités d'Ibuprofène en Mars en Bretagne.'
    """
    logger.info("[tool] write_add_sale : product_id=%s, qty=%s", product_id, quantity)
    try:
        dm = config["configurable"]["data_manager"]
        result = dm.add_sale(product_id, mois, annee, quantity, prix_vente_eur, region)
        msg = (
            f"Vente enregistrée ({result['sale_id']}).\n"
            f"   Produit  : {product_id}\n"
            f"   Quantité : {quantity} unités\n"
            f"   CA       : {result['ca_eur']:.2f} €\n"
            f"   Stock    : {result['previous_stock']} → {result['new_stock']} unités"
        )
        if result["stock_alert"]:
            msg += "\nALERTE : stock sous le seuil d'alerte."
        return msg
    except ValueError as e:
        return f"Erreur : {e}"
    except Exception as e:
        logger.error("[tool] write_add_sale erreur : %s\n%s", e, traceback.format_exc())
        return f"Erreur inattendue : {e}"


@tool
def write_delete_sale(sale_id: str, config: RunnableConfig) -> str:
    """
    Supprime une vente et restaure le stock du produit correspondant.
    Le stock est automatiquement incrémenté.

    Args:
        sale_id : ID de la vente, ex: 'V0042'

    À utiliser pour les questions comme:
      'Supprime la vente V0042.'
      'Annule la vente de Mars en Occitanie.' (first use query_data to find the ID)
    """
    logger.info("[tool] write_delete_sale : sale_id=%s", sale_id)
    try:
        dm = config["configurable"]["data_manager"]
        result = dm.delete_sale(sale_id)
        return (
            f"Vente {sale_id} supprimée.\n"
            f"   Produit  : {result['product_id']}\n"
            f"   Quantité : {result['quantity_restored']} unités restituées au stock\n"
            f"   Stock    : {result['previous_stock']} → {result['new_stock']} unités"
        )
    except ValueError as e:
        return f"Erreur : {e}"
    except Exception as e:
        logger.error("[tool] write_delete_sale erreur : %s\n%s", e, traceback.format_exc())
        return f"Erreur inattendue : {e}"


# --- Approvisionnements ---

@tool
def write_add_supply(
    product_id: str,
    supplier_id: str,
    quantity: int,
    cout_total_eur: float,
    delivery_date: str,
    config: RunnableConfig,
) -> str:
    """
    Enregistre un approvisionnement d'un produit dans le fichier Excel.
    Le stock est automatiquement incrémenté.

    Args:
        product_id     : ID du produit, ex: 'P001'
        supplier_id    : ID du fournisseur, ex: 'F001'
        quantity       : nombre d'unités reçues (> 0)
        cout_total_eur : coût total de la commande en EUR (>= 0)
        delivery_date  : date de livraison au format 'YYYY-MM-DD', ex: '2025-06-15'

    À utiliser pour les questions comme:
      'Enregistre une livraison de 5000 unités de Doliprane par Sanofi le 10 juin.'
    """
    logger.info("[tool] write_add_supply : product_id=%s, supplier_id=%s", product_id, supplier_id)
    try:
        dm = config["configurable"]["data_manager"]
        result = dm.add_supply(product_id, supplier_id, quantity, cout_total_eur, delivery_date)
        msg = (
            f"Approvisionnement enregistré ({result['appro_id']}).\n"
            f"   Produit     : {product_id}\n"
            f"   Fournisseur : {supplier_id}\n"
            f"   Quantité    : {quantity} unités\n"
            f"   Stock       : {result['previous_stock']} → {result['new_stock']} unités"
        )
        if result["stock_alert"]:
            msg += "\nALERTE : stock toujours sous le seuil malgré la livraison."
        return msg
    except ValueError as e:
        return f"Erreur : {e}"
    except Exception as e:
        logger.error("[tool] write_add_supply erreur : %s\n%s", e, traceback.format_exc())
        return f"Erreur inattendue : {e}"


@tool
def write_delete_supply(appro_id: str, config: RunnableConfig) -> str:
    """
    Supprime un approvisionnement et décrémente le stock du produit.
    Le stock est décrémenté automatiquement de la quantité reçu.

    Args:
        appro_id : ID de l'approvisionnement à supprimer, ex: 'A0007'

    À utiliser pour les questions comme:
      'Supprime l'approvisionnement A0007.'
    """
    logger.info("[tool] write_delete_supply : appro_id=%s", appro_id)
    try:
        dm = config["configurable"]["data_manager"]
        result = dm.delete_supply(appro_id)
        return (
            f"Approvisionnement {appro_id} supprimé.\n"
            f"   Produit  : {result['product_id']}\n"
            f"   Quantité : {result['quantity_removed']} unités retirées du stock\n"
            f"   Stock    : {result['previous_stock']} → {result['new_stock']} unités"
        )
    except ValueError as e:
        return f"Erreur : {e}"
    except Exception as e:
        logger.error("[tool] write_delete_supply erreur : %s\n%s", e, traceback.format_exc())
        return f"Erreur inattendue : {e}"


# --- Fournisseurs ---

@tool
def write_update_supplier(
    supplier_id: str,
    config: RunnableConfig,
    nom: str | None = None,
    pays: str | None = None,
    delai_livraison: int | None = None,
    note_qualite: float | None = None,
) -> str:
    """
    Met à jour un ou plusieurs champs d'un fournisseur existant.
    Donne seulement les champs que tu veux modifier, les autres ne sont pas modifié.

    Args:
        supplier_id     : ID du fournisseur à modifier, ex: 'F001'
        nom             : nouveau nom (optionnel)
        pays            : nouveau pays (optionnel)
        delai_livraison : nouveau délai de livraison en jours (optionnel, >= 0)
        note_qualite    : nouvelle note qualité (optionnel, entre 0 et 10)

    À utiliser pour les questions comme:
      'Met à jour le délai de livraison de Sanofi à 5 jours.'
    """
    logger.info("[tool] write_update_supplier : supplier_id=%s", supplier_id)
    try:
        dm = config["configurable"]["data_manager"]
        result = dm.update_supplier(
            supplier_id, nom=nom, pays=pays,
            delai_livraison=delai_livraison, note_qualite=note_qualite,
        )
        fields_str = ", ".join(f"{k}={v}" for k, v in result["updated_fields"].items())
        return f"Fournisseur {supplier_id} mis à jour : {fields_str}."
    except ValueError as e:
        return f"Erreur : {e}"
    except Exception as e:
        logger.error("[tool] write_update_supplier erreur : %s\n%s", e, traceback.format_exc())
        return f"Erreur inattendue : {e}"


@tool
def write_add_supplier(
    nom: str,
    pays: str,
    delai_livraison: int,
    note_qualite: float,
    config: RunnableConfig,
) -> str:
    """
    Ajoute un nouveau fournisseur. L'ID est auto-généré.

    Args:
        nom             : nom du fournisseur, ex: 'Pfizer France'
        pays            : pays, ex: 'France', 'Allemagne'
        delai_livraison : délai de livraison en jours (>= 0)
        note_qualite    : note qualité entre 0 et 10

    À utiliser pour les questions comme:
      'Ajoute un nouveau fournisseur Pfizer avec une note qualité de 8.5.'
    """
    logger.info("[tool] write_add_supplier : nom=%s", nom)
    try:
        dm = config["configurable"]["data_manager"]
        result = dm.add_supplier(nom, pays, delai_livraison, note_qualite)
        return (
            f"Fournisseur ajouté ({result['supplier_id']}) : {nom}\n"
            f"   Pays            : {pays}\n"
            f"   Délai livraison : {delai_livraison} jours\n"
            f"   Note qualité    : {note_qualite}/10"
        )
    except ValueError as e:
        return f"Erreur : {e}"
    except Exception as e:
        logger.error("[tool] write_add_supplier erreur : %s\n%s", e, traceback.format_exc())
        return f"Erreur inattendue : {e}"


@tool
def write_delete_supplier(supplier_id: str, config: RunnableConfig) -> str:
    """
    Supprime un fournisseur.
    Refusé si des approvisionnements le référencent encore.

    Args:
        supplier_id : ID du fournisseur à supprimer, ex: 'F003'

    À utiliser pour les questions comme:
      'Supprime le fournisseur F003.'
    """
    logger.info("[tool] write_delete_supplier : supplier_id=%s", supplier_id)
    try:
        dm = config["configurable"]["data_manager"]
        result = dm.delete_supplier(supplier_id)
        return f"Fournisseur {supplier_id} ('{result['nom']}') supprimé."
    except ValueError as e:
        return f"Erreur : {e}"
    except Exception as e:
        logger.error("[tool] write_delete_supplier erreur : %s\n%s", e, traceback.format_exc())
        return f"Erreur inattendue : {e}"


# ===========================================================================
# 4. VISUALISATION
# ===========================================================================

# @tool
# def generate_chart(code: str, chart_type: str, title: str, config: RunnableConfig) -> str:
#     """
#     Génère un graphique Plotly à partir d'une expression pandas et le sauvegarde en PNG.

#     Args:
#         code       : expression pandas produisant un DataFrame ou Series.
#                      Mêmes règles que query_data (single expression or multi-line avec `result`)..
#                      Exemple : Ventes.groupby('Mois')['CA_EUR'].sum().reset_index()
#         chart_type : type de graphique — 'bar', 'line', 'pie' ou 'scatter'
#         title      : titre du graphique

#     À utiliser pour les questions comme:
#       'Génère un graphique du CA par mois.'
#       'Montre la répartition des ventes par région en camembert.'
#     """
#     logger.info("[tool] generate_chart : chart_type=%s, title=%s", chart_type, title)
#     logger.debug("[tool] generate_chart code :\n%s", code)

#     try:
#         import plotly.express as px
#         dm = config["configurable"]["data_manager"]
#         namespace = {
#             name: dm.get(name)
#             for name in ["Produits", "Ventes", "Fournisseurs", "Approvisionnements"]
#         }
#         data = _safe_eval(code, namespace)

#         if isinstance(data, pd.Series):
#             data = data.reset_index()
#             data.columns = [str(c) for c in data.columns]

#         if not isinstance(data, pd.DataFrame):
#             return f"Erreur : le code doit produire un DataFrame ou une Series, pas un {type(data).__name__}."

#         cols = list(data.columns)
#         chart_type = chart_type.lower()

#         if chart_type == "bar":
#             fig = px.bar(data, x=cols[0], y=cols[1], title=title)
#         elif chart_type == "line":
#             fig = px.line(data, x=cols[0], y=cols[1], title=title)
#         elif chart_type == "pie":
#             fig = px.pie(data, names=cols[0], values=cols[1], title=title)
#         elif chart_type == "scatter":
#             fig = px.scatter(data, x=cols[0], y=cols[1], title=title)
#         else:
#             return f"Erreur : type inconnu '{chart_type}'. Utilise 'bar', 'line', 'pie' ou 'scatter'."

#         os.makedirs(CHARTS_DIR, exist_ok=True)
#         safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in title)[:50]
#         filepath = os.path.join(CHARTS_DIR, f"{safe_title}.png")
#         fig.write_image(filepath, width=900, height=500)

#         logger.info("[tool] generate_chart sauvegardé : %s", filepath)
#         return f"Chart saved to: {filepath}"

#     except Exception as e:
#         logger.error("[tool] generate_chart erreur : %s\n%s", e, traceback.format_exc())
#         return f"Erreur lors de la génération du graphique : {e}"

@tool
def generate_chart(code: str, chart_type: Literal["bar", "line", "pie","scatter"], title: str, config: RunnableConfig, tool_call_id: Annotated[str, InjectedToolCallId],) -> str:
    """
    Génère un graphique matplotlib à partir d'une expression pandas et le sauvegarde en PNG.

    DataFrames disponibles (mêmes que query_data) :
    - Produits           : catalogue de produit (ID_Produit, Nom_Produit, Categorie, Prix_Unitaire_EUR, Stock_Actuel, Seuil_Alerte)
    - Ventes             : historique de vente (ID_Vente, ID_Produit, Mois, Année, Quantite_Vendue, Prix_Vente_EUR, CA_EUR, Region)
    - Fournisseurs       : fournisseurs (ID_Fournisseur, Nom_Fournisseur, Pays, Delai_Livraison_Jours, Note_Qualite)
    - Approvisionnements : commande de stock (ID_Appro, ID_Produit, ID_Fournisseur, Date_Livraison, Quantite_Recue, Cout_Total_EUR)

    Args:
        code       : expression pandas produisant un DataFrame ou Series avec EXACTEMENT 2 colonnes :
                    - colonne 1 → axe X (catégories, labels, mois, régions…)
                    - colonne 2 → axe Y (valeurs numériques)
                    Mêmes règles que query_data :
                    - Expression simple → écrire directement
                    - Multi-line → assigner le résultat final à `result`
        chart_type : type de graphique parmi :
                    - 'bar'     → comparaison de catégories (CA par région, stock par produit…)
                    - 'line'    → évolution dans le temps (CA mensuel, tendances…)
                    - 'pie'     → répartition en pourcentage (part de marché, distribution…)
                    - 'scatter' → corrélation entre deux variables numériques
        title      : titre affiché sur le graphique, doit être descriptif (ex: 'CA par Région en 2025')

    Exemples de `code` valides :
    - Bar / Line (CA par mois) :
            Ventes[Ventes['Année'] == 2025].groupby('Mois')['CA_EUR'].sum().reset_index()
            → colonnes : ['Mois', 'CA_EUR']

    - Pie (répartition par région) :
            Ventes.groupby('Region')['CA_EUR'].sum().reset_index()
            → colonnes : ['Region', 'CA_EUR']

    - Scatter (quantité vs prix) :
            Ventes[['Quantite_Vendue', 'Prix_Vente_EUR']]
            → colonnes : ['Quantite_Vendue', 'Prix_Vente_EUR']

    - Multi-line (coût appro par mois) :
            result = Approvisionnements.copy()
            result['Mois'] = result['Date_Livraison'].dt.month
            result = result.groupby('Mois')['Cout_Total_EUR'].sum().reset_index()

    Règles importantes :
    - Le code DOIT produire exactement 2 colonnes, sinon le graphique sera incorrect.
    - Pour les séries temporelles, toujours filtrer par Année pour éviter les doublons de mois.
    - Ne PAS appeler query_data avant ce tool — ce tool lit les données directement.
    - Préférer 'line' sur 'bar' dès qu'il y a une notion de progression temporelle.
    - Préférer 'pie' uniquement si le nombre de catégories est ≤ 8 (lisibilité).

    À utiliser pour les demandes comme :
    'Génère un graphique ...'
    'Donne moi un visuel ...'
    'Génère un graphique du CA par mois.'
    'Montre la répartition des ventes par région.'
    'Affiche l'évolution du stock critique sur l'année.'
    """
    logger.info("[tool] generate_chart : chart_type=%s, title=%s", chart_type, title)
    logger.debug("[tool] generate_chart code :\n%s", code)
    try:

        dm = config["configurable"]["data_manager"]
        namespace = {
            name: dm.get(name)
            for name in ["Produits", "Ventes", "Fournisseurs", "Approvisionnements"]
        }

        data = _safe_eval(code, namespace)

        if isinstance(data, pd.Series):
            data = data.reset_index()
            data.columns = [str(c) for c in data.columns]
        if not isinstance(data, pd.DataFrame):
            return f"Erreur : le code doit produire un DataFrame ou une Series, pas un {type(data).__name__}."

        cols = list(data.columns)
        x, y = cols[0], cols[1]
        chart_type = chart_type.lower()

        plt.style.use("seaborn-v0_8-whitegrid")
        fig, ax = plt.subplots(figsize=(11, 5.5))
        colors = plt.get_cmap("tab10").colors

        if chart_type == "bar":
            bars = ax.bar(data[x].astype(str), data[y], color=colors[0], edgecolor="white", linewidth=0.6)
            ax.bar_label(bars, fmt="%.0f", padding=3, fontsize=8)
            ax.set_xlabel(x, fontsize=10)
            ax.set_ylabel(y, fontsize=10)
            plt.xticks(rotation=35, ha="right", fontsize=8)

        elif chart_type == "line":
            ax.plot(data[x].astype(str), data[y], marker="o", color=colors[0], linewidth=2, markersize=5)
            ax.fill_between(range(len(data)), data[y], alpha=0.08, color=colors[0])
            ax.set_xlabel(x, fontsize=10)
            ax.set_ylabel(y, fontsize=10)
            plt.xticks(range(len(data)), data[x].astype(str), rotation=35, ha="right", fontsize=8)

        elif chart_type == "pie":
            wedges, texts, autotexts = ax.pie(
                data[y],
                labels=data[x].astype(str),
                autopct="%1.1f%%",
                colors=list(colors[: len(data)]),
                startangle=140,
                pctdistance=0.82,
            )
            for t in autotexts:
                t.set_fontsize(8)
            ax.axis("equal")

        elif chart_type == "scatter":
            ax.scatter(data[x], data[y], color=colors[0], alpha=0.7, edgecolors="white", linewidth=0.5, s=60)
            ax.set_xlabel(x, fontsize=10)
            ax.set_ylabel(y, fontsize=10)

        else:
            plt.close(fig)
            return f"Erreur : type inconnu '{chart_type}'. Utilise 'bar', 'line', 'pie' ou 'scatter'."

        ax.set_title(title, fontsize=13, fontweight="bold", pad=14)
        if chart_type != "pie":
            ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
            ax.spines[["top", "right"]].set_visible(False)

        plt.tight_layout()
        
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
        plt.close(fig)
        b64 = base64.b64encode(buf.getvalue()).decode()

        logger.info("[tool] generate_chart sauvegardé")
        return Command(
            update={
                "charts": [b64],
                "messages": [ToolMessage(f"Graphique généré.", tool_call_id=tool_call_id)]  # ← obligatoire
            }
        )
    except Exception as e:
        logger.error("[tool] generate_chart erreur : %s\n%s", e, traceback.format_exc())
        return f"Erreur lors de la génération du graphique : {e}"


# ===========================================================================
# Registres d'outils — importés par agent.py
# ===========================================================================



READ_TOOLS = [
    query_data,
    get_product_by_name,
    get_stock_summary,
    get_low_stock_products,
    get_sales_by_month,
    get_sales_by_region,
    get_top_products,
    get_ca_by_region,
    get_monthly_ca_trend,
    get_sales_velocity,
    get_supplier_by_name,
    get_best_supplier,
    get_supplier_by_product,
    get_supply_by_supplier,
    get_all_regions,
    generate_chart,
]

WRITE_TOOL_LABELS = {
    "write_add_sale":       "Enregistrer une vente",
    "write_add_supply":     "Enregistrer un approvisionnement",
    "write_add_product":    "Ajouter un produit",
    "write_add_supplier":   "Ajouter un fournisseur",
    "write_update_product": "Modifier un produit",
    "write_update_supplier": "Modifier un fournisseur",
    "write_delete_sale":    "Supprimer une vente",
    "write_delete_supply":  "Supprimer un approvisionnement",
    "write_delete_product": "Supprimer un produit",
    "write_delete_supplier": "Supprimer un fournisseur",
}

WRITE_TOOLS = [
    write_add_sale,
    write_add_supply,
    write_add_product,
    write_add_supplier,
    write_update_product,
    write_update_supplier,
    write_delete_sale,
    write_delete_supply,
    write_delete_product,
    write_delete_supplier,
]