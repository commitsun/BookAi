# Import all models so Alembic's env.py detects them via Base.metadata
from app.models.channel import ChannelEndpoint  # noqa: F401
from app.models.contact import Contact  # noqa: F401
from app.models.conversation import Conversation, ConversationChannelState  # noqa: F401
from app.models.folio import Folio, SessionFolio  # noqa: F401
from app.models.instance import Instance, Property  # noqa: F401
from app.models.message import Message  # noqa: F401
from app.models.message_translation import MessageTranslation  # noqa: F401
from app.models.session import AttentionSession  # noqa: F401
from app.models.template import TemplateTranslationProperty  # noqa: F401
from app.models.template import WhatsAppTemplate  # noqa: F401
from app.models.template import WhatsAppTemplateTranslation  # noqa: F401
