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
from core.utils.utils_prompt import load_prompt

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
    
    # Args:.
    # Se usa dentro de `ThinkTool` en el flujo de tool de razonamiento auxiliar del agente.
    # Recibe `model_name` como entrada principal según la firma.
    # No devuelve valor; deja la instancia preparada con sus dependencias y estado inicial. Puede realizar llamadas externas o a modelos.
    def __init__(self, model_name: str = "gpt-4.1-mini"):
        """
        Args:
            model_name: Modelo de OpenAI a usar para la reflexión
        """
        self.llm = ChatOpenAI(model=model_name, temperature=0.3)
        
        # Cargar prompt específico para Think
        try:
            self.system_prompt = load_prompt("think.prompt.txt")
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
    
    # Realiza reflexión profunda sobre una pregunta compleja.
    # Se usa dentro de `ThinkTool` en el flujo de tool de razonamiento auxiliar del agente.
    # Recibe `pregunta` como entrada principal según la firma.
    # Devuelve un `str` con el resultado de esta operación. Puede realizar llamadas externas o a modelos.
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
    
    # Convierte esta clase en una herramienta compatible con LangChain.
    # Se usa dentro de `ThinkTool` en el flujo de tool de razonamiento auxiliar del agente.
    # No recibe parámetros externos; trabaja con estado capturado por el cierre o atributos de instancia.
    # Devuelve una tool configurada para que el agente la pueda invocar directamente. Puede activar tools o agentes.
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


# Factory function para crear la herramienta Think.
# Se usa en el flujo de tool de razonamiento auxiliar del agente para preparar datos, validaciones o decisiones previas.
# Recibe `model_name` como entrada principal según la firma.
# Devuelve una tool configurada para que el agente la pueda invocar directamente. Puede activar tools o agentes.
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
