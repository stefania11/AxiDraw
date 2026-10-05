# Source and Third-Party Notices

The application and geometry helpers derive from the AxiDraw demonstration developed in [SakanaAI/fugu](https://github.com/SakanaAI/fugu), at commit `36cae48488e267e0a8caeb7ab1214a81fc5fd52e`. This attribution records source provenance; the release does not require the original repository or model service.

This standalone release retains the studio, geometry validation, preview, webcam workflow, guarded plotter control, and regression tests. Unused legacy model clients, sample-artwork fallback, environment-file loading, private datasets, session artifacts, and Git history are excluded. Geometry helpers are in `scripts/svg_geometry.py`; offline preview helpers are in `scripts/plot_preview.py`.

Lucide icon notices and licenses are retained in [app/astra/icons/LICENSE](app/astra/icons/LICENSE). The optional AxiDraw SDK is downloaded separately from its manufacturer and retains its own licensing terms. No new blanket license is asserted for inherited code in this repository.
