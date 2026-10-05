# Paper and Plotter Setup

Use an A4-sized or larger AxiDraw, with firmware 2.6.2 or newer. Connect USB and the correct power supply to the computer running the app. Install `requirements-plotter.txt` in that computer's virtual environment and launch with `--enable-plotter`. Physical plotting is disabled by default.

![Top view of A4 paper and Home](../app/astra/paper-setup.svg)

## Establish Home

The origin is the A4 landscape sheet's upper-left corner, under the pen tip. X increases to the right and Y toward the front. The head must start fully left and back, as shown above. Do not align the paper to the machine base or the first drawing stroke.

1. Secure A4 landscape paper and the pen, verify the adapter, close other plotter applications, and clear the work area. Keep the physical pause button accessible.
2. Open **Calibration**, select the connected device and actual machine model, and choose **Check plotter** after changing settings. Opening a menu only checks the device; it does not move it.
3. If the carriage is already fully left and back at Home, confirm its physical position and choose **Set current position as Home**. The app raises the pen, releases XY if needed, and establishes that position without moving XY.
4. Otherwise choose **Raise pen & release XY**, wait for confirmed motor release, then gently place the solid carriage block fully left and back at Home. Align the paper's upper-left corner beneath the raised tip. Never force energized motors or push the vertical pen slide.
5. Open Calibration again, confirm physical placement, and choose **Set current position as Home**. The app raises the pen and enables XY at the current position; zero counters are required.
6. Check again before each pen test. Use **Raise pen**, then **Pen down / up** to verify clearance and paper contact. Adjust the pen so the raised tip clears the paper and the lowered pen rests under its own weight; do not force the slide.
7. After verifying pen clearance and unobstructed travel from Home, re-check, confirm the motion-test checkbox, and run **5 mm pen-up square**. Watch for smooth, correctly sized movement and return to Home before starting a drawing.

For detailed placement and pen setup, consult the [manufacturer's user guide](https://cdn.evilmadscientist.com/dl/ad/public/AxiDraw_Guide_v570.pdf).

## Plot or Return Home

Select a drawing, choose **Plot on AxiDraw**, and confirm the workspace and unchanged Home reference. **Home & start plot** first raises the pen, performs the official Walk Home command, and verifies idle motors, zero counters, and a powered raised pen before drawing. It does not infer the initial Home position.

The separate **Home** button runs only that pen-up return; it does not draw. It requires an established, valid Home reference and a clear workspace. Both Home and Calibration work with an empty drawing gallery.

Each action uses a short-lived, single-use check. Changing settings or running a test requires checking again. Default drawing settings are 25% pen-down speed, 30% pen-up travel, 25% acceleration, and 60%/30% pen-up/down heights. Return Home is limited to 15% travel speed and 20% acceleration within the A4 envelope. Start conservatively and check physical behavior.

## Stop and Reset

**Pause plot** is cooperative, not an emergency stop. Closing the browser does not stop the plotter, and already queued movement may finish after a communication failure. Use the physical pause button when needed.

After a server restart, motor release, power loss, collision, skipped steps, manual movement, or any uncertain position, release XY and establish Home again. There is no physical Home sensor. Reported zero counters cannot prove that the carriage did not slip.

Plot receipts in ignored `outputs/astra/` record commands and controller replies, not independently verified physical output. Inspect the paper and machine yourself. A successful software test or preview is not evidence of real plotter performance.

## Power Supply

For a standard AxiDraw, verify the label against the [manufacturer's specification](https://www.evilmadscientist.com/forums/topic/warning-low-voltage-detected/): regulated **9 V DC, at least 1.5 A, center-positive, 5.5 mm outer / 2.1 mm inner barrel connector**. The [brushless-servo upgrade](https://cdn.evilmadscientist.com/dl/ad/public/ad_brushless_2.pdf) requires a 9 V, 2.5 A supply. Verify your actual machine's requirements before connecting it.

USB detection and power ADC readings do not verify an adapter's voltage rating, polarity, or behavior under load. Stop testing if power is uncertain or movement is jerky.
