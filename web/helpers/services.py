"""Systemd service and timer control for DVD ripper."""
import os
import subprocess

# Services and timers managed by the DVD ripper
MANAGED_SERVICES = [
    {"name": "dvd-dashboard", "description": "Web Dashboard"},
    {"name": "dvd-encoder", "description": "Video Encoder (Stage 2)"},
    {"name": "dvd-transfer", "description": "NAS Transfer (Stage 3)"},
]

MANAGED_TIMERS = [
    {"name": "dvd-encoder", "description": "Encoder Timer (15 min)"},
    {"name": "dvd-transfer", "description": "Transfer Timer (15 min)"},
]


class ServiceController:
    """Controls systemd services and timers for the DVD ripper pipeline."""

    @staticmethod
    def get_service_status(service_name):
        """Get detailed status of a systemd service.

        Args:
            service_name: Name of the service (without .service suffix).

        Returns:
            dict: Service status including active, enabled, state, pid, started.
        """
        try:
            # Check if service is active
            result = subprocess.run(
                ["systemctl", "is-active", f"{service_name}.service"],
                capture_output=True, text=True, timeout=5
            )
            is_active = result.stdout.strip() == "active"

            # Check if service is enabled
            result = subprocess.run(
                ["systemctl", "is-enabled", f"{service_name}.service"],
                capture_output=True, text=True, timeout=5
            )
            is_enabled = result.stdout.strip() == "enabled"

            # Get more details
            result = subprocess.run(
                ["systemctl", "show", f"{service_name}.service",
                 "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp"],
                capture_output=True, text=True, timeout=5
            )
            props = {}
            for line in result.stdout.strip().split("\n"):
                if "=" in line:
                    key, _, value = line.partition("=")
                    props[key] = value

            return {
                "active": is_active,
                "enabled": is_enabled,
                "state": props.get("ActiveState", "unknown"),
                "substate": props.get("SubState", "unknown"),
                "pid": props.get("MainPID", "0"),
                "started": props.get("ExecMainStartTimestamp", ""),
            }
        except Exception as e:
            return {
                "active": False,
                "enabled": False,
                "state": "error",
                "substate": str(e),
                "pid": "0",
                "started": "",
            }

    @staticmethod
    def get_timer_status(timer_name):
        """Get detailed status of a systemd timer.

        Args:
            timer_name: Name of the timer (without .timer suffix).

        Returns:
            dict: Timer status including active, enabled, next/last trigger.
        """
        try:
            # Check if timer is active
            result = subprocess.run(
                ["systemctl", "is-active", f"{timer_name}.timer"],
                capture_output=True, text=True, timeout=5
            )
            is_active = result.stdout.strip() == "active"

            # Check if timer is enabled
            result = subprocess.run(
                ["systemctl", "is-enabled", f"{timer_name}.timer"],
                capture_output=True, text=True, timeout=5
            )
            is_enabled = result.stdout.strip() == "enabled"

            # Get timer details
            result = subprocess.run(
                ["systemctl", "show", f"{timer_name}.timer",
                 "--property=NextElapseUSecRealtime,LastTriggerUSec"],
                capture_output=True, text=True, timeout=5
            )
            props = {}
            for line in result.stdout.strip().split("\n"):
                if "=" in line:
                    key, _, value = line.partition("=")
                    props[key] = value

            return {
                "active": is_active,
                "enabled": is_enabled,
                "next_trigger": props.get("NextElapseUSecRealtime", ""),
                "last_trigger": props.get("LastTriggerUSec", ""),
            }
        except Exception as e:
            return {
                "active": False,
                "enabled": False,
                "next_trigger": "",
                "last_trigger": "",
                "error": str(e),
            }

    @staticmethod
    def get_all_service_status():
        """Get status of all managed services.

        Returns:
            list: Service status dicts.
        """
        services = []
        for svc in MANAGED_SERVICES:
            status = ServiceController.get_service_status(svc["name"])
            services.append({
                "name": svc["name"],
                "description": svc["description"],
                **status
            })
        return services

    @staticmethod
    def get_all_timer_status():
        """Get status of all managed timers.

        Returns:
            list: Timer status dicts.
        """
        timers = []
        for tmr in MANAGED_TIMERS:
            status = ServiceController.get_timer_status(tmr["name"])
            timers.append({
                "name": tmr["name"],
                "description": tmr["description"],
                **status
            })
        return timers

    @staticmethod
    def get_udev_trigger_status():
        """Get status of the udev disc detection trigger.

        Returns:
            dict: Status with enabled, status, and message.
        """
        udev_rule = "/etc/udev/rules.d/99-dvd-ripper.rules"
        udev_disabled = f"{udev_rule}.disabled"

        if os.path.exists(udev_rule):
            return {
                "enabled": True,
                "status": "active",
                "message": "Disc insertion triggers ISO creation"
            }
        elif os.path.exists(udev_disabled):
            return {
                "enabled": False,
                "status": "paused",
                "message": "Disc detection paused (rule disabled)"
            }
        else:
            return {
                "enabled": False,
                "status": "missing",
                "message": "Udev rule not installed"
            }

    @staticmethod
    def control_service(service_name, action):
        """Start, stop, or restart a systemd service.

        Args:
            service_name: Name of the service.
            action: One of "start", "stop", "restart".

        Returns:
            tuple: (success, message)
        """
        if action not in ["start", "stop", "restart"]:
            return False, "Invalid action"

        # Validate service name
        valid_services = [s["name"] for s in MANAGED_SERVICES]
        if service_name not in valid_services:
            return False, "Invalid service"

        # Don't allow stopping the dashboard from itself
        if service_name == "dvd-dashboard" and action == "stop":
            return False, "Cannot stop dashboard from web UI"

        try:
            result = subprocess.run(
                ["systemctl", action, f"{service_name}.service"],
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0, result.stderr.strip() or "OK"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def control_timer(timer_name, action):
        """Start (unpause), stop (pause), enable, or disable a systemd timer.

        Args:
            timer_name: Name of the timer.
            action: One of "start", "stop", "enable", "disable".

        Returns:
            tuple: (success, message)
        """
        if action not in ["start", "stop", "enable", "disable"]:
            return False, "Invalid action"

        # Validate timer name
        valid_timers = [t["name"] for t in MANAGED_TIMERS]
        if timer_name not in valid_timers:
            return False, "Invalid timer"

        try:
            result = subprocess.run(
                ["systemctl", action, f"{timer_name}.timer"],
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0, result.stderr.strip() or "OK"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def trigger_service(stage):
        """Trigger a systemd service for a pipeline stage.

        Args:
            stage: One of "encoder", "transfer", "distribute".

        Returns:
            tuple: (success, message)
        """
        if stage not in ["encoder", "transfer", "distribute"]:
            return False, "Invalid stage"

        service_name = f"dvd-{stage}.service"
        try:
            result = subprocess.run(
                ["systemctl", "start", "--no-block", service_name],
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0, result.stderr.strip() or "OK"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def trigger_distribute_force():
        """Trigger distribute with --force flag to bypass 'keep 1 for local' logic.

        Calls the script directly instead of using systemctl, since we need
        to pass the --force argument.

        Returns:
            tuple: (success, message)
        """
        try:
            result = subprocess.run(
                ["/usr/local/bin/dvd-distribute.sh", "--force"],
                capture_output=True, text=True, timeout=60
            )
            return result.returncode == 0, result.stderr.strip() or "OK"
        except Exception as e:
            return False, str(e)
