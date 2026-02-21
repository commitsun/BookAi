from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.template_registry import TemplateDefinition, TemplateRegistry


def test_registry_resolves_base_code_when_rows_use_language_suffix():
    registry = TemplateRegistry(
        templates=[
            TemplateDefinition.from_dict(
                {
                    "code": "booking_modification_aldahotels_v1_es",
                    "language": "es",
                    "whatsapp_name": "booking_modification_aldahotels_v1",
                    "active": True,
                }
            ),
            TemplateDefinition.from_dict(
                {
                    "code": "booking_modification_aldahotels_v1_en",
                    "language": "en",
                    "whatsapp_name": "booking_modification_aldahotels_v1",
                    "active": True,
                }
            ),
        ]
    )

    tpl_es = registry.resolve(
        instance_id=None,
        template_code="booking_modification_aldahotels_v1",
        language="es",
    )
    tpl_en = registry.resolve(
        instance_id=None,
        template_code="booking_modification_aldahotels_v1",
        language="en",
    )

    assert tpl_es is not None
    assert tpl_en is not None
    assert tpl_es.language == "es"
    assert tpl_en.language == "en"
    assert tpl_es.whatsapp_name == "booking_modification_aldahotels_v1"
    assert tpl_en.whatsapp_name == "booking_modification_aldahotels_v1"
