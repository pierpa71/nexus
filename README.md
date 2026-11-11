# Nexus — AI Chat con MCP e RAG

Web app containerizzata per chattare con qualunque LLM **OpenAI-compatibile** (Ollama, vLLM, LM Studio, LiteLLM, OpenAI…), con integrazione di **server MCP** (tool abilitabili singolarmente) e **RAG** sui tuoi documenti.

## Avvio rapido

```bash
cp .env.example .env          # poi imposta SEARXNG_SECRET (openssl rand -hex 32)
docker compose up -d --build
# apri http://localhost:8088
```

Per usare un'altra porta sull'host senza toccare i file: `NEXUS_PORT=9000 docker compose up -d` (oppure scrivi `NEXUS_PORT=9000` nel file `.env`).

Poi, dalla GUI:
1. **Impostazioni** → scegli il preset (es. *Ollama*), premi **Carica** per elencare i modelli e seleziona quello di chat. Imposta il modello di embedding (es. `nomic-embed-text`) e salva.
2. **Server MCP** → *Aggiungi server MCP*, scegli il trasporto, collega. Compare l'elenco dei tool con uno switch per ciascuno (più i pulsanti *Tutti* / *Nessuno* e il filtro).
3. **Knowledge base** → trascina i documenti (anche direttamente sulla barra di input della chat). Puoi provare la ricerca semantica dal pannello.

Con Ollama sull'host ricordati di scaricare i modelli: `ollama pull llama3.1` (o `qwen3`, che gestisce bene i tool) e `ollama pull nomic-embed-text`.

> **Ollama su Linux**: per default ascolta solo su 127.0.0.1. Perché il container lo raggiunga avvialo con `OLLAMA_HOST=0.0.0.0` oppure usa il servizio `ollama` commentato nel `docker-compose.yml` (in quel caso Base URL = `http://ollama:11434/v1`).

## Funzionalità

| Area | Dettagli |
|---|---|
| **Endpoint LLM** | Base URL, API key, modello, temperatura, max token, system prompt, tutto da GUI. Elenco modelli letto da `/v1/models`. Selettore rapido del modello nella barra in alto. |
| **MCP** | Trasporti **Streamable HTTP**, **SSE** (legacy) e **stdio**. Campo dedicato per il **Bearer token** (salvato mascherato, sostituibile o rimovibile dalla GUI), header personalizzati aggiuntivi (es. `X-API-Key`), variabili d'ambiente per stdio. Abilitazione per server **e per singolo tool**; lo stato viene mantenuto quando si ricarica l'elenco dei tool. Tool calling multi-round (configurabile) con card espandibili di argomenti e risultati in chat. |
| **RAG** | PDF, DOCX, TXT/MD/CSV/HTML/JSON e codice sorgente. Chunking ricorsivo con overlap, embedding tramite endpoint `/v1/embeddings` (anche diverso da quello di chat), ricerca vettoriale coseno, soglia minima e top-K. Le fonti usate compaiono sotto la risposta con anteprima al passaggio del mouse. Documenti attivabili/disattivabili singolarmente. |
| **Thinking** | Pulsante **Thinking** nella barra in alto: *Auto* (decide il modello), *On* (forzato, con intensità low/medium/high), *Off* (disattivato, risposte più rapide). Nelle Impostazioni scegli i metodi in base al backend (vedi sotto). I blocchi `<think>…</think>` restituiti dentro il testo vengono separati e mostrati nel riquadro "Ragionamento". |
| **Aspetto** | Pannello *Aspetto* con anteprima dal vivo: tema scuro/chiaro/automatico, 7 palette di colori o colori personalizzati, sfondo (aurora animata o ferma, gradiente, tinta unita, immagine caricata con oscuramento regolabile), carattere e dimensione del testo, angoli, densità, larghezza della chat, stile dei messaggi, trasparenza dei pannelli, grana e animazioni. Le preferenze sono salvate sul server e nel browser. |
| **Chat** | Streaming, Markdown con evidenziazione del codice, visualizzazione del "ragionamento" per i modelli che lo espongono (qwen3, deepseek-r1…), stop della generazione, cronologia con ricerca, rinomina ed eliminazione. |
| **Offline** | Tutte le librerie frontend sono incluse nell'immagine: funziona anche in reti isolate (il font Inter viene caricato da Google se raggiungibile, altrimenti si usa il font di sistema). |

## Server MCP: esempi

**Streamable HTTP** (il più comune): URL `http://host.docker.internal:8000/mcp`.

**Importa JSON**: nel pannello Server MCP puoi incollare direttamente una configurazione nel formato di Claude Desktop, Cursor o VS Code (`{"mcpServers": {...}}`). Nexus crea e collega tutti i server; un header `Authorization: Bearer …` viene spostato nel campo Bearer token.

**stdio** (il processo gira *dentro* il container):
- Python, con `uvx` già incluso → Comando `uvx`, Argomenti `mcp-server-time`
- Node → ricostruisci con `INSTALL_NODE: "true"` nel compose, poi Comando `npx`, Argomenti `-y` e `@modelcontextprotocol/server-filesystem` e `/data/files`

Per i server stdio che devono accedere a file, monta i percorsi necessari come volume.

Alla prima esecuzione `uvx`/`npx` scaricano il pacchetto e le dipendenze: Nexus attende fino a 4 minuti (`MCP_STDIO_TIMEOUT`). La cache di `uvx` è nel volume dati (`/data/uv-cache`), quindi sopravvive ai riavvii e agli aggiornamenti del container. Nexus avvia un processo nuovo per ogni chiamata, quindi ogni uso di un tool stdio richiede 1–3 secondi in più rispetto a un server HTTP.

## Thinking: quale metodo per quale backend

| Backend | Metodo da attivare |
|---|---|
| Ollama (`/v1`) | `reasoning_effort` (default): con Off invia `"none"` |
| llama.cpp server | `reasoning_effort` oppure `chat_template_kwargs` |
| vLLM / SGLang (Qwen3, DeepSeek…) | `chat_template_kwargs.enable_thinking` |
| OpenAI e cloud | `reasoning_effort`, solo con modelli di ragionamento |
| Backend datati con Qwen3 | tag `/think` · `/no_think` nel prompt |

Se l'endpoint rifiuta un parametro (HTTP 400), Nexus rigenera la risposta senza e mostra un avviso in chat.

## Home Assistant come server MCP

1. In Home Assistant aggiungi l'integrazione **Model Context Protocol Server** e scegli quali entità esporre in *Impostazioni → Assistenti vocali → Esponi*.
2. Crea un **token di accesso a lunga durata** (profilo utente → Sicurezza).
3. In Nexus: *Aggiungi server MCP* → **Streamable HTTP**, URL `http://<IP-di-HA>:8123/api/mcp`, incolla il token nel campo **Bearer token**.
   - Usa l'**IP** e non `homeassistant.local`: il nome mDNS di norma non si risolve dentro un container.
4. Premi ▶ accanto a `GetLiveContext` per verificare che il tool funzioni e quanto è lungo il risultato.

Se il tool funziona dal pannello ma in chat il modello non lo usa:
- **Modello**: serve un modello con buon supporto ai tool (qwen3 da 8B in su, llama3.1, mistral-nemo, gpt-oss). I modelli da 1–3B spesso non chiamano i tool.
- **Contesto**: Home Assistant espone molti tool, e `GetLiveContext` può restituire decine di migliaia di caratteri. Con il contesto di default di Ollama (4096 token in molte installazioni) istruzioni e tool vengono **troncati in silenzio**. Avvia Ollama con `OLLAMA_CONTEXT_LENGTH=16384` (o più) e imposta lo stesso valore in *Impostazioni → Finestra di contesto*, così Nexus ti avvisa se lo superi. Il parametro `num_ctx` non si può passare dall'API OpenAI di Ollama.
- **Tool**: disattiva quelli che non ti servono (es. timer, media player) per alleggerire il prompt.

## Ricerca web

Il pulsante **Web** nella barra in alto (On/Off) dà al modello due strumenti: `web_search` per cercare e `fetch_url` per leggere una pagina. Il modello li usa quando gli manca un'informazione o serve un dato aggiornato, e cita le fonti come link. Nel prompt riceve anche data e ora correnti (variabile `TZ`).

**Motori disponibili** (*Impostazioni → Ricerca web*):

| Motore | Chiave | Note |
|---|---|---|
| **SearXNG** | no | Incluso nel `docker-compose.yml` (servizio `searxng`, solo sulla rete interna). Consigliato. |
| DuckDuckGo | no | Legge la pagina HTML: può essere limitato dalle protezioni anti-bot. |
| Brave Search | sì | Piano gratuito disponibile su api.search.brave.com. |
| Tavily | sì | API pensata per gli LLM. |
| Automatico | — | SearXNG se configurato, altrimenti DuckDuckGo. |

Per SearXNG imposta una chiave segreta nel file `.env` (`SEARXNG_SECRET=$(openssl rand -hex 32)`). La configurazione è in `searxng/settings.yml`: il formato `json` deve restare abilitato.

**Sicurezza**: per default `fetch_url` non può raggiungere indirizzi della rete locale (router, NAS, Home Assistant…), così una pagina web malevola non può indurre il modello a leggere servizi interni. Il contenuto delle pagine arriva al modello marcato come materiale esterno, non come istruzioni.

## Nomi delle entità approssimativi (Home Assistant)

Gli intent di Home Assistant accettano solo il nome esatto o un alias di un'entità esposta. Nexus interpreta i nomi approssimativi prima di inviarli:

- legge il catalogo delle entità da `GetLiveContext` (in cache per 2 minuti);
- confronta il nome richiesto con nomi e alias, tenendo conto di underscore, articoli, accenti, parole unite o separate e sinonimi italiano/inglese ("faro" ≈ Floodlight, "ricarica" ≈ Charge, "condizionatore" ≈ climate). Se l'opzione è attiva usa anche gli embedding per la somiglianza di significato;
- considera se il tipo di entità è adatto all'azione e l'area indicata.

Se l'abbinamento è chiaro corregge il nome e lo segnala nella scheda del tool («lampada del tavolo» → `lampada_tavolo_soggiorno Light`). Se è ambiguo ("luce cantina" con "uno" e "due") non esegue l'azione e fa chiedere a te quale intendi. Soglia e opzioni sono in *Impostazioni → Endpoint LLM*.

Conviene comunque dare alias significativi alle entità in Home Assistant (*Impostazioni → Assistenti vocali → Esponi → entità → Alias*): sono il modo più affidabile per farle riconoscere.

## Ollama su Mac (Apple Silicon): memoria e contesto

Su Mac la GPU usa la memoria unificata. La memoria richiesta è circa **modello + cache del contesto**, e la cache cresce con `OLLAMA_CONTEXT_LENGTH` e con le richieste parallele. Un errore come `mlx runner failed … Insufficient Memory` vuol dire che non entra.

Con l'app Ollama le variabili si impostano così, poi si chiude e si riapre l'app:
```bash
launchctl setenv OLLAMA_CONTEXT_LENGTH 8192
launchctl setenv OLLAMA_NUM_PARALLEL 1
```
Regola pratica sulla memoria del Mac: 16 GB → modelli fino a ~8B (Q4) con contesto 8192; 32 GB → 14B o 8B con 16384; 64 GB e oltre → 30B+. Per non saturare il contesto, disattiva i tool di Home Assistant che non ti servono e abbassa *Top-K* e *Lunghezza max risultato tool*.

## Configurazione

Tutto si gestisce dalla GUI ed è salvato in SQLite nel volume `/data` (`nexus.db`). Le variabili d'ambiente del compose servono solo come valori iniziali:

| Variabile | Default |
|---|---|
| `LLM_BASE_URL` | `http://host.docker.internal:11434/v1` |
| `LLM_API_KEY` | `ollama` |
| `LLM_MODEL` | *(vuoto → primo modello disponibile)* |
| `EMBED_BASE_URL` / `EMBED_API_KEY` | *(vuoti → stessi dell'LLM)* |
| `EMBED_MODEL` | `nomic-embed-text` |
| `PORT` | `8088` (porta interna del container) |
| `NEXUS_PORT` | `8088` (porta pubblicata sull'host, nel file `.env`) |
| `MCP_CONNECT_TIMEOUT` / `MCP_CALL_TIMEOUT` | `30` / `120` secondi |

## Note

- **Sicurezza**: l'app non ha autenticazione. Esponila solo in rete fidata o dietro un reverse proxy con login (es. Traefik/Nginx + OAuth2 Proxy). Le API key non vengono mai restituite in chiaro alla GUI, ma sono salvate in chiaro nel DB.
- **Cambio modello di embedding**: i vettori di modelli diversi non sono confrontabili. Dopo il cambio usa *Knowledge base → Reindicizza tutto* (gli originali sono conservati in `/data/uploads`).
- **Errore `model 'nomic-embed-text' not found`**: il modello di embedding non è installato. Esegui `ollama pull nomic-embed-text`, poi *Riprova*. Con *Impostazioni → Prova embedding* verifichi la configurazione prima di caricare i documenti.
- **Scalabilità RAG**: la ricerca è in memoria (numpy), adatta fino a qualche centinaio di migliaia di chunk. Oltre conviene sostituire `app/rag.py` con Qdrant o pgvector.
- Il modello deve supportare il **function calling** per usare i tool MCP (es. llama3.1+, qwen2.5/qwen3, mistral-nemo, gpt-4o…).

## Struttura

```
app/
  main.py         API FastAPI + streaming SSE della chat e ciclo di tool calling
  mcp_client.py   connessione ai server MCP (HTTP/SSE/stdio), mapping tool → function calling
  rag.py          estrazione testo, chunking, embedding, ricerca vettoriale
  db.py           schema SQLite e impostazioni
  llm.py          client OpenAI-compatibili
  static/         frontend (HTML/CSS/JS vanilla + librerie vendor)
tests/            server MCP di prova e finto endpoint LLM per test locali
```

Sviluppo locale senza Docker:
```bash
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:app --reload --port 8088
```
