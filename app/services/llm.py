"""Ollama-compatible completion with explicit degraded-mode diagnostics.

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


def _endpoint(base_url: str, route: str) -> str:
    base = base_url.rstrip('/')
    if base.endswith('/api'):
        return base + '/' + route
    # Accept bare Ollama roots as a convenience, but don't double-insert /api.
    if base.endswith('/api/chat'):
        return base if route == 'chat' else base.rsplit('/', 1)[0] + '/' + route
    return base + '/api/' + route


async def llm_health() -> dict:
    cfg = settings()
    headers = {'Authorization': f'Bearer {cfg.ollama_api_key}'} if cfg.ollama_api_key else {}
    try:
        async with httpx.AsyncClient(timeout=6, trust_env=False) as client:
            response = await client.get(_endpoint(cfg.ollama_base_url, 'tags'), headers=headers)
            response.raise_for_status()
        model_names = [str(m.get('name')) for m in response.json().get('models', [])]
        return {'reachable': True, 'model': cfg.ollama_model,
                'model_installed': (cfg.ollama_model in model_names) if model_names else None,
                'available_models': model_names[:15]}
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        return {'reachable': False, 'model': cfg.ollama_model,
                'message': type(exc).__name__ + ': ' + str(exc)[:130]}


async def json_completion(system: str, prompt: str, schema: dict | None = None) -> dict:
    cfg = settings()
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
    try:
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            response = await client.post(_endpoint(cfg.ollama_base_url, 'chat'), json=body,
                                         headers=headers)
            response.raise_for_status()
        content = response.json()['message']['content'].strip()
        if content.startswith('```'):
            content = content.removeprefix('```json').removeprefix('```').strip()
            content = content.rsplit('```', 1)[0].strip()
        obj = json.loads(content)
        if not isinstance(obj, dict):
            raise ValueError('Expected a JSON object from the model')
        return obj
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        reason = f'{type(exc).__name__}: {str(exc)[:180]}'
        log.warning('Ollama extraction failed (%s model=%s): %s',
                    cfg.ollama_base_url.split('?')[0], cfg.ollama_model, reason)
        if _issues.get() is not None:
            _issues.get().append(reason)
        if cfg.llm_optional:
            return {}
        raise RuntimeError('LLM extraction unavailable: ' + reason) from exc
