"""Smart Home layer — platform-agnostic device control."""
# Intentionally no eager imports here. Importing this package (e.g.
# when brain.py imports smarthome_tools) must not trigger the full
# smarthome_manager cascade — that would make any sub-module import
# error break the brain entirely. Callers import SmartHomeManager
# directly from smarthome_manager.py.
