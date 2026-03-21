# Testing Guide

Ce dossier contient les tests Python du backend et du proxy LLM.

## Commandes utiles

Lancer toute la suite:

```bash
uv run pytest -q
```

Lancer un seul fichier:

```bash
uv run pytest -q tests/test_unmute_handler.py
```

Lancer un seul test:

```bash
uv run pytest -q tests/test_unmute_handler.py -k non_block
```

Lancer la suite avec plus de détails:

```bash
uv run pytest -vv
```

Lint + typing avant de pousser un changement backend:

```bash
uv run ruff check unmute tests
uv run pyright
```

## Ce que couvre la suite actuelle

### LLM proxy

- `test_llm_proxy_streaming.py`
  Vérifie le streaming SSE, la transmission du contenu, et le comportement quand une approval utilisateur refuse un tool.

- `test_llm_proxy_tool_approval.py`
  Vérifie la logique d’approval, les résumés affichés à l’utilisateur, la normalisation des dates relatives en français, et quelques cas autour de la mémoire préchargée.

- `test_llm_proxy_config.py`
  Vérifie le chargement de la config MCP et de la liste des tools exclus.

- `test_llm_proxy_mcp_client.py`
  Vérifie qu’un échec de démarrage MCP remonte proprement.

- `test_llm_proxy_ollama_adapter.py`
  Vérifie l’adaptation OpenAI <-> Ollama autour des tool calls.

- `test_llm_proxy_thinking_mode.py`
  Vérifie la traduction des modes de thinking/reasoning dans les payloads.

### LLM / prompts

- `test_llm_utils.py`
  Vérifie `rechunk_to_words`, utilisé pour re-chunker le flux LLM en mots entiers.

- `test_system_prompt.py`
  Vérifie des morceaux du prompt système, notamment l’injection du contexte temporel.

### STT / logique locale

- `test_exponential_moving_average.py`
  Vérifie le comportement de l’EMA utilisée côté STT/VAD.

### Intégrations locales Google Workspace

- `test_mcp_google_workspace.py`
  Vérifie les helpers calendrier côté intégration Google Workspace, surtout sur les cas `primary` vs calendrier partagé.

### Handler temps réel

- `test_unmute_handler.py`
  Vérifie qu’un ack vocal de tool ne bloque pas le stream LLM principal.

## Ce qui manque encore

La suite est utile, mais elle couvre surtout le proxy LLM et les helpers. Il reste peu ou pas de couverture sur:

- `unmute/main_websocket.py`
- le cycle complet STT -> LLM -> TTS
- les files d’attente temps réel et le backpressure
- les interruptions en cours d’audio
- les fermetures/reconnexions côté client frontend

Si tu touches à ces zones, ajoute un test ciblé dans la même PR.

## Comment ajouter un test

Conventions actuelles:

- nommage `tests/test_*.py`
- tests simples en `pytest`
- utiliser `@pytest.mark.asyncio` pour les tests async
- patcher les dépendances réseau/LLM/TTS/STT au lieu de dépendre de services réels

Bon réflexe:

1. reproduire le bug ou le comportement voulu dans un test minimal
2. mocker les dépendances externes
3. vérifier un effet observable précis
4. garder le test petit et déterministe

Exemples de bons points d’assertion:

- un événement est émis
- un stream continue malgré une tâche en fond
- un tool call est transformé correctement
- une erreur remonte sous la bonne forme
- une date relative est normalisée correctement

## Quand écrire quel type de test

- Changement de logique pure: test unitaire direct
- Changement de streaming proxy: test type `test_llm_proxy_streaming.py`
- Changement de coordination handler/async tasks: test async ciblé avec mocks
- Changement d’intégration externe: patch des clients ou réponses au lieu d’un appel réel

## Conseils pratiques

- Évite les sleeps longs dans les tests async. Préfère `asyncio.Event()` et `asyncio.wait_for()`.
- Si un test a besoin d’un modèle LLM ou d’un prompt système, mocke l’auto-sélection du modèle.
- Si tu ajoutes un test pour un bug, nomme-le pour décrire la régression évitée.
- Si un test dépend du temps, fixe-le explicitement avec un patch.

## Commandes recommandées par type de changement

Changement proxy LLM:

```bash
uv run pytest -q tests/test_llm_proxy_streaming.py tests/test_llm_proxy_tool_approval.py
```

Changement prompts / utilitaires LLM:

```bash
uv run pytest -q tests/test_llm_utils.py tests/test_system_prompt.py
```

Changement handler temps réel:

```bash
uv run pytest -q tests/test_unmute_handler.py
```

Changement Google Workspace / MCP calendrier:

```bash
uv run pytest -q tests/test_mcp_google_workspace.py
```

Changement large backend:

```bash
uv run pytest -q
uv run ruff check unmute tests
uv run pyright
```
