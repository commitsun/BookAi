from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RoomType:
    id: int
    name: str
    default_code: str | None = None
    # Room type class
    class_id: int | None = None
    class_name: str | None = None
    overnight: bool = True
    # pms_notifications
    guest_display_name: str | None = None
    guest_short_description: str | None = None
    guest_long_description: str | None = None
    bed_configuration_text: str | None = None
    view_description: str | None = None
    amenities_summary: str | None = None


@dataclass
class Room:
    id: int
    name: str
    room_type_id: int | None = None
    room_type_name: str | None = None
    capacity: int = 0
    extra_beds_allowed: int = 0
    ubication_name: str | None = None
    description_sale: str | None = None
    # pms_notifications
    guest_visible_name: str | None = None
    location_hint: str | None = None
    building_label: str | None = None
