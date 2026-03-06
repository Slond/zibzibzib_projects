import logging
from typing import Optional

import httpx

from app.config import YANDEX_API_BASE, YANDEX_TOKEN

logger = logging.getLogger(__name__)


class YandexSmartHomeClient:
    def __init__(self, token: Optional[str] = None):
        self.token = token or YANDEX_TOKEN
        self.headers = {"Authorization": f"Bearer {self.token}"}

    async def get_user_info(self) -> dict:
        """Get full smart home info including all devices"""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{YANDEX_API_BASE}/user/info",
                headers=self.headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_device(self, device_id: str) -> dict:
        """Get device details including current property states"""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{YANDEX_API_BASE}/devices/{device_id}",
                headers=self.headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_all_devices_with_properties(self) -> list[dict]:
        """Get all devices and their current properties"""
        user_info = await self.get_user_info()
        devices = user_info.get("devices", [])

        result = []
        for device in devices:
            try:
                device_data = await self.get_device(device["id"])
                result.append(device_data)
            except Exception as e:
                logger.error(f"Failed to get device {device.get('id')}: {e}")

        return result
