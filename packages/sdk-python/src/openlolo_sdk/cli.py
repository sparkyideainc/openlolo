"""``openlolo-sdk``: approve once, then call any box tool from a shell or another language.

    openlolo-sdk login openlolo-ab12
    openlolo-sdk status openlolo-ab12
    openlolo-sdk screenshot openlolo-ab12 screen.jpg
    openlolo-sdk call openlolo-ab12 openlolo_button button=home observe=false

``call`` prints the tool's structured result as JSON; ``key=value`` values are parsed as JSON
when they can be (numbers, true/false, objects) and passed as strings otherwise.
"""

import argparse
import json
import sys
from uuid import uuid4

from openlolo_sdk.errors import OpenLoloError
from openlolo_sdk.phone import Phone


def _value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openlolo-sdk", description=__doc__.splitlines()[0])
    parser.add_argument("--name", help="client name shown on the box page")
    parser.add_argument("--fingerprint", help="expected SHA-256 of the box's LAN certificate")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("login", "status"):
        commands.add_parser(command).add_argument("box")
    shot = commands.add_parser("screenshot")
    shot.add_argument("box")
    shot.add_argument("path")
    call = commands.add_parser("call")
    call.add_argument("box")
    call.add_argument("tool")
    call.add_argument("arguments", nargs="*", metavar="key=value")
    args = parser.parse_args(argv)

    phone = Phone(
        args.box, name=args.name, fingerprint=args.fingerprint, allow_approval=args.command == "login" or None
    )
    try:
        if args.command == "login":
            phone.connect()
            print(f"Approved for {phone.url}.", file=sys.stderr)
        elif args.command == "status":
            print(json.dumps(phone.status(), indent=2))
        elif args.command == "screenshot":
            frame = phone.screenshot(format="png" if args.path.lower().endswith(".png") else "jpeg")
            frame.save(args.path)
            print(
                json.dumps(
                    {"path": args.path, "frame_id": frame.id, "width": frame.width, "height": frame.height}
                )
            )
        else:
            arguments = dict(pair.split("=", 1) for pair in args.arguments if "=" in pair)
            values = {key: _value(value) for key, value in arguments.items()}
            schema = phone.mcp.tools().get(args.tool, {})
            if "operation_id" in schema.get("properties", {}):
                values.setdefault("operation_id", str(uuid4()))
            result = phone.mcp.call(args.tool, values)
            print(json.dumps(result.data, indent=2))
            return 1 if result.is_error else 0
    except OpenLoloError as exc:
        print(f"openlolo-sdk: {exc}", file=sys.stderr)
        return 2
    finally:
        phone.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
