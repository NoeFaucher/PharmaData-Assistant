# PharmaData Assistant

Assistant conversationnel intelligent pour **PharmaTech SA** (fictif), construit avec LangGraph, permettant d'interroger et de modifier en langage naturel un fichier Excel multi-onglets de données pharmaceutiques.

Le projet expose deux interfaces :
- **`app.py`** — client Streamlit local, intéraction avec l'assistant via l'API
- **`api/`** — API REST FastAPI multi-utilisateur avec authentification JWT et persistance PostgreSQL

---

## Sommaire

- [Aperçu](#aperçu)
- [Architecture](#architecture)
- [Structure du projet](#structure-du-projet)
- [Schéma des données Excel](#schéma-des-données-excel)
- [Installation](#installation)
- [Configuration](#configuration)
- [Lancement](#lancement)
- [Fonctionnalités](#fonctionnalités)
- [Tests](#tests)
- [Logging](#logging)

---

## Aperçu

PharmaData Assistant est un chatbot ReAct (Reasoning + Acting) qui permet à un utilisateur non-technique d'interagir avec des données Excel via une interface conversationnelle.

**Capacités principales :**
- Lecture analytique des données (ventes, stocks, fournisseurs, approvisionnements)
- Écriture transactionnelle avec gestion automatique des effets de bord (mise à jour du stock lors d'une vente/d'un appro)
- Génération de graphiques à la demande
- **Human-in-the-loop** : toute opération d'écriture demande une confirmation explicite de l'utilisateur avant exécution
- Streaming des tokens LLM et des appels d'outils en temps réel
- Chargement d'un fichier Excel personnalisé via l'UI, avec validation du format

---

## Architecture

<center>
 <img src='langgraph_arch.png' width='50%' />
</center>


**Séparation lecture / écriture :**
Les outils sont divisés en deux `ToolNode` distincts. Tout appel à un outil d'écriture (`write_*`) passe par un nœud `approval` qui interrompt le graph via `langgraph.types.interrupt` et attend la décision de l'utilisateur (`approve` / `reject`) avant de reprendre.

---

## Structure du projet

```
test_api/
├── app.py                        # Interface Streamlit (client local)
├── pharmatech_data.xlsx          # Fichier Excel de démonstration
├── requirements.txt              # Dépendances Streamlit / agent
├── requirements_api.txt          # Dépendances API FastAPI
│
├── src/
│   ├── agent.py                  # Graphe LangGraph, nodes, conditions, config LLM
│   ├── data_manager.py           # ExcelDataManager : lecture/écriture Excel + CRUD
│   ├── tools.py                  # ~25 outils LangChain (READ_TOOLS + WRITE_TOOLS)
│   └── logging_config.py         # Logging rotatif console + fichier
│
├── api/
│   ├── main.py                   # Point d'entrée FastAPI (lifespan, routers)
│   ├── auth.py                   # Utilitaires JWT (access 15 min + refresh 7 j)
│   ├── models.py                 # Modèles SQLAlchemy (users, files, conversations)
│   ├── schemas.py                # Schémas Pydantic
│   ├── agent_manager.py          # Cache dm : conv_id → (ExcelDataManager, tmp_path)
│   ├── config.py / deps.py / database.py
│   └── routers/
│       ├── auth.py               # POST /auth/register, /auth/login, /auth/refresh
│       ├── files.py              # POST/GET /files (upload/download Excel)
│       └── conversations.py      # CRUD conversations + SSE streaming + approve
│
├── docker/
│   ├── Dockerfile                # Image Python 3.11-slim (API uniquement)
│   └── docker-compose.yml        # Services : postgres:17 + api
│
├── kubernetes/
│   ├── kubernetes_conf.yaml      # Deployment API + Nginx, Services, ConfigMaps
│   ├── namespace.yaml            # Namespace "pharma"
│   ├── secret.yaml               # Bootstrap token OVH Vault
│   ├── external-secret/
│   │   └── external-secret.yaml  # ClusterSecretStore + ExternalSecret (ESO)
│   └── kubernetes.md             # Guide de déploiement K8s ← lire ici
│
├── test/
│   ├── test_data_manager.py      # Tests unitaires ExcelDataManager (13 classes, ~65 tests)
│   └── test_tools.py             # Tests unitaires outils LangChain (24 classes, ~80 tests)
│
└── logs/
    └── pharmatech.log            # Log rotatif (2 MB × 3 fichiers)
```

---

## Schéma des données Excel

Les fichiers Excel accéptés doivent contenir 3 onglets obligatoire (+ n onglet de ventes avec 'vente' et l'année spécifiée dans le titre ) avec une ligne de titre décorative en première ligne puis les noms des colonnes dans la suivante.

| Onglet | Colonnes clés |
|---|----|
| **Produits** | `ID_Produit`, `Nom_Produit`, `Categorie`, `Prix_Unitaire_EUR`, `Stock_Actuel`, `Seuil_Alerte` |
| **Ventes_<periode_avec_année>** | `ID_Vente`, `ID_Produit`, `Mois`, `Quantite_Vendue`, `Prix_Vente_EUR`, `CA_EUR`, `Region` |
| **Fournisseurs** | `ID_Fournisseur`, `Nom_Fournisseur`, `Pays`, `Delai_Livraison_Jours`, `Note_Qualite` |
| **Approvisionnements** | `ID_Appro`, `ID_Produit`, `ID_Fournisseur`, `Date_Livraison`, `Quantite_Recue`, `Cout_Total_EUR` |

**Conventions :**
- `Mois` : nom français (`"Janvier"`, `"Février"`, …). Les filtres sont toujours combinés `Mois + Année`.
- Format des IDs : `P001` (produit), `V0180` (vente), `F003` (fournisseur), `A0035` (approvisionnement).

---

## Installation

**Prérequis :** Python 3.11+

```bash
# Cloner / se placer dans le répertoire
cd PharmaData-Assistant

# Créer et activer l'environnement virtuel
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows

# Dépendances Streamlit uniquement
pip install -r requirements.txt

# Dépendances API en plus (si vous lancez l'API FastAPI)
pip install -r requirements_api.txt
```

---

## Configuration

Créer un fichier `.env` à la racine du projet :

```env
# LLM (obligatoire)
OPENROUTER_API_KEY=sk-or-...

# Base de données (obligatoire pour l'API FastAPI)
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/pharmadb
DATABASE_URL_SYNC=postgresql://postgres:postgres@localhost:5432/pharmadb

# Sécurité JWT (obligatoire pour l'API FastAPI)
SECRET_KEY=changeme
```

**LLM utilisé :** `arcee-ai/trinity-large-preview:free` via [OpenRouter](https://openrouter.ai)

> En production Kubernetes, `DATABASE_URL`, `DATABASE_URL_SYNC` et `SECRET_KEY` sont injectés automatiquement depuis OVH Vault via l'External Secrets Operator. Voir [`kubernetes/kubernetes.md`](kubernetes/kubernetes.md).

**Variables d'environnement optionnelles pour le logging :**

| Variable | Défaut | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Niveau console |
| `LOG_LEVEL_FILE` | `DEBUG` | Niveau fichier |
| `LOG_FILE` | `logs/pharmatech.log` | Chemin du fichier de log |

---

## Lancement

### Interface Streamlit (client local)

```bash
# Depuis la racine du projet, avec le venv activé
streamlit run app.py
```

L'interface s'ouvre sur `http://localhost:8501`.

**Workflow utilisateur :**
1. L'utilisateur crée un compte / se connecte
1. Importer un fichier `.xlsx` via le panneau latéral
2. Poser des questions en langage naturel dans le chat
3. Pour toute opération d'écriture, approuver ou refuser l'action via les boutons dédiés
4. Télécharger le fichier modifié via le bouton d'export

---

### API FastAPI — 3 méthodes de lancement

#### Méthode 1 — uvicorn (développement)

La méthode la plus directe. Nécessite une instance PostgreSQL accessible.

```bash
# Installer les dépendances si ce n'est pas encore fait
pip install -r requirements.txt -r requirements_api.txt

# Configurer .env (DATABASE_URL, DATABASE_URL_SYNC, SECRET_KEY, OPENROUTER_API_KEY)

# Lancer le serveur avec hot-reload
uvicorn api.main:app --reload
```

L'API est disponible sur `http://localhost:8000`.
Documentation interactive : `http://localhost:8000/docs`

---

#### Méthode 2 — Docker Compose (recommandé en local)

Lance PostgreSQL 17 et l'API en un seul appel. Les `DATABASE_URL` sont configurés automatiquement entre les services.

```bash
cd docker/
docker compose up --build
```

Seules les variables `OPENROUTER_API_KEY` et `SECRET_KEY` sont à fournir dans le `.env` à la racine — les URLs de base de données sont surchargées par le `docker-compose.yml`.

| Service | URL |
|---|---|
| API FastAPI | `http://localhost:8000` |
| Swagger UI | `http://localhost:8000/docs` |
| PostgreSQL | `localhost:5432` (interne) |

```bash
# Arrêter et supprimer les conteneurs
docker compose down

# Supprimer aussi le volume PostgreSQL
docker compose down -v
```

---

#### Méthode 3 — Cluster Kubernetes (production)

Déploiement production avec 2 replicas API, Nginx en reverse proxy, et gestion des secrets via OVH Vault + External Secrets Operator.

Voir le guide complet : [**kubernetes/kubernetes.md**](kubernetes/kubernetes.md)

---

## Fonctionnalités

### Outils de lecture (15)

| Outil | Description |
|---|---|
| `get_product_by_name(name)` | Recherche un produit par nom (partiel, insensible à la casse). `""` liste tout. |
| `get_supplier_by_name(name)` | Idem pour les fournisseurs. |
| `get_low_stock_products()` | Produits dont le stock est sous le seuil d'alerte. |
| `get_stock_summary(limit)` | Vue d'ensemble des stocks avec ratio ventes/stock. |
| `get_sales_by_month(mois, annee)` | Ventes et CA d'un mois donné. |
| `get_sales_by_region(region, annee?)` | Ventes et CA par région, avec filtre année optionnel. |
| `get_top_products(n, type?, annee?, mois?, region?)` | Top N produits par CA ou quantité vendue. |
| `get_ca_by_region(mois?, annee?)` | CA par région avec part en %. |
| `get_monthly_ca_trend(annee)` | Tendance CA mensuelle + projection annuelle. |
| `get_sales_velocity()` | Ratio ventes totales / stock actuel pour chaque produit. |
| `get_best_supplier()` | Fournisseur avec la meilleure note qualité. |
| `get_supplier_by_product(product_id)` | Fournisseurs ayant livré un produit donné. |
| `get_supply_by_supplier(supplier_id)` | Historique des livraisons d'un fournisseur. |
| `get_all_regions()` | Liste toutes les régions présentes dans les ventes. |
| `query_data(code)` | Requête pandas libre (sandbox) — utilisé en dernier recours. |

### Outils d'écriture (10) — avec approbation humaine

| Outil | Description |
|---|---|
| `write_add_sale` | Enregistre une vente — décrémente le stock automatiquement. |
| `write_add_supply` | Enregistre un approvisionnement — incrémente le stock automatiquement. |
| `write_add_product` | Ajoute un produit au catalogue (ID auto-généré). |
| `write_add_supplier` | Ajoute un fournisseur (ID auto-généré). |
| `write_update_product` | Modifie un ou plusieurs champs d'un produit. |
| `write_update_supplier` | Modifie un ou plusieurs champs d'un fournisseur. |
| `write_delete_sale` | Supprime une vente — restaure le stock. |
| `write_delete_supply` | Supprime un approvisionnement — ajuste le stock. |
| `write_delete_product` | Supprime un produit (refusé si référencé par des ventes/appros). |
| `write_delete_supplier` | Supprime un fournisseur (refusé si référencé par des appros). |

### Visualisation

| Outil | Description |
|---|---|
| `generate_chart(code, chart_type, title)` | Génère un graphique matplotlib (`bar`, `line`, `pie`, `scatter`) affiché inline. |

---

## Tests

Les tests sont entièrement indépendants du LLM — aucun appel réseau n'est effectué.

```bash
# Depuis la racine du projet, avec le venv activé
python -m pytest test/ -v
```

**Couverture :**

| Fichier | Classes | Cas testés |
|---|---|---|
| `test/test_data_manager.py` | 13 | ~65 |
| `test/test_tools.py` | 24 | ~80 |

**Stratégie d'isolation :**
- `test_data_manager.py` : copie le fichier Excel dans un répertoire temporaire pytest (`tmp_path`) — le fichier original n'est jamais modifié.
- `test_tools.py` : substitue le singleton `ExcelDataManager` via `monkeypatch` pour chaque test, garantissant une isolation totale.

**Ce qui est testé :**
- Chargement et schéma des 4 onglets
- Persistance round-trip (sauvegarde → rechargement)
- Toutes les opérations CRUD avec leurs effets de bord (stock, alertes, intégrité référentielle)
- Validation des paramètres (valeurs négatives, entités inconnues, stock insuffisant)
- Chaque outil LangChain via l'API `.invoke()` (identique au runtime de l'agent)

---

## Logging

Le module `src/logging_config.py` configure automatiquement un logger rotatif à deux niveaux :

- **Console** : niveau `INFO` (flux haut niveau)
- **Fichier** `logs/pharmatech.log` : niveau `DEBUG` (entrées/sorties des outils, messages LLM)

Fichiers rotatifs : 2 MB × 3 fichiers de sauvegarde.

---

## Exemples de questions

```
Quel est le CA total de Janvier 2025 ?
Montre-moi les produits en rupture de stock.
Quels sont les 3 produits les plus vendus en Île-de-France ?
Affiche la tendance du CA mensuel pour 2025 sous forme de graphique.
Ajoute une vente de 50 unités de Doliprane en Mars 2025 à 8,50 € en région Occitanie.
Quel fournisseur a la meilleure note qualité ?
Met à jour le seuil d'alerte du produit P003 à 200.
```
