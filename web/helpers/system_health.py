"""System health monitoring for DVD ripper dashboard."""
import os
import re
import json
import time
import subprocess

# Module-level cache for I/O stats delta calculation
_io_stats_prev = {}
_io_stats_time = 0


class SystemHealth:
    """Provides system health metrics: CPU, memory, load, temps, I/O."""

    @staticmethod
    def get_cpu_usage():
        """Read /proc/stat and calculate CPU usage.

        Returns:
            dict: CPU usage data with total and per-core percentages.
        """
        try:
            with open('/proc/stat', 'r') as f:
                lines = f.readlines()

            cpu_data = {}
            for line in lines:
                if line.startswith('cpu'):
                    parts = line.split()
                    cpu_name = parts[0]
                    # user, nice, system, idle, iowait, irq, softirq, steal
                    values = [int(x) for x in parts[1:8]]
                    total = sum(values)
                    idle = values[3] + values[4]  # idle + iowait
                    usage = ((total - idle) / total * 100) if total > 0 else 0
                    cpu_data[cpu_name] = {
                        "usage": round(usage, 1),
                        "total": total,
                        "idle": idle
                    }

            return cpu_data
        except Exception as e:
            return {"cpu": {"usage": 0, "error": str(e)}}

    @staticmethod
    def get_memory_usage():
        """Read /proc/meminfo and return memory statistics.

        Returns:
            dict: Memory stats with total, used, available, cached, and percent used.
        """
        try:
            meminfo = {}
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        key = parts[0].rstrip(':')
                        # Convert kB to bytes
                        value = int(parts[1]) * 1024
                        meminfo[key] = value

            total = meminfo.get('MemTotal', 0)
            available = meminfo.get('MemAvailable', 0)
            cached = meminfo.get('Cached', 0) + meminfo.get('Buffers', 0)
            used = total - available

            return {
                "total": total,
                "used": used,
                "available": available,
                "cached": cached,
                "percent": round((used / total * 100) if total > 0 else 0, 1),
                "total_human": SystemHealth._format_bytes(total),
                "used_human": SystemHealth._format_bytes(used),
                "available_human": SystemHealth._format_bytes(available)
            }
        except Exception as e:
            return {"total": 0, "used": 0, "available": 0, "percent": 0, "error": str(e)}

    @staticmethod
    def _format_bytes(bytes_val):
        """Format bytes to human-readable string."""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if abs(bytes_val) < 1024.0:
                return f"{bytes_val:.1f} {unit}"
            bytes_val /= 1024.0
        return f"{bytes_val:.1f} PB"

    @staticmethod
    def get_load_average():
        """Read /proc/loadavg and return load averages.

        Returns:
            dict: Load averages with 1m, 5m, 15m and CPU count.
        """
        try:
            with open('/proc/loadavg', 'r') as f:
                parts = f.read().split()

            cpu_count = os.cpu_count() or 1
            load_1m = float(parts[0])
            load_5m = float(parts[1])
            load_15m = float(parts[2])

            return {
                "load_1m": load_1m,
                "load_5m": load_5m,
                "load_15m": load_15m,
                "cpu_count": cpu_count,
                "load_per_core": round(load_1m / cpu_count, 2),
                "status": "high" if load_1m > cpu_count else "normal"
            }
        except Exception as e:
            return {"load_1m": 0, "load_5m": 0, "load_15m": 0, "cpu_count": 1, "error": str(e)}

    @staticmethod
    def get_temperatures():
        """Run sensors command and parse temperature/fan output.

        Returns:
            dict: Temperature readings and fan speeds.
        """
        result = {
            "temperatures": [],
            "fans": [],
            "available": False
        }

        try:
            proc = subprocess.run(
                ["sensors", "-j"],
                capture_output=True, text=True, timeout=5
            )
            if proc.returncode != 0:
                return result

            data = json.loads(proc.stdout)
            result["available"] = True

            for chip_name, chip_data in data.items():
                if not isinstance(chip_data, dict):
                    continue

                for sensor_name, sensor_data in chip_data.items():
                    if not isinstance(sensor_data, dict):
                        continue

                    for reading_name, reading_val in sensor_data.items():
                        if 'temp' in reading_name.lower() and '_input' in reading_name:
                            temp_c = reading_val
                            result["temperatures"].append({
                                "chip": chip_name,
                                "sensor": sensor_name,
                                "temp_c": round(temp_c, 1),
                                "temp_f": round(temp_c * 9/5 + 32, 1),
                                "status": "critical" if temp_c > 85 else "warning" if temp_c > 70 else "ok"
                            })
                        elif 'fan' in reading_name.lower() and '_input' in reading_name:
                            result["fans"].append({
                                "chip": chip_name,
                                "sensor": sensor_name,
                                "rpm": int(reading_val)
                            })

        except FileNotFoundError:
            result["error"] = "lm-sensors not installed"
        except json.JSONDecodeError:
            # Fall back to text parsing
            try:
                proc = subprocess.run(
                    ["sensors"],
                    capture_output=True, text=True, timeout=5
                )
                if proc.returncode == 0:
                    result["available"] = True
                    result["raw_output"] = proc.stdout
                    # Parse text output for temperatures
                    for line in proc.stdout.split('\n'):
                        if '\u00b0C' in line:
                            match = re.search(r'([+-]?\d+\.?\d*)\s*\u00b0C', line)
                            if match:
                                temp_c = float(match.group(1))
                                label = line.split(':')[0].strip() if ':' in line else "CPU"
                                result["temperatures"].append({
                                    "sensor": label,
                                    "temp_c": round(temp_c, 1),
                                    "status": "critical" if temp_c > 85 else "warning" if temp_c > 70 else "ok"
                                })
                        if 'RPM' in line:
                            match = re.search(r'(\d+)\s*RPM', line)
                            if match:
                                rpm = int(match.group(1))
                                label = line.split(':')[0].strip() if ':' in line else "Fan"
                                result["fans"].append({
                                    "sensor": label,
                                    "rpm": rpm
                                })
            except Exception:
                pass
        except subprocess.TimeoutExpired:
            result["error"] = "sensors command timed out"
        except Exception as e:
            result["error"] = str(e)

        return result

    @staticmethod
    def get_io_stats():
        """Get disk I/O statistics and device information.

        Returns:
            dict: Device list with throughput and I/O wait percentage.
        """
        global _io_stats_prev, _io_stats_time

        result = {
            "devices": [],
            "iowait_percent": 0.0,
            "total_read_mb_s": 0.0,
            "total_write_mb_s": 0.0,
            "available": False,
            "error": None
        }

        current_time = time.time()

        try:
            # Get I/O wait from /proc/stat
            try:
                with open('/proc/stat', 'r') as f:
                    for line in f:
                        if line.startswith('cpu '):
                            parts = line.split()
                            # cpu user nice system idle iowait irq softirq
                            if len(parts) >= 6:
                                total = sum(int(p) for p in parts[1:8] if p.isdigit())
                                iowait = int(parts[5]) if len(parts) > 5 else 0
                                if total > 0:
                                    result["iowait_percent"] = round((iowait / total) * 100, 1)
                            break
            except Exception:
                pass

            # Get block devices with lsblk
            try:
                lsblk_result = subprocess.run(
                    ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,MODEL,TRAN,ROTA"],
                    capture_output=True, text=True, timeout=5
                )
                if lsblk_result.returncode == 0:
                    lsblk_data = json.loads(lsblk_result.stdout)
                    devices_info = {}

                    def process_device(dev, parent_tran=None):
                        """Process a device and its children recursively."""
                        name = dev.get("name", "")
                        dev_type = dev.get("type", "")

                        # Only process disks and partitions, skip loops/roms
                        if dev_type not in ("disk", "part"):
                            return

                        tran = dev.get("tran") or parent_tran
                        rota = dev.get("rota")  # 1=rotational (HDD), 0=SSD

                        # Determine device type label
                        if tran == "usb":
                            type_label = "usb"
                        elif tran == "nvme":
                            type_label = "nvme"
                        elif rota == "0" or rota == 0 or rota is False:
                            type_label = "ssd"
                        elif rota == "1" or rota == 1 or rota is True:
                            type_label = "hdd"
                        else:
                            type_label = "disk"

                        # For disks, store the info
                        if dev_type == "disk":
                            devices_info[name] = {
                                "name": name,
                                "model": (dev.get("model") or "").strip(),
                                "size": dev.get("size", ""),
                                "type": type_label,
                                "mountpoint": dev.get("mountpoint") or "",
                                "read_mb_s": 0.0,
                                "write_mb_s": 0.0
                            }

                        # Process children (partitions)
                        for child in dev.get("children", []):
                            # If partition has mountpoint, update parent disk's mountpoint
                            if child.get("mountpoint") and name in devices_info:
                                if not devices_info[name]["mountpoint"]:
                                    devices_info[name]["mountpoint"] = child.get("mountpoint")
                            process_device(child, tran)

                    for dev in lsblk_data.get("blockdevices", []):
                        process_device(dev)

                    # Read /proc/diskstats for I/O counters
                    diskstats = {}
                    try:
                        with open('/proc/diskstats', 'r') as f:
                            for line in f:
                                parts = line.split()
                                if len(parts) >= 14:
                                    dev_name = parts[2]
                                    # Fields: reads_completed, reads_merged, sectors_read, ms_reading,
                                    #         writes_completed, writes_merged, sectors_written, ms_writing
                                    diskstats[dev_name] = {
                                        "sectors_read": int(parts[5]),
                                        "sectors_written": int(parts[9]),
                                        "time": current_time
                                    }
                    except Exception:
                        pass

                    # Calculate throughput using delta from previous reading
                    time_delta = current_time - _io_stats_time if _io_stats_time > 0 else 1.0
                    if time_delta < 0.1:
                        time_delta = 0.1  # Minimum delta to avoid division issues

                    for dev_name, dev_info in devices_info.items():
                        if dev_name in diskstats:
                            current = diskstats[dev_name]
                            if dev_name in _io_stats_prev and _io_stats_time > 0:
                                prev = _io_stats_prev[dev_name]
                                read_sectors = current["sectors_read"] - prev["sectors_read"]
                                write_sectors = current["sectors_written"] - prev["sectors_written"]
                                # Sectors are typically 512 bytes
                                dev_info["read_mb_s"] = round((read_sectors * 512 / 1024 / 1024) / time_delta, 1)
                                dev_info["write_mb_s"] = round((write_sectors * 512 / 1024 / 1024) / time_delta, 1)
                                # Clamp negative values (can happen on counter wrap)
                                if dev_info["read_mb_s"] < 0:
                                    dev_info["read_mb_s"] = 0.0
                                if dev_info["write_mb_s"] < 0:
                                    dev_info["write_mb_s"] = 0.0

                    # Store current stats for next delta calculation
                    _io_stats_prev = diskstats
                    _io_stats_time = current_time

                    # Build device list and calculate totals
                    result["devices"] = list(devices_info.values())
                    result["total_read_mb_s"] = round(sum(d["read_mb_s"] for d in result["devices"]), 1)
                    result["total_write_mb_s"] = round(sum(d["write_mb_s"] for d in result["devices"]), 1)
                    result["available"] = True

            except subprocess.TimeoutExpired:
                result["error"] = "lsblk timed out"
            except json.JSONDecodeError:
                result["error"] = "Failed to parse lsblk output"

        except Exception as e:
            result["error"] = str(e)

        return result

    @staticmethod
    def get_dvd_processes():
        """Get list of DVD ripper related processes.

        Returns:
            list: Process info dicts sorted by CPU usage.
        """
        # Processes we care about
        target_commands = ['HandBrakeCLI', 'ddrescue', 'rsync', 'ffmpeg', 'dvd-iso', 'dvd-encoder', 'dvd-transfer']

        processes = []
        try:
            proc = subprocess.run(
                ["ps", "aux"],
                capture_output=True, text=True, timeout=5
            )

            for line in proc.stdout.split('\n')[1:]:  # Skip header
                if not line.strip():
                    continue

                # Check if line contains any target command
                matched_cmd = None
                for cmd in target_commands:
                    if cmd in line:
                        matched_cmd = cmd
                        break

                if matched_cmd:
                    parts = line.split(None, 10)  # Split into max 11 parts
                    if len(parts) >= 11:
                        pid = parts[1]
                        cpu_pct = float(parts[2])
                        mem_pct = float(parts[3])
                        start_time = parts[8]
                        elapsed = parts[9]
                        command = parts[10]

                        # Determine process type based on command
                        process_type = "unknown"
                        if 'HandBrakeCLI' in command:
                            process_type = "encoder"
                        elif 'ddrescue' in command:
                            process_type = "iso"
                        elif 'rsync' in command or 'scp' in command:
                            process_type = "transfer"
                        elif 'ffmpeg' in command:
                            process_type = "preview"

                        processes.append({
                            "pid": int(pid),
                            "cpu_percent": cpu_pct,
                            "mem_percent": mem_pct,
                            "start_time": start_time,
                            "elapsed": elapsed,
                            "command": command[:100],  # Truncate long commands
                            "command_full": command,
                            "type": process_type,
                            "matched": matched_cmd
                        })

        except Exception as e:
            processes.append({"error": str(e)})

        # Sort by CPU usage descending
        return sorted(processes, key=lambda x: x.get("cpu_percent", 0), reverse=True)
