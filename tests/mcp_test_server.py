"""Server MCP di prova (Streamable HTTP su :9100/mcp, oppure stdio con argomento 'stdio')."""
import sys
import uvicorn
from mcp.server import MCPServer

srv = MCPServer("demo", instructions="Usa GetLiveContext per conoscere lo stato attuale dei dispositivi.")

@srv.tool()
def somma(a: float, b: float) -> float:
    """Somma due numeri."""
    return a + b

@srv.tool()
def meteo(citta: str) -> str:
    """Restituisce il meteo (finto) di una città."""
    return f"A {citta} ci sono 18°C e cielo sereno."

@srv.tool(name="homeassistant__GetLiveContext")
def GetLiveContext(domain: str | list[str] | None = None, area: str | None = None) -> str:
    """Stato attuale dei dispositivi (finto, output molto lungo)."""
    if domain is not None:
        raise ValueError(f"No exposed entities found in domain(s): {domain}")
    import json, os
    if area is None:  # formato Home Assistant: usato da Nexus per il catalogo delle entità
        return json.dumps({"result": open(os.path.join(os.path.dirname(__file__), "fixtures", "ha_catalog.txt")).read()})
    return "\n".join(f"- sensor.temp_{i}: {20 + i % 5}°C" for i in range(3000))

@srv.tool()
def HaLiveContext() -> str:
    """Restituisce un Live Context in formato Home Assistant (fixture)."""
    import os
    return open(os.path.join(os.path.dirname(__file__), "fixtures", "ha_live_context.json")).read()

from typing import Literal
CALLS = []

@srv.tool(name="intent__HassTurnOff")
def HassTurnOff(name: str | None = None, area: str | None = None, floor: str | None = None,
                domain: list[str] | None = None,
                device_class: list[Literal["identify", "outlet", "switch", "tv", "speaker"]] | None = None) -> str:
    """Turns off a device or entity (simula Home Assistant)."""
    CALLS.append({"name": name, "area": area, "domain": domain, "device_class": device_class})
    import os
    cat = open(os.path.join(os.path.dirname(__file__), "fixtures", "ha_catalog.txt")).read()
    valid = {n.strip().lower() for line in cat.splitlines() if line.startswith("- names:")
             for n in line.split(":", 1)[1].strip().strip("'").split(",")}
    if name and name.lower() not in valid:
        raise ValueError(f"No entities matched the name (given name '{name}')")
    target = name or area or "TUTTE LE ENTITÀ " + str(device_class or domain or "")
    return '{"response_type": "action_done", "data": {"success": [{"name": "%s"}]}, "calls_so_far": %d}' % (target, len(CALLS))

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        srv.run("stdio")
    else:
        uvicorn.run(srv.streamable_http_app(), host="127.0.0.1", port=9100, log_level="warning")
