from core.state import GraphState
from core.mcp_client import mcp_client
from core.message_composition.reply_utils import normalize_reply
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from core.language import detect_language
from core.nodes.other_node import other_node
import numpy as np

llm_synonyms = ChatOpenAI(model="gpt-4o-mini", temperature=0)
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")

def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

async def general_info_node(state: GraphState) -> GraphState:
    user_question = state["messages"][-1]["content"]

    # 🔹 Detectar idioma
    try:
        lang = state.get("language") or detect_language(user_question)
    except Exception:
        lang = "es"

    # 🔹 Expansión de la consulta con sinónimos
    expanded = llm_synonyms.invoke([
        {"role": "system", "content": f"Expande la siguiente consulta en {lang} con sinónimos útiles para búsqueda."},
        {"role": "user", "content": user_question},
    ]).content

    # 🔹 Consultar la KB
    tools = await mcp_client.get_tools(server_name="InfoAgent")
    tool = next((t for t in tools if t.name == "Base_de_conocimientos_del_hotel"), None)

    if not tool:
        final_reply = "No dispongo de ese dato en este momento."
    else:
        try:
            raw_reply = await tool.ainvoke({"input": expanded})

            if isinstance(raw_reply, list) and raw_reply:
                # ---- Ranking semántico ----
                q_emb = embeddings.embed_query(user_question)

                best_item = None
                best_score = -1
                for item in raw_reply:
                    text = ""
                    if isinstance(item, dict):
                        text = item.get("pageContent") or item.get("text") or ""
                    elif isinstance(item, str):
                        text = item
                    if not text.strip():
                        continue

                    d_emb = embeddings.embed_query(text)
                    score = cosine_similarity(q_emb, d_emb)
                    if score > best_score:
                        best_score = score
                        best_item = text

                reply_text = best_item or str(raw_reply[0])
            else:
                reply_text = raw_reply

            # 🔹 Normalizar y forzar idioma
            final_reply = normalize_reply(reply_text, user_question, lang, source="InfoAgent")

            # Fallback a Interno si no hay nada útil
            if not final_reply.strip() or "no dispongo" in final_reply.lower():
                return await other_node(state)

        except Exception as e:
            final_reply = f"⚠️ Error consultando InfoAgent: {e}"

    return {
        **state,
        "messages": state["messages"] + [
            {"role": "assistant", "content": final_reply}
        ],
    }
