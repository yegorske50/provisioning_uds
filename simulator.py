"""
simulator.py — SimulatedEcuState + SimulatedEcuServer.

Real isotp.CanStack underneath, minimal business logic on top. Must enforce
a session timeout (reject non-default-session-only services after N seconds
idle) so this actually catches the TesterPresent/session-drop bug in dev
instead of only on real hardware — see the session discussion in this build.

STATUS: not yet implemented — build order step 2 (with transport.py).
"""
