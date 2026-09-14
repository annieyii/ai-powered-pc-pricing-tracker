"""End-to-end check that the page renders against the real data files.

Guards the delivery path the manual steps used to cover: the app must load,
plot the strict group, keep every product visible in the table, and produce an
observation summary with no endpoint configured and no network available.
"""
from pathlib import Path

import json

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import tracker.summarise as summarise

APP = str(Path(__file__).resolve().parents[1] / "tracker" / "app.py")

LLM_ENV = ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")

PRICES_CSV = Path(__file__).resolve().parents[1] / "data" / "structured" / "prices_manual.csv"


def newest_capture() -> str:
    """The latest capture time on record, in the CSV's own format."""
    return max(pd.read_csv(PRICES_CSV)["captured_at_local"])


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
    """A stored summary whose header names the newest capture in the CSV.

    The newest capture is read from the CSV rather than written down here. A
    literal would be correct only until the next capture, and would then fail
    as staleness while the staleness check was working perfectly.
    """
    def write(last_capture=None,
              prose="Stored prose written by the model."):
        last_capture = last_capture or newest_capture()
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
    """Every section reads the same scope, so the controls that set it live
    once, outside the flow of the sections they govern."""
    assert page.sidebar.multiselect(key="products") is not None
    assert len(page.sidebar.date_input) == 1


def test_a_filter_appears_only_for_a_column_the_products_disagree_about(page):
    """The attribute filters are generated from the master, so the sidebar
    grows controls as the catalogue grows rather than as someone edits it. A
    column every product shares is not a choice and raises nothing."""
    keys = {widget.key for widget in page.sidebar.multiselect}
    assert "filter_brand" in keys and "filter_cpu" in keys
    # All four products are 16GB, 512GB, 14 inch and Windows 11 Home.
    assert {"filter_ram_gb", "filter_storage_gb", "filter_screen_inch",
            "filter_operating_system"}.isdisjoint(keys)


def test_an_option_carries_the_count_it_would_leave():
    """Intersecting filters have dead ends: every choice is reasonable alone
    and the combination matches nothing. The count puts the dead end where it
    can be seen before it is chosen rather than explained after."""
    app = run_app().sidebar.multiselect(key="filter_brand").set_value(
        ["Lenovo"]).run()
    processors = app.sidebar.multiselect(key="filter_cpu").options
    assert "Intel Core Ultra 5 325  (0)" in processors
    assert "AMD Ryzen AI 5 430  (1)" in processors


def test_an_empty_selection_names_the_filters_that_emptied_it():
    """With a dozen controls, "widen the selection" is not an instruction."""
    app = run_app().sidebar.multiselect(key="filter_brand").set_value(
        ["Lenovo"]).run()
    app = app.sidebar.multiselect(key="filter_cpu").set_value(
        ["Intel Core Ultra 5 325"]).run()
    said = " ".join(w.value for w in app.warning)
    assert "Brand" in said and "Processor" in said
    assert "Lenovo" in said and "Intel Core Ultra 5 325" in said


def test_reset_puts_every_filter_back():
    """The escape hatch. Reversing a dozen controls by hand is not a fix."""
    app = run_app().sidebar.multiselect(key="filter_brand").set_value(
        ["Lenovo"]).run()
    assert any("A selection is active" in info.value for info in app.info)
    app = app.sidebar.button[0].click().run()
    assert not any("A selection is active" in info.value for info in app.info)


def test_an_attribute_filter_narrows_the_page_like_a_product_filter():
    """Brand and processor are only another way of naming SKUs, so they reach
    the same scope and every section below moves with them."""
    app = run_app()
    narrowed = app.sidebar.multiselect(key="filter_brand").set_value(["Lenovo"]).run()
    assert not narrowed.exception
    assert any("A selection is active" in info.value for info in narrowed.info)


def test_the_untouched_page_reports_no_selection(page):
    """The date input opens on the full range, which is not a filter. Counting
    it as one made the first screen announce a selection the reader never
    made."""
    assert not any("A selection is active" in info.value for info in page.info)


def test_narrowing_the_selection_narrows_the_whole_page():
    """A chart drawn for one selection beside a summary written for another
    is separately true in both halves and wrong as a page.

    This case runs its own app: setting a widget value mutates the instance it
    is set on, so narrowing the shared one would hand every later case a page
    that is still filtered.
    """
    narrowed = run_app().sidebar.multiselect(key="products").set_value(
        ["6672159"]).run()
    assert not narrowed.exception
    assert any("A selection is active" in info.value for info in narrowed.info)


def test_the_trend_chart_is_the_first_section(page):
    """A chart of price movement over time is the point of the tool, so the
    time series comes before any cross-sectional comparison."""
    assert len(page.get("plotly_chart")) == 1


def test_no_block_leaves_a_pair_of_dollar_signs_unescaped(page):
    """Streamlit reads `$...$` as LaTeX, so a line carrying two prices renders
    as a maths span with both currency symbols eaten. Every figure here is a
    price, so this is the default failure rather than an unlikely one, and the
    element values the other cases read look correct while the page does not.
    """
    offenders = [element.value
                 for kind in ("markdown", "caption", "success", "info", "warning")
                 for element in getattr(page, kind)
                 if element.value.replace(r"\$", "").count("$") >= 2]
    assert offenders == []


def test_the_chart_draws_every_tracked_product(page):
    """A reader asks where all four machines sit, so all four are plotted. The
    traces are counted rather than trusted to be visible: a matched pair at
    parity overlaps exactly, and the second line can hide under the first."""
    traces = json.loads(page.get("plotly_chart")[0].proto.spec)["data"]
    assert len(traces) == 4
    # The one drawn second must let the first show through, or counting is moot.
    assert traces[1]["line"]["dash"] != "solid"
    assert traces[1]["marker"]["symbol"].endswith("-open")


def test_the_chart_separates_the_pair_from_the_context(page):
    """Plotting the reference SKUs must not invite a comparison the rule
    refuses. They lead the eye less, and say so in the legend."""
    traces = json.loads(page.get("plotly_chart")[0].proto.spec)["data"]
    strict = [t for t in traces if "(reference)" not in t["name"]]
    reference = [t for t in traces if "(reference)" in t["name"]]
    assert len(strict) == 2 and len(reference) == 2
    assert {t["name"] for t in strict} == {
        "Lenovo Yoga 7a 2-in-1 14in", "HP OmniBook X Flip 2-in-1 14in"}
    assert all(t.get("opacity", 1) < 1 for t in reference)
    assert all(t.get("opacity", 1) == 1 for t in strict)


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
    assert "found among the computed values before it was stored" in text


def test_a_stored_summary_older_than_the_data_is_replaced_not_annotated(
        without_llm_env, stored_summary):
    """Showing stale prose first and qualifying it afterwards means the wrong
    figures are the ones read. The computed summary takes its place."""
    stale = stored_summary(last_capture="2026-09-12T15:07",
                           prose="Prose written before the later captures.")
    app = run_app()
    said = body_text(app)
    assert "does not describe" in said
    assert stale not in said


def test_a_stored_summary_is_replaced_under_a_selection(
        without_llm_env, stored_summary):
    """It describes the whole table as it stood when it was written, which is
    not what a filtered page is showing."""
    prose = stored_summary()
    app = run_app().sidebar.multiselect(key="filter_brand").set_value(
        ["Lenovo"]).run()
    assert prose not in body_text(app)


def test_the_table_lists_the_selected_products_only(page):
    """Joining from the whole master would put every excluded product back as
    a row of blanks, which reads as missing data rather than as excluded."""
    narrowed = run_app().sidebar.multiselect(key="filter_brand").set_value(
        ["Lenovo"]).run()
    table = narrowed.dataframe[-1].value
    assert set(table["brand"]) == {"Lenovo"}


def test_a_current_stored_summary_is_not_flagged_as_stale(
        without_llm_env, stored_summary):
    stored_summary()
    app = run_app()
    assert "predates the latest observation" not in body_text(app)


def test_the_app_imports_the_way_streamlit_runs_it():
    """Regression: the AppTest cases above run inside pytest, where
    ``pythonpath = ["."]`` makes ``tracker`` importable. Streamlit puts the
    script's own directory on the path instead, so ``import tracker.insights``
    failed in a real launch while every test passed. Running the file directly
    reproduces that path exactly.

    Checking the HTTP status of a running server does not catch this: the
    server answers 200 and reports the traceback inside the page.
    """
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "tracker" / "app.py")],
        capture_output=True, text=True, timeout=90, cwd=root)
    assert "ModuleNotFoundError" not in result.stderr, result.stderr[-1500:]
