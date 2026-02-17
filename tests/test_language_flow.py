import asyncio
import importlib
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeLanguageManager:
    def __init__(self, detected_lang: str):
        self.detected_lang = detected_lang

    def detect_language(self, text: str, prev_lang: str | None = None) -> str:
        return self.detected_lang

    def ensure_language(self, text: str, lang_code: str) -> str:
        return f"[{lang_code}] {text}"


class _FakeSupervisor:
    async def validate(self, *args, **kwargs):
        return {"estado": "Aprobado", "motivo": ""}


class _FakeInterno:
    async def escalate(self, **kwargs):
        return None


class _FakeMemory:
    def __init__(self, history=None, flags=None):
        self._history = history or []
        self._flags = flags or {}
        self.saved = []

    def set_flag(self, chat_id, key, value):
        self._flags[(chat_id, key)] = value

    def get_flag(self, chat_id, key):
        return self._flags.get((chat_id, key))

    def save(self, conversation_id, role, content, channel=None):
        self.saved.append(
            {
                "conversation_id": conversation_id,
                "role": role,
                "content": content,
                "channel": channel,
            }
        )

    def get_memory(self, conversation_id, limit=20):
        return self._history[-limit:]

    def get_memory_as_messages(self, conversation_id):
        return []


class _FakeState:
    def __init__(self, memory):
        self.memory_manager = memory
        self.tracking = {}
        self.supervisor_input = _FakeSupervisor()
        self.supervisor_output = _FakeSupervisor()
        self.interno_agent = _FakeInterno()
        self.socket_manager = None


def _load_pipeline_with_stubs(detected_lang: str, agent_reply: str = "ok"):
    fake_lang_mod = types.ModuleType("core.language_manager")
    fake_lang_mod.language_manager = _FakeLanguageManager(detected_lang)

    class _DummyMainAgent:
        async def ainvoke(self, **kwargs):
            return agent_reply

    fake_main_agent_mod = types.ModuleType("core.main_agent")
    fake_main_agent_mod.create_main_agent = lambda **kwargs: _DummyMainAgent()

    fake_instance_mod = types.ModuleType("core.instance_context")
    fake_instance_mod.hydrate_dynamic_context = lambda **kwargs: None

    sys.modules["core.language_manager"] = fake_lang_mod
    sys.modules["core.main_agent"] = fake_main_agent_mod
    sys.modules["core.instance_context"] = fake_instance_mod
    sys.modules.pop("core.pipeline", None)

    return importlib.import_module("core.pipeline")


def test_pipeline_persists_guest_lang_before_main_agent_flow():
    pipeline = _load_pipeline_with_stubs("en", agent_reply="Main response")
    memory = _FakeMemory(history=[])
    state = _FakeState(memory)

    out = asyncio.run(
        pipeline.process_user_message(
            user_message="ok",
            chat_id="34600111222",
            state=state,
            channel="whatsapp",
        )
    )

    assert out == "Main response"
    assert memory.get_flag("34600111222", "guest_lang") == "en"


def test_pipeline_localizes_locator_quick_reply():
    pipeline = _load_pipeline_with_stubs("fr")
    memory = _FakeMemory(history=[], flags={("34600333444", "reservation_locator"): "ZX/987"})
    state = _FakeState(memory)

    out = asyncio.run(
        pipeline.process_user_message(
            user_message="cual es mi localizador",
            chat_id="34600333444",
            state=state,
            channel="whatsapp",
        )
    )

    assert out == "[fr] El localizador de tu reserva es ZX/987."
    assert memory.get_flag("34600333444", "guest_lang") == "fr"


def test_super_and_telegram_have_active_language_rewrite_hook():
    super_src = Path("api/superintendente_routes.py").read_text(encoding="utf-8")
    telegram_src = Path("channels_wrapper/telegram/webhook_telegram.py").read_text(encoding="utf-8")

    assert "def _ensure_guest_language(msg: str, guest_id: str) -> str:" in super_src
    assert "def _ensure_guest_language(msg: str, guest_id: str) -> str:" in telegram_src
    assert "language_manager.ensure_language(msg, lang)" in super_src
    assert "language_manager.ensure_language(msg, lang)" in telegram_src

    # Evita volver al no-op histórico.
    assert not re.search(
        r"def _ensure_guest_language\(msg: str, guest_id: str\) -> str:\s*return msg",
        super_src,
    )
    assert not re.search(
        r"def _ensure_guest_language\(msg: str, guest_id: str\) -> str:\s*return msg",
        telegram_src,
    )
