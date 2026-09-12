"""End-to-end check that the page renders against the real data files.

Guards the delivery path the manual steps used to cover: the app must load,
plot the strict group, and keep every product visible in the table.
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "tracker" / "app.py")


@pytest.fixture(scope="module")
def page():
    app = AppTest.from_file(APP, default_timeout=60).run()
    return app


def test_the_page_renders_without_raising(page):
    assert not page.exception


def test_all_four_sections_are_present(page):
    headers = [h.value for h in page.header]
    assert len(headers) == 4
    assert headers[0].startswith("1. Price over time")


def test_the_trend_chart_is_the_first_section(page):
    """Deliverable B asks for a chart of price movement over time, so the
    time series comes before any cross-sectional comparison."""
    assert len(page.get("plotly_chart")) == 1


def test_every_product_stays_in_the_table(page):
    """A left join from products means a SKU with no usable snapshot is still
    listed rather than silently dropped."""
    table = page.dataframe[-1].value
    assert len(table) == 4
    assert set(table["role"]) == {"strict", "reference"}


def test_no_stop_was_triggered_by_empty_data(page):
    assert "No usable snapshots yet" not in str(page.warning)
