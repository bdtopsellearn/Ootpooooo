DEMO / FAKE OTP SYSTEM

This build adds an isolated simulator to air.py.
- Default: OFF
- Separate demo_group_id; never falls back to the real OTP group list.
- Synthetic OTP and masked demo number are generated locally.
- Messages are explicitly labeled DEMO / FAKE OTP.
- Settings use the existing bot_settings persistence path.
- Scheduler runs independently from real OTP/API polling.
- Tested with: python -m py_compile air.py

Render start command remains: python air.py
