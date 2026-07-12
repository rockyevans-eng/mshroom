"""MSHroom launcher package.

``python -m mshroom`` is the front door to the app in ``app/``:

* default (desktop) mode -- serve the UI on a loopback port and show it
  in a native window (pywebview);
* ``--headless`` mode -- run the same server bound for the network, for
  service/production listener duty.

All the actual product code (parser, MLLP, routes, UI) lives in
``hl7kit/`` and ``app/``; this package only starts and stops it.
See ``mshroom.__main__`` for the details and the load-bearing invariants.
"""
