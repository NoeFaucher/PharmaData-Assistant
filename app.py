"""
app.py — PharmaData Assistant (Streamlit) — client API
=======================================================
Lance avec :  streamlit run app.py

Consomme l'API FastAPI (api/) via HTTP.
L'API doit tourner sur API_URL (défaut : http://localhost:8000).
"""

import base64
import json
from io import BytesIO

import requests
import streamlit as st
from PIL import Image

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

API_URL = "http://localhost:8000"
# API_URL = "http://<ip_public"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ══════════════════════════════════════════════════════════════════════════════
#  APPELS API
# ══════════════════════════════════════════════════════════════════════════════

def api_register(email: str, password: str, full_name: str) -> dict:
    r = requests.post(f"{API_URL}/auth/register", json={
        "email": email, "password": password, "full_name": full_name,
    })
    return r.json(), r.status_code


def api_login(email: str, password: str) -> dict:
    r = requests.post(f"{API_URL}/auth/login", json={
        "email": email, "password": password,
    })
    return r.json(), r.status_code


def api_upload_file(file_bytes: bytes, filename: str, token: str) -> dict:
    r = requests.post(
        f"{API_URL}/files/upload",
        headers=_headers(token),
        files={"file": (filename, file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    return r.json(), r.status_code


def api_create_conversation(file_id: str, token: str) -> dict:
    r = requests.post(
        f"{API_URL}/conversations",
        headers={**_headers(token), "Content-Type": "application/json"},
        json={"file_id": file_id},
    )
    return r.json(), r.status_code


def api_download_file(file_id: str, token: str) -> bytes | None:
    r = requests.get(f"{API_URL}/files/{file_id}/download", headers=_headers(token))
    return r.content if r.status_code == 200 else None


def api_approve(conv_id: str, decision: str, token: str) -> dict:
    r = requests.post(
        f"{API_URL}/conversations/{conv_id}/approve",
        headers={**_headers(token), "Content-Type": "application/json"},
        json={"decision": decision},
    )
    return r.json(), r.status_code


def api_get_conversation(conv_id: str, token: str) -> dict:
    r = requests.get(f"{API_URL}/conversations/{conv_id}", headers=_headers(token))
    return r.json() if r.status_code == 200 else {}


def stream_message(conv_id: str, content: str, token: str):
    """Générateur synchrone qui yield des events SSE parsés depuis l'API."""
    with requests.post(
        f"{API_URL}/conversations/{conv_id}/messages",
        headers={**_headers(token), "Accept": "text/event-stream"},
        json={"content": content},
        stream=True,
        timeout=180,
    ) as r:
        for raw_line in r.iter_lines():
            if raw_line and raw_line.startswith(b"data: "):
                try:
                    yield json.loads(raw_line[6:])
                except json.JSONDecodeError:
                    pass


# ══════════════════════════════════════════════════════════════════════════════
#  SESSION STATE
# ══════════════════════════════════════════════════════════════════════════════

def init_session():
    defaults = {
        "api_token":         None,
        "refresh_token":     None,
        "user_email":        None,
        "file_id":           None,
        "conv_id":           None,
        "file_name":         None,
        "file_loaded":       False,
        "messages":          [],
        "pending_interrupt": False,
        "interrupt_info":    None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def reset_file_session():
    st.session_state.update({
        "file_id":           None,
        "conv_id":           None,
        "file_name":         None,
        "file_loaded":       False,
        "messages":          [],
        "pending_interrupt": False,
        "interrupt_info":    None,
    })


# ══════════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

def handle_login(email: str, password: str):
    data, status = api_login(email, password)
    if status == 200:
        st.session_state["api_token"]     = data["access_token"]
        st.session_state["refresh_token"] = data["refresh_token"]
        st.session_state["user_email"]    = email
    else:
        detail = data.get("detail", "Erreur inconnue")
        st.error(f"❌ {detail}")


def handle_register(email: str, password: str, full_name: str):
    data, status = api_register(email, password, full_name)
    if status == 201:
        st.success("✅ Compte créé ! Connectez-vous.")
    else:
        detail = data.get("detail", "Erreur inconnue")
        st.error(f"❌ {detail}")


def handle_file_upload(uploaded_file) -> list[str]:
    file_bytes = uploaded_file.read()
    token = st.session_state["api_token"]

    data, status = api_upload_file(file_bytes, uploaded_file.name, token)
    if status != 201:
        errors = data.get("detail", {})
        if isinstance(errors, dict):
            return errors.get("errors", [str(errors)])
        return [str(errors)]

    file_id = data["id"]

    conv_data, conv_status = api_create_conversation(file_id, token)
    if conv_status != 201:
        return [f"Erreur création conversation : {conv_data.get('detail')}"]

    reset_file_session()
    st.session_state.update({
        "file_id":     file_id,
        "conv_id":     conv_data["id"],
        "file_name":   uploaded_file.name,
        "file_loaded": True,
    })
    return []


def handle_send(user_input: str):
    token   = st.session_state["api_token"]
    conv_id = st.session_state["conv_id"]

    st.session_state["messages"].append({"role": "user", "content": user_input})

    accumulated      = ""
    interrupt_val    = None
    final_type       = "done"
    tool_steps: list[dict] = []
    status_obj       = None
    status_finalized = False

    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant", avatar="💊"):
        status_placeholder = st.empty()
        text_placeholder   = st.empty()

        for event in stream_message(conv_id, user_input, token):
            etype = event.get("type")

            if etype == "tool_call":
                if status_obj is None:
                    status_obj = status_placeholder.status("🔍 Appel des outils…", expanded=True)
                icon = _tool_icon(event.get("name", ""))
                status_obj.write(f"{icon} **`{event['name']}`**")
                if event.get("args"):
                    status_obj.json(event["args"])
                tool_steps.append({"type": "call", "name": event["name"], "args": event.get("args", {})})

            elif etype == "tool_result":
                if status_obj:
                    c = event["content"]
                    status_obj.caption(f"↳ {c[:300]}{'…' if len(c) > 300 else ''}")
                tool_steps.append({"type": "result", "content": event["content"]})
                accumulated = ""

            elif etype == "chart_gen":
                chart_b64 = event.get("chart", "")
                try:
                    tool_steps.append({"type": "chart", "image": chart_b64})
                except Exception:
                    pass

            elif etype == "token":
                if status_obj and not status_finalized:
                    status_obj.update(label="✅ Outils exécutés", state="complete", expanded=False)
                    status_finalized = True
                accumulated += event["token"]
                text_placeholder.markdown(accumulated + "▌")

            elif etype == "interrupt":
                interrupt_val = event["value"]
                final_type    = "interrupt"
                if status_obj and not status_finalized:
                    status_obj.update(state="complete", expanded=False)
                    status_finalized = True

            elif etype == "error":
                final_type    = "error"
                accumulated  += f"\n\n❌ Erreur : {event.get('error', 'Inconnue')}"

        if status_obj and not status_finalized:
            state = "error" if final_type == "error" else "complete"
            status_obj.update(label="✅ Outils exécutés", state=state, expanded=False)

        if accumulated:
            text_placeholder.markdown(accumulated)

    # ── Mise à jour session ──────────────────────────────────────────────────
    if final_type == "interrupt":
        st.session_state["pending_interrupt"] = True
        st.session_state["interrupt_info"]    = {"value": interrupt_val}
        if accumulated:
            st.session_state["messages"].append({
                "role": "assistant", "content": accumulated, "tool_steps": tool_steps,
            })
        st.session_state["messages"].append({"role": "interrupt", "content": str(interrupt_val)})
    else:
        if accumulated.strip():
            st.session_state["messages"].append({
                "role": "assistant", "content": accumulated, "tool_steps": tool_steps,
            })


def handle_approval(decision: str):
    token   = st.session_state["api_token"]
    conv_id = st.session_state["conv_id"]
    label   = "Application de la modification…" if decision == "approve" else "Annulation…"

    with st.spinner(label):
        data, status = api_approve(conv_id, decision, token)

    if status != 200:
        st.session_state["messages"].append({
            "role": "assistant",
            "content": f"❌ Erreur : {data.get('detail', 'Inconnue')}",
        })
        st.session_state["pending_interrupt"] = False
        return

    # Supprimer le message interrupt de l'historique
    if st.session_state["messages"] and st.session_state["messages"][-1]["role"] == "interrupt":
        st.session_state["messages"].pop()

    st.session_state["messages"].append({
        "role":    "assistant",
        "content": data.get("message", ""),
    })
    st.session_state["pending_interrupt"] = False
    st.session_state["interrupt_info"]    = None


# ── Icônes par type de tool ──────────────────────────────────────────────────

_TOOL_ICONS: list[tuple[str, str]] = [
    ("get_",            "🔍"),
    ("create_",         "➕"),
    ("write_update_",   "✏️"),
    ("write_delete_",   "🗑️"),
    ("generate_",       "📊"),
]

def _tool_icon(name: str) -> str:
    for prefix, icon in _TOOL_ICONS:
        if prefix in name.lower():
            return icon
    return "⚙️"


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE CONFIG & CSS
# ══════════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="PharmaData Assistant",
    page_icon="💊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Syne:wght@400;700;800&display=swap');

html, body, [class*="css"] { font-family: 'Syne', sans-serif; }

[data-testid="stSidebar"] {
    background: #13131a !important;
    border-right: 1px solid #1f1f2e;
}

.pharma-title {
    font-family: 'Syne', sans-serif;
    font-size: 1.5rem;
    font-weight: 800;
    letter-spacing: -0.03em;
    margin-bottom: 0.1rem;
}
.pharma-title span { color: #4f8ef7; }

.pharma-sub {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    color: #6b6b8a;
    margin-bottom: 1.25rem;
}

.interrupt-card {
    background: #1a160a;
    border: 1px solid #4a3d10;
    border-left: 4px solid #f59e0b;
    border-radius: 10px;
    padding: 0.9rem 1.1rem;
    margin-bottom: 0.75rem;
}
.interrupt-label {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: #856d30;
    font-weight: 700;
    margin-bottom: 0.4rem;
}
.interrupt-body {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.82rem;
    color: #d4c870;
    white-space: pre-wrap;
    word-break: break-word;
}

.file-badge {
    background: #0d1a0d;
    border: 1px solid #1a3a1a;
    border-radius: 8px;
    padding: 0.5rem 0.75rem;
    font-size: 0.76rem;
    font-family: 'JetBrains Mono', monospace;
    color: #5a9a5a;
    word-break: break-all;
    margin-bottom: 0.5rem;
}

.user-badge {
    background: #0d0d1a;
    border: 1px solid #1f1f3a;
    border-radius: 8px;
    padding: 0.4rem 0.75rem;
    font-size: 0.76rem;
    font-family: 'JetBrains Mono', monospace;
    color: #4f8ef7;
    margin-bottom: 0.75rem;
}

.welcome-box {
    border: 1px dashed #252535;
    border-radius: 12px;
    padding: 3rem 2rem;
    text-align: center;
    color: #3a3a5a;
    margin: 4rem auto;
    max-width: 420px;
}
.welcome-box .wi { font-size: 2.5rem; margin-bottom: 0.75rem; }
.welcome-box h3  { color: #5a5a8a; margin-bottom: 0.4rem; }
.welcome-box p   { font-size: 0.85rem; line-height: 1.6; }
</style>
""", unsafe_allow_html=True)

init_session()


# ══════════════════════════════════════════════════════════════════════════════
#  SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("### 💊 PharmaData")
    st.markdown("---")

    # ── Auth ─────────────────────────────────────────────────────────────────
    if not st.session_state["api_token"]:
        st.markdown("#### 🔐 Connexion")
        auth_tab, register_tab = st.tabs(["Se connecter", "Créer un compte"])

        with auth_tab:
            with st.form("login_form"):
                email    = st.text_input("Email")
                password = st.text_input("Mot de passe", type="password")
                if st.form_submit_button("Connexion", use_container_width=True):
                    if email and password:
                        handle_login(email, password)
                        st.rerun()
                    else:
                        st.warning("Remplissez tous les champs.")

        with register_tab:
            with st.form("register_form"):
                r_name     = st.text_input("Nom complet")
                r_email    = st.text_input("Email")
                r_password = st.text_input("Mot de passe", type="password")
                if st.form_submit_button("Créer un compte", use_container_width=True):
                    if r_email and r_password:
                        handle_register(r_email, r_password, r_name)
                    else:
                        st.warning("Email et mot de passe requis.")

        st.stop()

    # ── Connecté ─────────────────────────────────────────────────────────────
    st.markdown(
        f'<div class="user-badge">👤 {st.session_state["user_email"]}</div>',
        unsafe_allow_html=True,
    )
    if st.button("Déconnexion", use_container_width=True):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()

    st.markdown("---")
    st.markdown("#### 📂 Fichier de données")

    if st.session_state["file_loaded"]:
        st.markdown(
            f'<div class="file-badge">✅ {st.session_state["file_name"]}</div>',
            unsafe_allow_html=True,
        )

        st.markdown("---")
        st.markdown("#### 📥 Exporter")
        if st.button("⬇️ Télécharger le fichier", use_container_width=True):
            excel_bytes = api_download_file(
                st.session_state["file_id"], st.session_state["api_token"]
            )
            if excel_bytes:
                from pathlib import Path
                base = Path(st.session_state["file_name"]).stem
                ext  = Path(st.session_state["file_name"]).suffix
                st.download_button(
                    label="💾 Cliquez pour sauvegarder",
                    data=excel_bytes,
                    file_name=f"{base}_modifié{ext}",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )

        st.markdown("---")
        if st.button("🔄 Changer de fichier", use_container_width=True):
            reset_file_session()
            st.rerun()
        if st.button("🗑️ Effacer la conversation", use_container_width=True):
            token = st.session_state["api_token"]
            file_id = st.session_state["file_id"]
            st.session_state["messages"]          = []
            st.session_state["pending_interrupt"] = False
            # Nouvelle conversation sur le même fichier
            conv_data, _ = api_create_conversation(file_id, token)
            st.session_state["conv_id"] = conv_data.get("id")
            st.rerun()

    else:
        uploaded = st.file_uploader(
            "Importer un fichier Excel",
            type=["xlsx"],
            label_visibility="collapsed",
        )
        if uploaded:
            with st.spinner("Chargement…"):
                errors = handle_file_upload(uploaded)
            if errors:
                st.error("❌ Format invalide")
                for err in errors:
                    st.markdown(f"- {err}")
            else:
                st.success("✅ Fichier chargé !")
                st.rerun()

        with st.expander("📋 Format attendu"):
            st.markdown("""
**Onglets obligatoires :**

`Produits` — `ID_Produit`, `Nom_Produit`, `Categorie`, `Prix_Unitaire_EUR`, `Stock_Actuel`, `Seuil_Alerte`

`Fournisseurs` — `ID_Fournisseur`, `Nom_Fournisseur`, `Pays`, `Delai_Livraison_Jours`, `Note_Qualite`

`Approvisionnements` — `ID_Appro`, `ID_Produit`, `ID_Fournisseur`, `Date_Livraison`, `Quantite_Recue`, `Cout_Total_EUR`

**Ventes** *(année dans le nom, ex: `Ventes_Q1_2025`)* — `ID_Vente`, `ID_Produit`, `Mois`, `Quantite_Vendue`, `Prix_Vente_EUR`, `CA_EUR`, `Region`
""")

    st.markdown("---")
    st.caption("PharmaData Assistant · v1.0")
    st.caption("Alimenté par LangGraph + FastAPI")


# ══════════════════════════════════════════════════════════════════════════════
#  ZONE PRINCIPALE
# ══════════════════════════════════════════════════════════════════════════════

st.markdown(
    '<div class="pharma-title">Pharma<span>Data</span> Assistant</div>'
    '<div class="pharma-sub">Posez vos questions en langage naturel sur vos données Excel</div>',
    unsafe_allow_html=True,
)

if not st.session_state["file_loaded"]:
    st.markdown("""
<div class="welcome-box">
  <div class="wi">📊</div>
  <h3>Importez votre fichier Excel</h3>
  <p>Chargez votre fichier de données via le panneau de gauche pour commencer à interagir avec l'assistant.</p>
</div>
""", unsafe_allow_html=True)
    st.stop()

# ── Historique ───────────────────────────────────────────────────────────────
for msg in st.session_state["messages"]:
    role    = msg["role"]
    content = msg["content"]

    if role == "user":
        with st.chat_message("user"):
            st.markdown(content)

    elif role == "assistant":
        with st.chat_message("assistant", avatar="💊"):
            tool_steps = msg.get("tool_steps", [])
            images = []

            if tool_steps:
                n_calls = sum(1 for s in tool_steps if s["type"] == "call")
                label   = f"🔍 {n_calls} outil{'s' if n_calls > 1 else ''} utilisé{'s' if n_calls > 1 else ''}"
                with st.expander(label, expanded=False):
                    for step in tool_steps:
                        if step["type"] == "call":
                            st.write(f"{_tool_icon(step['name'])} **`{step['name']}`**")
                            if step.get("args"):
                                st.json(step["args"])
                        elif step["type"] == "result":
                            c = step["content"]
                            st.caption(f"↳ {c[:300]}{'…' if len(c) > 300 else ''}")
                        elif step["type"] == "chart":
                            try:
                                img_bytes = base64.b64decode(step["image"])
                                images.append(Image.open(BytesIO(img_bytes)))
                            except Exception:
                                pass

            st.markdown(content)
            for image in images:
                st.image(image, width=800)

    elif role == "interrupt":
        with st.chat_message("assistant", avatar="⏳"):
            st.markdown(
                '<div class="interrupt-card">'
                '<div class="interrupt-label">✏️ Opération d\'écriture — approbation requise</div>'
                f'<div class="interrupt-body">{content}</div>'
                '</div>',
                unsafe_allow_html=True,
            )

# ── Boutons d'approbation ────────────────────────────────────────────────────
if st.session_state["pending_interrupt"]:
    col_ok, col_ko, _ = st.columns([1, 1, 4])
    with col_ok:
        if st.button("✅ Approuver", type="primary", use_container_width=True):
            handle_approval("approve")
            st.rerun()
    with col_ko:
        if st.button("🚫 Refuser", use_container_width=True):
            handle_approval("reject")
            st.rerun()

# ── Input ────────────────────────────────────────────────────────────────────
if not st.session_state["pending_interrupt"]:
    user_input = st.chat_input(
        "Posez votre question… (ex : Quel est le CA de Février 2025 ?)"
    )
    if user_input:
        handle_send(user_input)
        st.rerun()
