"""Protected supply-selection endpoints; private adapters remain optional."""
from dataclasses import asdict
import hmac
import secrets
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from chanapp.engine import data as engine_data
from chanapp.engine import supply

try:
    from chanapp.engine import display_feed as engine_feed
    from chanapp.engine.feeds import candidate
except ImportError:
    engine_feed = candidate = None

router = APIRouter()
_CSRF = secrets.token_urlsafe(32)


class Selection(BaseModel):
    scheme: Literal['baseline', 'primary_candidate']
    expected_generation: int = Field(ge=0)
    expected_epoch: str | None = Field(default=None, max_length=128)
    code: str | None = Field(default=None, pattern=r'^(sh|sz|hk)\d+$')
    freq: Literal['day', 'm30', 'm60', 'm15', 'm5'] = 'day'


def _identity_headers(response, state):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Supply-Scheme'] = state.scheme
    response.headers['X-Supply-Generation'] = str(state.generation)
    response.headers['X-Supply-Epoch'] = state.epoch


def _unavailable(exc):
    return HTTPException(503, detail={'error_code': 'state_unavailable',
                                     'recovery_required': True, 'reason': exc.reason})


@router.get('/api/supply')
def status(response: Response):
    try:
        state = supply.snapshot()
    except supply.StateUnavailable as exc:
        raise _unavailable(exc) from exc
    _identity_headers(response, state)
    if candidate is None:
        raise HTTPException(503, detail='供数适配层未安装', headers=dict(response.headers))
    try:
        reasons = candidate.readiness()
    except Exception as exc:
        raise HTTPException(503, detail={'error_code': 'readiness_unavailable'},
                            headers=dict(response.headers)) from exc
    return {**asdict(state), 'csrf_token': _CSRF,
            'options': [{'id': 'baseline', 'ready': True},
                        {'id': 'primary_candidate', 'ready': not reasons}]}


def prepare(target: supply.Snapshot, code: str | None, freq: str) -> dict:
    """Prepare in a captured target context; baseline never consults the candidate."""
    if engine_feed is None:
        raise RuntimeError('供数适配层未安装')
    strict = target.scheme == 'primary_candidate'
    if strict:
        try:
            reasons = candidate.readiness()
        except Exception as exc:
            raise HTTPException(503, detail={'error_code': 'readiness_unavailable'}) from exc
        if reasons:
            raise HTTPException(409, detail='候选方案暂不可用，当前方案未变更')
        candidate.preflight()
    from chanapp.api.main import _read_watchlist_raw
    codes = list(dict.fromkeys([item['code'] for item in _read_watchlist_raw()] + ([code] if code else [])))
    if not codes and strict:
        raise HTTPException(409, detail='请选择标的后再切换方案')
    jobs = [(symbol, period) for symbol in codes for period in ('day', 'm30', 'm60')]
    if code and (code, freq) not in jobs:
        jobs.append((code, freq))
    stale, unavailable = [], []
    counts = {'charts': 0, 'quotes': 0, 'f10': 0}

    def missing(symbol, period, kind, reason='data_unavailable'):
        unavailable.append({'code': symbol, 'freq': period, 'kind': kind, 'reason': reason})
        if strict:
            raise RuntimeError('Target preparation incomplete')

    with supply.use(target):
        for symbol, period in jobs:
            try:
                if strict:
                    with engine_data._refresh_lock_for(symbol, period):
                        dataset = engine_data._read_cache(symbol, period)
                        if dataset is None:
                            dataset = engine_data._fetch_and_store(symbol, period)
                    if len(dataset['bars']) < engine_data.N_BARS:
                        raise RuntimeError('Target history window incomplete')
                else:
                    dataset = engine_data.get_bars(symbol, period)
                if not dataset.get('bars'):
                    raise RuntimeError('Target history missing')
                counts['charts'] += 1
                if dataset.get('stale'):
                    stale.append(f'{symbol}/{period}')
            except (supply.StateUnavailable, supply.PublicationRejected):
                raise
            except Exception:
                missing(symbol, period, 'chart')
        try:
            quotes = engine_feed.get_quotes(codes, require_fresh=strict)
        except (supply.StateUnavailable, supply.PublicationRejected):
            raise
        except Exception:
            quotes = {'quotes': {}, 'degraded': True}
        returned = quotes.get('quotes', {})
        missing_quotes = set(quotes.get('missing_codes', [])) | set(quotes.get('invalid_codes', []))
        for symbol in codes:
            if symbol not in returned or symbol in missing_quotes:
                missing(symbol, None, 'quotes')
            else:
                counts['quotes'] += 1
        if quotes.get('degraded'):
            stale.append('quotes')
        for symbol in codes:
            try:
                f10 = engine_feed.get_f10(symbol, require_fresh=strict)
                if not f10.get('f10'):
                    raise RuntimeError('Target company data missing')
                counts['f10'] += 1
                if f10.get('degraded'):
                    stale.append(f'{symbol}/f10')
            except (supply.StateUnavailable, supply.PublicationRejected):
                raise
            except Exception:
                missing(symbol, None, 'f10')
        if strict and stale:
            raise RuntimeError('Target preparation is stale')
    return {**counts, 'stale': stale, 'available': sum(counts.values()),
            'unavailable': unavailable, 'degraded': bool(stale or unavailable)}


@router.post('/api/supply')
def select(selection: Selection, request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        # State failure must surface as a controlled recovery signal, not as a
        # misleading credential rejection (GET no longer issues tokens then).
        supply.snapshot()
    except supply.StateUnavailable as exc:
        raise _unavailable(exc) from exc
    token = request.headers.get('x-supply-csrf', '')
    origin = request.headers.get('origin')
    if not hmac.compare_digest(token, _CSRF):
        raise HTTPException(403, detail='切换凭据无效，请刷新页面')
    if origin and (urlsplit(origin).netloc != request.headers.get('host') or
                   urlsplit(origin).scheme != request.url.scheme):
        raise HTTPException(403, detail='不允许跨站切换方案')
    try:
        result = supply.switch(selection.scheme, selection.expected_generation,
                               lambda target: prepare(target, selection.code, selection.freq),
                               expected_epoch=selection.expected_epoch)
        _identity_headers(response, supply.Snapshot(result['scheme'], result['generation'], result.get('epoch', '')))
        return result
    except supply.StateUnavailable as exc:
        raise _unavailable(exc) from exc
    except supply.ConflictError as exc:
        raise HTTPException(409, detail='方案版本已变化，请刷新状态后重试') from exc
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(503, detail='方案状态未能保存，活动方案未变更') from exc
    except Exception as exc:
        raise HTTPException(502, detail='目标方案预热失败，活动方案未变更') from exc
