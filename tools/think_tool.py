from langchain.tools import tool
from langchain_openai import ChatOpenAI
from core.message_composition.utils_prompt import load_prompt

think_prompt = load_prompt("think_prompt.txt")
llm_think = ChatOpenAI(model="gpt-4o-mini", temperature=0)

@tool(
    name="think_tool",
    description="Analiza la intención del usuario para decidir si se requiere datos reales o respuesta directa."
)
async def think_tool(input: str) -> str:
    """Reflexión previa sobre el mensaje del usuario."""
    result = await llm_think.ainvoke([
        {"role": "system", "content": think_prompt},
        {"role": "user", "content": input}
    ])
    return result.content.strip()
