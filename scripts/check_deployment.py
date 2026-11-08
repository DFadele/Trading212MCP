"""Offline checks for release inputs; never read the operator's .env files."""

import argparse
import tarfile
import zipfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from trading212_mcp.runtime_config import RuntimeSettings


def check_archives(directory: Path) -> None:
    wheels, sdists = list(directory.glob("*.whl")), list(directory.glob("*.tar.gz"))
    assert wheels and sdists, "Expected wheel and source distribution"
    archives = [*wheels, *sdists]
    for path in archives:
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as wheel:
                names = wheel.namelist()
        else:
            with tarfile.open(path) as sdist:
                names = sdist.getnames()
        for name in names:
            parts = Path(name).parts
            assert not set(parts) & {".cache", ".venv", ".git", "__pycache__"}, (
                "Private directories in build artifact"
            )
            filename = Path(name).name
            assert not filename.startswith(".env") or filename in {
                ".env.example",
                ".env.http.example",
            }, "Private environment file in build artifact"
            assert Path(name).suffix not in {".pem", ".key", ".sqlite3", ".pyc"}, (
                "Private/runtime file in build artifact"
            )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    docker = (root / "Dockerfile").read_text()
    ignore = (root / ".dockerignore").read_text().splitlines()
    assert "USER 10001:10001" in docker
    assert "uv sync --locked --no-dev" in docker
    assert 'ENTRYPOINT ["trading212-mcp"]' in docker
    assert "MCP_AUTH_MODE=oauth" in docker
    assert "COPY . " not in docker and "COPY .env" not in docker
    assert "**" in ignore and ".env" in ignore and ".env.*" in ignore
    assert not any(line.startswith("!.env") for line in ignore)
    for example in (".env.example", ".env.http.example"):
        for line in (root / example).read_text().splitlines():
            if line.startswith(
                (
                    "TRADING212_API_KEY=",
                    "TRADING212_API_SECRET=",
                    "MARKET_FMP_API_KEY=",
                    "MARKET_OPENFIGI_API_KEY=",
                )
            ):
                assert not line.partition("=")[2].strip(), (
                    "Example credentials must be empty"
                )
    unsafe_overrides: list[dict[str, Any]] = [
        {},
        {"host": "0.0.0.0", "auth_mode": "local"},
    ]
    for overrides in unsafe_overrides:
        try:
            configuration: dict[str, Any] = {
                "transport": "streamable-http",
                "auth_mode": "oauth",
                "public_url": None,
                "oauth_issuer": None,
                "oauth_jwks_url": None,
                "oauth_subject": None,
                **overrides,
            }
            RuntimeSettings(**configuration)
        except ValidationError:
            pass
        else:
            raise AssertionError("Insecure or incomplete HTTP configuration accepted")
    workflow = (root / ".github/workflows/ci.yml").read_text()
    assert "pull_request_target" not in workflow and "contents: read" in workflow
    print("Deployment safety checks passed (no operator secrets read).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path)
    arguments = parser.parse_args()
    main()
    if arguments.dist:
        check_archives(arguments.dist)
        print("Build artifact exclusion checks passed.")
