import logging
import json
from agents.base_agent import MCPBackedAgent
from core.language_manager import enforce_language, detect_language
from core.utils.utils_prompt import load_prompt
from core.observability import ls_context
from core.utils.normalize_reply import normalize_reply
from openai import OpenAI  # 🧠 para resumir automáticamente
from core.config import Settings as C

log = logging.getLogger("InfoAgent")

info_prompt = load_prompt("info_prompt.txt")
agent = MCPBackedAgent("InfoAgent")
client = OpenAI(api_key=C.OPENAI_API_KEY)


@agent.mcp.tool()
async def consulta_info(pregunta: str) -> str:
    """
    Consulta información general del hotel (horarios, servicios, ubicación...),
    y devuelve una versión resumida y en el idioma del usuario.
    """
    with ls_context(
        name="InfoAgent.consulta_info",
        metadata={"pregunta": pregunta},
        tags=["info", "consulta"],
    ):
        try:
            lang = detect_language(pregunta)
            tool = await agent.kb_client.get_tool("Base_de_conocimientos_del_hotel")
            raw_reply = await tool.ainvoke({"input": pregunta})

            cleaned = normalize_reply(raw_reply, pregunta, "InfoAgent")

            # ✨ Nuevo paso: resumir si la respuesta es demasiado larga
            if len(cleaned) > 800:
                log.info(f"Resumiendo respuesta larga ({len(cleaned)} caracteres)...")
                summary_prompt = (
                    f"Resumir el siguiente texto en máximo 3 frases claras, "
                    f"resaltando solo lo relevante para la pregunta: '{pregunta}'.\n\n{cleaned}"
                )
                summary = client.chat.completions.create(
                    model="gpt-4.1-mini",
                    messages=[
                        {"role": "system", "content": "Eres un asistente hotelero."},
                        {"role": "user", "content": summary_prompt},
                    ],
                    temperature=0.4,
                )
                cleaned = summary.choices[0].message.content.strip()

            return enforce_language(pregunta, cleaned, lang)

        except Exception as e:
            log.error(f"❌ Error en InfoAgent: {e}", exc_info=True)
            return f"⚠️ Error en InfoAgent: {e}"


if __name__ == "__main__":
    print("✅ InfoAgent conectado a la Base de Conocimientos del Hotel")
    agent.mcp.run(transport="stdio", show_banner=False)
