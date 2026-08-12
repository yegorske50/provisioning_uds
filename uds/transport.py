"""
uds/transport.py — the ONE module allowed to differ between dev and prod.

Builds the isotp.CanStack from AppConfig.can_bus / AppConfig.uds_address.
Nothing outside this file imports `can` or `isotp` directly.

STATUS: not yet implemented — build order step 2 (with simulator.py).
"""

# def build_isotp_stack(cfg: "AppConfig") -> "isotp.CanStack":
#     ...
