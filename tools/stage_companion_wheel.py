"""Stage a self-contained companion-wheel build tree for cibuildwheel."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("component", choices=("linux", "http3"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    output = args.output.resolve()
    stage_root = (ROOT / "packages/.wheel-staging").resolve()
    if output == stage_root or stage_root not in output.parents:
        raise SystemExit(
            "stage output must be a dedicated directory under packages/.wheel-staging/"
        )
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)

    package = ROOT / "packages" / f"wreath-{args.component}"
    for name in ("pyproject.toml", "setup.py"):
        shutil.copy2(package / name, output / name)
    shutil.copy2(ROOT / "LICENSE", output / "LICENSE")
    shutil.copy2(ROOT / "tools/wheel_smoke.py", output / "wheel_smoke.py")
    shutil.copy2(
        package / "THIRD_PARTY_NOTICES.md",
        output / "THIRD_PARTY_NOTICES.md",
    )
    if args.component == "linux":
        shutil.copy2(
            ROOT / "tools/build_openssl_wheel_dep.sh",
            output / "build_openssl_wheel_dep.sh",
        )
    if args.component == "http3":
        shutil.copy2(
            ROOT / "tools/build_openssl_wheel_dep.sh",
            output / "build_openssl_wheel_dep.sh",
        )
        shutil.copy2(
            ROOT / "tools/build_http3_wheel_deps.sh",
            output / "build_http3_wheel_deps.sh",
        )

    shutil.copytree(ROOT / "src/wreath/_native", output / "src/wreath/_native")

    dependencies = ROOT / "wheel-deps"
    if dependencies.is_dir():
        shutil.copytree(dependencies, output / "wheel-deps")


if __name__ == "__main__":
    main()
