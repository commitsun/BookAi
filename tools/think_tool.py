 #think_tool_v2.py
"""
🧠 Think Tool - Herramienta de reflexión profunda para el agente
=================================================================
Permite al agente realizar razonamiento paso a paso cuando enfrenta
consultas complejas o necesita planificar acciones múltiples.
"""

import logging
from typing import Optional
from pydantic import BaseModel, Field
from langchain.tools import StructuredTool
from langchain_openai import ChatOpenAI

log = logging.getLogger("ThinkTool")


class ThinkInput(BaseModel):
    """Input schema para la herramienta Think."""
    pregunta: str = Field(
        description="La pregunta o situación compleja que requiere reflexión profunda"
    )


class ThinkTool:
    """
    Herramienta de razonamiento que permite al agente pensar paso a paso
    cuando enfrenta situaciones complejas o ambiguas.
    """
    
    def __init__(self, model_name: str = "gpt-4.1-mini"):
        """
        Args:
            model_name: Modelo de OpenAI a usar para la reflexión
        """
        self.llm = ChatOpenAI(model=model_name, temperature=0.3)
        
        # Cargar prompt específico para Think
        try:
            with open("prompts/think.prompt.txt", "r", encoding="utf-8") as f:
                self.system_prompt = f.read()
        except Exception as e:
            log.warning(f"⚠️ No se pudo cargar think.prompt.txt: {e}. Usando prompt por defecto.")
            self.system_prompt = """Eres un asistente de razonamiento para un sistema de IA de hotel.
Tu tarea es analizar situaciones complejas paso a paso y proporcionar un razonamiento claro.

Cuando recibas una consulta:
1. Identifica qué información tienes
2. Identifica qué información te falta
3. Determina qué acciones o herramientas serían necesarias
4. Proporciona una conclusión clara y accionable

Sé conciso, claro y estructurado."""
        
        log.info("✅ ThinkTool inicializado")
    
    def _think(self, pregunta: str) -> str:
        """
        Realiza reflexión profunda sobre una pregunta compleja.
        
        Args:
            pregunta: La consulta que requiere análisis
            
        Returns:
            Razonamiento estructurado paso a paso
        """
        try:
            log.info(f"🧠 Reflexionando sobre: {pregunta[:100]}...")
            
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"Analiza la siguiente situación paso a paso:\n\n{pregunta}"}
            ]
            
            response = self.llm.invoke(messages)
            reasoning = response.content.strip()
            
            log.info(f"✅ Reflexión completada: {len(reasoning)} caracteres")
            
            return reasoning
            
        except Exception as e:
            log.error(f"❌ Error durante reflexión: {e}")
            return f"❌ Error al procesar el razonamiento: {str(e)}"
    
    def as_tool(self) -> StructuredTool:
        """
        Convierte esta clase en una herramienta compatible con LangChain.
        
        Returns:
            StructuredTool configurado para usar con agentes
        """
        return StructuredTool(
            name="Think",
            description=(
                "Usa esta herramienta cuando necesites reflexionar profundamente sobre una consulta compleja "
                "o cuando te sientas atascado. Te ayuda a razonar paso a paso, identificar qué información "
                "tienes, qué te falta, y qué acciones debes tomar. Úsala SIEMPRE antes de decidir qué "
                "herramienta invocar cuando la consulta sea ambigua o requiera múltiples pasos."
            ),
            func=self._think,
            args_schema=ThinkInput,
        )


def create_think_tool(model_name: str = "gpt-4.1-mini") -> StructuredTool:
    """
    Factory function para crear la herramienta Think.
    
    Args:
        model_name: Modelo de OpenAI para la reflexión
        
    Returns:
        StructuredTool configurado
    """
    tool_instance = ThinkTool(model_name=model_name)
    return tool_instance.as_tool()