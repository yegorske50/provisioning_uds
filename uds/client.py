"""
uds/client.py — EcuUdsClient: domain-specific facade over udsoncan.Client.

Per FR-002..040, plus the session re-entry fix agreed on: re-send `10 03`
(DiagnosticSessionControl, extended session) immediately before every
write-sensitive step, not just once at connect — covers the cloud API wait
without needing a background TesterPresent thread.

STATUS: not yet implemented — build order step 3.
"""
