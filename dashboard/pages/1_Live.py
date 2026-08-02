"""Live view: serves the standalone `dashboard/live.html` inside the
Streamlit app, so the deployed URL carries both the run comparison and the
live page under one address.

The page itself is a plain self-contained HTML file with no Streamlit in it
(see dashboard/build_live_page.py, which generates it from the measured runs
in runs/). It is embedded rather than reimplemented because everything that
makes it live -- the per-site clock, the reveal-up-to-now drawing, the
Open-Meteo fetch -- happens in the browser on a 1-second tick. Doing that
through Streamlit would mean a server rerun per frame; here Streamlit ships
the file once and the browser does the rest, so this page costs nothing to
keep open and works even when the app's server is idle.

The weather fetch is a cross-origin call from inside the component iframe.
It works because Open-Meteo sends `Access-Control-Allow-Origin: *`; nothing
here needs credentials, a proxy, or a Streamlit secret.
"""

import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PAGE = Path(__file__).resolve().parents[1] / "live.html"

st.set_page_config(page_title="Eco-Loop: Live", layout="wide", page_icon="🏢")

# The component iframe is the whole page here, so strip the padding Streamlit
# would otherwise wrap it in -- the embedded document has its own.
# padding-top has to clear Streamlit's sticky toolbar: at 1.2rem the back-link
# rendered *underneath* it and looked like it had not rendered at all.
st.html(
    """<style>
      .block-container { padding-top: 4.5rem; padding-bottom: 0; max-width: 1180px; }
      iframe { color-scheme: normal; }
    </style>"""
)

# icon= is validated as an emoji, so a plain "←" raises rather than rendering.
st.page_link("app.py", label="Back to the run comparison", icon="⬅️")

if not PAGE.exists():
    st.error(
        "`dashboard/live.html` has not been generated yet. Build it with:\n\n"
        "```\npython dashboard/build_live_page.py\n```"
    )
    st.stop()

html = PAGE.read_text()

# The app pins Streamlit to its light theme (.streamlit/config.toml), so start
# the embedded page in light too -- otherwise a viewer whose OS is in dark mode
# gets a dark panel sitting inside a light app. The page's own theme toggle
# still overrides this, since it sets the same attribute.
html = html.replace('<html lang="en">', '<html lang="en" data-theme="light">', 1)

# A component iframe cannot size itself to its content, so the height is fixed
# here: 1720 fits the document (measured 1672 at full width) without leaving a
# band of dead space under the footer. scrolling=True is the safety net rather
# than the intent -- at a narrow viewport the text rewraps taller, and an inner
# scrollbar in that case is much better than silently clipping the footer.
components.html(html, height=1720, scrolling=True)
