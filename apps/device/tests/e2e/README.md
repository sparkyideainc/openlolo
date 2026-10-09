Browser smoke test: start the simulator, issue a single-use credential, log in,
acquire control, refresh, click and drag the image, inspect SUCCEEDED plus one
post-action refresh, reject non-ASCII before transmission, Pause, Resume, and release.
Check a narrow viewport and ensure pointer coordinates use actual image bounds.
Also test an expired credential and a second session attempting to acquire control.
Automated HTTP/worker end-to-end contracts are under `tests/integration` and `tests/contract`.
