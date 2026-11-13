"""JSON completion with a primary/secondary LLM provider and explicit degraded-mode diagnostics.

Primary is Groq (OpenAI-compatible ``/chat/completions``); secondary is Ollama
(local or cloud), tried only if the primary call fails. Every module in this
service (gap questions, product extraction, competitor queries/judging) goes
through ``json_completion`` so this is the single place that provider
selection and fallback live.

IMPORTANT: Return {} only when LLM_OPTIONAL is enabled; the active extraction job
will record the connection / model error in its warnings so users can see it.
"""
from contextvars import ContextVar
import json
import logging
import httpx
from app.core.config import settings

log = logging.getLogger(__name__)
_issues: ContextVar[list[str] | None] = ContextVar('coirei_llm_issues', default=None)


def start_llm_audit():
    return _issues.set([])


def finish_llm_audit(token) -> list[str]:
    issues = list(_issues.get() or [])
    _issues.reset(token)
    return list(dict.fromkeys(issues))


def _record(reason: str) -> None:
    if _issues.get() is not None:
        _issues.get().append(reason)


def _strip_json_fence(content: str) -> str:
    content = content.strip()
    if content.startswith('```'):
        content = content.removeprefix('```json').removeprefix('```').strip()
        content = content.rsplit('```', 1)[0].strip()
    return content


def _parse_object(content: str) -> dict:
    obj = json.loads(_strip_json_fence(content))
    if not isinstance(obj, dict):
        raise ValueError('Expected a JSON object from the model')
    return obj


async def _groq_completion(system: str, prompt: str, schema: dict | None, cfg) -> dict:
    """Primary provider: Groq's OpenAI-compatible chat endpoint."""
    if not cfg.groq_api_key:
        raise RuntimeError('Groq is not configured (set GROQ_API_KEY)')
    body = {
        'model': cfg.groq_model,
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}],
        'temperature': 0,
        'response_format': {'type': 'json_object'},
    }
    if schema:
        body['messages'][0]['content'] += '\nReturn JSON matching schema: ' + json.dumps(schema)[:5000]
    url = cfg.groq_base_url.rstrip('/') + '/chat/completions'
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        response = await client.post(url, json=body,
                                     headers={'Authorization': f'Bearer {cfg.groq_api_key}'})
        response.raise_for_status()
    content = response.json()['choices'][0]['message']['content'] or ''
    return _parse_object(content)


def _ollama_endpoint(base_url: str, route: str) -> str:
    base = base_url.rstrip('/')
    if base.endswith('/api'):
        return base + '/' + route
    if base.endswith('/api/chat'):
        return base if route == 'chat' else base.rsplit('/', 1)[0] + '/' + route
    return base + '/api/' + route


async def _ollama_completion(system: str, prompt: str, schema: dict | None, cfg) -> dict:
    """Secondary provider: an Ollama-compatible endpoint, tried only if the primary fails."""
    if not cfg.ollama_model:
        raise RuntimeError('Ollama fallback is not configured (set OLLAMA_MODEL)')
    headers = {'Authorization': f'Bearer {cfg.ollama_api_key}'} if cfg.ollama_api_key else {}
    body = {'model': cfg.ollama_model, 'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': prompt}],
            'stream': False, 'options': {'temperature': 0, 'num_ctx': 4096}}
    # Some cloud gateways reject Ollama structured format. Constrain in prompt and validate JSON.
    if 'ollama.com' not in cfg.ollama_base_url:
        body['format'] = schema if schema else 'json'
    if schema:
        body['messages'][0]['content'] += '\nReturn JSON matching schema: ' + json.dumps(schema)[:5000]
    async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
        response = await client.post(_ollama_endpoint(cfg.ollama_base_url, 'chat'), json=body,
                                     headers=headers)
        response.raise_for_status()
    content = response.json()['message']['content'] or ''
    return _parse_object(content)


async def llm_health() -> dict:
    cfg = settings()
    out = {'primary': {'provider': 'groq', 'model': cfg.groq_model}, 'secondary': {'provider': 'ollama', 'model': cfg.ollama_model}}
    if cfg.groq_api_key:
        try:
            async with httpx.AsyncClient(timeout=6, trust_env=False) as client:
                response = await client.get(cfg.groq_base_url.rstrip('/') + '/models',
                                            headers={'Authorization': f'Bearer {cfg.groq_api_key}'})
                response.raise_for_status()
            out['primary']['reachable'] = True
        except httpx.HTTPError as exc:
            out['primary']['reachable'] = False
            out['primary']['message'] = type(exc).__name__ + ': ' + str(exc)[:130]
    else:
        out['primary']['reachable'] = False
        out['primary']['message'] = 'GROQ_API_KEY not set'
    headers = {'Authorization': f'Bearer {cfg.ollama_api_key}'} if cfg.ollama_api_key else {}
    try:
        async with httpx.AsyncClient(timeout=6, trust_env=False) as client:
            response = await client.get(_ollama_endpoint(cfg.ollama_base_url, 'tags'), headers=headers)
            response.raise_for_status()
        model_names = [str(m.get('name')) for m in response.json().get('models', [])]
        out['secondary']['reachable'] = True
        out['secondary']['model_installed'] = (cfg.ollama_model in model_names) if model_names else None
        out['secondary']['available_models'] = model_names[:15]
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        out['secondary']['reachable'] = False
        out['secondary']['message'] = type(exc).__name__ + ': ' + str(exc)[:130]
    # Backward-compatible top-level fields (previously the only provider was Ollama).
    out['reachable'] = out['primary']['reachable'] or out['secondary']['reachable']
    out['model'] = cfg.groq_model if out['primary']['reachable'] else cfg.ollama_model
    return out


async def json_completion(system: str, prompt: str, schema: dict | None = None) -> dict:
    cfg = settings()
    errors: list[str] = []

    try:
        return await _groq_completion(system, prompt, schema, cfg)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        reason = f'groq: {type(exc).__name__}: {str(exc)[:180]}'
        log.warning('Primary LLM (Groq, model=%s) failed: %s', cfg.groq_model, reason)
        errors.append(reason)
        _record(reason)

    try:
        return await _ollama_completion(system, prompt, schema, cfg)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        reason = f'ollama: {type(exc).__name__}: {str(exc)[:180]}'
        log.warning('Secondary LLM (Ollama, base=%s model=%s) failed: %s',
                    cfg.ollama_base_url.split('?')[0], cfg.ollama_model, reason)
        errors.append(reason)
        _record(reason)

    combined = '; '.join(errors)
    if cfg.llm_optional:
        return {}
    raise RuntimeError('LLM extraction unavailable (primary and secondary both failed): ' + combined)
