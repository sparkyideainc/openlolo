Hardware checks are opt-in because they send input to an owner-bound phone. Use
`scripts/hardware_acceptance.py` and the calibration/recovery runbooks. A matching
phone-origin telemetry event is required to score each grid selection. Store real
screenshots, identities, and raw evidence only under ignored `.tmp/validation`.
Do not turn historical bench evidence or simulator results into hardware passes.
Run the matrix in both transports: USB and Wi-Fi are separate acceptance rows.
Browser smoke test: start the simulator, issue a single-use credential, log in,
acquire control, refresh, tap and drag the image, press a hardware button, inspect
SUCCEEDED plus one post-action refresh, reject non-ASCII before transmission, list apps,
switch transport, Pause, Resume, and release. Check a narrow viewport and ensure touch
coordinates use actual image bounds. Also test an expired credential and a second session
attempting to acquire control.
