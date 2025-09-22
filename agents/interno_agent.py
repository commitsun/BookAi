from fastmcp import FastMCP
from utils.logging_config import silence_logs
from pathlib import Path

silence_logs()

def load_prompt(filename: str) -> str:
    return (Path("prompts") / filename).read_text(encoding="utf-8")

interno_prompt = load_prompt("interno_prompt.txt")
mcp = FastMCP("InternoAgent")

def detect_language(text: str) -> str:
    if not text:
        return "es"
    english_words = ["hello", "hi", "thanks", "please", "ok", "manager", "supervisor"]
    if any(word in text.lower() for word in english_words):
        return "en"
    return "es"

@mcp.tool()
def consulta_encargado(mensaje: str = "", prompt: str = "", fechas: str = "", personas: int = 0) -> str:
    user_input = mensaje or prompt
    lang = detect_language(user_input)

    # --- Saludos ---
    if lang == "es" and any(g in user_input.lower() for g in ["hola", "buenas", "qué tal", "buenos días", "buenas tardes"]):
        return "¡Hola! 👋 Ahora mismo informo al encargado."
    if lang == "en" and any(g in user_input.lower() for g in ["hi", "hello", "good morning", "good afternoon"]):
        return "Hello! 👋 I'll notify the manager right away."

    # --- Contacto con encargado ---
    texto = mensaje or prompt or f"Consulta de {personas} personas para {fechas}"
    return f"📢 He avisado al encargado del hotel: {texto}. Esperando respuesta..." if lang == "es" else f"📢 I have notified the hotel manager: {texto}. Waiting for a reply..."

if __name__ == "__main__":
    print(f"🔹 [INTERNO AGENT] Prompt cargado:\n{interno_prompt[:200]}...\n")
    mcp.run(transport="stdio", show_banner=False)
