"""
uds/codecs.py — StringCodec: one udsoncan.DidCodec class, fixed or variable
length depending on the `length` constructor argument. Fixed for VIN (17) and
device_id/vin_alias (36); variable (length=None) for CSR/certs — see
architecture doc section 4 for the udsoncan __len__ / ReadAllRemainingData detail.

STATUS: not yet implemented — build order step 3 (with uds/client.py).
"""
