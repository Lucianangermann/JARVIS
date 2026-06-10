"""JARVIS security & monitoring layer.

Phased build (see tasks/todo.md):
  Phase 1 — db, system_monitor, access_control          [done]
  Phase 2 — voice_auth, anomaly_detector
  Phase 3 — camera_monitor, home_security, digital_security
  Phase 4 — emergency, security_manager

Imports are intentionally lazy/guarded: a missing optional dependency
(psutil, resemblyzer, opencv…) in one component must never stop the
others — or the rest of JARVIS — from loading.
"""
from __future__ import annotations

from .db import SecurityDB
from .system_monitor import SystemHealth, SystemMonitor
from .access_control import AccessController
from .voice_auth import VoiceAuthenticator
from .anomaly_detector import AnomalyDetector
from .camera_monitor import CameraMonitor, DetectionResult
from .home_security import HomeSecuritySystem
from .digital_security import DigitalSecurityMonitor
from .emergency import EmergencySystem
from .security_manager import SecurityManager

__all__ = [
    "SecurityDB",
    "SystemMonitor",
    "SystemHealth",
    "AccessController",
    "VoiceAuthenticator",
    "AnomalyDetector",
    "CameraMonitor",
    "DetectionResult",
    "HomeSecuritySystem",
    "DigitalSecurityMonitor",
    "EmergencySystem",
    "SecurityManager",
]
