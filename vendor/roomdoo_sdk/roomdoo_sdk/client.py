from __future__ import annotations

from .repositories.agents import AgentRepository
from .repositories.availability import AvailabilityRepository
from .repositories.folios import FolioRepository
from .repositories.guests import GuestRepository
from .repositories.invoices import InvoiceRepository
from .repositories.knowledge import KBRepository
from .repositories.llm_accounts import LLMAccountRepository
from .repositories.mcp import McpRepository
from .repositories.payments import PaymentRepository
from .repositories.properties import PropertyRepository
from .repositories.reporting import ReportingRepository
from .repositories.reservations import ReservationRepository
from .repositories.revenue import RevenueRepository
from .repositories.templates import TemplateRepository
from .repositories.tools import ToolRepository
from .repositories.usage import UsageRepository
from .repositories.users import UsersRepository
from .transports.base import Transport


class RoomdooClient:
    """Facade that exposes typed repositories over a transport."""

    def __init__(self, transport: Transport):
        self._transport = transport
        self._agents: AgentRepository | None = None
        self._availability: AvailabilityRepository | None = None
        self._folios: FolioRepository | None = None
        self._guests: GuestRepository | None = None
        self._invoices: InvoiceRepository | None = None
        self._kb: KBRepository | None = None
        self._llm_accounts: LLMAccountRepository | None = None
        self._mcp: McpRepository | None = None
        self._payments: PaymentRepository | None = None
        self._properties: PropertyRepository | None = None
        self._reporting: ReportingRepository | None = None
        self._reservations: ReservationRepository | None = None
        self._revenue: RevenueRepository | None = None
        self._templates: TemplateRepository | None = None
        self._tools: ToolRepository | None = None
        self._usage: UsageRepository | None = None
        self._users: UsersRepository | None = None

    @property
    def agents(self) -> AgentRepository:
        if self._agents is None:
            self._agents = AgentRepository(self._transport)
        return self._agents

    @property
    def availability(self) -> AvailabilityRepository:
        if self._availability is None:
            self._availability = AvailabilityRepository(
                self._transport
            )
        return self._availability

    @property
    def folios(self) -> FolioRepository:
        if self._folios is None:
            self._folios = FolioRepository(self._transport)
        return self._folios

    @property
    def guests(self) -> GuestRepository:
        if self._guests is None:
            self._guests = GuestRepository(self._transport)
        return self._guests

    @property
    def invoices(self) -> InvoiceRepository:
        if self._invoices is None:
            self._invoices = InvoiceRepository(self._transport)
        return self._invoices

    @property
    def kb(self) -> KBRepository:
        if self._kb is None:
            self._kb = KBRepository(self._transport)
        return self._kb

    @property
    def llm_accounts(self) -> LLMAccountRepository:
        if self._llm_accounts is None:
            self._llm_accounts = LLMAccountRepository(self._transport)
        return self._llm_accounts

    @property
    def mcp(self) -> McpRepository:
        if self._mcp is None:
            self._mcp = McpRepository(self._transport)
        return self._mcp

    @property
    def payments(self) -> PaymentRepository:
        if self._payments is None:
            self._payments = PaymentRepository(self._transport)
        return self._payments

    @property
    def properties(self) -> PropertyRepository:
        if self._properties is None:
            self._properties = PropertyRepository(self._transport)
        return self._properties

    @property
    def reporting(self) -> ReportingRepository:
        if self._reporting is None:
            self._reporting = ReportingRepository(
                self._transport
            )
        return self._reporting

    @property
    def reservations(self) -> ReservationRepository:
        if self._reservations is None:
            self._reservations = ReservationRepository(
                self._transport
            )
        return self._reservations

    @property
    def revenue(self) -> RevenueRepository:
        if self._revenue is None:
            self._revenue = RevenueRepository(self._transport)
        return self._revenue

    @property
    def templates(self) -> TemplateRepository:
        if self._templates is None:
            self._templates = TemplateRepository(
                self._transport
            )
        return self._templates

    @property
    def tools(self) -> ToolRepository:
        if self._tools is None:
            self._tools = ToolRepository(self._transport, self)
        return self._tools

    @property
    def usage(self) -> UsageRepository:
        if self._usage is None:
            self._usage = UsageRepository(self._transport)
        return self._usage

    @property
    def users(self) -> UsersRepository:
        if self._users is None:
            self._users = UsersRepository(self._transport)
        return self._users

    async def close(self) -> None:
        if hasattr(self._transport, "close"):
            await self._transport.close()
