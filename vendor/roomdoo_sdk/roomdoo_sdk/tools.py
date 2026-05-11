"""Declarative catalog of SDK tools.

This is the source of truth for all tools the SDK exposes.
BooKAI serves this via GET /sdk/tools and Odoo syncs nightly.
When adding a new SDK method that should be available as an
agent tool, declare it here.
"""

SDK_TOOLS = [
    # =================================================================
    # Properties
    # =================================================================
    {
        "name": "properties.list",
        "description": "List all active properties",
        "sdk_method": "properties.list",
        "input_schema": {},
        "requires_confirm": False,
    },
    {
        "name": "properties.get",
        "description": "Get property details: name, address, "
        "contact, timezone, arrival/departure info, "
        "parking, digital checkin help",
        "sdk_method": "properties.get",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
            },
            "required": ["property_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "properties.get_room_types",
        "description": "Room types for a property with "
        "guest descriptions, bed config, view, amenities",
        "sdk_method": "properties.get_room_types",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
            },
            "required": ["property_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "properties.get_rooms",
        "description": "All rooms for a property with "
        "capacity, location, guest-facing names",
        "sdk_method": "properties.get_rooms",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
            },
            "required": ["property_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "properties.get_pricelists",
        "description": "Pricelists available via BooKAI "
        "channel with rate descriptions and "
        "cancellation policies",
        "sdk_method": "properties.get_pricelists",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
            },
            "required": ["property_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "properties.get_amenities",
        "description": "Amenities available at a property",
        "sdk_method": "properties.get_amenities",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
            },
            "required": ["property_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "properties.get_board_services",
        "description": "Board services (breakfast, "
        "half-board, etc.) with prices",
        "sdk_method": "properties.get_board_services",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
            },
            "required": ["property_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "properties.get_cancelation_policy",
        "description": "Get full cancelation policy details: "
        "free cancellation days, penalties, no-show policy, "
        "refund terms, guest-facing descriptions",
        "sdk_method": "properties.get_cancelation_policy",
        "input_schema": {
            "type": "object",
            "properties": {
                "policy_id": {
                    "type": "integer",
                    "description": "Cancelation rule ID",
                },
            },
            "required": ["policy_id"],
        },
        "requires_confirm": False,
    },
    # =================================================================
    # Availability
    # =================================================================
    {
        "name": "availability.check",
        "description": "Check room availability for dates. "
        "Returns available rooms per type with "
        "restriction info",
        "sdk_method": "availability.check",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "checkin": {
                    "type": "string",
                    "description": "Check-in date in ISO format "
                    "YYYY-MM-DD (e.g. 2026-04-25)",
                },
                "checkout": {
                    "type": "string",
                    "description": "Check-out date in ISO format "
                    "YYYY-MM-DD (e.g. 2026-04-27)",
                },
                "pricelist_id": {
                    "type": "integer",
                    "description": "Pricelist/rate ID",
                },
                "room_type_id": {
                    "type": "integer",
                    "description": "Optional room type filter",
                },
            },
            "required": [
                "property_id",
                "checkin",
                "checkout",
                "pricelist_id",
            ],
        },
        "requires_confirm": False,
    },
    {
        "name": "availability.get_prices",
        "description": "Get nightly prices for a room type "
        "and pricelist",
        "sdk_method": "availability.get_prices",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "checkin": {
                    "type": "string",
                    "description": "Check-in date YYYY-MM-DD",
                },
                "checkout": {
                    "type": "string",
                    "description": "Check-out date YYYY-MM-DD",
                },
                "room_type_id": {
                    "type": "integer",
                    "description": "Room type ID",
                },
                "pricelist_id": {
                    "type": "integer",
                    "description": "Pricelist/rate ID",
                },
            },
            "required": [
                "property_id",
                "checkin",
                "checkout",
                "room_type_id",
                "pricelist_id",
            ],
        },
        "requires_confirm": False,
    },
    {
        "name": "availability.get_all_prices",
        "description": "Get prices for ALL available rates "
        "for a room type. Returns one price breakdown per "
        "pricelist with cancellation policy info. Use this "
        "to compare rates.",
        "sdk_method": "availability.get_all_prices",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "checkin": {
                    "type": "string",
                    "description": "Check-in date YYYY-MM-DD",
                },
                "checkout": {
                    "type": "string",
                    "description": "Check-out date YYYY-MM-DD",
                },
                "room_type_id": {
                    "type": "integer",
                    "description": "Room type ID",
                },
            },
            "required": [
                "property_id",
                "checkin",
                "checkout",
                "room_type_id",
            ],
        },
        "requires_confirm": False,
    },
    # =================================================================
    # My Bookings (phone-validated, for external guests)
    # =================================================================
    {
        "name": "folios.my_folios",
        "description": "List my bookings by phone number",
        "sdk_method": "folios.my_folios",
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "Guest phone in E.164 "
                    "format (e.g. +34600000000)",
                },
            },
            "required": ["phone"],
        },
        "requires_confirm": False,
    },
    {
        "name": "folios.my_folio",
        "description": "Get my booking details (validated by phone)",
        "sdk_method": "folios.my_folio",
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "Guest phone E.164",
                },
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
            },
            "required": ["phone", "folio_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "folios.my_reservations",
        "description": "Get my reservations (validated by phone)",
        "sdk_method": "folios.my_reservations",
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "Guest phone E.164",
                },
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
            },
            "required": ["phone", "folio_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "folios.my_reservation_lines",
        "description": "Get my nightly price breakdown "
        "(validated by phone)",
        "sdk_method": "folios.my_reservation_lines",
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "Guest phone E.164",
                },
                "reservation_id": {
                    "type": "integer",
                    "description": "Reservation ID",
                },
            },
            "required": ["phone", "reservation_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "folios.my_services",
        "description": "Get my booking services "
        "(validated by phone)",
        "sdk_method": "folios.my_services",
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "Guest phone E.164",
                },
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
            },
            "required": ["phone", "folio_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "folios.my_payments",
        "description": "Get my booking payments "
        "(validated by phone)",
        "sdk_method": "folios.my_payments",
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "Guest phone E.164",
                },
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
            },
            "required": ["phone", "folio_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "folios.my_checkin_partners",
        "description": "Get my check-in data "
        "(validated by phone)",
        "sdk_method": "folios.my_checkin_partners",
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "Guest phone E.164",
                },
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
            },
            "required": ["phone", "folio_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "folios.my_update_arrival",
        "description": "Update my arrival time "
        "(validated by phone)",
        "sdk_method": "folios.my_update_arrival",
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "Guest phone E.164",
                },
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
                "arrival_hour": {
                    "type": "string",
                    "description": "Arrival time in HH:MM "
                    "format (e.g. 14:00)",
                },
            },
            "required": ["phone", "folio_id", "arrival_hour"],
        },
        "requires_confirm": True,
    },
    # =================================================================
    # Folios (internal — unrestricted access)
    # =================================================================
    {
        "name": "folios.get_folio",
        "description": "Folio summary: partner, dates, "
        "amounts, payment state",
        "sdk_method": "folios.get_folio",
        "input_schema": {
            "type": "object",
            "properties": {
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
            },
            "required": ["folio_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "folios.get_reservations",
        "description": "Reservations for a folio: rooms, "
        "guests, dates, prices",
        "sdk_method": "folios.get_reservations",
        "input_schema": {
            "type": "object",
            "properties": {
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
            },
            "required": ["folio_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "folios.get_reservation_lines",
        "description": "Daily price breakdown for a "
        "reservation",
        "sdk_method": "folios.get_reservation_lines",
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {
                    "type": "integer",
                    "description": "Reservation ID",
                },
            },
            "required": ["reservation_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "folios.get_checkin_partners",
        "description": "Guest check-in data for a folio",
        "sdk_method": "folios.get_checkin_partners",
        "input_schema": {
            "type": "object",
            "properties": {
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
            },
            "required": ["folio_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "folios.get_payments",
        "description": "Payments linked to a folio",
        "sdk_method": "folios.get_payments",
        "input_schema": {
            "type": "object",
            "properties": {
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
            },
            "required": ["folio_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "folios.get_services",
        "description": "Services linked to a folio",
        "sdk_method": "folios.get_services",
        "input_schema": {
            "type": "object",
            "properties": {
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
            },
            "required": ["folio_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "folios.search_by_guest",
        "description": "Search folios by guest email, "
        "phone or name",
        "sdk_method": "folios.search_by_guest",
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "Guest email to search",
                },
                "phone": {
                    "type": "string",
                    "description": "Guest phone to search",
                },
                "name": {
                    "type": "string",
                    "description": "Guest name to search",
                },
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID filter",
                },
            },
        },
        "requires_confirm": False,
    },
    {
        "name": "folios.update_arrival_hour",
        "description": "Update arrival time for "
        "reservations in a folio (HH:MM)",
        "sdk_method": "folios.update_arrival_hour",
        "input_schema": {
            "type": "object",
            "properties": {
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
                "arrival_hour": {
                    "type": "string",
                    "description": "Arrival time in HH:MM "
                    "format (e.g. 14:00)",
                },
            },
            "required": ["folio_id", "arrival_hour"],
        },
        "requires_confirm": True,
    },
    # =================================================================
    # Reservations
    # =================================================================
    {
        "name": "reservations.create_booking",
        "description": "Create a complete booking: "
        "folio + one or more reservations + confirmation. "
        "Use the reservations array to book multiple rooms "
        "in a single folio. Returns folio code and "
        "reservation details.",
        "sdk_method": "reservations.create_booking",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "partner_name": {
                    "type": "string",
                    "description": "Guest full name",
                },
                "partner_phone": {
                    "type": "string",
                    "description": "Guest phone in E.164 "
                    "format (e.g. +34600000000)",
                },
                "partner_email": {
                    "type": "string",
                    "description": "Guest email address",
                },
                "pricelist_id": {
                    "type": "integer",
                    "description": "Pricelist/rate ID",
                },
                "sale_channel_id": {
                    "type": "integer",
                    "description": "Sale channel ID "
                    "(use BooKAI channel)",
                },
                "reservations": {
                    "type": "array",
                    "description": "Rooms to book. Use one "
                    "entry per room. For multiple rooms, "
                    "add multiple entries here instead of "
                    "calling create_booking multiple times.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "room_type_id": {
                                "type": "integer",
                                "description": "Room type ID",
                            },
                            "checkin": {
                                "type": "string",
                                "description": "Check-in "
                                "date YYYY-MM-DD",
                            },
                            "checkout": {
                                "type": "string",
                                "description": "Check-out "
                                "date YYYY-MM-DD",
                            },
                            "adults": {
                                "type": "integer",
                                "default": 2,
                                "description": "Number of "
                                "adult guests",
                            },
                            "children": {
                                "type": "integer",
                                "default": 0,
                                "description": "Number of "
                                "child guests",
                            },
                        },
                        "required": [
                            "room_type_id",
                            "checkin",
                            "checkout",
                        ],
                    },
                },
            },
            "required": [
                "property_id",
                "partner_name",
                "pricelist_id",
                "sale_channel_id",
                "reservations",
            ],
        },
        "requires_confirm": True,
    },
    # =================================================================
    # Agents
    # =================================================================
    {
        "name": "agents.get",
        "description": "Get agent configuration by "
        "technical name",
        "sdk_method": "agents.get",
        "input_schema": {
            "type": "object",
            "properties": {
                "technical_name": {
                    "type": "string",
                    "description": "Agent technical name "
                    "(e.g. booking-agent)",
                },
            },
            "required": ["technical_name"],
        },
        "requires_confirm": False,
    },
    {
        "name": "agents.list",
        "description": "List active agents",
        "sdk_method": "agents.list",
        "input_schema": {
            "type": "object",
            "properties": {
                "active": {
                    "type": "boolean",
                    "default": True,
                    "description": "Filter by active status",
                },
                "caller_type": {
                    "type": "string",
                    "description": "Filter by caller type "
                    "(internal, external_guest, system, any)",
                },
            },
        },
        "requires_confirm": False,
    },
    # =================================================================
    # Knowledge Base
    # =================================================================
    {
        "name": "kb.list_by_agent",
        "description": "KB documents linked to an agent",
        "sdk_method": "kb.list_by_agent",
        "input_schema": {
            "type": "object",
            "properties": {
                "technical_name": {
                    "type": "string",
                    "description": "Agent technical name",
                },
            },
            "required": ["technical_name"],
        },
        "requires_confirm": False,
    },
    # =================================================================
    # Usage
    # =================================================================
    {
        "name": "usage.log",
        "description": "Log usage metrics for a conversation",
        "sdk_method": "usage.log",
        "input_schema": {
            "type": "object",
            "properties": {
                "pms_property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "agent_id": {
                    "type": "integer",
                    "description": "Agent ID",
                },
                "llm_account_id": {
                    "type": "integer",
                    "description": "LLM account ID",
                },
                "tokens_in": {
                    "type": "integer",
                    "description": "Input tokens count",
                },
                "tokens_out": {
                    "type": "integer",
                    "description": "Output tokens count",
                },
                "model": {
                    "type": "string",
                    "description": "LLM model name "
                    "(e.g. gpt-4)",
                },
                "conversation_id": {
                    "type": "string",
                    "description": "Conversation identifier",
                },
                "status": {
                    "type": "string",
                    "description": "Execution status "
                    "(success, error, escalated)",
                },
            },
            "required": [
                "pms_property_id",
                "agent_id",
                "llm_account_id",
                "tokens_in",
                "tokens_out",
                "model",
                "conversation_id",
                "status",
            ],
        },
        "requires_confirm": False,
    },
    # =================================================================
    # Reservations — Staff operations
    # =================================================================
    {
        "name": "reservations.search",
        "description": "Search reservations by dates, "
        "state, guest name, channel",
        "sdk_method": "reservations.search",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "checkin_from": {
                    "type": "string",
                    "description": "Filter check-in from YYYY-MM-DD",
                },
                "checkin_to": {
                    "type": "string",
                    "description": "Filter check-in to YYYY-MM-DD",
                },
                "state": {
                    "type": "string",
                    "description": "State filter "
                    "(draft, confirm, onboard, done, cancel)",
                },
                "partner_name": {
                    "type": "string",
                    "description": "Guest name to search",
                },
            },
        },
        "requires_confirm": False,
    },
    {
        "name": "reservations.get",
        "description": "Get full reservation details",
        "sdk_method": "reservations.get",
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {
                    "type": "integer",
                    "description": "Reservation ID",
                },
            },
            "required": ["reservation_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "reservations.confirm",
        "description": "Confirm a draft reservation",
        "sdk_method": "reservations.confirm",
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {
                    "type": "integer",
                    "description": "Reservation ID",
                },
            },
            "required": ["reservation_id"],
        },
        "requires_confirm": True,
    },
    {
        "name": "reservations.cancel",
        "description": "Cancel reservation. Applies "
        "cancellation penalty automatically",
        "sdk_method": "reservations.cancel",
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {
                    "type": "integer",
                    "description": "Reservation ID",
                },
                "reason": {
                    "type": "string",
                    "description": "Cancellation reason",
                },
            },
            "required": ["reservation_id"],
        },
        "requires_confirm": True,
    },
    {
        "name": "reservations.assign_room",
        "description": "Assign a specific room to a "
        "reservation",
        "sdk_method": "reservations.assign_room",
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {
                    "type": "integer",
                    "description": "Reservation ID",
                },
                "room_id": {
                    "type": "integer",
                    "description": "Room ID to assign",
                },
            },
            "required": ["reservation_id", "room_id"],
        },
        "requires_confirm": True,
    },
    {
        "name": "reservations.checkin",
        "description": "Check in a guest (mark as on board)",
        "sdk_method": "reservations.checkin",
        "input_schema": {
            "type": "object",
            "properties": {
                "checkin_partner_id": {
                    "type": "integer",
                    "description": "Checkin partner ID",
                },
            },
            "required": ["checkin_partner_id"],
        },
        "requires_confirm": True,
    },
    {
        "name": "reservations.checkout",
        "description": "Check out a reservation",
        "sdk_method": "reservations.checkout",
        "input_schema": {
            "type": "object",
            "properties": {
                "reservation_id": {
                    "type": "integer",
                    "description": "Reservation ID",
                },
            },
            "required": ["reservation_id"],
        },
        "requires_confirm": True,
    },
    # =================================================================
    # Guests
    # =================================================================
    {
        "name": "guests.search",
        "description": "Search guests by name, email, "
        "phone or document number",
        "sdk_method": "guests.search",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Guest name to search",
                },
                "email": {
                    "type": "string",
                    "description": "Guest email to search",
                },
                "phone": {
                    "type": "string",
                    "description": "Guest phone to search",
                },
                "document_number": {
                    "type": "string",
                    "description": "ID document number",
                },
            },
        },
        "requires_confirm": False,
    },
    {
        "name": "guests.get",
        "description": "Get guest contact details",
        "sdk_method": "guests.get",
        "input_schema": {
            "type": "object",
            "properties": {
                "partner_id": {
                    "type": "integer",
                    "description": "Guest partner ID",
                },
            },
            "required": ["partner_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "guests.get_history",
        "description": "Get guest stay history "
        "(past bookings)",
        "sdk_method": "guests.get_history",
        "input_schema": {
            "type": "object",
            "properties": {
                "partner_id": {
                    "type": "integer",
                    "description": "Guest partner ID",
                },
            },
            "required": ["partner_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "guests.update_contact",
        "description": "Update guest email or phone",
        "sdk_method": "guests.update_contact",
        "input_schema": {
            "type": "object",
            "properties": {
                "partner_id": {
                    "type": "integer",
                    "description": "Guest partner ID",
                },
                "email": {
                    "type": "string",
                    "description": "New email address",
                },
                "phone": {
                    "type": "string",
                    "description": "New phone number",
                },
                "mobile": {
                    "type": "string",
                    "description": "New mobile number",
                },
            },
            "required": ["partner_id"],
        },
        "requires_confirm": True,
    },
    # =================================================================
    # Invoices
    # =================================================================
    {
        "name": "invoices.list_by_folio",
        "description": "Get invoices linked to a folio",
        "sdk_method": "invoices.list_by_folio",
        "input_schema": {
            "type": "object",
            "properties": {
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
            },
            "required": ["folio_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "invoices.get",
        "description": "Get invoice details",
        "sdk_method": "invoices.get",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {
                    "type": "integer",
                    "description": "Invoice ID",
                },
            },
            "required": ["invoice_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "invoices.create_from_folio",
        "description": "Generate invoice from a folio",
        "sdk_method": "invoices.create_from_folio",
        "input_schema": {
            "type": "object",
            "properties": {
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
            },
            "required": ["folio_id"],
        },
        "requires_confirm": True,
    },
    {
        "name": "invoices.validate",
        "description": "Post/validate a draft invoice",
        "sdk_method": "invoices.validate",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {
                    "type": "integer",
                    "description": "Invoice ID",
                },
            },
            "required": ["invoice_id"],
        },
        "requires_confirm": True,
    },
    {
        "name": "invoices.get_pdf_url",
        "description": "Get portal URL for invoice PDF",
        "sdk_method": "invoices.get_pdf_url",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {
                    "type": "integer",
                    "description": "Invoice ID",
                },
            },
            "required": ["invoice_id"],
        },
        "requires_confirm": False,
    },
    # =================================================================
    # Payments
    # =================================================================
    {
        "name": "payments.record",
        "description": "Record a payment for a folio",
        "sdk_method": "payments.record",
        "input_schema": {
            "type": "object",
            "properties": {
                "folio_id": {
                    "type": "integer",
                    "description": "Folio/booking ID",
                },
                "amount": {
                    "type": "number",
                    "description": "Payment amount",
                },
                "journal_id": {
                    "type": "integer",
                    "description": "Payment journal ID "
                    "(cash, bank, etc.)",
                },
            },
            "required": [
                "folio_id",
                "amount",
                "journal_id",
            ],
        },
        "requires_confirm": True,
    },
    {
        "name": "payments.list_by_property",
        "description": "List payments for a property "
        "in a date range",
        "sdk_method": "payments.list_by_property",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
            },
            "required": ["property_id"],
        },
        "requires_confirm": False,
    },
    {
        "name": "payments.get_pending_by_property",
        "description": "Get folios with pending balance "
        "for a property",
        "sdk_method": "payments.get_pending_by_property",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
            },
            "required": ["property_id"],
        },
        "requires_confirm": False,
    },
    # =================================================================
    # Reporting
    # =================================================================
    {
        "name": "reporting.occupancy",
        "description": "Occupancy data by room type "
        "for a date range",
        "sdk_method": "reporting.occupancy",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
            },
            "required": [
                "property_id",
                "date_from",
                "date_to",
            ],
        },
        "requires_confirm": False,
    },
    {
        "name": "reporting.revenue_summary",
        "description": "Revenue summary for a property "
        "and date range",
        "sdk_method": "reporting.revenue_summary",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
            },
            "required": [
                "property_id",
                "date_from",
                "date_to",
            ],
        },
        "requires_confirm": False,
    },
    {
        "name": "reporting.arrivals_departures",
        "description": "Arrivals and departures for a "
        "specific date",
        "sdk_method": "reporting.arrivals_departures",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "date": {
                    "type": "string",
                    "description": "Date YYYY-MM-DD",
                },
            },
            "required": ["property_id", "date"],
        },
        "requires_confirm": False,
    },
    {
        "name": "reporting.pending_checkins",
        "description": "Reservations pending check-in",
        "sdk_method": "reporting.pending_checkins",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
            },
            "required": ["property_id"],
        },
        "requires_confirm": False,
    },
    # =================================================================
    # Availability — Staff
    # =================================================================
    {
        "name": "availability.check_real",
        "description": "Check real room availability "
        "(no sale restrictions). For staff only.",
        "sdk_method": "availability.check_real",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "checkin": {
                    "type": "string",
                    "description": "Check-in date YYYY-MM-DD",
                },
                "checkout": {
                    "type": "string",
                    "description": "Check-out date YYYY-MM-DD",
                },
                "room_type_id": {
                    "type": "integer",
                    "description": "Optional room type filter",
                },
            },
            "required": [
                "property_id",
                "checkin",
                "checkout",
            ],
        },
        "requires_confirm": False,
    },
    # =================================================================
    # Revenue Management
    # =================================================================
    {
        "name": "revenue.get_availability_rules",
        "description": "Get availability restrictions "
        "(min/max stay, closed dates, quotas) for a "
        "date range and room type",
        "sdk_method": "revenue.get_availability_rules",
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
                "room_type_id": {
                    "type": "integer",
                    "description": "Optional room type filter",
                },
                "availability_plan_id": {
                    "type": "integer",
                    "description": "Optional plan filter",
                },
            },
            "required": [
                "property_id",
                "date_from",
                "date_to",
            ],
        },
        "requires_confirm": False,
    },
    {
        "name": "revenue.get_pricelist_items",
        "description": "Get configured prices for a "
        "pricelist in a date range",
        "sdk_method": "revenue.get_pricelist_items",
        "input_schema": {
            "type": "object",
            "properties": {
                "pricelist_id": {
                    "type": "integer",
                    "description": "Pricelist/rate ID",
                },
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
                "room_type_id": {
                    "type": "integer",
                    "description": "Optional room type filter",
                },
            },
            "required": [
                "pricelist_id",
                "date_from",
                "date_to",
            ],
        },
        "requires_confirm": False,
    },
    {
        "name": "revenue.set_prices",
        "description": "Set daily price for a room type "
        "in a pricelist. Can filter by days of week.",
        "sdk_method": "revenue.set_prices",
        "input_schema": {
            "type": "object",
            "properties": {
                "pricelist_id": {
                    "type": "integer",
                    "description": "Pricelist/rate ID",
                },
                "room_type_id": {
                    "type": "integer",
                    "description": "Room type ID",
                },
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
                "price": {
                    "type": "number",
                    "description": "Price per night",
                },
                "days_of_week": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: mon,tue,wed,"
                    "thu,fri,sat,sun. Null = all days.",
                },
            },
            "required": [
                "pricelist_id",
                "room_type_id",
                "date_from",
                "date_to",
                "price",
            ],
        },
        "requires_confirm": True,
    },
    {
        "name": "revenue.close_sales",
        "description": "Stop-sell: close all sales for "
        "dates and room type",
        "sdk_method": "revenue.close_sales",
        "input_schema": {
            "type": "object",
            "properties": {
                "availability_plan_id": {
                    "type": "integer",
                    "description": "Availability plan ID",
                },
                "room_type_id": {
                    "type": "integer",
                    "description": "Room type ID",
                },
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
                "days_of_week": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional day filter",
                },
            },
            "required": [
                "availability_plan_id",
                "room_type_id",
                "property_id",
                "date_from",
                "date_to",
            ],
        },
        "requires_confirm": True,
    },
    {
        "name": "revenue.open_sales",
        "description": "Reopen previously closed sales",
        "sdk_method": "revenue.open_sales",
        "input_schema": {
            "type": "object",
            "properties": {
                "availability_plan_id": {
                    "type": "integer",
                    "description": "Availability plan ID",
                },
                "room_type_id": {
                    "type": "integer",
                    "description": "Room type ID",
                },
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
                "days_of_week": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional day filter",
                },
            },
            "required": [
                "availability_plan_id",
                "room_type_id",
                "property_id",
                "date_from",
                "date_to",
            ],
        },
        "requires_confirm": True,
    },
    {
        "name": "revenue.close_arrivals",
        "description": "Close arrivals only for dates",
        "sdk_method": "revenue.close_arrivals",
        "input_schema": {
            "type": "object",
            "properties": {
                "availability_plan_id": {
                    "type": "integer",
                    "description": "Availability plan ID",
                },
                "room_type_id": {
                    "type": "integer",
                    "description": "Room type ID",
                },
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
                "days_of_week": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional day filter",
                },
            },
            "required": [
                "availability_plan_id",
                "room_type_id",
                "property_id",
                "date_from",
                "date_to",
            ],
        },
        "requires_confirm": True,
    },
    {
        "name": "revenue.close_departures",
        "description": "Close departures only for dates",
        "sdk_method": "revenue.close_departures",
        "input_schema": {
            "type": "object",
            "properties": {
                "availability_plan_id": {
                    "type": "integer",
                    "description": "Availability plan ID",
                },
                "room_type_id": {
                    "type": "integer",
                    "description": "Room type ID",
                },
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
                "days_of_week": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional day filter",
                },
            },
            "required": [
                "availability_plan_id",
                "room_type_id",
                "property_id",
                "date_from",
                "date_to",
            ],
        },
        "requires_confirm": True,
    },
    {
        "name": "revenue.set_min_stay",
        "description": "Set minimum stay requirement "
        "for dates and room type",
        "sdk_method": "revenue.set_min_stay",
        "input_schema": {
            "type": "object",
            "properties": {
                "availability_plan_id": {
                    "type": "integer",
                    "description": "Availability plan ID",
                },
                "room_type_id": {
                    "type": "integer",
                    "description": "Room type ID",
                },
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
                "min_stay": {
                    "type": "integer",
                    "description": "Minimum nights "
                    "(0 = no restriction)",
                },
                "days_of_week": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional day filter",
                },
            },
            "required": [
                "availability_plan_id",
                "room_type_id",
                "property_id",
                "date_from",
                "date_to",
                "min_stay",
            ],
        },
        "requires_confirm": True,
    },
    {
        "name": "revenue.set_max_stay",
        "description": "Set maximum stay limit "
        "for dates and room type",
        "sdk_method": "revenue.set_max_stay",
        "input_schema": {
            "type": "object",
            "properties": {
                "availability_plan_id": {
                    "type": "integer",
                    "description": "Availability plan ID",
                },
                "room_type_id": {
                    "type": "integer",
                    "description": "Room type ID",
                },
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
                "max_stay": {
                    "type": "integer",
                    "description": "Maximum nights "
                    "(0 = unlimited)",
                },
                "days_of_week": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional day filter",
                },
            },
            "required": [
                "availability_plan_id",
                "room_type_id",
                "property_id",
                "date_from",
                "date_to",
                "max_stay",
            ],
        },
        "requires_confirm": True,
    },
    {
        "name": "revenue.set_quota",
        "description": "Set sales quota for dates "
        "and room type (-1 = unlimited)",
        "sdk_method": "revenue.set_quota",
        "input_schema": {
            "type": "object",
            "properties": {
                "availability_plan_id": {
                    "type": "integer",
                    "description": "Availability plan ID",
                },
                "room_type_id": {
                    "type": "integer",
                    "description": "Room type ID",
                },
                "property_id": {
                    "type": "integer",
                    "description": "PMS property ID",
                },
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
                "quota": {
                    "type": "integer",
                    "description": "Sales quota "
                    "(-1 = unlimited)",
                },
                "days_of_week": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional day filter",
                },
            },
            "required": [
                "availability_plan_id",
                "room_type_id",
                "property_id",
                "date_from",
                "date_to",
                "quota",
            ],
        },
        "requires_confirm": True,
    },
    # =================================================================
    # Agents — Admin
    # =================================================================
    {
        "name": "agents.create",
        "description": "Create a new agent",
        "sdk_method": "agents.create",
        "input_schema": {
            "type": "object",
            "properties": {
                "technical_name": {
                    "type": "string",
                    "description": "Unique technical name",
                },
                "name": {
                    "type": "string",
                    "description": "Display name",
                },
                "description": {
                    "type": "string",
                    "description": "Agent description",
                },
                "system_prompt": {
                    "type": "string",
                    "description": "System prompt text",
                },
                "caller_type": {
                    "type": "string",
                    "description": "Caller type "
                    "(internal, external_guest, system, any)",
                },
            },
            "required": [
                "technical_name",
                "name",
                "description",
                "system_prompt",
            ],
        },
        "requires_confirm": True,
    },
    {
        "name": "agents.update",
        "description": "Update agent configuration",
        "sdk_method": "agents.update",
        "input_schema": {
            "type": "object",
            "properties": {
                "technical_name": {
                    "type": "string",
                    "description": "Agent technical name",
                },
            },
            "required": ["technical_name"],
        },
        "requires_confirm": True,
    },
    {
        "name": "agents.update_prompt",
        "description": "Update agent system prompt",
        "sdk_method": "agents.update_prompt",
        "input_schema": {
            "type": "object",
            "properties": {
                "technical_name": {
                    "type": "string",
                    "description": "Agent technical name",
                },
                "system_prompt": {
                    "type": "string",
                    "description": "New system prompt text",
                },
            },
            "required": [
                "technical_name",
                "system_prompt",
            ],
        },
        "requires_confirm": True,
    },
    # =================================================================
    # KB — Admin
    # =================================================================
    {
        "name": "kb.create",
        "description": "Create a KB document",
        "sdk_method": "kb.create_document",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Document name",
                },
                "source_type": {
                    "type": "string",
                    "description": "Type: markdown, pdf, url",
                },
                "content": {
                    "type": "string",
                    "description": "Content (for markdown)",
                },
                "doc_type": {
                    "type": "string",
                    "description": "Classification: "
                    "instruction, skill, faq, manual, context",
                },
            },
            "required": ["name", "source_type"],
        },
        "requires_confirm": True,
    },
    {
        "name": "kb.update",
        "description": "Update a KB document",
        "sdk_method": "kb.update_document",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "integer",
                    "description": "KB document ID",
                },
            },
            "required": ["doc_id"],
        },
        "requires_confirm": True,
    },
    # =================================================================
    # Usage — Analytics
    # =================================================================
    {
        "name": "usage.summary_by_agent",
        "description": "Usage aggregated by agent",
        "sdk_method": "usage.summary_by_agent",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
            },
        },
        "requires_confirm": False,
    },
    {
        "name": "usage.summary_by_property",
        "description": "Usage aggregated by property",
        "sdk_method": "usage.summary_by_property",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
            },
        },
        "requires_confirm": False,
    },
    {
        "name": "usage.summary_by_model",
        "description": "Usage aggregated by LLM model",
        "sdk_method": "usage.summary_by_model",
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {
                    "type": "string",
                    "description": "From date YYYY-MM-DD",
                },
                "date_to": {
                    "type": "string",
                    "description": "To date YYYY-MM-DD",
                },
            },
        },
        "requires_confirm": False,
    },
    # =================================================================
    # Templates
    # =================================================================
    {
        "name": "templates.update_status",
        "description": "Update WhatsApp template "
        "translation status from Meta",
        "sdk_method": "templates.update_translation_status",
        "input_schema": {
            "type": "object",
            "properties": {
                "template_code": {
                    "type": "string",
                    "description": "Template code in Odoo",
                },
                "language": {
                    "type": "string",
                    "description": "Language code (es, en...)",
                },
                "meta_status": {
                    "type": "string",
                    "description": "Status: draft, pending, "
                    "approved, rejected, error",
                },
                "meta_template_id": {
                    "type": "string",
                    "description": "Meta template ID",
                },
                "waba_id": {
                    "type": "string",
                    "description": "WABA ID",
                },
            },
            "required": [
                "template_code",
                "language",
                "meta_status",
            ],
        },
        "requires_confirm": False,
    },
]
