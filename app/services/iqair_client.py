import logging
import re
import json
from typing import Optional, Dict, Any
import httpx

logger = logging.getLogger(__name__)


class IQAirClient:
    """Client for fetching air quality data from IQAir website."""
    
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    
    async def fetch_station_data(self, url: str) -> Optional[Dict[str, Any]]:
        """Fetch PM2.5 and PM10 data from an IQAir station page."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=self.headers, follow_redirects=True)
                response.raise_for_status()
                html = response.text
                
                data = self._parse_html(html)
                if data:
                    data["url"] = url
                return data
                
        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching IQAir data from {url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error fetching IQAir data from {url}: {e}")
            return None
    
    def _parse_html(self, html: str) -> Optional[Dict[str, Any]]:
        """Parse IQAir HTML page to extract air quality data."""
        result = {}
        
        # Try to find __NEXT_DATA__ JSON (Next.js pages)
        next_data_match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html,
            re.DOTALL
        )
        
        if next_data_match:
            try:
                next_data = json.loads(next_data_match.group(1))
                return self._extract_from_next_data(next_data)
            except json.JSONDecodeError:
                logger.warning("Failed to parse __NEXT_DATA__ JSON")
        
        # Fallback: try to find AQI values directly in HTML
        pm25_match = re.search(r'"pm25":\s*(\d+(?:\.\d+)?)', html)
        if pm25_match:
            result["pm25"] = float(pm25_match.group(1))
        
        pm10_match = re.search(r'"pm10":\s*(\d+(?:\.\d+)?)', html)
        if pm10_match:
            result["pm10"] = float(pm10_match.group(1))
        
        if "pm25" not in result:
            pm25_conc = re.search(r'PM2\.5.*?(\d+(?:\.\d+)?)\s*µg/m³', html, re.IGNORECASE | re.DOTALL)
            if pm25_conc:
                result["pm25"] = float(pm25_conc.group(1))
        
        if "pm10" not in result:
            pm10_conc = re.search(r'PM10.*?(\d+(?:\.\d+)?)\s*µg/m³', html, re.IGNORECASE | re.DOTALL)
            if pm10_conc:
                result["pm10"] = float(pm10_conc.group(1))
        
        name_match = re.search(r'"stationName":\s*"([^"]+)"', html)
        if name_match:
            result["station_name"] = name_match.group(1)
        else:
            title_match = re.search(r'<title>([^|<]+)', html)
            if title_match:
                result["station_name"] = title_match.group(1).strip()
        
        return result if result else None
    
    def _extract_from_next_data(self, data: dict) -> Optional[Dict[str, Any]]:
        """Extract air quality data from Next.js __NEXT_DATA__ structure."""
        result = {}
        
        try:
            props = data.get("props", {})
            page_props = props.get("pageProps", {})
            
            station = page_props.get("station", {})
            current = station.get("current", {}) or page_props.get("current", {})
            
            pollutants = current.get("pollutants", []) or current.get("p", [])
            
            for pollutant in pollutants:
                name = pollutant.get("name", "").lower() or pollutant.get("n", "").lower()
                conc = pollutant.get("concentration", {}).get("value") or pollutant.get("c")
                
                if "pm2" in name or name == "p2":
                    result["pm25"] = float(conc) if conc else None
                elif "pm10" in name or name == "p1":
                    result["pm10"] = float(conc) if conc else None
            
            result["station_name"] = station.get("name") or page_props.get("stationName", "IQAir Station")
            
            aqi = current.get("aqi") or current.get("a")
            if aqi:
                result["aqi"] = int(aqi)
            
        except Exception as e:
            logger.warning(f"Error extracting from __NEXT_DATA__: {e}")
            return None
        
        return result if result.get("pm25") or result.get("pm10") else None


def extract_station_name_from_url(url: str) -> str:
    """Extract a readable name from IQAir URL."""
    try:
        parts = url.rstrip("/").split("/")
        if len(parts) >= 2:
            station = parts[-1].replace("-", " ").title()
            city = parts[-2].replace("-", " ").title() if len(parts) >= 3 else ""
            if city:
                return f"{station}, {city}"
            return station
    except Exception:
        pass
    return "IQAir Station"
