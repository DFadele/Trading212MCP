import importlib.util
import tarfile
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def deployment_checks():
    spec = importlib.util.spec_from_file_location(
        "deployment_checks",
        Path(__file__).parents[1] / "scripts" / "check_deployment.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "name",
    [
        "package/.env",
        "package/.env.http",
        "package/.cache/data.json",
        "package/key.pem",
        "package/cache.sqlite3",
    ],
)
def test_build_artifact_guard_rejects_private_files(tmp_path, deployment_checks, name):
    with zipfile.ZipFile(tmp_path / "package.whl", "w") as archive:
        archive.writestr(name, "synthetic test fixture")
    with tarfile.open(tmp_path / "package.tar.gz", "w:gz"):
        pass
    with pytest.raises(AssertionError):
        deployment_checks.check_archives(tmp_path)


def test_build_artifact_guard_accepts_empty_secret_examples(
    tmp_path, deployment_checks
):
    with zipfile.ZipFile(tmp_path / "package.whl", "w") as archive:
        archive.writestr("package/.env.http.example", "TRADING212_API_KEY=\n")
    with tarfile.open(tmp_path / "package.tar.gz", "w:gz"):
        pass
    deployment_checks.check_archives(tmp_path)
