#!/usr/bin/env python3
"""Small, defensive client for the official Deye OpenAPI."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


class DeyeAPIError(RuntimeError):
    def __init__(self, message: str, code: Any = None, response: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.response = response or {}


OFFLINE_CODES = {2104006, "2104006", 2106002, "2106002"}
BUSY_CODES = {2104004, "2104004"}


def is_offline_error(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code in OFFLINE_CODES:
        return True
    text = str(exc).lower()
    return "2104006" in text or "device offline" in text or "2106002" in text or "data upload failed" in text


def is_busy_error(exc: BaseException) -> bool:
    """Return True only for an explicit Deye command-queue busy rejection.

    A 2104004 response means this submission was not accepted as a new order, so it
    does not consume the accepted-write/MCU-wear budget. The controller may retry
    after its normal retry interval.
    """
    code = getattr(exc, "code", None)
    if code in BUSY_CODES:
        return True
    text = str(exc).lower()
    return "2104004" in text or "command concurrent running" in text


class DeyeClient:
    def __init__(
        self,
        base_url: str,
        credentials_dir: Optional[str] = None,
        timeout: int = 12,
        *,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        login: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.credentials_dir = credentials_dir
        self.timeout = timeout
        if all(x is not None and str(x) != "" for x in (app_id, app_secret, login, password)):
            self.app_id = str(app_id)
            self.app_secret = str(app_secret)
            self.email = str(login)
            raw_password = str(password)
        elif credentials_dir:
            self.app_id = self._read_secret("app-id.txt")
            self.app_secret = self._read_secret("app-secret.txt")
            self.email = self._read_secret("login.txt")
            raw_password = self._read_secret("login-pass.txt")
        else:
            raise DeyeAPIError("Deye credentials were not supplied")
        self.password_sha256 = hashlib.sha256(raw_password.encode("utf-8")).hexdigest()
        self._token: Optional[str] = None
        self._token_expiry = 0.0

    def _read_secret(self, name: str) -> str:
        if not self.credentials_dir:
            raise DeyeAPIError("credentials_dir is not configured")
        path = os.path.join(self.credentials_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                value = fh.read().strip()
        except FileNotFoundError as exc:
            raise DeyeAPIError(f"Missing credential file: {path}") from exc
        if not value:
            raise DeyeAPIError(f"Credential file is empty: {path}")
        return value

    def get_token(self, force_refresh: bool = False) -> str:
        if self._token and time.time() < self._token_expiry - 300 and not force_refresh:
            return self._token
        url = f"{self.base_url}/account/token?appId={self.app_id}"
        payload = {
            "appSecret": self.app_secret,
            "email": self.email,
            "password": self.password_sha256,
        }
        data = self._request(url, payload, method="POST", auth=False)
        if not data.get("success") or not data.get("accessToken"):
            raise DeyeAPIError(
                f"Authentication failed: code={data.get('code')} msg={data.get('msg')}",
                data.get("code"), data,
            )
        self._token = str(data["accessToken"])
        self._token_expiry = time.time() + int(data.get("expiresIn", 3600))
        return self._token

    def _request(
        self,
        url: str,
        body: Optional[Dict[str, Any]] = None,
        method: str = "POST",
        auth: bool = True,
        retry_auth: bool = True,
    ) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.get_token()}"
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=encoded, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if auth and exc.code in (401, 403) and retry_auth:
                self.get_token(force_refresh=True)
                return self._request(url, body, method, auth=True, retry_auth=False)
            raise DeyeAPIError(f"HTTP {exc.code}: {detail[:500]}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise DeyeAPIError(f"Network error: {exc}") from exc
        except TimeoutError as exc:
            raise DeyeAPIError("Deye API timeout") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DeyeAPIError(f"Invalid JSON from Deye API: {raw[:300]}") from exc
        if not isinstance(parsed, dict):
            raise DeyeAPIError("Unexpected Deye API response type")
        return parsed

    @staticmethod
    def _require_success(data: Dict[str, Any], operation: str) -> Dict[str, Any]:
        if data.get("success") is False:
            raise DeyeAPIError(
                f"{operation} failed: {data.get('code')} {data.get('msg')}",
                data.get("code"), data,
            )
        return data

    def list_stations_with_devices(self, page: int = 1, size: int = 50) -> Dict[str, Any]:
        """Plants owned by this account, each with its device list.

        Read-only. Used by `deyeopt-discover` to find the inverter serial, so the
        serial does not have to be transcribed from a label by hand.
        """
        data = self._request(
            f"{self.base_url}/station/listWithDevice",
            {"page": int(page), "size": int(size)},
        )
        return self._require_success(data, "station/listWithDevice")

    def get_station_latest(self, station_id: int) -> Dict[str, Any]:
        data = self._request(f"{self.base_url}/station/latest", {"stationId": int(station_id)})
        return self._require_success(data, "station/latest")

    def get_device_latest(self, inverter_sn: str) -> Dict[str, Any]:
        data = self._request(
            f"{self.base_url}/device/latest",
            {"deviceList": [str(inverter_sn)], "deviceType": "INVERTER"},
        )
        return self._require_success(data, "device/latest")

    def get_system_config(self, inverter_sn: str) -> Dict[str, Any]:
        data = self._request(f"{self.base_url}/config/system", {"deviceSn": str(inverter_sn)})
        return self._require_success(data, "config/system")

    def get_battery_config(self, inverter_sn: str) -> Dict[str, Any]:
        data = self._request(f"{self.base_url}/config/battery", {"deviceSn": str(inverter_sn)})
        return self._require_success(data, "config/battery")

    def set_work_mode(self, inverter_sn: str, work_mode: str) -> Dict[str, Any]:
        allowed = {"SELLING_FIRST", "ZERO_EXPORT_TO_LOAD", "ZERO_EXPORT_TO_CT"}
        mode = str(work_mode).strip().upper()
        if mode not in allowed:
            raise ValueError(f"Unsupported Deye work mode: {work_mode}")
        data = self._request(
            f"{self.base_url}/order/sys/workMode/update",
            {"deviceSn": str(inverter_sn), "workMode": mode},
        )
        return self._require_success(data, "work mode update")

    def set_energy_pattern(self, inverter_sn: str, pattern: str) -> Dict[str, Any]:
        allowed = {"BATTERY_FIRST", "LOAD_FIRST"}
        value = str(pattern).strip().upper()
        if value not in allowed:
            raise ValueError(f"Unsupported Deye energy pattern: {pattern}")
        data = self._request(
            f"{self.base_url}/order/sys/energyPattern/update",
            {"deviceSn": str(inverter_sn), "energyPattern": value},
        )
        return self._require_success(data, "energy pattern update")

    def set_battery_parameter(self, inverter_sn: str, parameter_type: str, value: int) -> Dict[str, Any]:
        """Low-level official battery-parameter endpoint.

        v2.1 ships this for diagnostics/future use but the normal optimizer deliberately
        does not change MAX_CHARGE_CURRENT dynamically: work-mode switching is much
        lower churn and preserves the battery/BMS current ceiling.
        """
        ptype = str(parameter_type).strip().upper()
        data = self._request(
            f"{self.base_url}/order/battery/parameter/update",
            {"deviceSn": str(inverter_sn), "paramterType": ptype, "value": int(value)},
        )
        return self._require_success(data, f"battery parameter update {ptype}")

    def set_max_sell_power(self, inverter_sn: str, watts: int) -> Dict[str, Any]:
        body = {
            "deviceSn": str(inverter_sn),
            "powerType": "MAX_SELL_POWER",
            "value": int(watts),
        }
        data = self._request(f"{self.base_url}/order/sys/power/update", body)
        return self._require_success(data, "MAX_SELL_POWER update")

    def dynamic_control(
        self,
        inverter_sn: str,
        *,
        max_sell_power: Optional[int] = None,
        work_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Official /strategy/dynamicControl with a deliberately narrow surface.

        Only parameters explicitly supplied by this optimizer are included in the
        request body.  v3.1.0 uses this for infrequent profile commissioning so a
        work-mode change can carry the 1000 W hard export cap in the SAME accepted
        Deye order, avoiding dependence on the unreliable /config/system cache.
        """
        body: Dict[str, Any] = {"deviceSn": str(inverter_sn)}
        if max_sell_power is not None:
            body["maxSellPower"] = int(max_sell_power)
        if work_mode is not None:
            allowed = {"SELLING_FIRST", "ZERO_EXPORT_TO_LOAD", "ZERO_EXPORT_TO_CT"}
            mode = str(work_mode).strip().upper()
            if mode not in allowed:
                raise ValueError(f"Unsupported Deye work mode: {work_mode}")
            body["workMode"] = mode
        data = self._request(f"{self.base_url}/strategy/dynamicControl", body)
        return self._require_success(data, "strategy/dynamicControl")

    def dynamic_control_read(self, inverter_sn: str) -> Dict[str, Any]:
        data = self._request(
            f"{self.base_url}/strategy/dynamicControl/read",
            {"deviceSn": str(inverter_sn)},
        )
        return self._require_success(data, "strategy/dynamicControl/read")

    def dynamic_control_read_result(self, order_id: int) -> Dict[str, Any]:
        data = self._request(
            f"{self.base_url}/strategy/dynamicControl/readResult",
            {"orderId": int(order_id)},
        )
        return self._require_success(data, "strategy/dynamicControl/readResult")

    def check_order_status(self, order_id: int) -> Dict[str, Any]:
        return self._request(f"{self.base_url}/order/{int(order_id)}", None, method="GET")


def flatten_device_latest(data: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten deviceDataList[0].dataList into a simple key/value mapping."""
    devices = data.get("deviceDataList") or []
    if not devices or not isinstance(devices[0], dict):
        raise DeyeAPIError("device/latest returned no inverter data")
    dev = devices[0]
    metrics: Dict[str, Any] = {}
    for item in dev.get("dataList") or []:
        if isinstance(item, dict) and item.get("key") is not None:
            metrics[str(item["key"])] = item.get("value")
    return {
        "collectionTime": dev.get("collectionTime"),
        "deviceState": dev.get("deviceState"),
        "deviceSn": dev.get("deviceSn"),
        "metrics": metrics,
        "raw_device": dev,
    }


def parse_deye_timestamp(value: Any, timezone: dt.tzinfo) -> Optional[dt.datetime]:
    """Accept both epoch values and documented ISO/date-time strings."""
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc).astimezone(timezone)
    text = str(value).strip()
    if text.isdigit():
        return dt.datetime.fromtimestamp(float(text), tz=dt.timezone.utc).astimezone(timezone)
    text = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)
