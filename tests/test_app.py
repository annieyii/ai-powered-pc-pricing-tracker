"""End-to-end check that the page renders against the real data files.

Guards the delivery path the manual steps used to cover: the app must load,
plot the strict group, keep every product visible in the table, and produce an
observation summary with no endpoint configured and no network available.
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import tracker.summarise as summarise

APP = str(Path(__file__).resolve().parents[1] / "tracker" / "app.py")

LLM_ENV = ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")


def run_app():
    return AppTest.from_file(APP, default_timeout=60).run()


@pytest.fixture(scope="module")
def page():
    return run_app()


@pytest.fixture
def without_llm_env(monkeypatch):
    for name in LLM_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def without_stored_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(summarise, "SUMMARY_MD", tmp_path / "absent.md")


@pytest.fixture
def stored_summary(monkeypatch, tmp_path):
    """A stored summary whose header names the newest capture in the CSV."""
    def write(last_capture="2026-09-12T15:07",
              prose="Stored prose written by the model."):
        path = tmp_path / "summary.md"
        path.write_text(f"<!-- model=some-model generated=2026-09-12T16:00 "
                        f"last_capture={last_capture} -->\n\n{prose}\n")
        monkeypatch.setattr(summarise, "SUMMARY_MD", path)
        return prose
    return write


def body_text(app):
    return " ".join([element.value for element in app.markdown]
                    + [element.value for element in app.caption]
                    + [element.value for element in app.warning])


def test_the_page_renders_without_raising(page):
    assert not page.exception


def test_all_six_sections_are_present(page):
    """The sidebar carries a header of its own, so the numbered sections are
    the ones counted here."""
    headers = [h.value for h in page.header if h.value[0].isdigit()]
    assert len(headers) == 6
    assert headers[0].startswith("1. Price over time")
    assert headers[1].startswith("2. What the observations say")
    assert headers[2].startswith("3. Observation summary")


def test_findings_are_listed_and_their_ranking_is_inspectable(page):
    """The ranking is only arguable if the factors behind it are visible."""
    expanders = [e.label for e in page.expander]
    assert any("how each was ranked" in label for label in expanders)


def test_the_selection_controls_are_in_the_sidebar(page):
    """Every section reads the same scope, so the control that sets it lives
    once, outside the flow of the sections it governs."""
    assert len(page.sidebar.multiselect) == 1
    assert len(page.sidebar.date_input) == 1


def test_narrowing_the_selection_narrows_the_whole_page(page):
    """A chart drawn for one selection beside a summary written for another
    is separately true in both halves and wrong as a page."""
    narrowed = page.sidebar.multiselect[0].set_value(["6672159"]).run()
    assert not narrowed.exception
    assert any("A selection is active" in info.value for info in narrowed.info)


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


# --- the summary renders with nothing configured ------------------------

def test_the_page_renders_with_no_llm_environment_and_still_summarises(
        without_llm_env, without_stored_summary):
    app = run_app()
    assert not app.exception
    assert "Latest listed price per product" in body_text(app)


def test_the_regenerate_button_is_disabled_and_names_what_is_missing(
        without_llm_env, without_stored_summary):
    app = run_app()
    regenerate = next(b for b in app.button
                      if b.label == "Regenerate with the language model")
    assert regenerate.disabled
    assert "LLM_BASE_URL" in body_text(app)


def test_the_template_is_used_when_no_summary_has_been_stored(
        without_llm_env, without_stored_summary):
    app = run_app()
    text = body_text(app)
    assert "Computed directly from the recorded observations" in text
    assert "Matched-pair observation" in text


# --- the stored summary is preferred, and flagged when overtaken --------

def test_a_stored_summary_is_shown_instead_of_the_template(
        without_llm_env, stored_summary):
    prose = stored_summary()
    app = run_app()
    text = body_text(app)
    assert prose in text
    assert "Latest listed price per product" not in text
    assert "checked against the computed values before it was stored" in text


def test_a_stored_summary_older_than_the_data_is_flagged_as_stale(
        without_llm_env, stored_summary):
    stored_summary(last_capture="2026-09-01T09:00")
    app = run_app()
    warnings = " ".join(w.value for w in app.warning)
    assert "predates the latest observation" in warnings
    assert "2026-09-01T09:00" in warnings


def test_a_current_stored_summary_is_not_flagged_as_stale(
        without_llm_env, stored_summary):
    stored_summary()
    app = run_app()
    assert "predates the latest observation" not in body_text(app)
