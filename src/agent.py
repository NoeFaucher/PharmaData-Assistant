import os
from typing import Annotated, Dict, Literal, Optional, TypedDict, Sequence
from dotenv import load_dotenv
import operator


# from langchain_openrouter import ChatOpenRouter
from langchain_openai import ChatOpenAI

from langchain_core.messages import BaseMessage, AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph, MessagesState , add_messages
from langgraph.types import Command, interrupt
from langgraph.prebuilt import ToolNode
from src.logging_config import get_logger
from src.tools import READ_TOOLS, WRITE_TOOLS, WRITE_TOOL_LABELS

load_dotenv()
logger = get_logger(__name__)


SYSTEM_PROMPT = """Tu es un assistant intelligent pour PharmaTech SA, une PME pharmaceutique française.
Tu analyses et modifies les données internes stockées dans un fichier Excel multi-onglets.

## Contenu du fichier Excel

Le fichier contient 4 onglets liés entre eux :

| Onglet               | Description                                        | Colonnes principales                                                      |
|----------------------|----------------------------------------------------|---------------------------------------------------------------------------|
| Produits             | Catalogue des médicaments (12 produits)            | ID_Produit, Nom_Produit, Categorie, Prix_Unitaire_EUR, Stock_Actuel, Seuil_Alerte |
| Ventes               | Historique des ventes (180 lignes)                 | ID_Vente, ID_Produit, Mois, Année, Quantite_Vendue, Prix_Vente_EUR, CA_EUR, Region |
| Fournisseurs         | Liste des fournisseurs (6 fournisseurs)            | ID_Fournisseur, Nom_Fournisseur, Pays, Delai_Livraison_Jours, Note_Qualite |
| Approvisionnements   | Historique des commandes fournisseurs Q1 2025 (35) | ID_Appro, ID_Produit, ID_Fournisseur, Date_Livraison, Quantite_Recue, Cout_Total_EUR |

Particularités des données :
- Mois : nom français ("Janvier", "Février", "Mars", ... , "Décembre"). Toujours filtrer Mois et Année en même temps.
- Date_Livraison : datetime pandas — utiliser .dt.month / .dt.year, JAMAIS .str ou .startswith()
- Format des IDs : produit "Pxxx", vente "Vxxxx", fournisseur "Fxxx", approvisionnement "Axxxx" ex: P002, V0150, F012, A0013

## Outils disponibles

### Lecture (privilégie toujours un outil segmenté en priorité avant query_data)
| Outil | Usage |
|-------|-------|
| get_product_by_name(name) | Chercher un produit par nom → ID. "" pour lister tout. |
| get_supplier_by_name(name) | Chercher un fournisseur par nom → ID. "" pour lister tout. |
| get_low_stock_products() | Produits sous le seuil d'alerte |
| get_stock_summary(limit) | Vue d'ensemble des stocks |
| get_sales_by_month(mois, annee) | Ventes d'un mois donné |
| get_sales_by_region(region, annee) | Ventes par région |
| get_top_products(n, type, annee, mois, region) | Top N produits par CA ou unité vendu |
| get_ca_by_region(mois, annee) | CA par région |
| get_monthly_ca_trend(annee) | Tendance CA mensuelle |
| get_sales_velocity() | Ratio ventes/stock |
| get_best_supplier() | Meilleur fournisseur par note qualité |
| get_supplier_by_product(product_id) | Fournisseurs d'un produit |
| get_supply_by_supplier(supplier_id) | Livraisons d'un fournisseur |
| get_all_regions() | Toutes où il y a eu des ventes
| query_data(code) | Expression pandas libre — seulement si aucun outil segmenté si-dessus ne convient |

### Écriture (modifie le fichier Excel)
| Outil | Usage |
|-------|-------|
| write_add_sale | Enregistrer une vente (stock décrémenté automatiquement) |
| write_add_supply | Enregistrer un approvisionnement (stock incrémenté automatiquement) |
| write_add_product | Ajouter un produit au catalogue |
| write_add_supplier | Ajouter un fournisseur |
| write_update_product | Modifier un produit (stock, prix, nom, etc.) |
| write_update_supplier | Modifier un fournisseur |
| write_delete_sale | Supprimer une vente (stock restauré) |
| write_delete_supply | Supprimer un approvisionnement (stock ajusté) |
| write_delete_product | Supprimer un produit (refusé si références actives) |
| write_delete_supplier | Supprimer un fournisseur (refusé si références actives) |

### Visualisation
| Outil | Usage |
|-------|-------|
| generate_chart(code, chart_type, title) | Graphique bar, line, pie ou scatter |

## Règles

### Avant tout outil d'écriture
Tous les paramètres obligatoires doivent être fournis explicitement par l'utilisateur.
Si un paramètre manque : demande TOUS les paramètres manquants en une seule fois, sans rien inventer ni déduire.

Paramètres obligatoires :
- write_add_sale      : product_id, mois, annee, quantity, prix_vente_eur, region
- write_add_supply    : product_id, supplier_id, quantity, cout_total_eur, delivery_date
- write_add_product   : nom, categorie, prix_unitaire, stock, seuil_alerte
- write_add_supplier  : nom, pays, delai_livraison, note_qualite
- write_update_product : product_id (+ au moins un champ à modifier)
- write_update_supplier : supplier_id (+ au moins un champ à modifier)

Pour product_id : appelle d'abord get_product_by_name, affiche le résultat, attends confirmation.
Pour supplier_id : appelle d'abord get_supplier_by_name, affiche le résultat, attends confirmation.
Ne jamais estimer, calculer ou déduire un paramètre manquant.

### Résolution nom → ID
1. Appelle get_product_by_name ou get_supplier_by_name
2. Si plusieurs résultats : affiche la liste et demande à l'utilisateur de choisir
3. Si aucun résultat : appelle avec "" pour lister tout, affiche et demande
4. Ne jamais inventer un ID

### Références temporelles
Ne jamais supposer l'année. Si l'utilisateur dit "janvier" sans année, demande confirmation.
ex: "Vous voulez dire Janvier [année] ?"

### Régions
Avant de filtrer par région, vérifie les valeurs existantes si le nom semble ambigu (IDF, PACA…) en utilisant get_all_regions.
Si la région demandée ne correspond pas exactement (acronymes PACA, IDF...), affiche la liste de toutes celles déjà présente dans le fichier.

### Termes ambigus
"meilleur", "rentable", "suffisant" → explique l'hypothèse retenue avant de calculer.

### Effets de bord automatiques
write_add_sale et write_add_supply gèrent le stock automatiquement.
Ne jamais chaîner un ajout de vente/approvisionnement + une mise à jour de stock manuelle.

### Hallucination
Si une entité est introuvable : affiche les valeurs réelles disponibles, ne suggère rien.

### Alertes stock
Si un outil d'écriture retourne "ALERTE", le mentionner à l'utilisateur.

### Donnée manquante ou non trouvé
Si un outil retourne qu’aucune donnée n’a été trouvée, ne pas le rappeler.
Demander une clarification à l’utilisateur.

## Réponses
- Réponds dans la langue de l'utilisateur (français/anglais)
- Synthétise les résultats — ne répète pas les sorties brutes des outils
- Pour les calculs non triviaux : indique la méthode en une phrase
- Après écriture : confirme l'ID créé et le nouveau stock
"""


prompt = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_PROMPT),
    MessagesPlaceholder(variable_name="messages"),
])

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    # user_id: str
    # retry_count: int
    last_tool_used: Optional[Dict]
    charts: Annotated[list[str],operator.add]
    

llm = ChatOpenAI(
    model="arcee-ai/trinity-large-preview:free",
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
    temperature=0,
    timeout=60,
    max_retries=2,

)

WRITE_TOOL_NAMES = {t.name for t in WRITE_TOOLS}
tools = READ_TOOLS + WRITE_TOOLS
llm_with_tools = llm.bind_tools(tools)

## NODES

write_node = ToolNode(WRITE_TOOLS)
read_node = ToolNode(READ_TOOLS)


async def agent_node(state: AgentState):
    messages = state["messages"]

    formated_messages = prompt.format_messages(messages= messages)

    response = await llm_with_tools.ainvoke(formated_messages) # , parallel_tool_calls=False)
    
    return {
        "messages": [response],
        # "user_id": state["user_id"],
        # "retry_count" : 1,
        # "last_tool_used": None
    }

def tool_node(state: AgentState):
    last_message = state["messages"][-1]
    
    tool_call = last_message.tool_calls[0]

    return {
        "messages": state["messages"],
        # "user_id": state["user_id"],
        # "retry_count" : 1,
        "last_tool_used": tool_call
    }
    
def approval_node(state: AgentState) -> Command[Literal["writes", "__end__"]]:
    tool_to_approve = state["last_tool_used"]
    
    decision = interrupt({
        "type": "approval_tool_request",
        "title": "Confirmation d'action critique",
        "action": WRITE_TOOL_LABELS[tool_to_approve["name"]],
        "args": tool_to_approve["args"],
        "allowed_responses": ["approve", "reject"],
    })

    if decision == "approve":
        return Command(goto="writes")

    return Command(goto="__end__", update={
        "messages": state["messages"] + ToolMessage("Action annulée par l'utilisateur. Aucune modification effectuée.",tool_call_id=tool_to_approve["id"])
    })

## CONDITIONS

def should_continue(state: AgentState):
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END

def tool_routing(state: AgentState):
    last_message = state["messages"][-1]
    
    if any([tool["name"] in WRITE_TOOL_NAMES for tool in last_message.tool_calls]):
        return "writes"
    return "reads"




## Definition


def create_graph(checkpointer=None):
    """
    Compile et retourne le graph LangGraph avec le checkpointer fourni.
    Si checkpointer est None, utilise MemorySaver (comportement par défaut).
    Utilisé par l'API FastAPI pour injecter un AsyncPostgresSaver.
    """
    if checkpointer is None:
        checkpointer = MemorySaver()
    return (
        StateGraph(AgentState)
        .add_node("agent", agent_node)
        .add_node("tools", tool_node)
        .add_node("writes", write_node)
        .add_node("reads", read_node)
        .add_node("approval", approval_node)
        .set_entry_point("agent")
        .add_conditional_edges(
            "agent",
            should_continue,
            {"tools": "tools", END: END},
        )
        .add_conditional_edges(
            "tools",
            tool_routing,
            {"reads": "reads", "writes": "approval"},
        )
        .add_edge("writes", "agent")
        .add_edge("reads", "agent")
        .compile(checkpointer=checkpointer)
    )

_checkpointer = MemorySaver()

graph =  (
    StateGraph(AgentState)
    .add_node("agent",agent_node)
    .add_node("tools",tool_node)
    .add_node("writes",write_node)
    .add_node("reads",read_node)
    .add_node("approval",approval_node)
    
    .set_entry_point("agent")
    
    .add_conditional_edges(
        "agent",
        should_continue,
        {
            "tools":"tools",
            END: END
        }
    )
    .add_conditional_edges(
        "tools",
        tool_routing,
        {
            "reads":"reads",
            "writes":"approval",
        }
    )
    
    .add_edge("writes","agent")
    .add_edge("reads","agent")
    .compile(
        checkpointer=_checkpointer,
    )
)




async def test1():
    # result = await graph.ainvoke({"messages": [HumanMessage(content="Mets le stock de doliprane à 10000")],},
    #     config=_CONFIG,
    #     stream_mode="updates")
    
    # return resul
    
    # Mets le stock de doliprane à 10000
    
    async for chunk in graph.astream(
        {"messages": [HumanMessage(content="Montre la répartition des ventes par région sur une figure.")],"charts":[]},
        config=_CONFIG,
        stream_mode="updates"
    ):
        usage = ""
        if "agent" in chunk:
            print(len(chunk["agent"]["messages"]))
            usage = chunk["agent"]["messages"][0].usage_metadata
        print(chunk)
            
        print(usage)
    
    
    
    # async for chunk in graph.astream(
    #     {"messages": [HumanMessage(content="Le CA de Mars, stp")],},
    #     config=_CONFIG,
    #     stream_mode="updates"
    # ):
    #     usage = ""
    #     if "agent" in chunk:
    #         print(len(chunk["agent"]["messages"]))
    #         usage = chunk["agent"]["messages"][0].usage_metadata
    #     print(usage)

# async def test2():
#     result = await graph.ainvoke({
#         "messages": [HumanMessage(content="Combien est ce qu'on a fait de vente au total ?")],
#     }, config=_CONFIG)
    
#     return result

# async def keep():
#     print("oui")
#     result = await graph.ainvoke(Command(resume="approve"), config=_CONFIG)
    
#     return result

# async def cancel():
#     print("non")
#     result = await graph.ainvoke(Command(resume="reject"), config=_CONFIG)
    
#     return result




async def main():
    res = 1
    # while res != None:
    res = await test1()

        # print(chunk['__interrupt__'] if '__interrupt__' in chunk else "pas d'inter")
        # print(chunk["last_tool_used"])
        # print(chunk["messages"][-1])

    # print()
    # res = await keep()
    # # res = asyncio.run(cancel())
    # print(res["messages"][-1])

    # print()

    # res = await test2()
    # print(res["messages"][-1])


if __name__ == "__main__":
    import asyncio
    from pathlib import Path
    from src.data_manager import ExcelDataManager
    
    project_root = Path(__file__).resolve().parent.parent
    src = project_root / "pharmatech_data copy.xlsx"
    dm = ExcelDataManager(src)
    _CONFIG = {"configurable": {"thread_id": "session","data_manager": dm}, "recursion_limit": 20}
    
    asyncio.run(main())

    # from src.tools import get_top_products
    # df = dm.get("Ventes")
    
    # print(len(df))
    # print(df["Mois"].unique())
    
    # print(df.tail(5))
    # # dm.add_supplier("test","test",5,9.9)
    # # dm.add_sale("P001","Mars",2025,12,5,"Occitanie")
    # dm.save()
    
    # print(get_top_products.invoke({"n":3,"region":"Île-de-France"},config=_CONFIG))
