# -*- coding: utf-8 -*-
"""统一前缀缓存可观测性（CacheDiagnostics，P0）。

只做 observability：不修改历史语义、不接历史、不改 Context Builder。
本文件为**双胞胎单文件模块**（纯 stdlib、无第三方依赖），在以下两处保持字节级一致：

- ``back/LazyLLM/lazyllm/common/cache_diagnostics.py``（链路 B：OnlineChatModuleBase.forward）
- ``back/lcClaw/src/copaw/token_usage/cache_diagnostics.py``（链路 A：TokenRecordingModelWrapper）

修改任意一份时必须同步另一份（可用 diff 校验）。

功能：
1. ``normalize_cache_usage()``：统一解析各厂商 usage 中的缓存字段，
   兼容 DashScope / OpenAI-compatible（``prompt_tokens_details.cached_tokens``）、
   DeepSeek（``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``）、
   Anthropic（``cache_read_input_tokens``）、Gemini（``cachedContentTokenCount``）。
2. ``fingerprint_request()``：对 messages/tools 做分块 hash
   （system / tools / summary / 每条 history turn / dynamic 本轮 / complete）。
3. ``diff_fingerprints()`` + 进程内 session 存储：与同 session 上一请求对比，
   输出 longest common prefix、``first_diff_message_index``、``first_diff_block``、
   ``estimated_shared_prefix_tokens``。
4. ``run_request_diagnostics()``：一站式入口，输出单行 ``cache_diag`` INFO 日志：

   cache_diag chain=B session=xxx input=32480 shared_prefix≈28120 cached=27648
   hit=85.1% first_diff=turn[12] endpoint=dashscope/qwen...

注意：
- 进程内存储只保留每 session 最近一次请求指纹（有界 LRU）。多 worker 部署时
  跨进程请求对比不到上一轮，日志会显示 ``prev=n``，属预期行为。
- 所有公开入口内部吞掉自身异常：诊断绝不阻断模型调用链路。
- 通过环境变量 ``LCAGENT_CACHE_DIAG=0`` 可整体关闭。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

__all__ = [
    'cache_diag_enabled',
    'canonical_json',
    'short_hash',
    'estimate_tokens',
    'normalize_cache_usage',
    'classify_messages',
    'fingerprint_request',
    'diff_fingerprints',
    'peek_previous',
    'commit_request',
    'format_cache_diag_line',
    'format_cache_diag_detail',
    'run_request_diagnostics',
    'session_from_sid',
]

_DEFAULT_LOGGER = logging.getLogger('cache_diag')

# session 指纹存储上限（LRU）；每条只存 hash 与估算 token，内存开销很小。
_STORE_LIMIT = 512
_store: 'OrderedDict[str, Dict[str, Any]]' = OrderedDict()
_store_lock = threading.RLock()

# 旧 compress_history 生成的伪摘要块标记（见 parts/conversation/history_compressor.py）。
# 仅用于把这类消息归类为 summary 块做 hash 观测，不做任何语义修改。
_SUMMARY_MARKERS = ('历史摘要加载完毕', '[历史摘要')

_CJK_RE = re.compile(
    '['
    '\u3000-\u303f'  # CJK 标点
    '\u3400-\u4dbf'  # 扩展 A
    '\u4e00-\u9fff'  # 基本汉字
    '\uf900-\ufaff'  # 兼容汉字
    '\uff00-\uffef'  # 全角形式
    ']'
)


def cache_diag_enabled() -> bool:
    """``LCAGENT_CACHE_DIAG`` 环境变量开关，默认开启。"""
    return os.environ.get('LCAGENT_CACHE_DIAG', '1').strip().lower() not in (
        '0', 'false', 'no', 'off',
    )


def canonical_json(obj: Any) -> str:
    """稳定序列化：key 排序、无空白、非 JSON 类型退化为 str。"""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str
    )


def short_hash(obj: Any) -> str:
    """对象 canonical JSON 的 sha256 前 12 位（日志可读、碰撞概率可忽略）。"""
    return hashlib.sha256(canonical_json(obj).encode('utf-8')).hexdigest()[:12]


def estimate_tokens(text: str) -> int:
    """集中式粗估：CJK 按 1.5 char/token，其余按 4 char/token（方案 §2.3）。

    仅供 shared_prefix 估算，不用于计费；禁止在别处另写估算函数。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return int(math.ceil(cjk / 1.5 + other / 4.0))


# ---------------------------------------------------------------------------
# usage 归一化
# ---------------------------------------------------------------------------

def _to_plain(obj: Any, depth: int = 0) -> Any:
    """把 dict / pydantic / dataclass / 任意对象递归转成纯 JSON 结构。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if depth > 8:
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _to_plain(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v, depth + 1) for v in obj]
    dump = getattr(obj, 'model_dump', None)
    if callable(dump):
        try:
            plain = _to_plain(dump(), depth + 1)
            # pydantic extra='allow' 的未声明字段（如 DeepSeek 网关扩展）兜底合并。
            extra = getattr(obj, 'model_extra', None)
            if isinstance(extra, dict) and isinstance(plain, dict):
                for k, v in extra.items():
                    plain.setdefault(str(k), _to_plain(v, depth + 1))
            return plain
        except Exception:
            pass
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        try:
            return _to_plain(dataclasses.asdict(obj), depth + 1)
        except Exception:
            pass
    d = getattr(obj, '__dict__', None)
    if isinstance(d, dict) and d:
        return _to_plain(d, depth + 1)
    return str(obj)


def _as_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        i = int(v)
        return i if i >= 0 else None
    except (TypeError, ValueError):
        return None


def normalize_cache_usage(raw: Any) -> Dict[str, Any]:
    """统一解析厂商 usage 缓存字段。

    Args:
        raw: provider 原始 usage —— dict（链路 B 的响应 JSON）、
            agentscope ``ChatUsage``（dataclass，其 ``metadata`` 持有原始
            provider usage 对象）、openai SDK ``CompletionUsage`` 等均可。

    Returns:
        dict，字段（无法解析时为 None）::

            input_tokens        输入 token（prompt_tokens / input_tokens）
            output_tokens       输出 token
            cached_tokens       命中缓存的输入 token
            cache_miss_tokens   未命中的输入 token（厂商缺省时 = input - cached）
            cache_write_tokens  Anthropic cache_creation_input_tokens（其余厂商 None）
            cache_hit_ratio     cached / input（0..1）
            usage_source        命中的字段名，用于区分端点行为
    """
    out: Dict[str, Any] = {
        'input_tokens': None,
        'output_tokens': None,
        'cached_tokens': None,
        'cache_miss_tokens': None,
        'cache_write_tokens': None,
        'cache_hit_ratio': None,
        'usage_source': None,
    }
    if raw is None:
        return out
    d = _to_plain(raw)
    if not isinstance(d, dict):
        return out

    # agentscope ChatUsage：原始 provider usage 在 metadata 里，优先展开。
    meta = d.get('metadata')
    if isinstance(meta, dict):
        merged = dict(meta)
        for k, v in d.items():
            if k != 'metadata':
                merged.setdefault(k, v)
        d = merged

    # Gemini：token 数可能全部位于 usageMetadata 内。
    um = d.get('usageMetadata') or d.get('usage_metadata')
    um = um if isinstance(um, dict) else {}

    for key in ('prompt_tokens', 'input_tokens', 'total_input_tokens'):
        if (v := _as_int(d.get(key))) is not None:
            out['input_tokens'] = v
            break
    if out['input_tokens'] is None:
        for key in ('promptTokenCount', 'prompt_token_count'):
            if (v := _as_int(um.get(key))) is not None:
                out['input_tokens'] = v
                break
    for key in ('completion_tokens', 'output_tokens'):
        if (v := _as_int(d.get(key))) is not None:
            out['output_tokens'] = v
            break
    if out['output_tokens'] is None:
        for key in ('candidatesTokenCount', 'candidates_token_count'):
            if (v := _as_int(um.get(key))) is not None:
                out['output_tokens'] = v
                break

    out['cache_write_tokens'] = _as_int(d.get('cache_creation_input_tokens'))

    # --- cached_tokens：按厂商字段顺序探测 ---
    details = d.get('prompt_tokens_details')
    if isinstance(details, dict):
        if (v := _as_int(details.get('cached_tokens'))) is not None:
            out['cached_tokens'] = v
            out['usage_source'] = 'prompt_tokens_details.cached_tokens'
    if out['cached_tokens'] is None:
        for key, source in (
            ('cached_tokens', 'cached_tokens'),
            ('prompt_cache_hit_tokens', 'prompt_cache_hit_tokens'),  # DeepSeek
            ('cache_read_input_tokens', 'cache_read_input_tokens'),  # Anthropic
            ('cached_content_token_count', 'cached_content_token_count'),
        ):
            if (v := _as_int(d.get(key))) is not None:
                out['cached_tokens'] = v
                out['usage_source'] = source
                break
    if out['cached_tokens'] is None:
        # Gemini: usageMetadata.cachedContentTokenCount
        for key in ('cachedContentTokenCount', 'cached_content_token_count'):
            if (v := _as_int(um.get(key))) is not None:
                out['cached_tokens'] = v
                out['usage_source'] = f'usageMetadata.{key}'
                break

    # Anthropic 语义：input_tokens 不含缓存读/写部分，归一为完整 prompt，
    # 保证命中率跨厂商可比（其余厂商 input 已是完整 prompt）。
    if out['usage_source'] == 'cache_read_input_tokens' and out['input_tokens'] is not None:
        out['input_tokens'] += (out['cached_tokens'] or 0) + (out['cache_write_tokens'] or 0)

    if (v := _as_int(d.get('prompt_cache_miss_tokens'))) is not None:
        out['cache_miss_tokens'] = v
    elif out['input_tokens'] is not None and out['cached_tokens'] is not None:
        out['cache_miss_tokens'] = max(out['input_tokens'] - out['cached_tokens'], 0)

    if out['input_tokens'] and out['cached_tokens'] is not None:
        out['cache_hit_ratio'] = round(
            min(out['cached_tokens'] / out['input_tokens'], 1.0), 4
        )
    return out


# ---------------------------------------------------------------------------
# 请求指纹（分块 hash）
# ---------------------------------------------------------------------------

def _message_text(msg: Any) -> str:
    """提取消息文本用于 summary 标记探测（content 可能是 str 或多模态 list）。"""
    if not isinstance(msg, dict):
        return str(msg)
    content = msg.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and isinstance(c.get('text'), str):
                parts.append(c['text'])
        return '\n'.join(parts)
    return ''


def classify_messages(messages: List[Any]) -> List[Dict[str, Any]]:
    """逐条消息分类并计算 hash / 估算 token。

    block 取值：``system`` / ``summary[i]`` / ``turn[i]`` / ``dynamic``
    （dynamic = 最后一条消息，即本轮输入；summary 依据旧 compress_history
    的标记文本识别，仅观测不改写）。
    """
    entries: List[Dict[str, Any]] = []
    n = len(messages)
    summary_idx = set()
    for i, m in enumerate(messages):
        role = str((m or {}).get('role', '')) if isinstance(m, dict) else ''
        if role == 'assistant':
            text = _message_text(m)
            if any(marker in text for marker in _SUMMARY_MARKERS):
                summary_idx.add(i)
                # 伪摘要轮对：标记 assistant 前面的那条 user 也属于 summary 块。
                if i > 0 and i - 1 not in summary_idx:
                    prev_role = (
                        str((messages[i - 1] or {}).get('role', ''))
                        if isinstance(messages[i - 1], dict) else ''
                    )
                    if prev_role == 'user':
                        summary_idx.add(i - 1)

    for i, m in enumerate(messages):
        role = str((m or {}).get('role', '')) if isinstance(m, dict) else ''
        if role in ('system', 'developer'):
            block = 'system'
        elif i in summary_idx:
            block = f'summary[{i}]'
        elif i == n - 1:
            block = 'dynamic'
        else:
            block = f'turn[{i}]'
        entries.append({
            'index': i,
            'role': role,
            'block': block,
            'hash': short_hash(m),
            'est_tokens': estimate_tokens(canonical_json(m)),
        })
    return entries


def fingerprint_request(
    messages: Optional[List[Any]], tools: Optional[List[Any]] = None
) -> Dict[str, Any]:
    """计算一次请求的分块指纹（system/tools/summary/逐条 history/dynamic/complete）。"""
    msgs = list(messages or [])
    entries = classify_messages(msgs)
    sys_entries = [e for e in entries if e['block'] == 'system']
    summary_entries = [e for e in entries if e['block'].startswith('summary')]
    dyn = entries[-1] if entries else None
    tools_list = list(tools or [])
    return {
        'message_count': len(entries),
        'messages': entries,
        'system_hash': short_hash([e['hash'] for e in sys_entries]) if sys_entries else None,
        'system_est_tokens': sum(e['est_tokens'] for e in sys_entries),
        'tools_hash': short_hash(tools_list) if tools_list else 'none',
        'tools_count': len(tools_list),
        'tools_est_tokens': estimate_tokens(canonical_json(tools_list)) if tools_list else 0,
        'summary_hashes': [e['hash'] for e in summary_entries],
        'dynamic_hash': dyn['hash'] if dyn else None,
        'dynamic_est_tokens': dyn['est_tokens'] if dyn else 0,
        'complete_hash': short_hash({
            'm': [e['hash'] for e in entries],
            't': short_hash(tools_list) if tools_list else 'none',
        }),
        'est_input_tokens': sum(e['est_tokens'] for e in entries)
        + (estimate_tokens(canonical_json(tools_list)) if tools_list else 0),
    }


# ---------------------------------------------------------------------------
# 同 session 上一请求对比（longest common prefix）
# ---------------------------------------------------------------------------

def _state_from_fp(fp: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'system_hash': fp['system_hash'],
        'tools_hash': fp['tools_hash'],
        'message_hashes': [e['hash'] for e in fp['messages']],
        'message_blocks': [e['block'] for e in fp['messages']],
        'message_est': [e['est_tokens'] for e in fp['messages']],
        'system_est': fp['system_est_tokens'],
        'tools_est': fp['tools_est_tokens'],
        'complete_hash': fp['complete_hash'],
        'ts': time.time(),
    }


def peek_previous(session_key: str) -> Optional[Dict[str, Any]]:
    """读取（不消费）同 session 上一次已提交请求的状态。"""
    with _store_lock:
        return _store.get(session_key)


def commit_request(session_key: str, fp: Dict[str, Any]) -> None:
    """请求成功发出后提交指纹，作为下一次对比的基线。"""
    with _store_lock:
        _store.pop(session_key, None)
        _store[session_key] = _state_from_fp(fp)
        while len(_store) > _STORE_LIMIT:
            _store.popitem(last=False)


def diff_fingerprints(
    prev: Optional[Dict[str, Any]], fp: Dict[str, Any]
) -> Dict[str, Any]:
    """与上一请求状态对比，定位第一个差异块并估算共享前缀 token。

    共享前缀按 provider 实际序列化顺序近似：system → tools → messages[0..i)。
    ``first_diff_block`` 取值：``system`` / ``tools`` / 某条消息的 block 标签
    （``turn[i]``、``summary[i]``、``dynamic``）/ ``truncated``（本轮消息数变少且
    公共部分一致）/ ``none``（与上一请求完全一致）。
    """
    result: Dict[str, Any] = {
        'has_prev': prev is not None,
        'identical': False,
        'first_diff_block': None,
        'first_diff_message_index': None,
        'shared_prefix_messages': 0,
        'estimated_shared_prefix_tokens': 0,
    }
    if prev is None:
        return result
    result['identical'] = prev.get('complete_hash') == fp['complete_hash']
    if result['identical']:
        result['first_diff_block'] = 'none'
        result['shared_prefix_messages'] = fp['message_count']
        result['estimated_shared_prefix_tokens'] = fp['est_input_tokens']
        return result

    if (prev.get('system_hash') or 'none') != (fp['system_hash'] or 'none'):
        result['first_diff_block'] = 'system'
        result['first_diff_message_index'] = 0
        return result
    if (prev.get('tools_hash') or 'none') != (fp['tools_hash'] or 'none'):
        result['first_diff_block'] = 'tools'
        result['first_diff_message_index'] = 0
        result['estimated_shared_prefix_tokens'] = fp['system_est_tokens']
        return result

    prev_hashes = prev.get('message_hashes') or []
    cur = fp['messages']
    shared = fp['system_est_tokens'] + fp['tools_est_tokens']
    i = 0
    while i < min(len(prev_hashes), len(cur)):
        if prev_hashes[i] != cur[i]['hash']:
            break
        shared += cur[i]['est_tokens']
        i += 1
    result['shared_prefix_messages'] = i
    result['estimated_shared_prefix_tokens'] = shared
    if i < len(cur):
        result['first_diff_message_index'] = i
        result['first_diff_block'] = cur[i]['block']
    elif len(prev_hashes) > len(cur):
        # 本轮消息数变少（如换窗/截断）：公共前缀一致但尾部被裁掉。
        result['first_diff_message_index'] = len(cur)
        result['first_diff_block'] = 'truncated'
    else:
        result['first_diff_block'] = 'none'
    return result


# ---------------------------------------------------------------------------
# 日志格式化
# ---------------------------------------------------------------------------

def _fmt_int(v: Optional[int]) -> str:
    return '?' if v is None else str(v)


def format_cache_diag_line(
    *,
    chain: str,
    session: Optional[str],
    turn: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    endpoint: Optional[str] = None,
    fp: Dict[str, Any],
    diff: Dict[str, Any],
    usage: Dict[str, Any],
) -> str:
    """单行 cache_diag 日志：cached 与 shared_prefix 同行，便于直接对账。"""
    input_tokens = usage.get('input_tokens')
    if input_tokens is not None:
        input_part = f'input={input_tokens}'
    else:
        input_part = f'input≈{fp["est_input_tokens"]}'
    cached = usage.get('cached_tokens')
    ratio = usage.get('cache_hit_ratio')
    hit = '?' if ratio is None else f'{ratio * 100:.1f}%'
    parts = [
        'cache_diag',
        f'chain={chain}',
        f'session={session or "-"}',
    ]
    if turn:
        parts.append(f'turn={turn}')
    parts += [
        f'provider={provider or "-"}',
        f'model={model or "-"}',
        f'endpoint={endpoint or "-"}',
        f'msgs={fp["message_count"]}',
        f'n_tools={fp["tools_count"]}',
        input_part,
        f'shared_prefix≈{diff.get("estimated_shared_prefix_tokens", 0)}'
        if diff.get('has_prev') else 'shared_prefix≈-',
        f'cached={_fmt_int(cached)}',
        f'miss={_fmt_int(usage.get("cache_miss_tokens"))}',
        f'hit={hit}',
        f'first_diff={diff.get("first_diff_block") if diff.get("has_prev") else "first"}',
        f'sys={fp["system_hash"] or "-"}',
        f'tools={fp["tools_hash"]}',
        f'dyn={fp["dynamic_hash"] or "-"}',
        f'sum={len(fp["summary_hashes"])}',
        f'req={fp["complete_hash"]}',
        f'prev={"y" if diff.get("has_prev") else "n"}',
        f'usage_src={usage.get("usage_source") or "-"}',
    ]
    return ' '.join(parts)


def format_cache_diag_detail(
    *, chain: str, session: Optional[str], fp: Dict[str, Any]
) -> str:
    """DEBUG 级明细：逐条 message hash，用于精确比对哪条 history 变了。"""
    msgs = ','.join(
        f'{e["index"]}|{e["block"]}|{e["role"]}|{e["hash"]}|≈{e["est_tokens"]}'
        for e in fp['messages']
    )
    return (
        f'cache_diag_detail chain={chain} session={session or "-"} '
        f'req={fp["complete_hash"]} sys={fp["system_hash"] or "-"} '
        f'tools={fp["tools_hash"]} summaries={fp["summary_hashes"]} msgs=[{msgs}]'
    )


def session_from_sid(sid: Optional[str]) -> tuple:
    """LazyLLM engine sessionid ``app:mode:user:tenant:track:turn`` →
    (session_key=前 5 段, turn=第 6 段)；格式不符时原样返回。"""
    s = str(sid or '')
    parts = s.split(':')
    if len(parts) >= 6:
        return ':'.join(parts[:5]), parts[5]
    return s or None, None


# ---------------------------------------------------------------------------
# 一站式入口
# ---------------------------------------------------------------------------

def begin_request_diagnostics(
    *, session: Optional[str], messages: Optional[List[Any]],
    tools: Optional[List[Any]] = None,
) -> Optional[Dict[str, Any]]:
    """请求侧：计算指纹并与上一请求对比（**不**提交，见 commit 时机说明）。

    返回 state dict；关闭开关或内部异常时返回 None。请求成功拿到响应后
    调用 :func:`finish_request_diagnostics` 提交并落日志；请求失败则不提交，
    避免把未到达 provider 的请求当作缓存基线。
    """
    if not cache_diag_enabled():
        return None
    try:
        fp = fingerprint_request(messages, tools)
        prev = peek_previous(session) if session else None
        diff = diff_fingerprints(prev, fp)
        return {'session': session, 'fp': fp, 'diff': diff}
    except Exception as e:  # pragma: no cover - 诊断绝不阻断调用
        _DEFAULT_LOGGER.debug('cache_diag begin failed: %s', e)
        return None


def finish_request_diagnostics(
    state: Optional[Dict[str, Any]],
    *,
    chain: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    endpoint: Optional[str] = None,
    raw_usage: Any = None,
    turn: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
    level: int = logging.INFO,
) -> Dict[str, Any]:
    """响应侧：提交指纹、归一化 usage、输出单行 cache_diag 日志。

    Returns:
        ``{'fingerprint', 'diff', 'usage'}``；state 为 None 时返回 ``{}``。
    """
    if state is None:
        return {}
    log = logger or _DEFAULT_LOGGER
    try:
        fp = state['fp']
        diff = state['diff']
        session = state.get('session')
        usage = normalize_cache_usage(raw_usage)
        if session:
            commit_request(session, fp)
        log.log(
            level,
            format_cache_diag_line(
                chain=chain, session=session, turn=turn, provider=provider,
                model=model, endpoint=endpoint, fp=fp, diff=diff, usage=usage,
            ),
        )
        if log.isEnabledFor(logging.DEBUG):
            log.debug(format_cache_diag_detail(chain=chain, session=session, fp=fp))
        return {'fingerprint': fp, 'diff': diff, 'usage': usage}
    except Exception as e:  # pragma: no cover - 诊断绝不阻断调用
        try:
            log.debug('cache_diag finish failed: %s', e)
        except Exception:
            pass
        return {}


def run_request_diagnostics(
    *,
    chain: str,
    session: Optional[str],
    provider: Optional[str] = None,
    model: Optional[str] = None,
    endpoint: Optional[str] = None,
    messages: Optional[List[Any]] = None,
    tools: Optional[List[Any]] = None,
    raw_usage: Any = None,
    turn: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
    level: int = logging.INFO,
) -> Dict[str, Any]:
    """begin + finish 一次完成（请求与响应在同一调用点可见时使用）。"""
    state = begin_request_diagnostics(
        session=session, messages=messages, tools=tools
    )
    return finish_request_diagnostics(
        state, chain=chain, provider=provider, model=model, endpoint=endpoint,
        raw_usage=raw_usage, turn=turn, logger=logger, level=level,
    )
