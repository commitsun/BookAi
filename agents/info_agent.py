from fastmcp import FastMCP
from utils.logging_config import silence_logs
from pathlib import Path

silence_logs()

def load_prompt(filename: str) -> str:
    return (Path("prompts") / filename).read_text(encoding="utf-8")

info_prompt = load_prompt("info_prompt.txt")
mcp = FastMCP("InfoAgent")

def detect_language(text: str) -> str:
    if not text:
        return "es"
    english_words = ["hello", "hi", "thanks", "please", "ok", "pool", "pet"]
    if any(word in text.lower() for word in english_words):
        return "en"
    return "es"

@mcp.tool()
def consulta_info(pregunta: str = "", prompt: str = "", mensaje: str = "") -> str:
    user_input = pregunta or mensaje or prompt
    lang = detect_language(user_input)

    # --- Saludos ---
    if lang == "es" and any(g in user_input.lower() for g in ["hola", "buenas", "qué tal", "buenos días", "buenas tardes"]):
        return "¡Hola! 👋 Bienvenido al hotel, ¿en qué puedo ayudarte?"
    if lang == "en" and any(g in user_input.lower() for g in ["hi", "hello", "good morning", "good afternoon"]):
        return "Hello! 👋 Welcome to the hotel, how can I help you?"

    # --- Info ---
    if "mascota" in user_input.lower() or "pet" in user_input.lower():
        return "No se permiten mascotas en el hotel 🐶." if lang == "es" else "Pets are not allowed in the hotel 🐶."
    if "piscina" in user_input.lower() or "pool" in user_input.lower():
        return "Sí, contamos con piscina climatizada 🏊." if lang == "es" else "Yes, we have a heated swimming pool 🏊."

    return "No dispongo de ese dato, lo consultaré con el encargado." if lang == "es" else "I don't have that information right now, I'll check with the manager."

if __name__ == "__main__":
    print(f"🔹 [INFO AGENT] Prompt cargado:\n{info_prompt[:200]}...\n")
    mcp.run(transport="stdio", show_banner=False)
