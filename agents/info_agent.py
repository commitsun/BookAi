import os
import json
from fastmcp import FastMCP
from langchain_openai import ChatOpenAI
from core.utils_prompt import load_prompt
from core.language import enforce_language, detect_language
from utils.logging_config import silence_logs
from dotenv import load_dotenv

load_dotenv()
silence_logs()

info_prompt = load_prompt("info_prompt.txt")

api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("❌ Falta la variable OPENAI_API_KEY en el entorno.")

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3, api_key=api_key)
mcp = FastMCP("InfoAgent")


@mcp.tool()
async def consulta_info(pregunta: str) -> str:
    """Responde preguntas generales sobre el hotel usando la KB + fallback."""
    try:
        lang = detect_language(pregunta)
        kb_path = os.path.join("tools", "knowledge.json")

        if os.path.exists(kb_path):
            with open(kb_path, "r", encoding="utf-8") as f:
                kb_data = json.load(f)

            response = llm.invoke([
                {"role": "system", "content": f"Responde usando SOLO esta base de conocimientos:\n{json.dumps(kb_data, ensure_ascii=False)}"},
                {"role": "user", "content": pregunta},
            ])
            reply = enforce_language(pregunta, response.content, lang)
            return reply.encode("utf-8", errors="replace").decode("utf-8")

        # Fallback al prompt original
        response = llm.invoke([
            {"role": "system", "content": info_prompt},
            {"role": "user", "content": pregunta},
        ])
        return enforce_language(pregunta, response.content, lang)

    except Exception as e:
        return f"⚠️ Error en InfoAgent: {e}"


if __name__ == "__main__":
    print("✅ InfoAgent arrancado con tool: consulta_info")
    mcp.run(transport="stdio", show_banner=False)
