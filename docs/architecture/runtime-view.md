# Runtime View

This document describes key runtime executions and interactions.

## Trading Cycle Sequence

1. Scheduler triggers Bot Core  
2. Bot Core requests fresh data from Data Collector  
3. Data Collector fetches via Kraken Adapter  
4. Bot Core evaluates micro-grid  
5. If conditions met → Bot Core issues trade intent  
6. Orchestrator validates Safety Rules  
7. Kraken Adapter executes order  
8. Orchestrator logs cycle outcome

## Strategy Advisory Sequence

1. Orchestrator compiles sanitized summary  
2. Summary passed to Advisor Port  
3. LLM generates JSON recommendations  
4. Orchestrator stores output in DB  
5. Bot Core may incorporate new configs next cycle

## Harvester Sequence

1. Orchestrator checks Kraken balance  
2. Threshold logic applied  
3. If action required → Harvester builds transfer request  
4. Orchestrator enforces safety caps  
5. Banking Adapter executes transfer  
6. State recorded in DB