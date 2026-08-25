"""Entry point for `python -m digi_mouse_search`."""

from __future__ import annotations

from .server import run


def main() -> None:
    run()


if __name__ == "__main__":
    main()
