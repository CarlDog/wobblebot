# Deployment Architecture

WobbleBot runs entirely inside Docker Compose on a Synology NAS.

## Containers

- **wobblebot-core**  
  Main app container running orchestrator + modules

- **llm-container**  
  Ollama or LLM API proxy

- **grafana + prometheus (optional)**  
  For dashboards and metrics

- **sqlite-volume**  
  Mounted persistent DB location

## Networking Layout

- Internal Docker network for service-to-service communication  
- No container accessible from WAN unless explicitly proxied  
- LLM + core communicate via local HTTP port  

## Synology Considerations

- Use bind mounts for logs + DB  
- Resource limits on memory and CPU  
- Automatic restart policy