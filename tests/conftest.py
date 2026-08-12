import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import make_samples  # noqa: E402  (tests/make_samples.py)

SAMPLES = Path(__file__).parent / "samples"


@pytest.fixture(scope="session", autouse=True)
def samples() -> Path:
    """(Re)generate the sample PDFs the tests parse."""
    make_samples.main()
    return SAMPLES


@pytest.fixture
def nl(samples) -> Path:
    return samples / "invoice_nl.pdf"


@pytest.fixture
def en(samples) -> Path:
    return samples / "invoice_en.pdf"


@pytest.fixture
def ruled(samples) -> Path:
    return samples / "invoice_table.pdf"
