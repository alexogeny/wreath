from pathlib import Path


def load_versioned(name: str) -> tuple[bytes, ...]:
    directory = Path(__file__).with_name("corpus") / "v1" / name
    return tuple(bytes.fromhex(path.read_text()) for path in sorted(directory.glob("*.hex")))
