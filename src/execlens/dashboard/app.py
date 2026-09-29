"""ExecLens Streamlit dashboard entry point.

Run with ``execlens dashboard`` (or ``streamlit run src/execlens/dashboard/app.py``).
The dataset comes from ``EXECLENS_DATA_DIR`` (default ``data/sample``).
"""

import streamlit as st

from execlens.dashboard import pages

st.set_page_config(page_title="ExecLens", page_icon=":material/query_stats:", layout="wide")

navigation = st.navigation(
    [
        st.Page(pages.overview, title="Overview", icon=":material/dashboard:", default=True),
        st.Page(pages.order_explorer, title="Order explorer", icon=":material/search:"),
        st.Page(pages.diagnostics, title="Diagnostics", icon=":material/troubleshoot:"),
        st.Page(pages.strategy_lab, title="Strategy lab", icon=":material/science:"),
        st.Page(pages.benchmarks, title="Benchmarks", icon=":material/speed:"),
    ],
    position="top",
)
navigation.run()
