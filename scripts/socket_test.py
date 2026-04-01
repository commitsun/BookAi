"""
BookAI — Socket.IO interactive test client

Usage:
    python scripts/socket_test.py [--url URL] [--token TOKEN] [--property PROPERTY_ID]

Defaults (demo data):
    url        = http://localhost:8000
    token      = dev-token-demo-2026
    property   = 1

Commands (interactive prompt):
    join <phone_code>    — join chat room for a conversation
    leave <phone_code>   — leave chat room
    rooms                — list currently joined chat rooms
    quit / exit          — disconnect and exit
"""

import argparse
import json
import socketio

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser(description="BookAI Socket.IO test client")
parser.add_argument("--url",      default="http://localhost:8000")
parser.add_argument("--token",    default="dev-token-demo-2026")
parser.add_argument("--property", default=1, type=int, dest="property_id")
args = parser.parse_args()

# ---------------------------------------------------------------------------
# Client setup
# ---------------------------------------------------------------------------

sio = socketio.Client(logger=False, engineio_logger=False)
joined_rooms: set[str] = set()


def _print(prefix: str, data=None):
    if data:
        print(f"\n{prefix} {json.dumps(data, ensure_ascii=False, indent=2)}")
    else:
        print(f"\n{prefix}")
    print("> ", end="", flush=True)


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

@sio.event
def connect():
    _print(f"✓ Connected  (property={args.property_id})")


@sio.event
def connect_error(data):
    _print(f"✗ Connection rejected: {data}")


@sio.event
def disconnect():
    _print("✗ Disconnected")


@sio.on("conversation.created")
def on_conversation_created(data):
    _print("📥 conversation.created", data)


@sio.on("conversation.updated")
def on_conversation_updated(data):
    _print("🔄 conversation.updated", data)


@sio.on("message.created")
def on_message_created(data):
    direction = data.get("direction", "?")
    sender    = data.get("sender", "?")
    content   = data.get("content", "")
    icon = "⬅️ " if direction == "inbound" else "➡️ "
    _print(f"{icon} message.created  [{sender}]  {content!r}", data)


@sio.on("message.delivery_updated")
def on_delivery_updated(data):
    status = data.get("delivery_status", "?")
    _print(f"📬 message.delivery_updated  [{status}]", data)


# ---------------------------------------------------------------------------
# Interactive loop (runs in main thread after connect)
# ---------------------------------------------------------------------------

HELP = """
Commands:
  join <phone_code>   — subscribe to chat:<phone_code> room
  leave <phone_code>  — unsubscribe from chat:<phone_code> room
  rooms               — show active chat rooms
  quit                — disconnect and exit
"""


def interactive_loop():
    print(HELP)
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            continue

        parts = line.split(maxsplit=1)
        cmd   = parts[0].lower()
        arg   = parts[1] if len(parts) > 1 else ""

        if cmd in ("quit", "exit", "q"):
            break

        elif cmd == "join":
            if not arg:
                print("  Usage: join <phone_code>")
                continue
            sio.emit("join_chat", {"phone_code": arg})
            joined_rooms.add(arg)
            print(f"  → joined chat:{arg}")

        elif cmd == "leave":
            if not arg:
                print("  Usage: leave <phone_code>")
                continue
            sio.emit("leave_chat", {"phone_code": arg})
            joined_rooms.discard(arg)
            print(f"  → left chat:{arg}")

        elif cmd == "rooms":
            if joined_rooms:
                print(f"  Active chat rooms: {', '.join(sorted(joined_rooms))}")
            else:
                print("  No chat rooms joined yet.")

        elif cmd == "help":
            print(HELP)

        else:
            print(f"  Unknown command: {cmd!r}. Type 'help' for available commands.")

    sio.disconnect()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

print(f"Connecting to {args.url}  (token={args.token[:8]}...  property={args.property_id})")

sio.connect(
    args.url,
    auth={"token": args.token, "property_id": args.property_id},
)

interactive_loop()
